"""Geometry tests: the proof-of-concept checks, every option, several cuts, bad inputs, speed."""
import json
import math
import time

import numpy as np
import pytest
import shapely
import trimesh
from manifold3d import Manifold, OpType
from shapely.geometry import LineString, Point, Polygon

from conftest import reload_stl
from demo_mold import cone_shell, demo_stl, mask_stl, mask_with_marks, rounded_cup
from flangecutter.geometry import CutError, bolts, cutter, mesh_io
from flangecutter.geometry.mesh_io import RIM, stl_bytes

ZC = 140.0
# The two cases of reference/test_and_render.py
LENGTHWISE = {"plane": {"origin": [0, 0, 0], "normal": [0, 1, 0], "x_axis": [1, 0, 0]}, "count": 8, "bolt": "M4"}
TRANSVERSE = {"plane": {"origin": [30.0 * (ZC / 240) ** 2, 0, ZC],
                        "normal": [math.sin(math.radians(10)), 0, math.cos(math.radians(10))]},
              "count": 6, "bolt": "M4"}
# Extra cuts for multi-cut tests; ACROSS_Z goes exactly through a ring of mesh vertices (z = 120)
ACROSS_Z = {"plane": {"origin": [0, 0, 120], "normal": [0, 0, 1], "x_axis": [1, 0, 0]}, "count": 6}
ACROSS_X = {"plane": {"origin": [5, 0, 120], "normal": [1, 0, 0], "x_axis": [0, 1, 0]}, "count": 6, "bolt": "M5"}


def cuts(*raw):
    return cutter.parse_cuts({"cuts": list(raw)})


def plain_cells(model, cs) -> dict:
    """The mold split by the same planes with no flange: reference for intrusion and genus."""
    cells = [(model.manifold, "")]
    for c in cs:
        nxt = []
        for m, sig in cells:
            a, b = m.split_by_plane(tuple(c.plane.normal), c.plane.offset)
            nxt += [(a, sig + "A"), (b, sig + "B")]
        cells = [(m, s) for m, s in nxt if not m.is_empty()]
    return {s: m for m, s in cells}


def check_parts(res, model, cavity, cs):
    """Spec section 8: watertight after STL export and reload, no degenerate faces, no overlap,
    no extra material in the cavity, genus = plain genus + holes."""
    plain = plain_cells(model, cs)
    for p in res.parts:
        tm = reload_stl(p.stl())
        assert tm.is_watertight, f"part {p.name} not watertight after reload"
        assert tm.nondegenerate_faces().all(), f"part {p.name} has degenerate faces"
        assert abs(tm.bounds[0][2]) < 1e-6, "part does not sit on z = 0"
        extra = (p.solid ^ cavity).volume() - (plain[p.signature] ^ cavity).volume()
        assert extra < 0.05, f"part {p.name} adds {extra:.3f} mm3 inside the cavity"
    for i, a in enumerate(res.parts):
        for b in res.parts[i + 1:]:
            assert (a.solid ^ b.solid).volume() < 0.01
    # every hole runs through the two parts on either side of its own cut
    holes = sum(len(lay.pts) for lay in res.lays)
    assert sum(p.solid.genus() for p in res.parts) == sum(plain[p.signature].genus() for p in res.parts) + 2 * holes


# ---------------------------------------------------------------- proof-of-concept cases
def test_poc_lengthwise(model, cavity):
    cs = cuts(LENGTHWISE)
    res = cutter.generate(model, cs)
    lay = res.lays[0]
    assert [p.name for p in res.parts] == ["A", "B"]
    assert len(lay.pts) == 8 and lay.moved == 0
    assert (lay.c, lay.W, cs[0].d.thickness) == (5.5, 12.0, 4.0)
    check_parts(res, model, cavity, cs)
    plain = plain_cells(model, cs)
    for p in res.parts:
        assert p.solid.genus() == plain[p.signature].genus() + 8


def test_poc_transverse_moves_bolt_line(model, cavity):
    cs = cuts(TRANSVERSE)
    res = cutter.generate(model, cs)
    lay = res.lays[0]
    assert len(lay.pts) == 6
    assert lay.moved == 2.0 and lay.c == pytest.approx(7.5) and lay.W == pytest.approx(14.0)
    assert "bolt_line_moved" in {w["code"] for w in res.warnings}
    check_parts(res, model, cavity, cs)
    plain = plain_cells(model, cs)
    for p in res.parts:
        assert p.solid.genus() == plain[p.signature].genus() + 6


def test_print_orientation_follows_the_spec(model):
    """Part A as is (cut face down at z = 0); part B turned 180 deg about the plane x axis."""
    res = cutter.generate(model, cuts(TRANSVERSE))
    plane = res.secs[0].plane
    a, b = res.parts
    assert np.allclose(a.matrix[:3], plane.to_plane(), atol=1e-6)
    assert np.allclose(b.matrix[:3], (cutter.FLIP @ np.vstack([plane.to_plane(), [0, 0, 0, 1]]))[:3], atol=1e-6)
    center_a = np.asarray(a.solid.to_mesh().vert_properties)[:, :3].mean(axis=0)
    assert plane.normal @ center_a > plane.offset           # A lies on the side the normal points to
    for p in res.parts:                                       # the flange face lies flat on the sheet
        tm = reload_stl(p.stl())
        down = (tm.face_normals[:, 2] < -0.999) & (np.abs(tm.triangles_center[:, 2]) < 1e-6)
        assert tm.area_faces[down].sum() > 2000


# ---------------------------------------------------------------- every option
@pytest.mark.parametrize("fastening", list(bolts.FASTENINGS))
@pytest.mark.parametrize("side", ["outside", "inside"])
@pytest.mark.parametrize("bolt", list(bolts.BOLTS))
def test_every_option_builds(model, bolt, side, fastening):
    res = cutter.generate(model, cuts(dict(TRANSVERSE, bolt=bolt, side=side, fastening=fastening)))
    assert [p.name for p in res.parts] == ["A", "B"]
    assert len(res.lays[0].pts) >= 2
    for p in res.parts:
        assert reload_stl(p.stl()).is_watertight


@pytest.mark.parametrize("half", ["A", "B"])
def test_nut_pocket_size_and_depth(model, half):
    cs = cuts(dict(LENGTHWISE, bolt="M5", fastening="nut_pocket", nut_pocket_half=half))
    res = cutter.generate(model, cs)
    d = cs[0].d
    assert d.thickness == pytest.approx(4.7 + 1.5)                 # raised: nut height + 1.5 mm
    assert (d.pocket_af, d.pocket_depth) == (pytest.approx(8.3), pytest.approx(5.1))
    assert "thickness_raised" in {w["code"] for w in res.warnings}
    plane, (x, y) = res.secs[0].plane, res.lays[0].pts[3]
    sgn = 1 if half == "A" else -1

    def hole(part, z):
        """The hole around bolt 4 in a slice of `part` at height z (plane coordinates)."""
        g = cutter._cs_to_shapely(part.solid.transform(plane.to_plane()).slice(z))
        rings = [Polygon(r) for poly in getattr(g, "geoms", [g]) for r in poly.interiors]
        return next(r for r in rings if r.contains(Point(x, y)))

    T, depth = d.thickness, d.pocket_depth
    pocketed = next(p for p in res.parts if p.signature == half)
    other = next(p for p in res.parts if p.signature != half)
    across_flats = math.sqrt(2 * hole(pocketed, sgn * (T - depth + 0.2)).area / math.sqrt(3))
    assert across_flats == pytest.approx(d.pocket_af, abs=0.02)        # hexagon, inside the pocket
    round_area = math.pi * (d.hole_d / 2) ** 2
    assert hole(pocketed, sgn * (T - depth - 0.2)).area == pytest.approx(round_area, rel=0.02)  # below the floor
    assert hole(other, -sgn * (T - 0.5)).area == pytest.approx(round_area, rel=0.02)          # no pocket


def test_spacing_mode(model):
    pv = cutter.preview(model, cuts(dict(LENGTHWISE, distribution={"mode": "spacing", "spacing": 40})))
    pts = np.array([b["xy"] for b in pv["cuts"][0]["bolts"]])
    nearest = np.sort(np.linalg.norm(pts[:, None] - pts[None], axis=2), axis=1)[:, 1]
    assert len(pts) > 8 and np.median(nearest) == pytest.approx(40, rel=0.25)


def test_derived_defaults_and_bolt_length():
    d = bolts.derive("M4", "through_nut", bolts.check_params({}, "Cut 1"))
    assert (d.thickness, d.bolt_line(), d.flange_width(d.bolt_line()), d.hole_d) == (4.0, 5.5, 12.0, 4.7)
    for name, fast, thickness, length in [("M3", "through_nut", 3.5, 16), ("M4", "through_nut", 4.0, 16),
                                          ("M6", "through_nut", 6.0, 25), ("M4", "nut_pocket", 4.7, 12)]:
        assert bolts.bolt_length(bolts.BOLTS[name], bolts.FASTENINGS[fast], thickness)[1] == length
    thick = bolts.derive("M6", "through_nut", {"thickness": 20})
    assert thick.bolt_length is None and "no_bolt_length" in {w["code"] for w in thick.warnings}


def test_hardware_and_report(model):
    res = cutter.generate(model, cuts(LENGTHWISE))
    assert cutter.hardware(res.cuts, res.lays) == [
        {"item": "Bolt", "spec": "M4 × 16 mm", "qty": 8},
        {"item": "Nut", "spec": "M4 (ISO 4032)", "qty": 8},
        {"item": "Washer", "spec": "M4 (ISO 7089)", "qty": 16}]
    text = cutter.report_text(res, "demo_mold.stl", "abc", "1.0.0", {"A": "a.stl", "B": "b.stl"})
    assert "Part A: a.stl" in text and "8 x washer" not in text and "16 x washer M4" in text


# ---------------------------------------------------------------- several cuts
def hardware_overlap(res) -> float:
    """Volume shared by the bolts (with washers, nuts and bolt ends) of different cuts."""
    per_cut = []
    for c, s, lay in zip(res.cuts, res.secs, res.lays):
        reach = c.d.thickness + c.d.protrusion
        rod = Manifold.cylinder(2 * reach, c.d.bolt.washer / 2, -1, 24, True)
        per_cut.append(Manifold.batch_boolean([rod.translate((x, y, 0.0)) for x, y in lay.pts], OpType.Add)
                       .transform(s.plane.to_world()))
    return sum((a ^ b).volume() for i, a in enumerate(per_cut) for b in per_cut[i + 1:])


def test_cross_cut_gives_four_watertight_parts(model, cavity):
    lengthwise_mid = dict(LENGTHWISE, plane={"origin": [0, 0, 120], "normal": [0, 1, 0], "x_axis": [1, 0, 0]})
    cs = cuts(lengthwise_mid, ACROSS_Z)
    res = cutter.generate(model, cs)
    assert [p.signature for p in res.parts] == ["AA", "AB", "BA", "BB"]
    assert all(p.touches == [0, 1] for p in res.parts)
    check_parts(res, model, cavity, cs)
    assert hardware_overlap(res) < 0.01            # bolts of the two cuts never run into each other
    assert [[j[:2] for j in joins] for joins in res.joins] == [[("A", "C"), ("B", "D")], [("A", "B"), ("C", "D")]]
    assert all(p.down == (1, p.signature[1]) for p in res.parts)   # quarters lie on the transverse face
    codes = {w["code"] for w in res.warnings}
    assert not codes & {"joint_unbolted", "joint_one_bolt", "part_unbolted", "flange_overhang", "not_watertight"}


def test_three_crossing_cuts(model, cavity):
    cs = cuts(LENGTHWISE, ACROSS_Z, ACROSS_X)
    res = cutter.generate(model, cs)
    assert len(res.parts) == 8
    check_parts(res, model, cavity, cs)
    assert hardware_overlap(res) < 0.01


def test_three_slices_with_parallel_cuts(model, cavity):
    """The common 'too long for the printer' job: two parallel cuts give three parts."""
    low = dict(ACROSS_Z, plane={"origin": [0, 0, 80], "normal": [0, 0, 1]})
    high = dict(ACROSS_Z, plane={"origin": [0, 0, 160], "normal": [0, 0, 1]})
    cs = cuts(low, high)
    res = cutter.generate(model, cs)
    assert [p.signature for p in res.parts] == ["AA", "AB", "BB"]    # above both, between, below both
    check_parts(res, model, cavity, cs)
    summary = cutter.summary(res)
    json.loads(json.dumps(summary), parse_constant=lambda c: pytest.fail(f"{c} in JSON"))
    middle = res.parts[1]
    assert sorted(middle.touches) == [0, 1] and np.allclose(middle.explode, middle.explode)   # finite
    assert any(w["code"] == "flange_overhang" and "Part B" in w["message"] for w in res.warnings)


@pytest.mark.parametrize("normal, offset", [((1, 0, 0), 3.0), ((1, 0, 0), 7.5), ((1, 0, 0), 12.0),
                                            ((0, 1, 0), 6.0), ((0, 1, 0), -6.0)])
def test_float32_export_regressions(model, normal, offset):
    """Cut positions where outline vertices on the rim used to leave sub-micron edges."""
    raw = {"plane": {"origin": list(np.array(normal) * offset), "normal": list(normal)}, "count": 8}
    res = cutter.generate(model, cuts(raw))
    for p in res.parts:
        tm = reload_stl(p.stl())
        assert tm.is_watertight and tm.nondegenerate_faces().all(), f"part {p.name}"


def test_two_wall_loops_keep_both_cavities_clean():
    """One cut through two separate cups: each loop has its own cavity, so no flange goes inside."""
    cup = Manifold.cylinder(200, 25, -1, 96) - Manifold.cylinder(200, 22.5, -1, 96).translate((0, 0, 2.5))
    pair = cup.translate((-45, 0, 0)) + cup.translate((45, 0, 0)) + Manifold.cube((90, 10, 2.5)).translate((-45, -5, 0))
    model = mesh_io.load_stl(mesh_io.stl_bytes(pair), "two_cups.stl")
    pv = cutter.preview(model, cuts({"plane": {"origin": [0, 0, 60], "normal": [0, 0, 1]}, "count": 12}))
    flange = shapely.union_all([Polygon(p["exterior"], p["holes"]) for p in pv["cuts"][0]["flange"]])
    cavities = shapely.union_all([Polygon(h) for p in pv["cuts"][0]["section"] for h in p["holes"]])
    assert len(pv["cuts"][0]["cavities"]) == 2
    assert flange.intersection(cavities).area < 1e-3


def test_preview_and_generate_place_the_same_bolts(model):
    cs = cuts(LENGTHWISE, TRANSVERSE)
    pv = cutter.preview(model, cs)
    res = cutter.generate(model, cs)
    for cut_json, lay in zip(pv["cuts"], res.lays):
        assert np.allclose([b["xy"] for b in cut_json["bolts"]], lay.pts, atol=1e-3)
        assert cut_json["crossings"] and cut_json["free"] and cut_json["frame"]["normal"]


def test_each_cut_has_its_own_settings(model):
    cs = cuts(dict(LENGTHWISE, bolt="M3", side="outside"),
              dict(ACROSS_Z, bolt="M6", fastening="nut_pocket", distribution={"mode": "spacing", "spacing": 60}))
    res = cutter.generate(model, cs)
    assert [c.d.bolt.name for c in res.cuts] == ["M3", "M6"]
    assert res.lays[0].c == pytest.approx(4.5) and res.lays[1].c >= 7.0
    specs = {h["spec"] for h in cutter.hardware(res.cuts, res.lays) if h["item"] == "Bolt"}
    assert any(s.startswith("M3") for s in specs) and any(s.startswith("M6") for s in specs)


def test_parallel_cuts_too_close(model):
    near = dict(ACROSS_Z, plane={"origin": [500, 0, 125], "normal": [0, 0, 1]})   # origin far away on purpose
    pv = cutter.preview(model, cuts(ACROSS_Z, near))
    assert pv["cuts"][0]["error"] is None and "too close" in pv["cuts"][1]["error"]
    with pytest.raises(CutError, match="too close"):
        cutter.generate(model, cuts(ACROSS_Z, near))
    close = dict(ACROSS_Z, plane={"origin": [0, 0, 135], "normal": [0, 0, 1]})   # flanges fit, tools do not
    pv = cutter.preview(model, cuts(ACROSS_Z, close))
    assert all(c["count"] == 0 and "no_bolts" in {w["code"] for w in c["warnings"]} for c in pv["cuts"])


def test_one_bad_cut_does_not_hide_the_others(model):
    away = {"plane": {"origin": [0, 0, 900], "normal": [0, 0, 1]}}
    pv = cutter.preview(model, cuts(LENGTHWISE, away))
    assert pv["cuts"][0]["error"] is None and pv["cuts"][0]["count"] == 8
    assert "does not go through the model" in pv["cuts"][1]["error"]


# ---------------------------------------------------------------- thin shells (face-mask molds)
def inside_fraction(solid, points) -> float:
    """Share of the points that lie inside the solid (tested with 0.2 mm cubes)."""
    cube = Manifold.cube((0.2, 0.2, 0.2), True)
    cubes = [cube.translate(tuple(p)) for p in points]
    hit = sum((solid ^ Manifold.batch_boolean(cubes[i:i + 200], OpType.Add)).volume() for i in range(0, len(cubes), 200))
    return hit / 0.008 / max(len(points), 1)


def shell_checks(model, res, k=300):
    """Per cut: the protected mold face stays clean, the flange covers its own face, nothing goes past
    the mold's edge (rim). Probes sit just outside the faces, within the flange's thickness: k random
    probes per check, or all of them with k=None. "dirty" and "past_rim" count only what Generate added:
    on a creased mold face (real molds) a step along the face normal can land back inside the mold."""
    union = Manifold.batch_boolean([p.solid for p in res.parts], OpType.Add)
    added = union - model.manifold
    centre, normal, lab = model.mesh.triangles_center, model.normals, model.surfaces
    rng = np.random.default_rng(0)
    out = []
    for c, s in zip(res.cuts, res.secs):
        outer = np.isin(lab, model.outside)
        flange_face = outer if c.side == "outside" else (lab >= 0) & ~outer

        def probes(mask, gap, need=True):
            # The probe's own distance to the plane counts, not its face centre's: on a leaning wall
            # the step along the face normal also moves the probe off the plane, out of the flange.
            p = centre[mask] + gap * normal[mask]
            p = p[np.abs((p - s.plane.origin) @ s.plane.normal) < c.d.thickness - 0.3]
            assert len(p) or not need, "no probes near the cut: the check would pass without testing anything"
            return p if k is None else p[rng.choice(len(p), min(k, len(p)), replace=False)]

        out.append({"dirty": inside_fraction(added, probes((lab >= 0) & ~flange_face, 0.3)),
                    "covered": inside_fraction(union, probes(flange_face, 0.5)),
                    "past_rim": inside_fraction(added, probes(lab == RIM, 0.3, need=False))})
    return out


@pytest.fixture(scope="module")
def mask():
    return mesh_io.load_stl(mask_stl(), "mask.stl")


def plain_stl(solid) -> bytes:
    """An STL of the solid as it is (stl_bytes' clean-up could merge flat test meshes)."""
    return mesh_io.to_trimesh(solid).export(file_type="stl")


def test_shell_surfaces_are_found(mask, model):
    for m, far in ((mask, (-500, 0, 0)), (model, (500, 0, 120))):    # a face mask and the POC's cup
        labels, area = m.surfaces, m.mesh.area_faces
        assert len(m.outside) == 1 and set(np.unique(labels)) == {RIM, 0, 1}
        assert area[labels == RIM].sum() < 0.03 * area.sum()          # the rim is a narrow band
        nearest = np.argmin(np.linalg.norm(m.mesh.triangles_center - far, axis=1))
        assert labels[nearest] in m.outside                            # what faces the world is outside
    box = mesh_io.load_stl(trimesh.creation.box((30, 40, 50)).export(file_type="stl"), "box.stl")
    assert set(np.unique(box.surfaces)) == {mesh_io.OTHER}             # not a shell: the POC's rule


@pytest.mark.parametrize("side", ["outside", "inside"])
def test_face_mask_cut_through_the_nose(mask, side):
    """Through the nose the profile is wavy: one flange along one face of the whole profile, square at
    the mold's edge, never on the other face (the POC's rule gave 6-7 pieces on both faces)."""
    cs = cuts({"plane": {"origin": [0, 0, 0], "normal": [0, 1, 0], "x_axis": [1, 0, 0]}, "side": side, "count": 8})
    assert len(cutter.section(mask, cs[0]).f_chains) == 1
    res = cutter.generate(mask, cs)
    for p in res.parts:
        tm = reload_stl(p.stl())
        assert tm.is_watertight and tm.nondegenerate_faces().all() and len(p.solid.decompose()) == 1
    (check,) = shell_checks(mask, res)
    assert check["dirty"] < 0.005 and check["covered"] > 0.95 and check["past_rim"] < 0.01, check
    assert not {w["code"] for w in res.warnings} & {"unbolted_stretch", "offcut_dropped", "part_unbolted"}


@pytest.mark.parametrize("side", ["outside", "inside"])
def test_leaning_wall_gets_the_wedge_filled(side):
    """A wall 50 deg from square: on the half where it leans away, the flange joins it over the whole
    flange thickness. One straight strip would have to pass the protected face at the cut to reach
    that far; each layer of the half reaches only as far as the protected face allows at its faces."""
    shell, mid = cone_shell(lean=50)
    # Not through stl_bytes: its clean-up merges the cone's flat 1 mm rings into long triangles, which
    # leaves no face centres near the cut to probe.
    cone = mesh_io.load_stl(plain_stl(shell), "cone.stl")
    assert len(cone.outside) == 1 and set(np.unique(cone.surfaces)) == {RIM, 0, 1}
    cs = cuts({"plane": {"origin": [0, 0, mid], "normal": [0, 0, 1]}, "side": side, "count": 8})
    res = cutter.generate(cone, cs)
    lay = res.lays[0]
    assert not lay.uniform
    fill = (0, 1) if side == "outside" else (2, 3)            # the half where the wall leans away
    assert lay.layers[fill[1]][2].area > lay.layers[fill[0]][2].area + 50  # the outer layer reaches further
    for p in res.parts:
        tm = reload_stl(p.stl())
        assert tm.is_watertight and tm.nondegenerate_faces().all() and len(p.solid.decompose()) == 1
    (check,) = shell_checks(cone, res, k=None)      # every probe: the bolt holes alone are about 2 %
    assert check["dirty"] < 0.005 and check["covered"] > 0.97, check


def test_flange_stops_at_the_mold_edge(model):
    """The lengthwise cut of the cup reaches its open top: the flange must not wrap around the rim."""
    res = cutter.generate(model, cuts(LENGTHWISE))
    (check,) = shell_checks(model, res)
    assert check["past_rim"] < 0.01 and check["dirty"] < 0.005 and check["covered"] > 0.95, check


@pytest.mark.parametrize("normal", [[0, 1, 0], [math.sqrt(0.5), math.sqrt(0.5), 0]])
def test_flange_is_flush_with_a_slanted_mold_edge(mask, normal):
    """The mask's edge is the plane x = 10. The faces meet it off square, and the 45 deg cut meets it at
    a slant, so it moves with the height in the flange (review C1). No flange may reach past it: 1.0.0
    put 2,200 to 4,700 mm3 there with Outside, the first 1.0.1 up to 2,267 mm3."""
    origin = [0, 0, 0] if normal[0] == 0 else [-30, 0, 0]
    past = Manifold.cube((100, 400, 400)).translate((10.05, -200, -200))
    for side in cutter.SIDES:
        res = cutter.generate(mask, cuts({"plane": {"origin": origin, "normal": normal}, "side": side, "count": 8}))
        union = Manifold.batch_boolean([p.solid for p in res.parts], OpType.Add)
        assert (union ^ past).volume() < 0.5, side


def test_no_flange_above_the_top_of_the_mold(model):
    """A cut 1.5 mm below the cup's open top: the flange layers above the top have no wall to join, so
    they are left out instead of lying over the opening (review C4), and the user is told."""
    res = cutter.generate(model, cuts({"plane": {"origin": [0, 0, 238.5], "normal": [0, 0, 1]},
                                       "side": "outside", "bolt": "M4", "count": 6}))
    assert all(g.area < 1.0 for z0, _, g in res.lays[0].layers if z0 >= 0)
    assert "part_unbolted" in {w["code"] for w in res.warnings}


def test_a_fill_across_the_start_of_a_ring_comes_back_whole():
    """_stretches on a closed ring: a run across the ring's start point is one stretch, so its band keeps
    the joint there (review C3: two stretches left a slit in the wedge fill)."""
    t = np.linspace(0, 2 * np.pi, 33)[:-1]
    p = np.c_[np.cos(t), np.sin(t)] * 50
    ring = LineString(np.vstack([p, p[:1]]))                                 # exactly closed
    want = np.zeros(32, bool)
    want[[30, 31, 0, 1, 2]] = True
    (stretch,) = cutter._stretches(ring, want, 5.0)
    assert np.allclose(np.asarray(stretch.coords)[2], ring.coords[0])     # the old start, inside the stretch
    assert cutter._stretches(ring, np.ones(32, bool), 5.0) == [ring]


def test_marks_on_the_face_stay_part_of_it():
    """A raised pad and an engraved line on the outer face (reviews R1, R2). The pad is not taken for the
    outer surface and the line is not taken for the mold's edge, so the cut through the nose still gets
    one flange along one face, as on the plain mask."""
    m = mesh_io.load_stl(plain_stl(mask_with_marks()), "marks.stl")
    assert len(m.outside) == 2 and set(np.unique(m.surfaces)) == {RIM, 0, 1, 2}   # outer face + pad top; inner face
    c = m.mesh.triangles_center
    groove = (np.abs(c[:, 2] - 50) < 0.6) & (np.abs(c[:, 1]) < 15.5) & (c[:, 0] < -56.5)   # not the inner face
    assert groove.any() and np.isin(m.surfaces[groove], m.outside).all()     # its walls and floor too
    assert m.surfaces[np.argmin(np.linalg.norm(c - (0, -60, 0), axis=1))] in m.outside   # the pad's top
    for side in cutter.SIDES:
        cs = cuts({"plane": {"origin": [0, 0, 0], "normal": [0, 1, 0], "x_axis": [1, 0, 0]}, "side": side, "count": 8})
        assert len(cutter.section(m, cs[0]).f_chains) == 1, side


@pytest.fixture(scope="module")
def rounded():
    return mesh_io.load_stl(plain_stl(rounded_cup()), "cup.stl")


def test_rounded_edge_falls_back_with_a_warning(rounded):
    """A thin-walled mold with a rounded edge: no sharp edge separates its faces, so they cannot be told
    apart (review R4). The user is told, and the cutter uses the proof of concept's rule (decision 13)."""
    assert set(np.unique(rounded.surfaces)) == {mesh_io.OTHER}
    assert "edge_not_found" in {w["code"] for w in rounded.warnings}


@pytest.mark.parametrize("y", [0, 10, 20, 30])
def test_rounded_edge_keeps_the_protected_face_clean(rounded, y):
    """Cuts through and beside the axis of the rounded cup, both sides: the face that must stay clean
    does (the cavity with Outside, the outside of the cup with Inside). Off-axis cuts give long straight
    pieces, where a sign read from a simplified chain once flipped (critic: up to 1,683 mm3 in the cup)."""
    cavity = Manifold.cylinder(57.3, 37.4, 37.4, 160).translate((0, 0, 2.6))
    around = Manifold.cylinder(59.9, 60, 60, 160) - Manifold.cylinder(59.9, 40.1, 40.1, 160)
    for side, clean in (("outside", cavity), ("inside", around)):
        res = cutter.generate(rounded, cuts({"plane": {"origin": [0, y, 30], "normal": [0, 1, 0], "x_axis": [1, 0, 0]},
                                             "side": side, "bolt": "M4", "count": 6}))
        assert all(reload_stl(p.stl()).is_watertight for p in res.parts)
        union = Manifold.batch_boolean([p.solid for p in res.parts], OpType.Add)
        assert (union ^ clean).volume() < 0.5, side


def test_needle_merge_never_opens_the_surface():
    """stl_bytes merges the ends of a float32 needle's short edge only when the surface stays closed
    (review R5): here both ends share a third neighbour, and merging them gives an edge with 4 triangles,
    so the needle stays (a warning in a slicer, not an error)."""
    v = np.array([[0, 0, 0], [5e-5, 0, 0], [0.25, 1, 0], [0.5, 0, 0], [0.3, 0.5, 0.5], [0.3, -0.2, -0.5],
                  [0.3, 0.5, -0.5]], np.float32).astype(np.float64)
    f = np.array([[0, 1, 3], [1, 4, 3], [1, 2, 4], [2, 3, 4], [2, 0, 3], [1, 0, 5], [1, 5, 6], [2, 1, 6],
                  [2, 6, 5], [0, 2, 5]])
    assert mesh_io._closed(f)
    merged = mesh_io._merge_needles(v, f)
    assert len(merged) < len(f) and not mesh_io._closed(merged)          # stl_bytes keeps f then
    cube = Manifold.cube((10, 10, 10))                                    # an ordinary solid is untouched
    assert len(reload_stl(stl_bytes(cube)).faces) == 12 and reload_stl(stl_bytes(cube)).is_watertight


# ---------------------------------------------------------------- bad inputs
def test_plane_misses_the_model(model):
    pv = cutter.preview(model, cuts({"plane": {"origin": [0, 0, 500], "normal": [0, 0, 1]}}))
    assert "does not go through the model" in pv["cuts"][0]["error"]
    with pytest.raises(CutError, match="does not go through the model"):
        cutter.generate(model, cuts({"plane": {"origin": [0, 0, 500], "normal": [0, 0, 1]}}))


def test_too_many_bolts(model):
    assert "too many bolts" in cutter.preview(model, cuts(dict(TRANSVERSE, count=100)))["cuts"][0]["error"]


def test_non_watertight_mesh():
    box = trimesh.creation.box((20, 20, 20))
    box.update_faces(np.arange(len(box.faces)) != 0)
    with pytest.raises(CutError, match="has holes"):
        mesh_io.load_stl(box.export(file_type="stl"), "open.stl")


def test_wrong_units_warn():
    small = mesh_io.load_stl(trimesh.creation.box((2, 3, 4)).export(file_type="stl"), "small.stl")
    large = mesh_io.load_stl(trimesh.creation.box((2500, 30, 40)).export(file_type="stl"), "large.stl")
    assert [w["code"] for w in small.warnings] == ["units_small"]
    assert [w["code"] for w in large.warnings] == ["units_large"]


def test_not_an_stl():
    with pytest.raises(CutError, match="STL"):
        mesh_io.load_stl(b"hello, this is not a mesh", "notes.stl")


def test_bad_winding_and_inside_out_are_repaired():
    box = trimesh.creation.box((20, 20, 20))
    box.faces[0] = box.faces[0][::-1]
    fixed = mesh_io.load_stl(box.export(file_type="stl"), "flipped.stl")
    assert fixed.repaired and fixed.mesh.is_winding_consistent
    inside_out = trimesh.creation.box((20, 20, 20))
    inside_out.invert()
    fixed = mesh_io.load_stl(inside_out.export(file_type="stl"), "inverted.stl")
    assert fixed.repaired and fixed.mesh.volume > 0


@pytest.mark.parametrize("raw, message", [
    ({"plane": {"origin": [0, 0, 0], "normal": [0, 0, 0]}}, "not valid"),
    (dict(LENGTHWISE, bolt="M8"), "unknown bolt size"),
    (dict(LENGTHWISE, side="left"), "unknown flange side"),
    (dict(LENGTHWISE, params={"thickness": 50}), "between 2 and 20"),
    (dict(LENGTHWISE, count=0), "between 1 and 100"),
    (dict(LENGTHWISE, distribution={"mode": "spacing", "spacing": 1}), "spacing must be between"),
])
def test_bad_settings(raw, message):
    with pytest.raises(CutError, match=message):
        cutter.parse_cuts(raw)


def test_cut_count_limits():
    with pytest.raises(CutError, match="at least one cut"):
        cutter.parse_cuts({"cuts": []})
    with pytest.raises(CutError, match="up to 6 cuts"):
        cutter.parse_cuts({"cuts": [LENGTHWISE] * 7})


def test_spec_single_cut_payload_is_accepted():
    payload = {"model_id": "abc123", "plane": {"origin": [0, 0, 120], "normal": [0, 1, 0], "x_axis": [1, 0, 0]},
               "side": "outside", "bolt": "M4", "count": 8, "fastening": "through_nut", "nut_pocket_half": "B",
               "params": {"thickness": None, "width": None, "wall_gap": 1.0, "edge": 2.0, "hole_clearance": 0.2,
                          "overlap": 0.6, "max_wall_angle": 25, "tool_clearance": 12}}
    (cut,) = cutter.parse_cuts(payload)
    assert (cut.count, cut.mode, cut.d.thickness, cut.side) == (8, "count", 4.0, "outside")


# ---------------------------------------------------------------- speed (spec: 100k triangles)
@pytest.fixture(scope="module")
def model_100k():
    return mesh_io.load_stl(demo_stl(seg=320, n_div=78), "demo_100k.stl")


@pytest.mark.slow
def test_speed_on_100k_triangles(model_100k):
    assert 90_000 < len(model_100k.mesh.faces) < 130_000
    cs = cuts(TRANSVERSE)
    t = time.perf_counter()
    cutter.preview(model_100k, cs)
    preview_s = time.perf_counter() - t
    t = time.perf_counter()
    res = cutter.generate(model_100k, cs)
    generate_s = time.perf_counter() - t
    print(f"\n100k triangles: preview {preview_s * 1000:.0f} ms, generate {generate_s:.2f} s")
    assert preview_s < 0.6 and generate_s < 6.0      # spec targets 0.3 s / 3 s; margin for slow CI runners
    assert all(reload_stl(p.stl()).is_watertight for p in res.parts)
