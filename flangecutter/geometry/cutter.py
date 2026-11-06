"""Cut a mold with one or more planes and add a bolted flange along every cut.

Port of reference/flange_cut.py (the proof of concept). Every cut is worked out in its own
plane coordinates (cut plane = z 0, cut normal = +z):

1. section   the wall outline in the plane, and which face of the wall looks away from the cavity
2. layout    2D flange outline (a band on ONE face of the wall) and the bolt positions; the bolt
             line moves out while the wall would block a tool (POC rule 8)
3. generate  one 3D build for all cuts: mold + flange slabs - holes - nut pockets, and only
             THEN split by every plane (splitting last keeps every part watertight)

Cuts may cross. Bolts keep far enough from every other cut that their holes, washers, nuts,
bolt ends and tools clear the other cut's flange and hardware (docs/ARCHITECTURE.md).
"""
from __future__ import annotations

import io
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
from shapely.ops import linemerge, substring, unary_union

from . import CutError, warning
from .bolts import (BOLTS, COUNT_RANGE, DEFAULT_SPACING, FASTENINGS, MAX_CUTS, SPACING_RANGE,
                    SUGGEST_PITCH, Derived, check_params, derive)
from .mesh_io import Model, stl_bytes

SIDES = ("outside", "inside")
GAP_STEPS = 6                      # the bolt line may move out by up to 6 x 1 mm
BLOCK_AREA = 0.01                  # mm2: a tool disc overlapping the mold's shadow more is blocked
GRID = 0.01                        # mm: flange outlines are snapped to this grid (see _snap)
SAMPLE = 0.5                       # mm: bolt lines are checked against other cuts at this step
MIN_CROSS_DEG, SHALLOW_DEG = 15.0, 30.0
SMALL_PART_CM3, MOSTLY_FLANGE = 0.5, 0.25
OVERHANG_NZ = -math.cos(math.radians(45))   # faces looking down more steeply than 45 deg need support
FLIP = np.diag([1.0, -1.0, -1.0, 1.0])     # 180 deg about the plane x axis (print position of side B)
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

    Vertices exactly on the plane (e.g. "Across Z" through a ring of vertices) make mesh_plane
    return duplicate edges. Only the 2D outline uses the shifted plane (1 um away); Manifold
    slices and splits at the exact plane, which it handles robustly. Moving the 3D split instead
    leaves 1 um sliver triangles that break after float32 STL export.
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
    pocket_half: str     # "A" or "B": which side of this cut holds the nut pockets
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
        raise CutError(f"You can use up to {MAX_CUTS} cuts.")
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
            raise CutError(f"{label}: {exc.message[0].lower()}{exc.message[1:]}", exc.detail) from None
        raise CutError(f"{label}: the cut plane is not valid.", repr(pl)) from None
    side = _pick(r.get("side", "outside"), SIDES, "flange side", label)
    bolt = _pick(r.get("bolt", "M4"), BOLTS, "bolt size", label)
    fastening = _pick(r.get("fastening", "through_nut"), FASTENINGS, "fastening", label)
    half = _pick(r.get("nut_pocket_half", "B"), ("A", "B"), "nut pocket side", label)
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


def _polygons(g) -> list[Polygon]:
    return [p for p in getattr(g, "geoms", [g]) if p.geom_type == "Polygon" and not p.is_empty]


def _snap(g):
    """Snap an outline to a 0.01 mm grid.

    Outline vertices made from chain ends lie exactly on the mold surface (e.g. at a rim). Manifold
    then leaves edges under 1 um that collapse in a float32 STL, so the part is no longer watertight.
    Snapping moves them into general position (5 um at most).
    """
    return shapely.set_precision(g, GRID)


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


def _prolonged(ch: LineString, d: float) -> LineString:
    """An open chain extended by d along its end directions (closed rings are returned as is)."""
    if ch.is_closed:
        return ch
    p = np.asarray(ch.coords)
    k = min(1.0, ch.length / 2)
    t0 = p[0] - np.asarray(ch.interpolate(k).coords[0])
    t1 = p[-1] - np.asarray(ch.interpolate(ch.length - k).coords[0])
    return LineString(np.vstack([p[0] + d * t0 / np.linalg.norm(t0), p, p[-1] + d * t1 / np.linalg.norm(t1)]))


# ---------------------------------------------------------------- 1. section
@dataclass
class Section:
    """What one plane sees of the mold. Cached per model, plane, side, thickness and tool clearance."""

    plane: Plane
    area: object                 # shapely geometry of the wall section
    cavities: np.ndarray         # (k, 2) cavity reference per connected region (centroid of its hull)
    points: np.ndarray           # (n, 3) section outline in world coordinates
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


def _split_chains(seg, n2, regions: shapely.STRtree, refs: np.ndarray, side: str) -> tuple:
    """Chains on the flange side and on the guard side, each with its outward signs (POC step 2).

    A segment faces away from the cavity when its normal points away from the reference point of
    its own region of the section (each separate wall loop has its own cavity).
    """
    if len(seg) == 0:
        return [], [], [], [], np.zeros(0, bool)
    mids = seg.mean(axis=1)
    ref = refs[regions.nearest(shapely.points(mids))]
    away = np.einsum("ij,ij->i", n2, mids - ref) > 0
    mask = away if side == "outside" else ~away
    tree = shapely.STRtree(shapely.points(mids))
    f, g = _chains(seg[mask]), _chains(seg[~mask])
    return (f, [_outward_sign(c, tree, n2) for c in f], g, [_outward_sign(c, tree, n2) for c in g], mask)


def _section(model: Model, cut: Cut) -> Section:
    plane = cut.plane
    dots = (model.mesh.vertices - plane.origin) @ plane.normal
    if dots.min() > -0.05 or dots.max() < 0.05:
        raise CutError(f"{cut.label} does not go through the model. Drag it onto the model or delete it.",
                       f"vertex distances {dots.min():.3f} .. {dots.max():.3f}")
    mp = model.manifold.transform(plane.to_plane())               # this cut plane is now z = 0
    area = _cs_to_shapely(mp.slice(0.0))
    polys = [p for p in _polygons(area) if p.area > 1e-6]
    seg, n2, lean = _segments(model, plane, dots, 0.0)
    if not polys or len(seg) == 0:
        raise CutError(f"{cut.label} does not go through the model. Drag it onto the model or delete it.")
    regions = shapely.STRtree([p.boundary for p in polys])
    refs = np.array([p.convex_hull.centroid.coords[0] for p in polys])
    f_chains, f_signs, g_chains, g_signs, mask = _split_chains(seg, n2, regions, refs, cut.side)
    if not mask.any():
        raise CutError(f"{cut.label}: no wall faces the {cut.side} here. Choose the other flange side.")
    if not f_chains:
        raise CutError(f"{cut.label}: the cut is too small for a flange. Move the cut.")
    # cavity-side chains at the outer face of each flange half (z = +T and -T), for leaning walls
    T, H = cut.d.thickness, cut.d.tool_clearance
    g_levels = []
    for z in (T, -T):
        seg_z, n2_z, _ = _segments(model, plane, dots, z)
        g_levels.append(_split_chains(seg_z, n2_z, regions, refs, cut.side)[2:4])
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
    outline = np.vstack([np.asarray(r.coords) for p in polys for r in [p.exterior, *p.interiors]])
    return Section(plane, area, refs, plane.world(outline), f_chains, f_signs, g_chains, g_signs,
                   g_levels[0], g_levels[1], float(np.percentile(lean[mask], 95)), shadow)


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
    blocked: list[int]           # bolts whose tool space is blocked by the wall
    suggested: int
    lines: list                  # the whole bolt line(s) ...
    pieces: list                 # ... and the stretches where bolts may go (clear of other cuts)
    empty: int                   # stretches too short for any bolt
    culprits: set                # other cuts that kept bolts away from stretches of this cut
    warnings: list[dict] = field(default_factory=list)

    @property
    def uniform(self) -> bool:
        return self.flange_a is self.flange and self.flange_b is self.flange


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
    Returns (cut face outline, side-A outline, side-B outline).
    """
    parts = []
    for ch, s in zip(sec.f_chains, sec.f_signs):
        parts += [_side_band(ch, s, W), _side_band(ch, -s, d.overlap, "mitre")]
    flange = unary_union(parts)
    guard = _guard(sec.g_chains, sec.g_signs, W)
    flange = _snap(flange.difference(guard) if guard is not None else flange)
    halves = []
    for chains, signs in (sec.g_top, sec.g_bottom):
        g = _guard(chains, signs, W)
        half = _snap(flange.difference(g)) if g is not None else flange
        halves.append(flange if half.area > flange.area - 1e-6 else half)    # unchanged: share the object
    cut_face = flange if halves[0] is flange or halves[1] is flange else _snap(unary_union(halves))
    return cut_face, halves[0], halves[1]


def _clear_of_others(cut: Cut, plane: Plane, line: LineString, others: list[Cut]) -> tuple:
    """Sample a bolt line and keep the points where a bolt clears every other cut.

    A bolt's hardware and tools reach R = T + tool clearance along its own normal and r = washer/2 + 0.5
    sideways. At a sample p, the closest that reaches to another plane j is
    max(0, |dist(p, P_j)| - R |cos|) - r sin  (cos, sin of the angle between the planes); it must
    stay beyond flange j (T_j) and the bolt ends, nuts and heads sticking out of it (protrusion_j).
    """
    s = np.linspace(0.0, line.length, max(2, math.ceil(line.length / SAMPLE) + 1))
    p = plane.world(shapely.get_coordinates(shapely.line_interpolate_point(line, s)))
    d = cut.d
    reach = d.thickness + max(d.tool_clearance, d.protrusion)
    ok, culprits = np.ones(len(s), bool), set()
    for o in others:
        cos = abs(float(plane.normal @ o.plane.normal))
        sin = math.sqrt(max(0.0, 1.0 - cos * cos))
        dist = np.abs((p - o.plane.origin) @ o.plane.normal)
        bad = np.maximum(0.0, dist - reach * cos) - d.envelope_r * sin < o.d.thickness + o.d.protrusion + 0.5
        if bad.any():
            culprits.add(o.index)
            ok &= ~bad
    return s, ok, culprits


def _runs(s: np.ndarray, ok: np.ndarray, closed: bool) -> list[tuple[float, float, bool, bool]]:
    """Stretches (start, end, starts at line start, ends at line end) where ok is True."""
    edges = np.flatnonzero(np.diff(np.r_[0, ok.astype(int), 0]))
    runs = [(s[a], s[b - 1], a == 0, b == len(s)) for a, b in zip(edges[::2], edges[1::2]) if b - 1 > a]
    if closed and len(runs) > 1 and runs[0][2] and runs[-1][3]:      # joined across the ring's seam
        runs = [(runs[-1][0], runs[0][1], False, False)] + runs[1:-1]
    return runs


def _bolt_lines(cut: Cut, sec: Section, c: float, others: list[Cut]) -> tuple[list, list, set]:
    """The bolt line (each flange chain offset by c) and its stretches where bolts may go.

    Stretches are (line, closed, margin at start, margin at end). An open chain end keeps the POC
    end margin; a stretch end made by another cut needs none (the clearance test holds the washer).
    """
    end = c + cut.d.bolt.washer / 2 + cut.d.end_margin
    lines, out, culprits = [], [], set()
    for ch, sgn in zip(sec.f_chains, sec.f_signs):
        line = ch.offset_curve(sgn * c, quad_segs=16, join_style="round")
        if line.is_empty:
            continue
        if line.geom_type != "LineString":
            line = max(line.geoms, key=lambda g: g.length)
        closed = ch.is_closed
        lines.append(line)
        s, ok, who = _clear_of_others(cut, sec.plane, line, others)
        culprits |= who
        if ok.all():
            out.append((line, closed, end, end))
            continue
        for a, b, at_start, at_end in _runs(s, ok, closed):
            if a < b:
                piece = substring(line, a, b)
            else:   # wraps over the seam of a ring
                piece = LineString(list(substring(line, a, line.length).coords) + list(substring(line, 0, b).coords)[1:])
            m0 = end if at_start and not closed else 0.0
            m1 = end if at_end and not closed else 0.0
            out.append((piece, False, m0, m1))
    return lines, out, culprits


def _allocate(n: int, weights: np.ndarray, caps: np.ndarray) -> np.ndarray:
    """Share n bolts over stretches by length: at least 2 per stretch where they fit, largest remainder."""
    mins = np.minimum(2, caps)
    ideal = n * weights / max(weights.sum(), 1e-9)
    alloc = np.clip(np.floor(ideal).astype(int), mins, caps)
    while alloc.sum() < n and (alloc < caps).any():
        alloc[np.argmax(np.where(alloc < caps, ideal - alloc, -np.inf))] += 1
    while alloc.sum() > n and (alloc > mins).any():
        alloc[np.argmax(np.where(alloc > mins, alloc - ideal, -np.inf))] -= 1
    return alloc


def _place_bolts(cut: Cut, pieces: list[tuple], culprits: set) -> tuple[np.ndarray, int, int]:
    """Bolt positions by arc length (POC step 4). Returns points, suggested count, empty stretches."""
    d = cut.d
    if not pieces:
        return np.zeros((0, 2)), 0, 0
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
            near = f" Other cuts keep bolts away from part of it ({_cut_names(culprits)})." if culprits else ""
            raise CutError(f"{cut.label}: too many bolts. At most {total_cap} {d.bolt.name} bolts fit along "
                           f"this cut, {d.min_spacing:g} mm apart.{near} Lower the bolt count.",
                           f"capacity per stretch {caps.tolist()}")
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
    empty = int(np.sum((alloc == 0) & (lengths > 1.0)))
    return (np.vstack(pts) if pts else np.zeros((0, 2))), suggested, empty


def _cut_names(indices) -> str:
    return ", ".join(f"cut {i + 1}" for i in sorted(indices))


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


def layout(cut: Cut, sec: Section, others: list[Cut]) -> Layout:
    """Flange and bolts of one cut. Moves the bolt line out 1 mm at a time while tools are blocked."""
    d = cut.d
    best = None
    for k in range(GAP_STEPS + 1):
        c = d.bolt_line(k)
        lines, pieces, culprits = _bolt_lines(cut, sec, c, others)
        try:
            pts, suggested, empty = _place_bolts(cut, pieces, culprits)
        except CutError:
            if k == 0:
                raise
            break                    # a moved bolt line got too short for the count: keep the best so far
        blocked = _blocked(sec, d, pts)
        if best is None or len(blocked) < len(best[3]):
            best = (k, c, d.flange_width(c), blocked, pts, suggested, lines, pieces, empty, culprits)
        if not blocked:
            break
    k, c, W, blocked, pts, suggested, lines, pieces, empty, culprits = best
    lay = Layout(*_flange_outline(sec, d, W), pts, c, W, float(k), blocked, suggested, lines, pieces,
                 empty, culprits, [dict(w, message=f"{cut.label}: {w['message'][0].lower()}{w['message'][1:]}")
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
        why = f" because {_cut_names(lay.culprits)} {'is' if len(lay.culprits) == 1 else 'are'} too close" \
            if lay.culprits else ""
        w.append(warning("no_bolts", f"{label}: there is no room for bolts on this cut{why}.",
                         "Move the cuts further apart, or lower the tool clearance."))
        return
    if lay.empty:
        w.append(warning("unbolted_stretch", f"{label}: {lay.empty} stretch(es) of this cut are too short for a "
                                             f"bolt, next to {_cut_names(lay.culprits) or 'the end of the flange'}.",
                         "The parts may not be bolted there. Move the cuts so each stretch is longer."))
    if cut.mode == "count" and n > cut.count:
        w.append(warning("count_raised", f"{label}: each separate stretch of the cut needs 2 bolts, so {n} bolts "
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
@dataclass
class Plan:
    """Sections, layouts and errors of all cuts. One bad cut does not stop the others."""

    cuts: list[Cut]
    secs: list[Section | None]
    lays: list[Layout | None]
    errors: list[str | None]
    warnings: list[dict]

    def raise_errors(self) -> None:
        errors = [e for e in self.errors if e]
        if errors:
            raise CutError(" ".join(errors), f"{len(errors)} cut(s) with errors")


def _check_pairs(cuts: list[Cut], secs: list, errors: list) -> list[dict]:
    """Two cuts must either cross inside the wall at a fair angle, or stay clear of each other.

    Decided from where each plane lies relative to the other cut's wall section, never from the
    plane origins (which can sit anywhere on the plane).
    """
    out = []
    for i, a in enumerate(cuts):
        for b in cuts[i + 1:]:
            sa, sb = secs[a.index], secs[b.index]
            if sa is None or sb is None:
                continue
            da = (sa.points - b.plane.origin) @ b.plane.normal
            db = (sb.points - a.plane.origin) @ a.plane.normal
            sin = min(1.0, float(np.linalg.norm(np.cross(a.plane.normal, b.plane.normal))))
            angle = math.degrees(math.asin(sin))
            if da.min() < 0 < da.max() or db.min() < 0 < db.max():          # they cross inside the wall
                if angle < MIN_CROSS_DEG:
                    errors[b.index] = errors[b.index] or (
                        f"{b.label} crosses {a.label.lower()} inside the model at only {angle:.0f}°. Make the cuts "
                        f"cross at {SHALLOW_DEG:.0f}° or more, or move them apart.")
                elif angle < SHALLOW_DEG:
                    out.append(warning("shallow_crossing", f"{a.label} and {b.label.lower()} cross at {angle:.0f}°.",
                                       f"The flanges merge where the cuts cross. Cross at {SHALLOW_DEG:.0f}° or more."))
                continue
            gap = min(np.abs(da).min(), np.abs(db).min())
            widest = max(x.d.flange_width(x.d.bolt_line(GAP_STEPS)) for x in (a, b))
            need = a.d.thickness + b.d.thickness + 2.0 + widest * sin
            if gap < need:
                errors[b.index] = errors[b.index] or (
                    f"{b.label} is too close to {a.label.lower()}: their flanges would overlap. Move the cuts "
                    f"at least {math.ceil(need)} mm apart.")
    return out


def _offcut_warnings(cuts: list[Cut], secs: list, lays: list) -> list[dict]:
    """A plane that crosses another cut's flange outside the wall cuts off a loose piece of flange."""
    out = []
    for a in cuts:
        sa, la = secs[a.index], lays[a.index]
        if la is None:
            continue
        pts = sa.plane.world(shapely.get_coordinates(la.flange))
        for b in cuts:
            if b is a or secs[b.index] is None:
                continue
            df = (pts - b.plane.origin) @ b.plane.normal
            ds = (sa.points - b.plane.origin) @ b.plane.normal
            if df.min() < 0 < df.max() and not ds.min() < 0 < ds.max():
                out.append(warning("flange_offcut", f"{b.label} cuts through the flange of {a.label.lower()} outside "
                                                    "the wall. That piece of flange will be left out.",
                                   f"Move {b.label.lower()} so it crosses the wall of {a.label.lower()}, or away from it."))
    return out


def plan(model: Model, cuts: list[Cut]) -> Plan:
    """Sections, flange outlines and bolts for all cuts. Shared by preview and generate."""
    errors: list[str | None] = [None] * len(cuts)
    secs: list[Section | None] = [None] * len(cuts)
    for c in cuts:
        try:
            secs[c.index] = section(model, c)
        except CutError as exc:
            errors[c.index] = exc.message
    warnings = _check_pairs(cuts, secs, errors)
    lays: list[Layout | None] = [None] * len(cuts)
    for c in cuts:
        if errors[c.index] is None:
            try:
                lays[c.index] = layout(c, secs[c.index], [o for o in cuts if o is not c])
            except CutError as exc:
                errors[c.index] = exc.message
    return Plan(cuts, secs, lays, errors, warnings + _offcut_warnings(cuts, secs, lays))


# ---------------------------------------------------------------- preview (2D only)
def _polys(g, digits: int = 3) -> list[dict]:
    return [{"exterior": np.round(np.asarray(p.exterior.coords), digits).tolist(),
             "holes": [np.round(np.asarray(r.coords), digits).tolist() for r in p.interiors]}
            for p in _polygons(g)]


def _crossings(cut: Cut, sec: Section, lay: Layout, cuts: list[Cut]) -> list[dict]:
    """Where other cuts cross this plane, as 2D lines clipped to the area around the section."""
    x0, y0, x1, y1 = sec.area.bounds
    m = lay.W + 40
    view, out = box(x0 - m, y0 - m, x1 + m, y1 + m), []
    for o in cuts:
        nj = cut.plane.R @ o.plane.normal
        sin = float(np.hypot(nj[0], nj[1]))
        if o is cut or sin < 1e-6:
            continue
        a = nj[:2] / sin
        mid = a * float(nj @ cut.plane.local(o.plane.origin)) / sin
        t = np.array([-a[1], a[0]])
        seg = LineString([mid - 1e4 * t, mid + 1e4 * t]).intersection(view)
        if seg.geom_type == "LineString" and not seg.is_empty:
            out.append({"cut": o.index, "line": np.round(np.asarray(seg.coords), 3).tolist()})
    return out


def cut_json(cut: Cut, sec: Section | None, lay: Layout | None, error: str | None, cuts: list[Cut]) -> dict:
    """Everything the page draws for one cut. A cut with an error has only its frame and the error."""
    if error or lay is None:
        return {"frame": cut.plane.as_json(), "error": error or "This cut could not be laid out."}
    xyz = sec.plane.world(lay.pts) if len(lay.pts) else np.zeros((0, 3))
    derived = cut.d.as_dict() | {"bolt_line": round(lay.c, 2), "width": round(lay.W, 2), "moved": lay.moved}
    return {"frame": sec.plane.as_json(), "error": None,
            "section": _polys(sec.area), "flange": _polys(lay.flange),
            "bolts": [{"xy": p.round(3).tolist(), "xyz": q.round(3).tolist(), "blocked": i in lay.blocked}
                      for i, (p, q) in enumerate(zip(lay.pts, xyz))],
            "bolt_line": [np.round(np.asarray(g.coords), 3).tolist() for g in lay.lines],
            "free": [np.round(np.asarray(p[0].coords), 3).tolist() for p in lay.pieces],
            "crossings": _crossings(cut, sec, lay, cuts), "cavities": sec.cavities.round(3).tolist(),
            "derived": derived, "count": int(len(lay.pts)), "suggested_count": lay.suggested,
            "warnings": lay.warnings}


def preview(model: Model, cuts: list[Cut]) -> dict:
    """Fast 2D preview of every cut: section, flange outline, bolts, warnings, or the cut's error."""
    t0 = time.perf_counter()
    pl = plan(model, cuts)
    return {"cuts": [cut_json(c, s, l, e, cuts) for c, s, l, e in zip(cuts, pl.secs, pl.lays, pl.errors)],
            "warnings": pl.warnings, "ms": round(1000 * (time.perf_counter() - t0))}


# ---------------------------------------------------------------- generate (3D)
@dataclass
class Part:
    """One printed part: one body of a cell of the cut planes. `matrix` maps world -> print position."""

    name: str
    solid: Manifold              # world coordinates
    signature: str               # side of each cut, e.g. "AB"
    matrix: np.ndarray = field(default_factory=lambda: np.eye(4))
    down: tuple[int, str] | None = None   # (cut index, side) of the face on the print sheet
    touches: list[int] = field(default_factory=list)
    explode: np.ndarray = field(default_factory=lambda: np.zeros(3))
    mold_fraction: float = 1.0
    overhang_cm2: float = 0.0
    bolts: int = 0
    _stl: bytes | None = None

    def print_solid(self) -> Manifold:
        return self.solid.transform(self.matrix[:3])

    def stl(self) -> bytes:
        if self._stl is None:
            self._stl = stl_bytes(self.print_solid())
        return self._stl


@dataclass
class Result:
    plan: Plan
    parts: list[Part]
    joins: list[list[tuple[str, str, int]]]      # per cut: (part, part, bolts)
    warnings: list[dict]
    seconds: float

    @property
    def cuts(self) -> list[Cut]:
        return self.plan.cuts

    @property
    def secs(self) -> list[Section]:
        return self.plan.secs

    @property
    def lays(self) -> list[Layout]:
        return self.plan.lays


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
            trim = _snap(extra.buffer(0.5).difference(own))
            slab -= _shapely_to_cs(trim).extrude(T + 1 - e).translate((0, 0, z0))
    return slab


def _fastening_cutters(cut: Cut, lay: Layout) -> Manifold | None:
    """Bolt holes and, for nut pockets, hexagonal pockets, in plane coordinates."""
    if not len(lay.pts):
        return None
    d, T = cut.d, cut.d.thickness
    tools = [Manifold.cylinder(2 * T + 2, d.hole_d / 2, -1, 48, True).translate((x, y, 0.0)) for x, y in lay.pts]
    if d.fastening.nut_pocket:
        # pocket from the outer face of the chosen side's flange, 1 mm past it for a clean cut;
        # a 6-sided cylinder is a hexagon with circumradius = across flats / sqrt(3)
        h = d.pocket_depth + 1.0
        z0 = T - d.pocket_depth if cut.pocket_half == "A" else -T - 1.0
        hexa = Manifold.cylinder(h, d.pocket_af / math.sqrt(3), -1, 6)
        tools += [hexa.translate((x, y, z0)) for x, y in lay.pts]
    return Manifold.batch_boolean(tools, OpType.Add)


def _part_name(k: int) -> str:
    return chr(65 + k) if k < 26 else chr(64 + k // 26) + chr(65 + k % 26)


def _bodies(cell: Manifold) -> list[Manifold]:
    """Separate bodies of a cell. Enclosed voids (negative volume) go back into their body."""
    comps = cell.decompose()
    if len(comps) == 1:
        return comps
    solids = [c for c in comps if c.volume() > 0]
    for void in (c for c in comps if c.volume() <= 0):
        vb = np.asarray(void.bounding_box())
        for k, s in enumerate(solids):
            sb = np.asarray(s.bounding_box())
            if np.all(sb[:3] <= vb[:3]) and np.all(vb[3:] <= sb[3:]):
                solids[k] = Manifold.compose([s, void])
                break
    return solids


def _orient(part: Part, faces: dict, cuts: list[Cut], secs: list[Section]) -> None:
    """Choose which cut face goes on the print sheet: the one that leaves the least overhang.

    With one cut there is one candidate, so this is the spec's rule (side A as is, side B turned
    180 deg about the plane x axis). Ties go to the larger cut face. Also records how much of the
    other cuts' flanges then hangs in the air (`overhang_cm2`).
    """
    if not faces:         # no cut face (a separate body of the original model): keep it as loaded
        part.matrix[2, 3] -= part.solid.bounding_box()[2]
        return
    mesh = part.solid.to_mesh()
    verts, tris = np.asarray(mesh.vert_properties)[:, :3].astype(float), np.asarray(mesh.tri_verts)
    world = verts[tris]
    cross_w = np.cross(world[:, 1] - world[:, 0], world[:, 2] - world[:, 0])
    a2 = np.linalg.norm(cross_w, axis=1)
    normal_w, centre_w = cross_w / np.maximum(a2, 1e-12)[:, None], world.mean(axis=1)
    best = None
    for i, (area, side, _) in faces.items():
        m = np.vstack([secs[i].plane.to_plane(), [0, 0, 0, 1]])
        m = FLIP @ m if side == "B" else m
        m[2, 3] -= part.solid.transform(m[:3]).bounding_box()[2]   # double precision; to_mesh() is float32
        z = world @ m[2, :3] + m[2, 3]                               # print height of every corner
        down = (normal_w @ m[2, :3] < OVERHANG_NZ) & (z.min(axis=1) > 0.1)
        score = 0.5 * a2[down].sum()
        if best is None or (score, -area) < best[0]:
            best = ((score, -area), m, (i, side), down)
    _, part.matrix, part.down, down = best
    flange = np.zeros(len(tris), bool)                # faces lying on another cut's flange (slab face or cut face)
    for j in faces:
        if j != part.down[0]:
            s, T = secs[j].plane, cuts[j].d.thickness
            dist = np.abs(centre_w @ s.normal - s.offset)
            flange |= (np.abs(normal_w @ s.normal) > 0.99) & ((np.abs(dist - T) < 0.05) | (dist < 0.05))
    part.overhang_cm2 = 0.5 * a2[down & flange].sum() / 100


def generate(model: Model, cuts: list[Cut], progress: Progress | None = None) -> Result:
    """Full 3D build of all cuts and the parts in print position."""
    t0 = time.perf_counter()
    step = progress or (lambda stage, fraction: None)
    step("Checking the cuts", 0.05)
    pl = plan(model, cuts)
    pl.raise_errors()
    secs, lays = pl.secs, pl.lays
    warnings = list(pl.warnings)
    step("Adding the flanges", 0.25)
    adds, subs = [model.manifold], []
    for c, s, lay in zip(cuts, secs, lays):
        adds.append(_flange_solid(c, lay).transform(s.plane.to_world()))
        tools = _fastening_cutters(c, lay)
        if tools is not None:
            subs.append(tools.transform(s.plane.to_world()))
    solid = Manifold.batch_boolean(adds, OpType.Add)
    step("Drilling the bolt holes", 0.45)
    if subs:
        solid = solid - Manifold.batch_boolean(subs, OpType.Add)
    step("Splitting the parts", 0.6)
    cells, plain = [(solid, "")], [(model.manifold, "")]
    for s in secs:      # split LAST, by every plane in turn; the plain mold the same way, for comparison
        cells = [(m, sig + k) for m0, sig in cells
                 for m, k in zip(m0.split_by_plane(tuple(s.plane.normal), s.plane.offset), "AB")]
        plain = [(m, sig + k) for m0, sig in plain
                 for m, k in zip(m0.split_by_plane(tuple(s.plane.normal), s.plane.offset), "AB")]
    plain_vol = {sig: m.volume() for m, sig in plain}
    step("Checking the parts", 0.75)
    found = []
    for cell, sig in sorted(((m, sig) for m, sig in cells if not m.is_empty()), key=lambda t: t[1]):
        bodies = _bodies(cell)
        for body in sorted(bodies, key=lambda b: -b.volume()):
            vol = body.volume()
            if vol < 1e-3:
                continue
            mold = plain_vol.get(sig, 0.0) / vol if len(bodies) == 1 else \
                (body ^ next(m for m, s2 in plain if s2 == sig)).volume() / vol
            if mold < 0.01 and vol < 20_000:
                warnings.append(warning("offcut_dropped", f"A loose piece of flange ({vol / 1000:.1f} cm³) was "
                                                          "cut off by another cut and left out.",
                                        "Move the cuts so they cross inside the wall."))
                continue
            found.append(Part("", body, sig, mold_fraction=min(1.0, mold)))
    if len(found) < 2:
        raise CutError("The cuts do not divide the model into parts. Move the cuts into the model.")
    for k, p in enumerate(found):
        p.name = _part_name(k)
    all_faces = _cut_faces(found, secs)
    joins = _connect(found, cuts, lays, all_faces, warnings)
    step("Placing the parts for printing", 0.85)
    center = model.bounds.mean(axis=0)
    for p, faces in zip(found, all_faces):
        _orient(p, faces, cuts, secs)
        p.touches = sorted(faces)
        e = sum((1 if side == "A" else -1) * secs[i].plane.normal for i, (_, side, _) in faces.items()) \
            if faces else np.zeros(3)
        if np.linalg.norm(e) < 1e-6:          # e.g. the middle of three slices: move it from the centre
            e = np.asarray(p.solid.bounding_box()).reshape(2, 3).mean(axis=0) - center
        n = np.linalg.norm(e)
        p.explode = e / n if n > 1e-9 else np.zeros(3)
    _part_warnings(found, cuts, secs, warnings)
    for lay in lays:
        warnings += lay.warnings
    step("Done", 1.0)
    return Result(pl, found, joins, warnings, time.perf_counter() - t0)


def _cut_faces(parts: list[Part], secs: list[Section]) -> list[dict]:
    """For every part: {cut index: (face area, side, 2D outline)} of the cut faces it has.

    Sliced 0.05 mm off the plane on the part's own side, in that cut's plane coordinates.
    """
    out = []
    for p in parts:
        faces = {}
        for i, s in enumerate(secs):
            side = p.signature[i]
            g = _cs_to_shapely(p.solid.transform(s.plane.to_plane()).slice(0.05 if side == "A" else -0.05))
            if g.area > 1.0:
                faces[i] = (g.area, side, g)
        out.append(faces)
    return out


def _connect(parts: list[Part], cuts: list[Cut], lays: list, all_faces: list, warnings: list) -> list:
    """Which parts each cut bolts together, and warnings for parts or joints without bolts."""
    joins, bolted = [], set()
    for i, (c, lay) in enumerate(zip(cuts, lays)):
        side = {"A": {}, "B": {}}
        for p, faces in zip(parts, all_faces):
            if i in faces:
                side[faces[i][1]][p.name] = faces[i][2]
        count = Counter()
        for q in lay.pts:
            disc = Point(q).buffer(c.d.bolt.washer / 2)
            a = [n for n, g in side["A"].items() if g.intersects(disc)]
            b = [n for n, g in side["B"].items() if g.intersects(disc)]
            if a and b:
                count[(a[0], b[0])] += 1
        for na, ga in side["A"].items():
            for nb, gb in side["B"].items():
                if ga.intersection(gb).area < 1.0:
                    continue
                k = count[(na, nb)]
                if k == 0:
                    warnings.append(warning("joint_unbolted", f"Parts {na} and {nb} touch along cut {i + 1}, but no "
                                                              "bolt holds them together.",
                                            "Move the cuts so this joint is longer, or use more bolts."))
                elif k == 1:
                    warnings.append(warning("joint_one_bolt", f"Parts {na} and {nb} are held by only 1 bolt along "
                                                              f"cut {i + 1}.", "Use more bolts on this cut."))
        joins.append([(na, nb, k) for (na, nb), k in sorted(count.items())])
        bolted |= {n for pair in count for n in pair}
    for p in parts:
        p.bolts = sum(k for cut_joins in joins for na, nb, k in cut_joins if p.name in (na, nb))
        if p.name not in bolted:
            warnings.append(warning("part_unbolted", f"Part {p.name} is not held by any bolt.",
                                    "Move the cuts: this part would be loose."))
    return joins


def _part_warnings(parts: list[Part], cuts: list[Cut], secs: list, warnings: list) -> None:
    for p in parts:
        vol = p.solid.volume() / 1000
        if vol < SMALL_PART_CM3:
            warnings.append(warning("small_part", f"Part {p.name} is very small ({vol:.2f} cm³).",
                                    "Check the cut positions. A cut may run along the edge of the model."))
        elif p.mold_fraction < MOSTLY_FLANGE:
            warnings.append(warning("mostly_flange", f"Part {p.name} is mostly flange "
                                                     f"({100 * p.mold_fraction:.0f}% mold).",
                                    "Move the cut away from the end of the model."))
        if p.down is not None and p.overhang_cm2 > 1.0:
            others = [i for i in p.touches if i != p.down[0]]
            warnings.append(warning("flange_overhang", f"Part {p.name}: the flange of {_cut_names(others)} "
                                                       "hangs in the air when printed.",
                                    "Turn on supports for this part in PrusaSlicer."))
        try:
            tm = trimesh.load_mesh(io.BytesIO(p.stl()), file_type="stl")
            ok = tm.is_watertight
        except Exception:  # noqa: BLE001 - any failure here means the file needs a look
            ok = False
        if not ok:
            warnings.append(warning("not_watertight", f"Part {p.name} may need a repair in your slicer.",
                                    "Let PrusaSlicer repair it, or move the cut by 0.1 mm and generate again."))


# ---------------------------------------------------------------- report
def _bolt_spec(c: Cut) -> str:
    d = c.d
    return f"{d.bolt.name} × {d.bolt_length} mm" if d.bolt_length else f"{d.bolt.name} × {math.ceil(d.grip + 2)} mm or longer"


def hardware(cuts: list[Cut], lays: list[Layout]) -> list[dict]:
    """Shopping list for all cuts: bolts by size and length, nuts and washers by size."""
    bolts, nuts, washers = Counter(), Counter(), Counter()
    for c, lay in zip(cuts, lays):
        n = len(lay.pts)
        if n:
            bolts[_bolt_spec(c)] += n
            nuts[c.d.bolt.name] += n
            washers[c.d.bolt.name] += n * c.d.fastening.washers_per_bolt
    return ([{"item": "Bolt", "spec": k, "qty": v} for k, v in sorted(bolts.items())]
            + [{"item": "Nut", "spec": f"{k} (ISO 4032)", "qty": v} for k, v in sorted(nuts.items())]
            + [{"item": "Washer", "spec": f"{k} (ISO 7089)", "qty": v} for k, v in sorted(washers.items())])


def cut_report(c: Cut, lay: Layout, joins: list[tuple[str, str, int]]) -> dict:
    d, n = c.d, len(lay.pts)
    return {"cut": c.index + 1, "side": c.side, "bolt": d.bolt.name, "fastening": d.fastening.label,
            "bolts": n, "bolt_spec": _bolt_spec(c), "nuts": n, "washers": n * d.fastening.washers_per_bolt,
            "nut_pockets_on": c.pocket_half if d.fastening.nut_pocket else None,
            "flange_thickness": round(d.thickness, 2), "flange_width": round(lay.W, 2),
            "bolt_line": round(lay.c, 2), "bolt_line_moved": lay.moved, "hole_diameter": round(d.hole_d, 2),
            "pocket": ({"across_flats": d.pocket_af, "depth": d.pocket_depth} if d.fastening.nut_pocket else None),
            "joins": [{"parts": [a, b], "bolts": k} for a, b, k in joins]}


def part_report(p: Part) -> dict:
    bb = np.asarray(p.print_solid().bounding_box())
    return {"name": p.name, "signature": p.signature, "volume_cm3": round(p.solid.volume() / 1000, 2),
            "size": (bb[3:] - bb[:3]).round(1).tolist(), "triangles": int(p.solid.num_tri()),
            "face_down": (p.down[0] + 1 if p.down else None), "cuts": [i + 1 for i in p.touches],
            "bolts": p.bolts, "overhang_cm2": round(p.overhang_cm2, 1)}


def summary(result: Result) -> dict:
    """JSON-ready description of a result (the server adds the download links)."""
    pl = result.plan
    return {"seconds": round(result.seconds, 2),
            "parts": [part_report(p) | {"to_world": np.linalg.inv(p.matrix).ravel().round(9).tolist(),
                                         "explode": p.explode.round(6).tolist()} for p in result.parts],
            "cuts": [cut_report(c, l, j) for c, l, j in zip(pl.cuts, pl.lays, result.joins)],
            "hardware": hardware(pl.cuts, pl.lays), "warnings": result.warnings,
            "preview": {"cuts": [cut_json(c, s, l, None, pl.cuts) for c, s, l in zip(pl.cuts, pl.secs, pl.lays)],
                        "warnings": pl.warnings}}


def report_text(result: Result, model_name: str, sha256: str, version: str, files: dict[str, str]) -> str:
    """report.txt for the ZIP: plain text a field user can print."""
    lines = [f"Flange Cutter {version} report", f"Model: {model_name}", f"SHA-256: {sha256}", ""]
    for c, lay, joins in zip(result.cuts, result.lays, result.joins):
        r = cut_report(c, lay, joins)
        lines += [f"Cut {r['cut']}: {r['bolts']} x {r['bolt_spec']} bolts, {r['nuts']} nuts, {r['washers']} washers "
                  f"({r['fastening'].lower()}, flange on the {c.side})",
                  f"  joins: " + (", ".join(f"{j['parts'][0]}-{j['parts'][1]} ({j['bolts']} bolts)"
                                            for j in r["joins"]) or "nothing"),
                  f"  flange {r['flange_thickness']} mm thick per part, {r['flange_width']} mm wide; "
                  f"bolt line {r['bolt_line']} mm from the wall; holes {r['hole_diameter']} mm",
                  f"  plane origin {np.round(c.plane.origin, 2).tolist()}, normal {np.round(c.plane.normal, 4).tolist()}"]
        if r["pocket"]:
            lines.append(f"  nut pockets on side {c.pocket_half}: {r['pocket']['across_flats']:.1f} mm across flats, "
                         f"{r['pocket']['depth']:.1f} mm deep")
    lines += ["", "Parts (print each as it lies in its file: the cut face on the print sheet):"]
    for p in result.parts:
        r = part_report(p)
        down = f"cut {r['face_down']} face down" if r["face_down"] else "as loaded"
        lines.append(f"  Part {p.name}: {files.get(p.name, '')}  {r['volume_cm3']} cm³, "
                     f"{' x '.join(f'{v:g}' for v in r['size'])} mm, {down}, {r['bolts']} bolts")
    lines += ["", "Hardware (all cuts):"] + [f"  {h['qty']} x {h['item'].lower()} {h['spec']}"
                                              for h in hardware(result.cuts, result.lays)]
    if result.warnings:
        lines += ["", "Warnings:"] + [f"  - {w['message']} {w['action']}" for w in result.warnings]
    return "\n".join(lines) + "\n"
