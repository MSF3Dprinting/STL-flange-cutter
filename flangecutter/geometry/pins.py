"""Printed split snap pins that join two flange halves without bolts or nuts.

The shape follows the "Pin" of https://www.printables.com/model/16147-pin (a head, a shaft split by a slot
into two prongs, a barb on each prong, flat sides so it prints lying down), with proportions worked out for
a pin that goes through both flange halves and holds them together without play (decisions 57-62).

Pin frame (assembly position): pin axis +z, the head's underside at z = 0 (it bears on the head-side flange
face), the far flange face at z = grip = 2 T. The prongs part along x (slot plane x = 0); the flats lie at
y = +-w/2. Print position: lying on the flat y = -w/2, axis along print X, slot upright, so the prongs bend
in the layer plane and nothing needs support.

The mechanics are a beam model (Euler-Bernoulli cantilever per prong) with the stiffness and the one-snap
strain limit of PLA, the usual printing plastic (decision 60). Every size is on the 0.01 mm grid.
"""
from __future__ import annotations

import functools
import io
import math
from dataclasses import dataclass

import numpy as np
import trimesh
from manifold3d import CrossSection, JoinType, Manifold, OpType

from .mesh_io import stl_bytes, to_manifold

GRID = 0.01                 # every pin size snaps to this
SEGMENTS = 72               # per circle of the pin
HOLE_SEGMENTS = 48          # per circle of the hole (circumscribed)
FLATS = 0.70                # across the flats = 0.70 d (overhang at the flat asin(0.7) = 44.4 deg)
HEAD_RATIO = 1.5            # head diameter = 1.5 d
HEAD_H_MIN, HEAD_H_RATIO = 2.0, 0.3    # head height = max(2, 0.3 d)
HEAD_CHAMFER = 0.3
NECK = 0.5                  # 45 deg cone between shaft and head (the hole gets 0.6 mm on the head side)
BARB_MIN, BARB_RATIO = 0.5, 1 / 16     # barb = max(0.5, d / 16)
CATCH_DEG, LEAD_DEG = 45.0, 30.0
LAND = 0.6                  # barb crest length
NOSE_INSET = 0.5            # the nose starts this far inside the shaft radius
SLOT_ROOT, SLOT_CORNER = -0.5, 0.5     # slot end inside the head; its corner radius
SLOT_MIN = 1.5              # narrowest slot that stays open when printed
SLOT_GAP = 0.5              # slot left open when the barb passes the smallest expected hole
STRIP = 0.8                 # mm of each prong left on the print sheet at the flats (two 0.4 mm lines)
UNDERSIZE = 0.1             # radial: printed holes up to 0.2 mm (diameter) under the shaft, after the fit test
E_FLEX = 3100.0             # MPa, PLA flexural modulus (Prusament TDS, ISO 178, printed lying flat)
MU = 0.35                   # PLA on PLA (estimate)
DESIGN_STRAIN = 0.018       # the slot rule aims here
STRAIN_LIMIT = 0.020        # warning above (PLA, one snap: 70 % of the 2.9 % yield strain, Bayer)
PUSH_MAX = 70.0             # N: the slot rule keeps the push-in force under a firm thumb push
PULL_MIN = 60.0             # N: a pin cut that holds less than this per pin gets pin_weak (defaults: 104-110)
PUSH_ROOM = 8.0             # free length above the head of a pin that starts into its hole: a thumb or a jaw
PLATE_GAP = 4.0             # between pins on a plate and between fit-test pieces
PLATE_MAX = 170.0           # rows of a plate are at most this wide; larger plates get a warning
FIT_SQUARE = 24.0           # fit-test flange squares


def q(v: float) -> float:
    """Snap a size to the 0.01 mm grid (as cutter.GRID)."""
    return round(v / GRID) * GRID


# ---------------------------------------------------------------- sizes
@dataclass(frozen=True)
class PinSpec:
    """One pin: shaft diameter d, grip (both flange halves, 2 T), hole clearance and preload.

    All four are required, so a caller that drops the fit values fails at once. Hashable: the cache key of
    the mechanics and the solid, and the group key of the fit tests."""

    d: float
    grip: float
    hole_clearance: float       # the hole is d + this (printed holes come out a little small)
    preload: float              # the catch starts this far inside the far face: no play, a little pull

    @property
    def r(self) -> float:
        return self.d / 2

    @property
    def w(self) -> float:
        """Across the flats."""
        return q(FLATS * self.d)

    @property
    def head_d(self) -> float:
        return q(HEAD_RATIO * self.d)

    @property
    def head_h(self) -> float:
        return q(max(HEAD_H_MIN, HEAD_H_RATIO * self.d))

    @property
    def barb(self) -> float:
        """How far each barb stands out past the shaft (= prong travel at a hole of exactly d)."""
        return q(max(BARB_MIN, BARB_RATIO * self.d))

    @property
    def barb_d(self) -> float:
        return q(self.d + 2 * self.barb)

    @property
    def hole_d(self) -> float:
        return q(self.d + self.hole_clearance)

    @property
    def z_catch(self) -> float:
        return q(self.grip - self.preload)

    @property
    def z_land(self) -> float:
        return q(self.z_catch + self.barb / math.tan(math.radians(CATCH_DEG)))

    @property
    def z_lead(self) -> float:
        return q(self.z_land + LAND)

    @property
    def z_tip(self) -> float:
        return q(self.z_lead + (self.barb + NOSE_INSET) / math.tan(math.radians(LEAD_DEG)))

    @property
    def length(self) -> float:
        return q(self.head_h + self.z_tip)

    @property
    def load_arm(self) -> float:
        """Slot root (taken at the head face) to the middle of the barb crest."""
        return self.z_land + LAND / 2

    @property
    def slot_max(self) -> float:
        """Widest slot that leaves STRIP of each prong on the print sheet."""
        return 2 * (math.sqrt(self.r ** 2 - self.w ** 2 / 4) - STRIP)

    @property
    def slot_min(self) -> float:
        """Narrowest slot that lets the barb through the smallest expected hole."""
        return max(SLOT_MIN, 2 * (self.barb + UNDERSIZE) + SLOT_GAP)

    @property
    def slot(self) -> tuple[float, float]:
        """Slot widths at the head and at the barb (a V)."""
        return slot_rule(self.d, self.grip, self.preload)

    @property
    def protrusion(self) -> float:
        """How far the head or the tip stands out of a flange face."""
        return max(self.head_h, self.z_tip - self.grip)

    @property
    def insert_room(self) -> float:
        """Free length the head side needs along the axis: the whole pin, standing in line before it goes in
        (its nose only about 1.5 mm in the hole), plus PUSH_ROOM for a thumb on its head."""
        return q(self.length + PUSH_ROOM)

    @property
    def shape(self) -> tuple[float, float, float]:
        """What the printed pin depends on (the hole clearance changes only the holes)."""
        return (self.d, self.grip, self.preload)


# ---------------------------------------------------------------- mechanics
def prong_strain(spec: PinSpec, s0: float, s1: float, travel: float, steps: int = 120,
                 n: int = 200) -> tuple[float, float]:
    """Peak root strain and force of one prong for a barb travel.

    Cantilever from the head face, slot s0 wide there and growing linearly to s1 at the crest, tip load at
    the middle of the crest. The 0.5 mm slot end inside the head (softer) and the stress raiser at the root
    (the 45 deg neck keeps it low) are both left out; they roughly cancel."""
    arm, r = spec.load_arm, spec.r
    z = np.linspace(0.0, arm, steps)
    half = (s0 + (s1 - s0) * np.minimum(z / spec.z_land, 1.0))[:, None] / 2
    y = np.linspace(-spec.w / 2, spec.w / 2, n)
    x1 = np.sqrt(np.maximum(r * r - y * y, 0.0))[None, :]
    x0 = np.minimum(half, x1)
    dy = y[1] - y[0]
    a = np.trapezoid(x1 - x0, dx=dy, axis=1)
    xc = np.trapezoid((x1 ** 2 - x0 ** 2) / 2, dx=dy, axis=1) / a
    i = np.trapezoid((x1 ** 3 - x0 ** 3) / 3, dx=dy, axis=1) - a * xc ** 2
    c = r - xc
    compliance = np.trapezoid((arm - z) ** 2 / (E_FLEX * i), z)
    force = travel / compliance
    return float((force * (arm - z) * c / (E_FLEX * i)).max()), float(force)


def _wedge(prong: float, deg: float) -> float:
    """Axial force of two prongs pressed by `prong` each over a face at `deg` (Bayer: P (mu + tan a) /
    (1 - mu tan a))."""
    t = math.tan(math.radians(deg))
    return 2 * prong * (MU + t) / (1 - MU * t) if MU * t < 1 else math.inf


@functools.lru_cache(maxsize=256)
def slot_rule(d: float, grip: float, preload: float) -> tuple[float, float]:
    """Slot widths (at the head, at the barb), worked out for the smallest expected hole (travel barb + 0.1):

    1. the barb end as wide as printing allows, the head end at the printable minimum: a V strains the root
       about as much as a uniform wide slot but gives about 3 times the prong force (it holds better);
    2. if that strains more than DESIGN_STRAIN, open the head end (towards a uniform slot);
    3. otherwise close the barb end (stiffer, holds better) until the strain reaches DESIGN_STRAIN or the
       push-in force PUSH_MAX, whichever comes first."""
    spec = PinSpec(d, grip, 0.0, preload)
    travel = spec.barb + UNDERSIZE
    lo, hi = max(spec.slot_min, SLOT_MIN), max(spec.slot_max, spec.slot_min)
    s0 = SLOT_MIN

    def eps(a: float, b: float) -> float:
        return prong_strain(spec, a, b, travel)[0]

    def too_stiff(a: float, b: float) -> bool:
        strain, prong = prong_strain(spec, a, b, travel)
        return strain > DESIGN_STRAIN or _wedge(prong, LEAD_DEG) > PUSH_MAX

    def bisect(a: float, b: float, test) -> float:
        for _ in range(30):
            m = (a + b) / 2
            a, b = (m, b) if test(m) else (a, m)
        return math.ceil(round(b / GRID, 6)) * GRID

    if eps(s0, hi) > DESIGN_STRAIN:                 # 2. open the root
        if eps(hi, hi) > DESIGN_STRAIN:
            return q(hi), q(hi)                     # the widest uniform slot is still too stiff: warn
        return bisect(s0, hi, lambda m: too_stiff(m, hi)), q(hi)
    if too_stiff(s0, hi):                           # pushes too hard even at the widest barb end
        return bisect(s0, hi, lambda m: too_stiff(m, hi)), q(hi)
    if not too_stiff(s0, lo):
        return q(s0), q(lo)
    return q(s0), bisect(lo, hi, lambda m: too_stiff(s0, m))     # 3. close the barb end


@functools.lru_cache(maxsize=256)
def mechanics(spec: PinSpec) -> dict:
    """Root strain on the way in (smallest expected hole), push-in and pull-out force of one pin (N), and the
    pull of the preload. Beam model, about +-30 % on the forces."""
    s0, s1 = spec.slot
    strain, p_in = prong_strain(spec, s0, s1, spec.barb + UNDERSIZE)
    _, p_out = prong_strain(spec, s0, s1, spec.barb)
    _, p_pre = prong_strain(spec, s0, s1, spec.preload * math.tan(math.radians(CATCH_DEG)))
    return {"strain": strain, "push_in": _wedge(p_in, LEAD_DEG), "pull_out": _wedge(p_out, CATCH_DEG),
            "preload_force": 2 * p_pre / math.tan(math.radians(CATCH_DEG))}


# ---------------------------------------------------------------- solids
def _ring(radius: float, cx: float, z: float, n: int) -> np.ndarray:
    """Circle points; half a step of phase, so no vertex lies on y = 0, the flats or the slot planes."""
    a = (np.arange(n) + 0.5) * (2 * math.pi / n)
    return np.c_[cx + radius * np.cos(a), radius * np.sin(a), np.full(n, z)]


def _loft(n: int, *rings: tuple[float, float, float]) -> Manifold:
    """Convex hull of circles (radius, x centre, z). Consecutive lofts share their end circles exactly."""
    return Manifold.hull_points(np.vstack([_ring(r, cx, z, n) for r, cx, z in rings]))


def _box(x0: float, x1: float, y0: float, y1: float, z0: float, z1: float) -> Manifold:
    return Manifold.cube((x1 - x0, y1 - y0, z1 - z0)).translate((x0, y0, z0))


@functools.lru_cache(maxsize=16)
def pin_solid(spec: PinSpec) -> Manifold:
    """One pin in its assembly frame (module docstring). Manifold is immutable, so caching is safe."""
    r, n, h, R = spec.r, SEGMENTS, spec.head_h, spec.head_d / 2
    c, b = HEAD_CHAMFER, spec.barb
    s0, s1 = spec.slot
    head = _loft(n, (R - c, 0, -h), (R, 0, -h + c), (R, 0, -c), (R - c, 0, 0.0))
    # 45 deg neck: crosses the head face at r + NECK and the shaft at z = NECK, both at an angle (never tangent)
    neck = _loft(n, (r + NECK + 0.1, 0, -0.1), (r - 0.05, 0, NECK + 0.05))
    shaft = _loft(n, (r, 0, -h + 0.3), (r, 0, spec.z_catch))
    parts = [head, neck, shaft]
    k = s1 / 2 - 0.3                                  # clip plane, inside the slot (the barbs start past the V)
    for sg in (1, -1):
        # every section of a prong is the shaft circle moved along x: 0 -> b (catch), b (crest),
        # b -> -NOSE_INSET (lead-in); so the barb prints like the shaft and meets the hole along its arc
        barb = Manifold.batch_boolean([
            _loft(n, (r, 0, spec.z_catch), (r, sg * b, spec.z_land)),
            _loft(n, (r, sg * b, spec.z_land), (r, sg * b, spec.z_lead)),
            _loft(n, (r, sg * b, spec.z_lead), (r, -sg * NOSE_INSET, spec.z_tip))], OpType.Add)
        side = _box(k, R + 1, -R - 1, R + 1, spec.z_catch - 1, spec.z_tip + 1) if sg > 0 else \
            _box(-R - 1, -k, -R - 1, R + 1, spec.z_catch - 1, spec.z_tip + 1)
        parts.append(barb ^ side)
    body = Manifold.batch_boolean(parts, OpType.Add)
    body = body ^ _box(-R - 1, R + 1, -spec.w / 2, spec.w / 2, -h - 1, spec.z_tip + 1)
    return body - _slot(spec, s0, s1)


def _slot(spec: PinSpec, s0: float, s1: float) -> Manifold:
    """V slot: s0 wide inside the head, s1 at the crest and on to past the tip, corners rounded (an x-z
    profile extruded along y through both flats)."""
    rc = min(SLOT_CORNER, s0 / 2 - 0.05)
    z0, zl, z1 = SLOT_ROOT, spec.z_land, spec.z_tip + 1.0
    prof = CrossSection([np.array([(-s0 / 2, z0), (s0 / 2, z0), (s1 / 2, zl), (s1 / 2, z1), (-s1 / 2, z1),
                                   (-s1 / 2, zl)], float)])
    prof = prof.offset(-rc, JoinType.Round, circular_segments=32).offset(rc, JoinType.Round, circular_segments=32)
    height = spec.w + 2.0
    solid = prof.extrude(height)                    # profile (x, z) in the XY plane, extruded along +Z
    # (X, Y, Z) -> (x = X, y = height/2 - Z, z = Y): a rotation, never a mirror, so the mesh keeps its orientation
    return solid.transform(np.array([[1, 0, 0, 0], [0, 0, -1, height / 2], [0, 1, 0, 0]], float))


def print_pose(spec: PinSpec) -> np.ndarray:
    """3x4 matrix: assembly frame -> print position (the flat y = -w/2 on the sheet, the head at print x 0)."""
    return np.array([[0, 0, 1, spec.head_h], [1, 0, 0, 0], [0, 1, 0, spec.w / 2]], float)


def plate(spec: PinSpec, count: int) -> Manifold:
    """`count` pins lying flat in rows at most PLATE_MAX wide, PLATE_GAP apart (one body per pin)."""
    one = pin_solid(spec).transform(print_pose(spec))
    lx, ly = spec.length + PLATE_GAP, spec.head_d + PLATE_GAP
    most = max(1, int((PLATE_MAX + PLATE_GAP) // lx))
    cols = min(most, max(1, math.ceil(math.sqrt(count * ly / lx))))
    return Manifold.compose([one.translate((q(k % cols * lx), q(k // cols * ly + spec.head_d / 2), 0.0))
                             for k in range(count)])        # disjoint copies: no boolean, no merged bodies


def hole_cutter(spec: PinSpec, thickness: float, head_side: int) -> Manifold:
    """What the flange loses for one pin, in plane coordinates (cut plane z = 0, flange -T..T; head_side +1
    = side A, -1 = side B): a through hole on the circumscribed polygon (no facet inside d + clearance), a
    0.6 mm 45 deg bevel on the head-side face (room for the pin's neck), and a 0.3 mm bevel at the cut face
    of both halves (first-layer squish) with a 0.1 mm cylindrical band across z = 0, so the split cuts it
    squarely and no face lies on the plane. The barb-side face keeps a sharp edge: the catch seats on it."""
    n, T, s = HOLE_SEGMENTS, thickness, head_side
    rh = spec.hole_d / 2 / math.cos(math.pi / n)
    through = _loft(n, (rh, 0, -T - 1), (rh, 0, T + 1))
    bevel = _loft(n, (rh + 1.1, 0, s * (T + 0.5)), (rh - 0.05, 0, s * (T - 0.65)))
    joint = _loft(n, (rh - 0.05, 0, -0.4), (rh + 0.3, 0, -0.05), (rh + 0.3, 0, 0.05), (rh - 0.05, 0, 0.4))
    return Manifold.batch_boolean([through, bevel, joint], OpType.Add)


def fit_test(spec: PinSpec, thickness: float) -> Manifold:
    """Two flange squares with one pin hole, split as the app splits (union first, split last), both lying
    cut face down, and one pin in print position. Assembled: the two faces that lay on the print sheet go
    together, the square with the bevelled hole on top, and the pin goes in from there."""
    s, T = FIT_SQUARE, thickness
    stack = _box(-s / 2, s / 2, -s / 2, s / 2, -T, T) - hole_cutter(spec, T, 1)
    a, b = stack.split_by_plane((0.0, 0.0, 1.0), 0.0)
    flip = np.array([[1, 0, 0, 0], [0, -1, 0, 0], [0, 0, -1, 0]], float)      # 180 deg about x: cut face down
    one = pin_solid(spec).transform(print_pose(spec))
    return Manifold.compose([a, b.transform(flip).translate((s + PLATE_GAP, 0.0, 0.0)),
                             one.translate((-s / 2, -s / 2 - PLATE_GAP - spec.head_d / 2, 0.0))])


@functools.lru_cache(maxsize=16)
def pin_ok(spec: PinSpec) -> bool:
    """One pin in print position stays watertight after STL export and reload, as one body of genus 0."""
    try:
        one = pin_solid(spec).transform(print_pose(spec))
        tm = trimesh.load_mesh(io.BytesIO(stl_bytes(one)), file_type="stl")
        return bool(tm.is_watertight) and len(to_manifold(tm).decompose()) == 1 and one.genus() == 0
    except Exception:  # noqa: BLE001 - any failure means the pin needs a look
        return False
