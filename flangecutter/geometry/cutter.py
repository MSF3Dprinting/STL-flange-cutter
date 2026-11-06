"""Cut a mold with one or more planes and add a bolted flange along every cut.

Port of reference/flange_cut.py (the proof of concept). Every cut is worked out in its own
plane coordinates (cut plane = z 0, cut normal = +z):

1. section   the wall outline in the plane, and which face of the wall looks away from the cavity
2. layout    2D flange outline (a band on ONE face of the wall) and the bolt positions; the bolt
             line moves out while the wall would block a tool (POC rule 8)
3. generate  one 3D build for all cuts: mold + flange slabs - holes - nut pockets, and only
             THEN split by every plane (splitting last keeps every part watertight)

Cuts may cross. Bolts keep out of a band around each crossing, so holes, washers and tools stay
clear of the other cut's flange (docs/ARCHITECTURE.md, "Crossing cuts").
"""
from __future__ import annotations

import math
import time
from collections import Counter
from dataclasses import dataclass, field
from typing import Callable

import numpy as np
import shapely
import trimesh
from manifold3d import CrossSection, Manifold, OpType
from shapely.geometry import LineString, Point, Polygon, box
from shapely.geometry.polygon import orient
from shapely.ops import linemerge, unary_union

from . import CutError, warning
from .bolts import (BOLTS, COUNT_RANGE, DEFAULT_SPACING, FASTENINGS, MAX_CUTS, SPACING_RANGE,
                    SUGGEST_PITCH, Derived, check_params, derive)
from .mesh_io import Model, stl_bytes

SIDES = ("outside", "inside")
GAP_STEPS = 6                              # bolt line may move out by up to 6 x 1 mm
BLOCK_VOLUME = 0.01                        # mm3; another flange overlapping a tool cylinder more blocks it
BLOCK_AREA = 0.01                          # mm2; a tool disc overlapping the mold's shadow more is blocked
PARALLEL_SIN = math.sin(math.radians(5))   # planes closer than 5 deg to parallel do not "cross"
SHALLOW_DEG = 30.0
SMALL_PART_CM3 = 0.5
FLIP = np.diag([1.0, -1.0, -1.0, 1.0])     # 180 deg about the plane x axis (print orientation of side B)
Progress = Callable[[str, float], None]


# ---------------------------------------------------------------- plane frames
@dataclass
class Plane:
    """Orthonormal frame. Rows of R: in-plane x axis, in-plane y axis, normal."""

    origin: np.ndarray
    R: np.ndarray

    @property
    def normal(self) -> np.ndarray:
        return self.R[2]

    @property
    def offset(self) -> float:
        """The plane is {p : normal . p = offset}."""
        return float(self.normal @ self.origin)

    def to_plane(self) -> np.ndarray:
        """3x4 matrix world -> plane coordinates."""
        return np.hstack([self.R, (-self.R @ self.origin)[:, None]])

    def to_world(self) -> np.ndarray:
        """3x4 matrix plane coordinates -> world."""
        return np.hstack([self.R.T, self.origin[:, None]])

    def local(self, pts: np.ndarray) -> np.ndarray:
        return (np.asarray(pts, float) - self.origin) @ self.R.T

    def world(self, pts2: np.ndarray) -> np.ndarray:
        """In-plane 2D points -> world 3D points."""
        p = np.asarray(pts2, float).reshape(-1, 2)
        return self.origin + p[:, :1] * self.R[0] + p[:, 1:2] * self.R[1]

    def as_json(self) -> dict:
        return {"origin": self.origin.tolist(), "x_axis": self.R[0].tolist(),
                "y_axis": self.R[1].tolist(), "normal": self.R[2].tolist()}


def plane_frame(origin, normal, x_axis=None) -> Plane:
    """Frame from the viewer's plane: origin, normal (+z) and in-plane x axis, orthonormalised."""
    o, n = np.asarray(origin, float), np.asarray(normal, float)
    if o.shape != (3,) or n.shape != (3,) or not (np.all(np.isfinite(o)) and np.all(np.isfinite(n))) \
            or np.linalg.norm(n) < 1e-9:
        raise CutError("The cut plane is not valid. Reset the cut and place it again.", f"o={o}, n={n}")
    n = n / np.linalg.norm(n)
    x = np.zeros(3) if x_axis is None else np.asarray(x_axis, float).reshape(3)
    x = x - (x @ n) * n
    if not np.all(np.isfinite(x)) or np.linalg.norm(x) < 1e-6:   # missing or along the normal: POC choice
        x = np.cross(np.array([1.0, 0, 0]) if abs(n[0]) < 0.9 else np.array([0, 1.0, 0]), n)
    x = x / np.linalg.norm(x)
    return Plane(o, np.vstack([x, np.cross(n, x), n]))


def _segment_shift(base: np.ndarray) -> float:
    """Offset (mm) for the trimesh outline so that no vertex lies on it.

    Vertices exactly on the plane (e.g. "Across Z" through a ring of vertices) make
    mesh_plane return duplicate edges. Only the 2D outline uses the shifted plane (1 um away);
    Manifold slices and splits at the exact plane, which it handles robustly. Moving the
    3D split instead would leave 1 um sliver triangles that break after float32 STL export.
    """
    for k in range(21):
        s = 0.001 * ((k + 1) // 2) * (1 if k % 2 else -1)     # 0, +1, -1, +2, -2 ... um
        if np.abs(base - s).min() > 1e-6:
            return s
    return 0.0


# ---------------------------------------------------------------- settings
@dataclass
class Cut:
    """One cut with all its own settings, validated."""

    index: int
    plane: Plane
    side: str
    d: Derived
    mode: str            # "count" or "spacing"
    count: int
    spacing: float
    pocket_half: str     # "A" or "B": which part of this cut holds the nut pockets
    settings: dict       # normalised copy for settings.json

    @property
    def label(self) -> str:
        return f"Cut {self.index + 1}"


def parse_cuts(payload: dict) -> list[Cut]:
    """Validate the JSON of /api/preview and /api/generate. Accepts the spec's single-cut form too."""
    raw = payload.get("cuts") if isinstance(payload, dict) else None
    if raw is None and isinstance(payload, dict) and "plane" in payload:
        raw = [payload]
    if not isinstance(raw, list) or not raw:
        raise CutError("Add at least one cut.")
    if len(raw) > MAX_CUTS:
        raise CutError(f"Use at most {MAX_CUTS} cuts.")
    return [_parse_cut(r, i) for i, r in enumerate(raw)]


def _pick(value, allowed, what: str, label: str):
    if value not in allowed:
        raise CutError(f"{label}: unknown {what} '{value}'.", f"allowed: {list(allowed)}")
    return value


def _parse_cut(r: dict, i: int) -> Cut:
    label = f"Cut {i + 1}"
    if not isinstance(r, dict):
        raise CutError(f"{label}: the settings are missing.")
    pl = r.get("plane") or {}
    try:
        plane = plane_frame(pl.get("origin"), pl.get("normal"), pl.get("x_axis"))
    except (TypeError, ValueError) as exc:
        if isinstance(exc, CutError):
            raise
        raise CutError(f"{label}: the cut plane is not valid.", repr(pl)) from None
    side = _pick(r.get("side", "outside"), SIDES, "flange side", label)
    bolt = _pick(r.get("bolt", "M4"), BOLTS, "bolt size", label)
    fastening = _pick(r.get("fastening", "through_nut"), FASTENINGS, "fastening", label)
    half = _pick(r.get("nut_pocket_half", "B"), ("A", "B"), "nut pocket part", label)
    dist = r.get("distribution") or {}
    mode = _pick(dist.get("mode", "count"), ("count", "spacing"), "bolt distribution", label)
    try:
        count = int(dist.get("count", r.get("count", 8)))
        spacing = float(dist.get("spacing", DEFAULT_SPACING))
    except (TypeError, ValueError):
        raise CutError(f"{label}: the bolt count and spacing must be numbers.") from None
    if not COUNT_RANGE[0] <= count <= COUNT_RANGE[1]:
        raise CutError(f"{label}: the bolt count must be between {COUNT_RANGE[0]} and {COUNT_RANGE[1]}.")
    if not SPACING_RANGE[0] <= spacing <= SPACING_RANGE[1]:
        raise CutError(f"{label}: the bolt spacing must be between {SPACING_RANGE[0]:g} and "
                       f"{SPACING_RANGE[1]:g} mm.")
    params = check_params(r.get("params"), label)
    settings = {"plane": {"origin": plane.origin.tolist(), "normal": plane.normal.tolist(),
                          "x_axis": plane.R[0].tolist()},
                "side": side, "bolt": bolt, "fastening": fastening, "nut_pocket_half": half,
                "distribution": {"mode": mode, "count": count, "spacing": spacing}, "params": params}
    return Cut(i, plane, side, derive(bolt, fastening, params), mode, count, spacing, half, settings)


# ---------------------------------------------------------------- 2D helpers (from the POC)
def _cs_to_shapely(cs: CrossSection):
    pos, neg = [], []
    for p in cs.to_polygons():
        p = np.asarray(p)
        if len(p) < 3:
            continue
        a = 0.5 * np.sum(p[:, 0] * np.roll(p[:, 1], -1) - np.roll(p[:, 0], -1) * p[:, 1])
        (pos if a > 0 else neg).append(Polygon(p).buffer(0))
    g = unary_union(pos)
    return g.difference(unary_union(neg)) if neg else g


def _shapely_to_cs(g) -> CrossSection:
    contours = []
    for poly in getattr(g, "geoms", [g]):
        if poly.is_empty or poly.geom_type != "Polygon" or poly.area < 1e-6:
            continue
        poly = orient(poly, 1.0)
        contours.append(np.asarray(poly.exterior.coords)[:-1])
        contours += [np.asarray(r.coords)[:-1] for r in poly.interiors]
    return CrossSection(contours)


def _chains(seg: np.ndarray) -> list[LineString]:
    """Merge outline segments into polylines (chains); drop crumbs under 1 mm."""
    if len(seg) == 0:
        return []
    g = linemerge(shapely.multilinestrings(shapely.linestrings(seg)))
    parts = [g] if g.geom_type == "LineString" else list(getattr(g, "geoms", []))
    return [ch for ch in parts if ch.length > 1.0]


def _outward_sign(ch: LineString, tree: shapely.STRtree, n2: np.ndarray) -> float:
    """+1 if the wall-face normals behind this chain point to its left, else -1."""
    p = np.asarray(ch.coords)
    d = np.diff(p, axis=0)
    length = np.linalg.norm(d, axis=1)
    k = length > 1e-9
    left = np.c_[-d[k, 1], d[k, 0]] / length[k, None]
    nearest = tree.nearest(shapely.points((p[:-1][k] + p[1:][k]) / 2))   # segment behind each piece
    return 1.0 if np.sum(np.einsum("ij,ij->i", left, n2[nearest]) * length[k]) > 0 else -1.0


def _side_band(ch: LineString, s: float, dist: float, join: str = "round"):
    """Band of width `dist` on side s of a chain (+1 left, -1 right).

    Closed rings go through polygon offsets: GEOS single-sided buffers misbehave on closed rings.
    """
    if not ch.is_closed:
        return ch.buffer(s * dist, single_sided=True, join_style=join, quad_segs=16)
    poly = Polygon(ch.coords).buffer(0)
    p = np.asarray(ch.coords)
    d = np.diff(p, axis=0)
    j = int(np.argmax(np.linalg.norm(d, axis=1)))
    probe = (p[j] + p[j + 1]) / 2 + s * 1e-3 * np.array([-d[j, 1], d[j, 0]]) / np.linalg.norm(d[j])
    if poly.contains(Point(probe)):
        return poly.difference(poly.buffer(-dist, join_style=join, quad_segs=16))
    return poly.buffer(dist, join_style=join, quad_segs=16).difference(poly)


# ---------------------------------------------------------------- 1. section
@dataclass
class Section:
    """What one plane sees of the mold. Cached per model, plane, side, thickness and tool clearance."""

    plane: Plane
    area: object                 # shapely geometry of the wall section
    cavity: np.ndarray           # 2D reference point inside the cavity (centroid of the convex hull)
    f_chains: list               # chains on the flange side, with their outward signs
    f_signs: list
    g_chains: list               # chains on the cavity side (guard band)
    g_signs: list
    g_top: tuple                 # (chains, signs) on the cavity side at z = +T and z = -T: guard bands
    g_bottom: tuple              # that keep each flange half out of a cavity behind a leaning wall
    lean_p95: float              # 95th percentile of the flange-side wall angle from square, deg
    shadow: object               # mold just beyond both flange faces, seen along the normal (tool space)


def section(model: Model, cut: Cut) -> Section:
    d = cut.d
    key = (tuple(cut.plane.origin), tuple(cut.plane.R.ravel()), cut.side, d.thickness, d.tool_clearance)
    if key not in model.cache:
        if len(model.cache) > 32:
            model.cache.pop(next(iter(model.cache)))
        model.cache[key] = _section(model, cut)
    return model.cache[key]


def _segments(model: Model, plane: Plane, dots: np.ndarray, z: float) -> tuple:
    """Outline at height z as 2D segments, each with the in-plane normal and lean of the mesh face
    behind it (POC step 1). Coordinates rounded to 1e-5 mm so chains merge cleanly."""
    s = z + _segment_shift(dots - z)
    lines, fidx = trimesh.intersections.mesh_plane(model.mesh, plane.normal, plane.origin + s * plane.normal,
                                                   return_faces=True, cached_dots=dots - s)
    seg = np.round(plane.local(lines.reshape(-1, 3)).reshape(-1, 2, 3)[:, :, :2], 5)
    nf = model.normals[fidx] @ plane.R.T
    keep = np.linalg.norm(seg[:, 1] - seg[:, 0], axis=1) > 1e-9
    seg, nf = seg[keep], nf[keep]
    n2 = nf[:, :2] / np.maximum(np.linalg.norm(nf[:, :2], axis=1), 1e-9)[:, None]
    return seg, n2, np.degrees(np.arcsin(np.clip(np.abs(nf[:, 2]), 0, 1)))


def _split_chains(seg: np.ndarray, n2: np.ndarray, cavity: np.ndarray, side: str) -> tuple:
    """Chains on the flange side and on the guard side, each with its outward signs (POC step 2):
    a segment faces away from the cavity when its normal points away from the reference point."""
    if len(seg) == 0:
        return [], [], [], [], np.zeros(0, bool)
    mids = seg.mean(axis=1)
    away = np.einsum("ij,ij->i", n2, mids - cavity) > 0
    mask = away if side == "outside" else ~away
    tree = shapely.STRtree(shapely.points(mids))
    f, g = _chains(seg[mask]), _chains(seg[~mask])
    return (f, [_outward_sign(c, tree, n2) for c in f], g, [_outward_sign(c, tree, n2) for c in g], mask)


def _section(model: Model, cut: Cut) -> Section:
    plane = cut.plane
    dots = (model.mesh.vertices - plane.origin) @ plane.normal
    if dots.min() > -0.05 or dots.max() < 0.05:
        raise CutError(f"{cut.label}: the cut plane does not go through the model. Move it so it crosses "
                       "the model.", f"vertex distances {dots.min():.3f} .. {dots.max():.3f}")
    mp = model.manifold.transform(plane.to_plane())               # this cut plane is now z = 0
    area = _cs_to_shapely(mp.slice(0.0))
    seg, n2, lean = _segments(model, plane, dots, 0.0)
    if area.is_empty or len(seg) == 0:
        raise CutError(f"{cut.label}: the cut plane does not go through the model.")
    cavity = np.asarray(area.convex_hull.centroid.coords[0])
    f_chains, f_signs, g_chains, g_signs, mask = _split_chains(seg, n2, cavity, cut.side)
    if not mask.any():
        raise CutError(f"{cut.label}: no wall faces the {cut.side} here. Choose the other flange side.")
    if not f_chains:
        raise CutError(f"{cut.label}: the cut is too small for a flange. Move the cut plane.")
    # cavity-side chains at the outer face of each flange half (z = +T and -T), for leaning walls
    T, H = cut.d.thickness, cut.d.tool_clearance
    g_levels = []
    for z in (T, -T):
        seg_z, n2_z, _ = _segments(model, plane, dots, z)
        g_levels.append(_split_chains(seg_z, n2_z, cavity, cut.side)[2:4])

    shadow = None
    if H > 0:
        # The POC's tool cylinder runs from 0.01 mm above each flange face over the tool clearance H.
        # It hits the mold exactly when its disc hits the mold's shadow over that height range, so
        # one boolean + projection per cut replaces a 3D boolean per bolt and per bolt-line step.
        x0, y0, _, x1, y1, _ = mp.bounding_box()
        size = (x1 - x0 + 2, y1 - y0 + 2, H)
        boxes = (Manifold.cube(size).translate((x0 - 1, y0 - 1, T + 0.01))
                 + Manifold.cube(size).translate((x0 - 1, y0 - 1, -T - 0.01 - H)))
        shadow = _cs_to_shapely((mp ^ boxes).project())
    return Section(plane, area, cavity, f_chains, f_signs, g_chains, g_signs, g_levels[0], g_levels[1],
                   float(np.percentile(lean[mask], 95)), shadow)


# ---------------------------------------------------------------- crossings
@dataclass
class Band:
    """Bolt-free band around the line where another cut crosses this one: |a . p - c| < half."""

    other: int
    a: np.ndarray
    c: float
    half: float


def _bands(cut: Cut, others: list[Cut]) -> list[Band]:
    """One band per crossing cut.

    A point at in-plane distance d from the crossing line, lifted by z along this normal, is
    |d sin(t) + z cos(t)| from the other plane (t = angle between the planes). Holes, washers and
    tools reach |z| <= T + H, so they clear the other flange (|dist| <= T_other) when
    |d| >= (T_other + r + (T + H) |cos t|) / sin t.
    """
    out = []
    for o in others:
        nj = cut.plane.R @ o.plane.normal                 # other normal, in this cut's frame
        sin = float(np.hypot(nj[0], nj[1]))
        if sin < PARALLEL_SIN:
            continue
        a = nj[:2] / sin
        c = float(nj @ cut.plane.local(o.plane.origin)) / sin
        reach = cut.d.thickness + max(cut.d.tool_clearance, 1.0)
        half = (o.d.thickness + cut.d.envelope_r + reach * abs(nj[2])) / sin + 0.5
        out.append(Band(o.index, a, c, half))
    return out


def _band_shape(b: Band, half: float, size: float = 1e4):
    t = np.array([-b.a[1], b.a[0]])
    mid = b.a * b.c
    return LineString([mid - size * t, mid + size * t]).buffer(half, cap_style="flat")


# ---------------------------------------------------------------- 2. layout
@dataclass
class Layout:
    """Flange outline and bolts of one cut, after moving the bolt line out where needed."""

    flange: object               # outline on the cut face (both halves)
    flange_a: object             # outline of the side-A half (z 0..T) and of the side-B half (z -T..0);
    flange_b: object             # the same object as `flange` unless a leaning wall trimmed one of them
    pts: np.ndarray              # (n, 2) bolt positions in plane coordinates
    c: float                     # bolt line distance from the wall
    W: float                     # flange width
    moved: float                 # how far the bolt line was moved out, mm
    blocked: list[int]           # bolt indices whose tool space is blocked
    suggested: int
    bands: list[Band]
    warnings: list[dict] = field(default_factory=list)

    @property
    def uniform(self) -> bool:
        return self.flange_a is self.flange and self.flange_b is self.flange


def _prolonged(ch: LineString, d: float) -> LineString:
    """An open chain extended by d along its end directions (closed rings are returned as is)."""
    if ch.is_closed:
        return ch
    p = np.asarray(ch.coords)
    k = min(1.0, ch.length / 2)
    t0 = p[0] - np.asarray(ch.interpolate(k).coords[0])
    t1 = p[-1] - np.asarray(ch.interpolate(ch.length - k).coords[0])
    return LineString(np.vstack([p[0] + d * t0 / np.linalg.norm(t0), p, p[-1] + d * t1 / np.linalg.norm(t1)]))


def _guard(chains: list, signs: list, W: float):
    """Guard band W + 2 on the cavity side of the cavity-facing chains (POC step 3).

    Open chains are prolonged past their ends first: where the wall ends (a rim), the cavity
    beyond the chain end must be guarded too, or a leaning wall lets the flange in off-plane.
    """
    bands = [_side_band(_prolonged(ch, W + 2), s, W + 2) for ch, s in zip(chains, signs)]
    return unary_union(bands) if bands else None


def _flange_outline(sec: Section, d: Derived, W: float) -> tuple:
    """Band W on the flange side + overlap strip into the wall - guard band on the cavity side (POC 3).

    Each half also loses the guard band found at its own outer face (z = +T or -T): where the wall
    leans, the straight flange slab would otherwise poke through the wall into the cavity.
    """
    parts = []
    for ch, s in zip(sec.f_chains, sec.f_signs):
        parts += [_side_band(ch, s, W), _side_band(ch, -s, d.overlap, "mitre")]
    flange = unary_union(parts)
    guard = _guard(sec.g_chains, sec.g_signs, W)
    flange = (flange.difference(guard) if guard is not None else flange).buffer(0)
    halves = []
    for chains, signs in (sec.g_top, sec.g_bottom):
        g = _guard(chains, signs, W)
        half = flange.difference(g).buffer(0) if g is not None else flange
        halves.append(flange if half.area > flange.area - 1e-6 else half)    # unchanged: share the object
    cut_face = flange if halves[0] is flange or halves[1] is flange else unary_union(halves)
    return cut_face, halves[0], halves[1]


def _bolt_lines(sec: Section, c: float, end: float, bands: list[Band]) -> list[tuple]:
    """Bolt line pieces as (line, closed, margin at start, margin at end).

    The bolt line is each flange chain offset by c. Crossing bands cut it into pieces; a piece
    end made by a band needs no margin (the band already holds the washer), an open chain end
    keeps the POC end margin.
    """
    cutter = unary_union([_band_shape(b, b.half) for b in bands]) if bands else None
    out = []
    for ch, s in zip(sec.f_chains, sec.f_signs):
        line = ch.offset_curve(s * c, quad_segs=16, join_style="round")
        if line.is_empty:
            continue
        if line.geom_type != "LineString":
            line = max(line.geoms, key=lambda g: g.length)
        if cutter is None or not line.intersects(cutter):
            out.append((line, ch.is_closed, end, end))
            continue
        rest = line.difference(cutter)
        if rest.is_empty:
            continue
        rest = linemerge(rest) if rest.geom_type == "MultiLineString" else rest
        ends = [] if ch.is_closed else [Point(line.coords[0]), Point(line.coords[-1])]
        for piece in ([rest] if rest.geom_type == "LineString" else list(rest.geoms)):
            m = [end if any(Point(piece.coords[k]).distance(e) < 1e-6 for e in ends) else 0.0 for k in (0, -1)]
            out.append((piece, False, m[0], m[1]))
    return out


def _allocate(n: int, weights: np.ndarray, caps: np.ndarray) -> np.ndarray:
    """Share n bolts over pieces by length: at least 2 per piece where they fit, largest remainder."""
    mins = np.minimum(2, caps)
    ideal = n * weights / max(weights.sum(), 1e-9)
    alloc = np.clip(np.floor(ideal).astype(int), mins, caps)
    while alloc.sum() < n and (alloc < caps).any():
        alloc[np.argmax(np.where(alloc < caps, ideal - alloc, -np.inf))] += 1
    while alloc.sum() > n and (alloc > mins).any():
        alloc[np.argmax(np.where(alloc > mins, alloc - ideal, -np.inf))] -= 1
    return alloc


def _place_bolts(cut: Cut, pieces: list[tuple]) -> tuple[np.ndarray, int, int]:
    """Bolt positions by arc length (POC step 4). Returns points, capacity and suggested count."""
    d = cut.d
    lengths = np.array([p[0].length for p in pieces], float)
    usable = np.array([L if closed else L - m0 - m1 for (_, closed, m0, m1), L in zip(pieces, lengths)])
    caps = np.array([int(u // d.min_spacing) if closed else (int(u // d.min_spacing) + 1 if u >= 0 else 0)
                     for (_, closed, _, _), u in zip(pieces, usable)], int)
    total_cap = int(caps.sum())
    mins = int(np.minimum(2, caps).sum())
    suggested = int(np.clip(max(4, round(usable.clip(0).sum() / SUGGEST_PITCH)), mins, max(total_cap, mins)))
    if cut.mode == "spacing":
        target = np.array([round(u / cut.spacing) if closed else round(u / cut.spacing) + 1
                           for (_, closed, _, _), u in zip(pieces, usable.clip(0))])
        alloc = np.clip(target, np.minimum(2, caps), caps)
    else:
        if 0 < total_cap < cut.count:
            raise CutError(f"{cut.label}: too many bolts. At most {total_cap} {d.bolt.name} bolts fit along "
                           f"this cut, {d.min_spacing:g} mm apart. Lower the bolt count.",
                           f"capacity per piece {caps.tolist()}")
        alloc = _allocate(cut.count, usable.clip(0), caps)
    pts = []
    for (line, closed, m0, _), L, u, n in zip(pieces, lengths, usable, alloc):
        if n <= 0:
            continue
        if closed:
            s = (np.arange(n) + 0.5) * L / n                       # evenly around a ring
        else:
            s = np.linspace(m0, m0 + u, n) if n > 1 else np.array([m0 + u / 2])
        pts.append(shapely.get_coordinates(shapely.line_interpolate_point(line, s)))
    return (np.vstack(pts) if pts else np.zeros((0, 2))), total_cap, suggested


def _blocked(sec: Section, d: Derived, pts: np.ndarray) -> list[int]:
    """Bolts whose tool space beyond a flange face hits the mold (POC rule 8).

    Same cylinder as the POC, tested against the mold just beyond the flange faces instead of
    the finished halves: flange, holes and pockets never reach past |z| = T + 1, so the 3D solid
    needs to be built only once, after the bolt line has found its place.
    """
    if sec.shadow is None or sec.shadow.is_empty or len(pts) == 0:
        return []
    discs = shapely.buffer(shapely.points(pts), d.envelope_r, quad_segs=16)
    return [int(i) for i in np.flatnonzero(shapely.area(shapely.intersection(discs, sec.shadow)) > BLOCK_AREA)]


def layout(cut: Cut, sec: Section, bands: list[Band]) -> Layout:
    """Flange and bolts of one cut. Moves the bolt line out 1 mm at a time while tools are blocked."""
    d = cut.d
    best = None
    for k in range(GAP_STEPS + 1):
        c = d.bolt_line(k)
        W = d.flange_width(c)
        pieces = _bolt_lines(sec, c, c + d.bolt.washer / 2 + d.end_margin, bands)
        try:
            pts, _, suggested = _place_bolts(cut, pieces)
        except CutError:
            if k == 0:
                raise
            break                    # a moved bolt line got too short for the count: keep the best so far
        blocked = _blocked(sec, d, pts)
        if best is None or len(blocked) < len(best[3]):
            best = (k, c, W, blocked, pts, suggested)
        if not blocked:
            break
    k, c, W, blocked, pts, suggested = best
    lay = Layout(*_flange_outline(sec, d, W), pts, c, W, float(k), blocked, suggested, bands,
                 [dict(w, message=f"{cut.label}: {w['message'][0].lower()}{w['message'][1:]}")
                  for w in d.warnings])
    _check_layout(cut, sec, lay)
    return lay


def _check_layout(cut: Cut, sec: Section, lay: Layout) -> None:
    """Plain-language warnings for one cut (POC checks plus the new ones)."""
    d, w, label = cut.d, lay.warnings, cut.label
    n = len(lay.pts)
    if sec.lean_p95 > d.max_wall_angle:
        w.append(warning("wall_angle", f"{label}: the wall meets the cut at up to {sec.lean_p95:.0f}° from square.",
                         "The flange may not sit flat. Rotate the cut so it crosses the wall more squarely."))
    if n == 0:
        w.append(warning("no_bolts", f"{label}: there is no room for bolts on this cut.",
                         "Move the cut away from the other cuts, or make the flange wider."))
        return
    if cut.mode == "count" and n > cut.count:
        w.append(warning("count_raised", f"{label}: each separate stretch of wall needs 2 bolts, so {n} bolts "
                                         f"are used instead of {cut.count}.", "No action needed."))
    if lay.blocked:
        w.append(warning("bolts_blocked", f"{label}: the wall blocks the tool space at bolt "
                                          f"{_numbers(lay.blocked)}.",
                         "Move or rotate the cut, lower the tool clearance, or check that you can reach these bolts."))
    elif lay.moved > 0:
        w.append(warning("bolt_line_moved", f"{label}: the wall flares over the flange, so the bolt line was "
                                            f"moved out to {lay.c:.1f} mm from the wall.",
                         "No action needed. The flange is wider than usual."))
    if n > 1:
        dist = np.linalg.norm(lay.pts[:, None] - lay.pts[None], axis=2) + np.eye(n) * 1e9
        if dist.min() < d.min_spacing - 1e-6:
            w.append(warning("bolts_close", f"{label}: some bolts are only {dist.min():.1f} mm apart.",
                             "Lower the bolt count or use a smaller bolt."))
    discs = shapely.buffer(shapely.points(lay.pts), d.bolt.washer / 2, quad_segs=8)
    both = (lay.flange if lay.uniform else lay.flange_a.intersection(lay.flange_b)).buffer(0.05)  # a washer each side
    bad = shapely.intersects(discs, sec.area) | ~shapely.contains(both, discs)
    if bad.any():
        w.append(warning("washer_fit", f"{label}: the washer does not fit on the flange at bolt "
                                       f"{_numbers(np.flatnonzero(bad))}.",
                         "Make the flange wider, or move the cut."))


def _numbers(idx) -> str:
    return ", ".join(str(int(i) + 1) for i in idx)


# ---------------------------------------------------------------- all cuts together
def _flange_solid(cut: Cut, lay: Layout) -> Manifold:
    """Flange slab straddling the plane, from -T to +T (POC step 5), in plane coordinates.

    Where a leaning wall gave the two halves different outlines, one slab is built over both
    outlines and each half loses the other half's extra strip, from 0.02 mm off the plane outwards.
    The trimming prisms reach past the slab edge, so no two faces coincide (coplanar faces break
    after float32 STL export) and no face lies on the plane where the parts are split later.
    """
    T, e = cut.d.thickness, 0.02
    slab = _shapely_to_cs(lay.flange).extrude(2 * T).translate((0, 0, -T))
    if lay.uniform:
        return slab
    for own, other, z0 in ((lay.flange_a, lay.flange_b, e), (lay.flange_b, lay.flange_a, -T - 1)):
        extra = other.difference(own)
        if extra.area > 1e-3:
            trim = extra.buffer(0.5).difference(own)
            slab -= _shapely_to_cs(trim).extrude(T + 1 - e).translate((0, 0, z0))
    return slab


def _fastening_cutters(cut: Cut, lay: Layout) -> Manifold | None:
    """Bolt holes and, for nut pockets, hexagonal pockets, in plane coordinates."""
    if not len(lay.pts):
        return None
    d, T = cut.d, cut.d.thickness
    tools = [Manifold.cylinder(2 * T + 2, d.hole_d / 2, -1, 48, True).translate((x, y, 0.0)) for x, y in lay.pts]
    if d.fastening.nut_pocket:
        # pocket from the outer face of the chosen part's flange, 1 mm past it for a clean cut;
        # a 6-sided cylinder is a hexagon with circumradius = across flats / sqrt(3)
        h = d.pocket_depth + 1.0
        z0 = T - d.pocket_depth if cut.pocket_half == "A" else -T - 1.0
        hexa = Manifold.cylinder(h, d.pocket_af / math.sqrt(3), -1, 6)
        tools += [hexa.translate((x, y, z0)) for x, y in lay.pts]
    return Manifold.batch_boolean(tools, OpType.Add)


def _check_pairs(model: Model, cuts: list[Cut]) -> list[dict]:
    """Errors and warnings that involve two cuts."""
    out = []
    lo, hi = model.bounds
    for i, a in enumerate(cuts):
        for b in cuts[i + 1:]:
            sin = float(np.linalg.norm(np.cross(a.plane.normal, b.plane.normal)))
            if sin < PARALLEL_SIN:
                gap = abs(float(a.plane.normal @ (b.plane.origin - a.plane.origin)))
                need = a.d.thickness + b.d.thickness + 2.0
                if gap < need:
                    raise CutError(f"{a.label} and {b.label} are too close together. Move them at least "
                                   f"{math.ceil(need)} mm apart.", f"gap {gap:.2f} mm")
                room = a.d.thickness + b.d.thickness + max(a.d.tool_clearance, b.d.tool_clearance)
                if gap < room:
                    out.append(warning("cuts_close", f"{a.label} and {b.label} are only {gap:.0f} mm apart, so "
                                                     "there may be no room for tools between their flanges.",
                                       f"Move them at least {math.ceil(room)} mm apart, or lower the tool clearance."))
            elif math.degrees(math.asin(min(sin, 1.0))) < SHALLOW_DEG and _crossing_in_box(a.plane, b.plane, lo, hi):
                out.append(warning("shallow_crossing", f"{a.label} and {b.label} cross at a shallow angle "
                                                       f"({math.degrees(math.asin(sin)):.0f}°).",
                                   "The flanges merge where the cuts cross. Make the cuts cross at more than 30°."))
    return out


def _crossing_in_box(pa: Plane, pb: Plane, lo: np.ndarray, hi: np.ndarray, margin: float = 20.0) -> bool:
    """Does the line where two planes meet pass through the model's box (slab test)?"""
    d = np.cross(pa.normal, pb.normal)
    p0 = np.linalg.lstsq(np.vstack([pa.normal, pb.normal]), np.array([pa.offset, pb.offset]), rcond=None)[0]
    t0, t1 = -np.inf, np.inf
    for k in range(3):
        if abs(d[k]) < 1e-12:
            if not lo[k] - margin <= p0[k] <= hi[k] + margin:
                return False
            continue
        ta, tb = sorted(((lo[k] - margin - p0[k]) / d[k], (hi[k] + margin - p0[k]) / d[k]))
        t0, t1 = max(t0, ta), min(t1, tb)
    return t0 <= t1


def _cross_flange_check(cuts: list[Cut], secs: list[Section], lays: list[Layout]) -> None:
    """Warn when another cut's flange sits in the space a bolt needs (hole, washer, tool)."""
    if len(cuts) < 2:
        return
    slabs = [_flange_solid(c, l).transform(s.plane.to_world()) for c, s, l in zip(cuts, secs, lays)]
    for c, s, lay in zip(cuts, secs, lays):
        if not len(lay.pts):
            continue
        reach, r = c.d.thickness + max(c.d.tool_clearance, 1.0), c.d.envelope_r
        cyl = Manifold.cylinder(2 * reach, r, -1, 32, True)
        for o, slab in zip(cuts, slabs):
            if o is c:
                continue
            other = slab.transform(s.plane.to_plane())
            bb = np.asarray(other.bounding_box())
            hits = [i for i, (x, y) in enumerate(lay.pts)
                    if bb[0] < x + r and x - r < bb[3] and bb[1] < y + r and y - r < bb[4]
                    and bb[2] < reach and -reach < bb[5]
                    and (other ^ cyl.translate((x, y, 0.0))).volume() > BLOCK_VOLUME]
            if hits:
                lay.warnings.append(warning(
                    "flange_in_way", f"{c.label}: the flange of {o.label} is in the way at bolt {_numbers(hits)}.",
                    "Move the cuts further apart, or use fewer bolts so they stay away from the other cut."))


def plan(model: Model, cuts: list[Cut]) -> tuple[list[Section], list[Layout], list[dict]]:
    """Sections, flange outlines and bolts for all cuts. Shared by preview and generate."""
    pair_warnings = _check_pairs(model, cuts)
    secs = [section(model, c) for c in cuts]
    lays = [layout(c, s, _bands(c, [o for o in cuts if o is not c])) for c, s in zip(cuts, secs)]
    _cross_flange_check(cuts, secs, lays)
    return secs, lays, pair_warnings


# ---------------------------------------------------------------- preview (2D only)
def _polys(g, digits: int = 3) -> list[dict]:
    out = []
    for poly in getattr(g, "geoms", [g]):
        if poly.is_empty or poly.geom_type != "Polygon":
            continue
        out.append({"exterior": np.round(np.asarray(poly.exterior.coords), digits).tolist(),
                    "holes": [np.round(np.asarray(r.coords), digits).tolist() for r in poly.interiors]})
    return out


def _cut_json(cut: Cut, sec: Section, lay: Layout) -> dict:
    x0, y0, x1, y1 = sec.area.bounds
    m = lay.W + 40
    view = box(x0 - m, y0 - m, x1 + m, y1 + m)
    bands = []
    for b in lay.bands:      # crossing lines, clipped to the area around the section, for drawing
        t, mid = np.array([-b.a[1], b.a[0]]), b.a * b.c
        seg = LineString([mid - 1e4 * t, mid + 1e4 * t]).intersection(view)
        if seg.geom_type == "LineString" and not seg.is_empty:
            bands.append({"cut": b.other, "line": np.round(np.asarray(seg.coords), 3).tolist(),
                          "half_width": round(b.half, 2)})
    xyz = sec.plane.world(lay.pts) if len(lay.pts) else np.zeros((0, 3))
    derived = cut.d.as_dict() | {"bolt_line": round(lay.c, 2), "width": round(lay.W, 2), "moved": lay.moved}
    return {"frame": sec.plane.as_json(), "section": _polys(sec.area), "flange": _polys(lay.flange),
            "bolts": [{"xy": p.round(3).tolist(), "xyz": q.round(3).tolist(), "blocked": i in lay.blocked}
                      for i, (p, q) in enumerate(zip(lay.pts, xyz))],
            "bands": bands, "cavity": sec.cavity.round(3).tolist(), "derived": derived,
            "count": int(len(lay.pts)), "suggested_count": lay.suggested, "warnings": lay.warnings}


def preview(model: Model, cuts: list[Cut]) -> dict:
    """Fast 2D preview of every cut: section, flange outline, bolts, warnings."""
    t0 = time.perf_counter()
    secs, lays, pair_warnings = plan(model, cuts)
    return {"cuts": [_cut_json(c, s, l) for c, s, l in zip(cuts, secs, lays)],
            "warnings": pair_warnings, "ms": round(1000 * (time.perf_counter() - t0))}


# ---------------------------------------------------------------- generate (3D)
@dataclass
class Part:
    """One printed part: a cell of the cut planes. `matrix` maps world -> print position."""

    name: str
    solid: Manifold              # world coordinates
    signature: str               # side of each cut, e.g. "AB"
    matrix: np.ndarray           # 4x4 world -> print (cut face down on the print sheet, z min = 0)
    down: tuple[int, str] | None # (cut index, side) of the face that lies on the print sheet
    touches: list[int]           # cuts that have a face on this part
    explode: np.ndarray
    bodies: int
    _stl: bytes | None = None

    def print_solid(self) -> Manifold:
        return self.solid.transform(self.matrix[:3])

    def stl(self) -> bytes:
        if self._stl is None:
            self._stl = stl_bytes(self.print_solid())
        return self._stl


@dataclass
class Result:
    cuts: list[Cut]
    secs: list[Section]
    lays: list[Layout]
    parts: list[Part]
    warnings: list[dict]
    seconds: float


def _part_name(k: int) -> str:
    return chr(65 + k) if k < 26 else chr(64 + k // 26) + chr(65 + k % 26)


def _make_part(name: str, solid: Manifold, sig: str, cuts: list[Cut], secs: list[Section],
               center: np.ndarray) -> Part:
    mesh = solid.to_mesh()
    tri = np.asarray(mesh.vert_properties)[:, :3][np.asarray(mesh.tri_verts)]
    cross = np.cross(tri[:, 1] - tri[:, 0], tri[:, 2] - tri[:, 0])
    area2 = np.linalg.norm(cross, axis=1)
    fn = cross / np.maximum(area2, 1e-12)[:, None]
    faces = []        # (area, cut index, side) of every cut face on this part
    for i, sec in enumerate(secs):
        n, side = sec.plane.normal, sig[i]
        on = np.all(np.abs(tri @ n - sec.plane.offset) < 1e-4, axis=1)
        outward = -n if side == "A" else n          # a part on side A faces the plane with -normal
        a = 0.5 * area2[on & (fn @ outward > 0.99)].sum()
        if a > 1.0:
            faces.append((a, i, side))
    touches = sorted(i for _, i, _ in faces)
    if faces:   # the largest cut face goes down on the print sheet (spec rule 11 for one cut)
        _, i, side = max(faces)
        m = np.vstack([secs[i].plane.to_plane(), [0, 0, 0, 1]])
        m = FLIP @ m if side == "B" else m
        down = (i, side)
        explode = sum((1 if s == "A" else -1) * secs[j].plane.normal for _, j, s in faces)
    else:
        m, down, explode = np.eye(4), None, np.zeros(3)
    m[2, 3] -= solid.transform(m[:3]).bounding_box()[2]   # double precision; to_mesh() is float32
    verts = np.asarray(mesh.vert_properties)[:, :3]
    if np.linalg.norm(explode) < 1e-6:
        explode = verts.mean(axis=0) - center
    explode = explode / max(np.linalg.norm(explode), 1e-9)
    return Part(name, solid, sig, m, down, touches, explode, len(solid.decompose()))


def generate(model: Model, cuts: list[Cut], progress: Progress | None = None) -> Result:
    """Full 3D build of all cuts and the parts in print position."""
    t0 = time.perf_counter()
    step = progress or (lambda stage, fraction: None)
    step("Checking the cuts", 0.05)
    secs, lays, warnings = plan(model, cuts)
    step("Adding the flanges", 0.3)
    adds, subs = [model.manifold], []
    for c, s, lay in zip(cuts, secs, lays):
        to_world = s.plane.to_world()
        adds.append(_flange_solid(c, lay).transform(to_world))
        tools = _fastening_cutters(c, lay)
        if tools is not None:
            subs.append(tools.transform(to_world))
    solid = Manifold.batch_boolean(adds, OpType.Add)
    step("Drilling the bolt holes", 0.5)
    if subs:
        solid = solid - Manifold.batch_boolean(subs, OpType.Add)
    step("Splitting the parts", 0.7)
    cells = [(solid, "")]
    for s in secs:      # split LAST, by every plane in turn
        nxt = []
        for m, sig in cells:
            a, b = m.split_by_plane(tuple(s.plane.normal), s.plane.offset)
            nxt += [(a, sig + "A"), (b, sig + "B")]
        cells = [(m, sig) for m, sig in nxt if not m.is_empty() and m.volume() > 1e-3]
    if len(cells) < 2:
        raise CutError("The cuts do not divide the model into parts. Move the cut planes into the model.")
    step("Checking the parts", 0.85)
    center = model.bounds.mean(axis=0)
    cells.sort(key=lambda t: t[1])
    parts = [_make_part(_part_name(k), m, sig, cuts, secs, center) for k, (m, sig) in enumerate(cells)]
    for p in parts:
        if p.solid.volume() < SMALL_PART_CM3 * 1000:
            warnings.append(warning("small_part", f"Part {p.name} is very small "
                                                  f"({p.solid.volume() / 1000:.2f} cm³).",
                                    "Check the cut positions. A cut may run along the edge of the model."))
    for lay in lays:
        warnings += lay.warnings
    step("Done", 1.0)
    return Result(cuts, secs, lays, parts, warnings, time.perf_counter() - t0)


# ---------------------------------------------------------------- report
def hardware(cuts: list[Cut], lays: list[Layout]) -> list[dict]:
    """Parts list: bolts by size and length, nuts and washers by size."""
    bolts, nuts, washers = Counter(), Counter(), Counter()
    for c, lay in zip(cuts, lays):
        n, b = len(lay.pts), c.d.bolt.name
        if not n:
            continue
        length = f"{c.d.bolt_length} mm" if c.d.bolt_length else f"{math.ceil(c.d.grip + 2)} mm or longer"
        bolts[f"{b} × {length}"] += n
        nuts[b] += n
        washers[b] += n * c.d.fastening.washers_per_bolt
    return ([{"item": "Bolt", "spec": k, "qty": v} for k, v in sorted(bolts.items())]
            + [{"item": "Nut", "spec": f"{k} (ISO 4032)", "qty": v} for k, v in sorted(nuts.items())]
            + [{"item": "Washer", "spec": f"{k} (ISO 7089)", "qty": v} for k, v in sorted(washers.items())])


def cut_report(cut: Cut, lay: Layout) -> dict:
    d = cut.d
    return {"cut": cut.index + 1, "side": cut.side, "bolt": d.bolt.name, "fastening": d.fastening.label,
            "nut_pocket_part_side": cut.pocket_half if d.fastening.nut_pocket else None,
            "bolts": int(len(lay.pts)), "requested": cut.count if cut.mode == "count" else None,
            "spacing": cut.spacing if cut.mode == "spacing" else None,
            "flange_thickness": round(d.thickness, 2), "flange_width": round(lay.W, 2),
            "bolt_line": round(lay.c, 2), "bolt_line_moved": lay.moved, "hole_diameter": round(d.hole_d, 2),
            "bolt_length": d.bolt_length, "pocket": ({"across_flats": d.pocket_af, "depth": d.pocket_depth}
                                                     if d.fastening.nut_pocket else None)}


def part_report(p: Part, cuts: list[Cut]) -> dict:
    ps = p.print_solid()
    bb = np.asarray(ps.bounding_box())
    sides = ", ".join(f"side {p.signature[i]} of cut {i + 1}" for i in p.touches)
    return {"name": p.name, "sides": sides, "volume_cm3": round(p.solid.volume() / 1000, 2),
            "size": (bb[3:] - bb[:3]).round(1).tolist(), "triangles": int(p.solid.num_tri()),
            "bodies": p.bodies, "face_down": (f"cut {p.down[0] + 1}" if p.down else None)}


def report_text(result: Result, model_name: str, sha256: str, version: str, files: dict[str, str]) -> str:
    """report.txt for the ZIP: plain text a field user can print."""
    lines = [f"Flange Cutter {version} report", f"Model: {model_name}", f"SHA-256: {sha256}", ""]
    for c, lay in zip(result.cuts, result.lays):
        r = cut_report(c, lay)
        lines += [f"Cut {r['cut']}: {r['bolts']} x {r['bolt']} bolts, {r['fastening'].lower()}, flange {c.side}",
                  f"  flange {r['flange_thickness']} mm thick per part, {r['flange_width']} mm wide; "
                  f"bolt line {r['bolt_line']} mm from the wall; holes {r['hole_diameter']} mm",
                  f"  plane origin {np.round(c.plane.origin, 2).tolist()}, normal {np.round(c.plane.normal, 4).tolist()}"]
        if r["pocket"]:
            lines.append(f"  nut pockets in the side-{c.pocket_half} part: {r['pocket']['across_flats']:.1f} mm "
                         f"across flats, {r['pocket']['depth']:.1f} mm deep")
    lines += ["", "Parts (print each one as it is in its file: cut face down, no supports for the flange):"]
    for p in result.parts:
        r = part_report(p, result.cuts)
        lines.append(f"  Part {p.name}: {files.get(p.name, '')}  {r['volume_cm3']} cm³, "
                     f"{' x '.join(f'{v:g}' for v in r['size'])} mm, {r['sides']}"
                     + (f", {r['bodies']} separate pieces" if r["bodies"] > 1 else ""))
    lines += ["", "Hardware:"] + [f"  {h['qty']} x {h['item'].lower()} {h['spec']}"
                                   for h in hardware(result.cuts, result.lays)]
    if result.warnings:
        lines += ["", "Warnings:"] + [f"  - {w['message']} {w['action']}" for w in result.warnings]
    return "\n".join(lines) + "\n"
