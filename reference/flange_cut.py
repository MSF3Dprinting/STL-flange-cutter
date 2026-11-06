#!/usr/bin/env python3
"""
flange_cut.py - proof of concept

Cut a thin-walled (2-3 mm) mold with a plane, grow a bolting flange along the
cut on ONE side of the wall only (the mold face stays clean), and add N bolt
holes perpendicular to the cut plane. Outputs two watertight halves.

User inputs: STL, cutting plane (origin + normal), bolt size, bolt count,
flange side ("outer" = away from the cavity, "inner" = into it).

Engine: Manifold (booleans, plane split, cross-section) + shapely (one-sided
2D offsets, arc-length bolt spacing) + trimesh (I/O, section -> face normals).
"""
import argparse, json, time
from pathlib import Path

import numpy as np
import trimesh
from manifold3d import Manifold, Mesh, CrossSection, OpType, Error
from scipy.spatial import cKDTree
from shapely.geometry import LineString, MultiLineString, Polygon, Point
from shapely.geometry.polygon import orient
from shapely.ops import linemerge, unary_union

# ISO 273 medium clearance hole, ISO 7089 washer OD/thickness, ISO 4032 nut AF/height
BOLTS = {
    "M3": dict(d=3, clear=3.4, washer=7.0, washer_t=0.5, nut_af=5.5, nut_h=2.4),
    "M4": dict(d=4, clear=4.5, washer=9.0, washer_t=0.8, nut_af=7.0, nut_h=3.2),
    "M5": dict(d=5, clear=5.5, washer=10.0, washer_t=1.0, nut_af=8.0, nut_h=4.7),
    "M6": dict(d=6, clear=6.6, washer=12.0, washer_t=1.6, nut_af=10.0, nut_h=5.2),
}
STD_LENGTHS = [8, 10, 12, 16, 20, 25, 30, 35, 40, 45, 50]


# ---------------------------------------------------------------- conversions
def tm_to_manifold(tm):
    m = Manifold(Mesh(vert_properties=np.asarray(tm.vertices, np.float32),
                      tri_verts=np.asarray(tm.faces, np.uint32)))
    if m.status() != Error.NoError:
        raise ValueError(f"Mesh is not watertight/manifold ({m.status()}); repair it first.")
    return m


def manifold_to_tm(m):
    mm = m.to_mesh()
    return trimesh.Trimesh(np.asarray(mm.vert_properties)[:, :3],
                           np.asarray(mm.tri_verts), process=False)


def cs_to_shapely(cs):
    pos, neg = [], []
    for p in cs.to_polygons():
        p = np.asarray(p)
        if len(p) < 3:
            continue
        a = 0.5 * np.sum(p[:, 0] * np.roll(p[:, 1], -1) - np.roll(p[:, 0], -1) * p[:, 1])
        (pos if a > 0 else neg).append(Polygon(p).buffer(0))
    g = unary_union(pos)
    return g.difference(unary_union(neg)) if neg else g


def shapely_to_cs(g):
    contours = []
    for P in getattr(g, "geoms", [g]):
        if P.is_empty or P.area < 1e-6:
            continue
        P = orient(P, 1.0)
        contours.append(np.asarray(P.exterior.coords)[:-1])
        contours += [np.asarray(r.coords)[:-1] for r in P.interiors]
    return CrossSection(contours)


def plane_frame(origin, normal):
    """3x4 matrices world->plane (cut plane becomes z=0, normal +z) and back."""
    o = np.asarray(origin, float)
    n = np.asarray(normal, float); n /= np.linalg.norm(n)
    a = np.array([1.0, 0, 0]) if abs(n[0]) < 0.9 else np.array([0, 1.0, 0])
    u = np.cross(a, n); u /= np.linalg.norm(u)
    v = np.cross(n, u)
    R = np.vstack([u, v, n])
    to_plane = np.hstack([R, (-R @ o)[:, None]])
    to_world = np.hstack([R.T, o[:, None]])
    return to_plane, to_world


# ---------------------------------------------------------------- core
def flange_cut(mold_tm, origin, normal, bolt="M4", count=8, side="outer",
               flange_t=None, wall_gap=1.0, edge=2.0, overlap=0.6,
               print_clear=0.2, cavity_point=None, max_lean=25.0):
    t0 = time.time()
    B = BOLTS[bolt]
    T = flange_t or max(3.5, float(B["d"]))          # flange thickness per half
    hole_r = (B["clear"] + print_clear) / 2
    warnings = []

    to_plane, to_world = plane_frame(origin, normal)
    mp = tm_to_manifold(mold_tm).transform(to_plane)   # cut plane is now z = 0

    # 1. section outline + the mesh face behind every outline segment
    S = cs_to_shapely(mp.slice(0.0))
    if S.is_empty:
        raise ValueError("The plane does not cut the model.")
    tm_p = manifold_to_tm(mp)
    lines, fidx = trimesh.intersections.mesh_plane(
        tm_p, plane_normal=[0, 0, 1], plane_origin=[0, 0, 0], return_faces=True)
    seg = np.round(lines[:, :, :2], 5)
    mids = seg.mean(axis=1)
    nf = tm_p.face_normals[fidx]
    n2 = nf[:, :2] / np.maximum(np.linalg.norm(nf[:, :2], axis=1), 1e-9)[:, None]
    lean = np.degrees(np.arcsin(np.clip(np.abs(nf[:, 2]), 0, 1)))

    # 2. which outline segments face away from the cavity
    C = np.asarray(cavity_point[:2] if cavity_point is not None
                   else S.convex_hull.centroid.coords[0])
    away = np.einsum("ij,ij->i", n2, mids - C) > 0
    flange_mask = away if side == "outer" else ~away
    if np.percentile(lean[flange_mask], 95) > max_lean:
        warnings.append(f"Wall meets the cut at up to {lean[flange_mask].max():.0f} deg from "
                        f"perpendicular; flange may not sit cleanly. Rotate the plane.")

    def chains(mask):
        g = linemerge(MultiLineString([LineString(s) for s in seg[mask]]))
        return [g] if g.geom_type == "LineString" else list(g.geoms)

    tree = cKDTree(mids)

    def side_band(ch, s, dist, join="round"):
        """Band of width `dist` on side s of a chain (+1 left, -1 right).
        Closed rings go through polygon offsets (GEOS single-sided buffers
        misbehave on closed rings)."""
        if not ch.is_closed:
            return ch.buffer(s * dist, single_sided=True, join_style=join, quad_segs=16)
        P = Polygon(ch.coords).buffer(0)
        p = np.asarray(ch.coords); d = p[1] - p[0]
        probe = (p[0] + p[1]) / 2 + s * 1e-3 * np.array([-d[1], d[0]]) / np.linalg.norm(d)
        if P.contains(Point(probe)):
            return P.difference(P.buffer(-dist, join_style=join, quad_segs=16))
        return P.buffer(dist, join_style=join, quad_segs=16).difference(P)

    def outward_sign(ch):
        """+1 if the flange side is to the left of the chain direction."""
        p = np.asarray(ch.coords)
        d = np.diff(p, axis=0); L = np.linalg.norm(d, axis=1); k = L > 1e-9
        left = np.c_[-d[k, 1], d[k, 0]] / L[k, None]
        _, i = tree.query((p[:-1][k] + p[1:][k]) / 2)
        return 1.0 if np.sum(np.einsum("ij,ij->i", left, n2[i]) * L[k]) > 0 else -1.0

    f_chains = [ch for ch in chains(flange_mask) if ch.length > 1.0]
    g_chains = [ch for ch in chains(~flange_mask) if ch.length > 1.0]

    def build(gap):
        c = gap + B["washer"] / 2                      # bolt axis distance from wall
        W = c + B["washer"] / 2 + edge                 # flange width from wall
        notes = []
        # 3. one-sided flange: band on the flange side + small overlap into the
        #    wall, minus a guard zone on the cavity side
        parts, centrelines = [], []
        for ch in f_chains:
            s = outward_sign(ch)
            parts += [side_band(ch, s, W), side_band(ch, -s, overlap, "mitre")]
            centrelines.append((ch, ch.offset_curve(s * c, quad_segs=16, join_style="round")))
        flange2d = unary_union(parts)
        guard = [side_band(ch, outward_sign(ch), W + 2) for ch in g_chains]
        if guard:
            flange2d = flange2d.difference(unary_union(guard))
        flange2d = flange2d.buffer(0)

        # 4. bolts spaced by arc length along the flange centre line
        lengths = np.array([cl.length for _, cl in centrelines])
        alloc = np.maximum(2, np.round(count * lengths / lengths.sum())).astype(int)
        while alloc.sum() > count and alloc.max() > 2:
            alloc[np.argmax(alloc)] -= 1
        pts, end_margin = [], c + B["washer"] / 2 + 3.0
        for (ch, cl), n in zip(centrelines, alloc):
            if cl.geom_type != "LineString":
                cl = max(cl.geoms, key=lambda g: g.length)
            L = cl.length
            s_pos = ((np.arange(n) + 0.5) * L / n if ch.is_closed
                     else np.linspace(end_margin, L - end_margin, n))
            pts += [np.asarray(cl.interpolate(si).coords[0]) for si in s_pos]
        pts = np.array(pts)
        if len(pts) > 1:
            dmin = cKDTree(pts).query(pts, k=2)[0][:, 1].min()
            if dmin < B["washer"] + 2:
                notes.append(f"Bolts only {dmin:.1f} mm apart; reduce the count.")
        for i, p in enumerate(pts):
            disc = Point(p).buffer(B["washer"] / 2)
            if disc.intersects(S) or not flange2d.buffer(0.05).contains(disc):
                notes.append(f"Bolt {i + 1}: washer does not fit on the flange.")

        # 5. solids: one flange slab straddling the plane, drill, then split
        #    (splitting last avoids coplanar unions and sliver faces)
        F = shapely_to_cs(flange2d).extrude(2 * T).translate((0, 0, -T))
        holes = Manifold.batch_boolean(
            [Manifold.cylinder(2 * T + 2, hole_r, -1, 48, True).translate((x, y, 0.0))
             for x, y in pts], OpType.Add)
        half_a, half_b = ((mp + F) - holes).split_by_plane((0.0, 0.0, 1.0), 0.0)
        if half_a.bounding_box()[5] < half_b.bounding_box()[5]:
            half_a, half_b = half_b, half_a                   # A: z >= 0, B: z <= 0

        # 6. tool clearance beyond each flange face (head/nut + washer, 12 mm)
        env_r, env_h, blocked = B["washer"] / 2 + 0.5, 12.0, []
        for i, (x, y) in enumerate(pts):
            cyl = Manifold.cylinder(env_h, env_r, -1, 32)
            if ((half_a ^ cyl.translate((x, y, T + 0.01))).volume() > 0.01 or
                    (half_b ^ cyl.translate((x, y, -T - env_h - 0.01))).volume() > 0.01):
                blocked.append(i + 1)
        return dict(c=c, W=W, flange2d=flange2d, pts=pts, half_a=half_a,
                    half_b=half_b, notes=notes, blocked=blocked)

    # flared walls can lean over the bolts: push the bolt line outward until clear
    for gap in np.arange(wall_gap, wall_gap + 6.01, 1.0):
        r = build(gap)
        if not r["blocked"]:
            break
    if r["blocked"]:
        r["notes"].append(f"Bolts {r['blocked']}: wall blocks the head/nut; move the plane.")
    elif gap > wall_gap:
        r["notes"].append(f"Wall flares over the flange: bolt line moved out to "
                          f"{r['c']:.1f} mm from the wall.")
    warnings += r["notes"]
    c, W, flange2d, bolts_xy = r["c"], r["W"], r["flange2d"], r["pts"]
    half_a, half_b = r["half_a"], r["half_b"]

    grip = 2 * T + 2 * B["washer_t"] + B["nut_h"] + 2.0
    length = next(l for l in STD_LENGTHS if l >= grip)
    report = dict(
        bolt=bolt, count=int(len(bolts_xy)), flange_thickness_mm=T, flange_width_mm=W,
        bolt_axis_from_wall_mm=c, hole_diameter_mm=round(2 * hole_r, 2),
        shopping_list=f"{len(bolts_xy)} x {bolt}x{length} bolt, {len(bolts_xy)} nuts, "
                      f"{2 * len(bolts_xy)} washers",
        warnings=warnings, seconds=round(time.time() - t0, 2),
    )
    return dict(half_a=half_a, half_b=half_b, to_world=to_world, section=S,
                flange2d=flange2d, bolts_xy=bolts_xy, report=report,
                washer=B["washer"], hole_r=hole_r, cavity_ref=C)


def export(res, out_dir, stem):
    out = Path(out_dir); out.mkdir(parents=True, exist_ok=True)
    flip = np.array([[1, 0, 0, 0], [0, -1, 0, 0], [0, 0, -1, 0]], float)
    files = {}
    for key, m_print in (("A", res["half_a"]), ("B", res["half_b"].transform(flip))):
        tm = manifold_to_tm(m_print)                       # cut face down on the bed
        tm.apply_translation([0, 0, -tm.bounds[0][2]])
        p = out / f"{stem}_half_{key}_print.stl"; tm.export(p); files[key] = str(p)
    return files


# ---------------------------------------------------------------- test mold
def demo_mold(H=240.0, a=60.0, b=42.0, wall=2.5, taper=0.72, seg=160, bend=30.0):
    """Tapered, bent elliptical cup (open top) - stand-in for a limb mold."""
    th = np.linspace(0, 2 * np.pi, seg, endpoint=False)
    ell = lambda ax, by: CrossSection([np.c_[ax * np.cos(th), by * np.sin(th)]])
    outer = ell(a, b).extrude(H, n_divisions=48, scale_top=(taper, taper))
    z0, z1 = wall, H + 1.0
    ai = lambda z, s: s * (1 - (1 - taper) * z / H) - wall
    inner = ell(ai(z0, a), ai(z0, b)).extrude(
        z1 - z0, n_divisions=48,
        scale_top=(ai(z1, a) / ai(z0, a), ai(z1, b) / ai(z0, b))).translate((0, 0, z0))
    warp = lambda v: (v[0] + bend * (v[2] / H) ** 2, v[1], v[2])
    shell = (outer - inner).warp(warp)
    cavity = (inner ^ Manifold.cube((400, 400, H - 0.01)).translate((-200, -200, 0))).warp(warp)
    return manifold_to_tm(shell), cavity


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[1])
    ap.add_argument("stl", nargs="?", help="mold STL (omit with --demo)")
    ap.add_argument("--demo", action="store_true")
    ap.add_argument("--origin", nargs=3, type=float, default=[0, 0, 0])
    ap.add_argument("--normal", nargs=3, type=float, default=[0, 1, 0])
    ap.add_argument("--bolt", choices=BOLTS, default="M4")
    ap.add_argument("--count", type=int, default=8)
    ap.add_argument("--side", choices=["outer", "inner"], default="outer")
    ap.add_argument("--out", default="out")
    a = ap.parse_args()
    mold = demo_mold()[0] if a.demo else trimesh.load_mesh(a.stl)
    res = flange_cut(mold, a.origin, a.normal, a.bolt, a.count, a.side)
    res["report"]["files"] = export(res, a.out, "mold")
    print(json.dumps(res["report"], indent=2))
