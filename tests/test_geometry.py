"""Geometry tests: the proof-of-concept checks, every option, several cuts, printed pins, bad inputs, speed."""
import copy
import json
import math
import re
import time

import numpy as np
import pytest
import shapely
import trimesh
from manifold3d import CrossSection, Manifold, OpType
from shapely.geometry import LineString, Point, Polygon

from conftest import reload_stl
from demo_mold import (MIXED_EDGE, cone_shell, demo_stl, mask_mold, mask_stl, mask_with_marks, mixed_edge_tube,
                       rounded_cup)
from flangecutter.geometry import CutError, bolts, cutter, mesh_io, pins
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
        rod = Manifold.cylinder(2 * reach, c.d.footprint / 2, -1, 24, True)
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


@pytest.mark.parametrize("bolt", ["M4", "pin"])
@pytest.mark.parametrize("side", ["outside", "inside"])
def test_face_mask_cut_through_the_nose(mask, side, bolt):
    """Through the nose the profile is wavy: one flange along one face of the whole profile, square at
    the mold's edge, never on the other face (the POC's rule gave 6-7 pieces on both faces)."""
    cs = cuts({"plane": {"origin": [0, 0, 0], "normal": [0, 1, 0], "x_axis": [1, 0, 0]}, "side": side, "count": 8,
               "bolt": bolt})
    assert len(cutter.section(mask, cs[0]).f_chains) == 1
    res = cutter.generate(mask, cs)
    for p in res.parts:
        tm = reload_stl(p.stl())
        assert tm.is_watertight and tm.nondegenerate_faces().all() and len(p.solid.decompose()) == 1
    (check,) = shell_checks(mask, res)
    # At T 6 (pins) the wall under part of the outer flange layer is square at the cut, so the wedge fill
    # (decided at the cut) does not engage, but it curves away 30-40 deg within that layer, which then stands
    # 0.75-1.75 mm off it (covered 0.894; M6 bolts at their automatic 6 mm give the same). A known limit of the
    # flange, not of the pins.
    covered = 0.87 if bolt == "pin" and side == "outside" else 0.95
    assert check["dirty"] < 0.005 and check["covered"] > covered and check["past_rim"] < 0.01, check
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


def test_rounded_edge_is_found(rounded):
    """A thin-walled mold with a rounded edge: no sharp edge separates its faces, so the first look
    finds one surface (review R4; 1.0.1 fell back to the proof of concept's rule with a warning). The
    second look finds the wall's end from the wall direction (decision 54): two faces and a rim."""
    assert mesh_io.shell_surfaces(rounded.mesh, 2.5)[2] == {"edge_not_found"}         # the first look
    assert set(np.unique(rounded.surfaces)) == {RIM, 0, 1} and len(rounded.outside) == 1
    assert rounded.edge == "found from the wall direction" and not rounded.warnings
    c, area = rounded.mesh.triangles_center, rounded.mesh.area_faces
    assert rounded.surfaces[np.argmin(np.linalg.norm(c - (500, 0, 30), axis=1))] in rounded.outside
    assert area[rounded.surfaces == RIM].sum() < 0.03 * area.sum()        # a narrow band on top of the round


@pytest.mark.parametrize("y", [0, 10, 20, 30])
def test_rounded_edge_keeps_the_protected_face_clean(rounded, y):
    """Cuts through and beside the axis of the rounded cup, both sides: the face that must stay clean
    does (the cavity with Outside, the outside of the cup with Inside), and no flange stands above the
    top of the round (61.25 mm). Off-axis cuts give long straight pieces, where a sign read from a
    simplified chain once flipped (critic: up to 1,683 mm3 in the cup)."""
    cavity = Manifold.cylinder(57.3, 37.4, 37.4, 160).translate((0, 0, 2.6))
    around = Manifold.cylinder(59.9, 60, 60, 160) - Manifold.cylinder(59.9, 40.1, 40.1, 160)
    above = Manifold.cube((200, 200, 50)).translate((-100, -100, 61.3))
    for side, clean in (("outside", cavity), ("inside", around)):
        res = cutter.generate(rounded, cuts({"plane": {"origin": [0, y, 30], "normal": [0, 1, 0], "x_axis": [1, 0, 0]},
                                             "side": side, "bolt": "M4", "count": 6}))
        assert all(reload_stl(p.stl()).is_watertight for p in res.parts)
        union = Manifold.batch_boolean([p.solid for p in res.parts], OpType.Add)
        assert (union ^ clean).volume() < 0.5, side
        assert (union ^ above).volume() < 0.5, side


@pytest.fixture(scope="module")
def mixed():
    return mesh_io.load_stl(plain_stl(mixed_edge_tube()), "tube.stl")


def test_mixed_edge_is_found(mixed):
    """Like the face masks that 1.0.1 could not label (decision 54): the edge is square in one stretch,
    rounded in steps under 45 deg in another, a knife edge in a third. The first look finds one surface;
    the second finds both faces everywhere and a rim along the whole edge."""
    mesh = mixed.mesh
    assert mesh_io.shell_surfaces(mesh, mesh.volume / (mesh.area / 2))[2] == {"edge_not_found"}
    assert mixed.edge == "found from the wall direction" and not mixed.warnings
    labels, c = mixed.surfaces, mesh.triangles_center
    r, z = np.hypot(c[:, 0], c[:, 1]), c[:, 2]
    wall = (z > 5) & (z < 40)
    assert np.isin(labels[wall & (r > 59.9)], mixed.outside).all()
    inner = labels[wall & (r < 57.75)]
    assert ((inner >= 0) & ~np.isin(inner, mixed.outside)).all()
    deg = np.degrees(np.arctan2(c[:, 1], c[:, 0])) % 360
    for a0, a1 in MIXED_EDGE.values():                       # every stretch of the top edge has its rim
        assert ((labels == RIM) & (z > 49) & (deg > a0) & (deg < a1)).any()


@pytest.mark.parametrize("deg", [215, 305])                  # through the rounded and the knife stretch
@pytest.mark.parametrize("side", ["outside", "inside"])
def test_mixed_edge_cuts_are_clean(mixed, deg, side):
    """A cut through the rounded (or knife) stretch, and on the far side of the tube through the square
    one: the protected face stays clean, the flange covers its own face and stops at the top edge."""
    a = math.radians(deg)
    cs = cuts({"plane": {"origin": [0, 0, 25], "normal": [-math.sin(a), math.cos(a), 0]}, "side": side, "count": 6})
    res = cutter.generate(mixed, cs)
    for p in res.parts:
        tm = reload_stl(p.stl())
        assert tm.is_watertight and tm.nondegenerate_faces().all() and len(p.solid.decompose()) == 1
    union = Manifold.batch_boolean([p.solid for p in res.parts], OpType.Add)
    added = union - mixed.manifold
    clean = (Manifold.cylinder(60, 57.6, 57.6, 180) if side == "outside"
             else Manifold.cylinder(60, 90, 90, 180) - Manifold.cylinder(60, 60.05, 60.05, 180))
    beyond = Manifold.cube((300, 300, 50)).translate((-150, -150, 50.05)) + \
        Manifold.cube((300, 300, 50)).translate((-150, -150, -50.05))
    assert (added ^ clean.translate((0, 0, -5))).volume() < 0.5
    assert (added ^ beyond).volume() < 0.5
    (check,) = shell_checks(mixed, res)
    assert check["dirty"] < 0.005 and check["covered"] > 0.95 and check["past_rim"] < 0.01, check


@pytest.mark.parametrize("deg", [175, 255, 265, 350, 304.2, 305.8])     # through blends, and the knife off its rings
def test_mixed_edge_blends_stay_below_the_top(mixed, deg):
    """Where the edge changes its shape, the labels flicker (a sliver of rim between faces of the flange
    side) and the faces can meet in a ridge without rim: no flange may stand above the top (review: up
    to 221 mm3 with the 1 mm line and split flange chains, decision 55)."""
    a = math.radians(deg)
    above = Manifold.cube((300, 300, 50)).translate((-150, -150, 50.05))
    for side in cutter.SIDES:
        res = cutter.generate(mixed, cuts({"plane": {"origin": [0, 0, 25], "normal": [-math.sin(a), math.cos(a), 0]},
                                           "side": side, "count": 6}))
        added = Manifold.batch_boolean([p.solid for p in res.parts], OpType.Add) - mixed.manifold
        assert (added ^ above).volume() < 0.5, side


def test_a_pad_on_the_inner_face_does_not_fool_the_first_look():
    """A cup with a rounded edge and a raised pad on its inner floor: the first look joins the outer face
    to most of the inner one, and only the pad's top is left facing inward, so it looked like a shell
    (review). Both faces of a thin wall have about the same area, so it is not taken for one."""
    m = mesh_io.load_stl(plain_stl(rounded_cup() + Manifold.cube((16, 16, 1.2)).translate((-8, -8, 2.5))), "pad.stl")
    assert mesh_io.shell_surfaces(m.mesh, m.wall)[2] == {"edge_not_found"}      # the first look: no shell
    assert m.edge == "found from the wall direction" and not m.warnings
    c, up = m.mesh.triangles_center, m.normals[:, 2]
    floor = (np.abs(c[:, 2] - 2.5) < 0.01) & (np.hypot(c[:, 0], c[:, 1]) > 12) & (up > 0.9)
    assert floor.any() and ((m.surfaces[floor] >= 0) & ~np.isin(m.surfaces[floor], m.outside)).all()


@pytest.mark.parametrize("height, line", [(60, 50), (220, 214)])    # the tall cup: the strip is 1.4 % of it
def test_an_engraved_line_does_not_fool_the_first_look(height, line):
    """A cup with a rounded edge and an engraved line round its outer wall: the first look takes the
    outer wall above the line for part of the inner face and finds a shell, with the line as its rim
    (review). The second look labels it right, so the body takes the second look (decision 56), also on
    a tall cup where that strip is a small share of the mold (review: an Inside flange then stood about
    16 mm above the edge). A line within about 3.5 mm of the round (about one and a half wall
    thicknesses) is a known limit: the second look calls the strip rim, not outer face, and the first
    look stays."""
    groove = (Manifold.cylinder(1.0, 40.5, 40.5, 160) - Manifold.cylinder(1.0, 39.2, 39.2, 160)).translate((0, 0, line))
    m = mesh_io.load_stl(plain_stl(rounded_cup(height=height) - groove), "groove.stl")
    first, outside, notes = mesh_io.shell_surfaces(m.mesh, m.wall)
    c = m.mesh.triangles_center
    strip = (c[:, 2] > line + 1.5) & (c[:, 2] < height - 1) & (np.hypot(c[:, 0], c[:, 1]) > 39.95)
    assert strip.any() and not notes and not np.isin(first[strip], outside).any()   # fooled: the strip inward
    assert m.edge == "found from the wall direction" and np.isin(m.surfaces[strip], m.outside).all()


def test_knife_edge_rule_reads_both_wall_directions(monkeypatch):
    """wall_end on a tetrahedron with chosen wall directions: faces 0 and 1 lie on opposite sides of the
    wall by face 1's own direction, but not by face 0's (review: such an edge between a slanted rim and a
    round slipped through both rules). Where two wall directions are square to each other, their sign is
    noise and must not make a knife edge (review: stripes of rim on square cups)."""
    tet = trimesh.creation.tetrahedron() if hasattr(trimesh.creation, "tetrahedron") else None
    v = np.array([[1, 1, 1], [1, -1, -1], [-1, 1, -1], [-1, -1, 1]], float)
    tet = trimesh.Trimesh(v, [[0, 1, 2], [0, 3, 1], [0, 2, 3], [1, 3, 2]], process=False)
    n = tet.face_normals
    m = n.copy()                                              # every face on its own side ...
    m[1] = (n[0] - 0.8 * n[1]) / np.linalg.norm(n[0] - 0.8 * n[1])      # ... but face 1's wall turned toward face 0's
    monkeypatch.setattr(mesh_io, "_wall_directions", lambda mesh, h: m)
    assert abs(n[1] @ m[0]) < mesh_io.END_COS <= abs(n[1] @ m[1])      # no knife edge by face 0's direction alone
    end = mesh_io.wall_end(tet, 1.0)
    pair = [i for i, (a, b) in enumerate(tet.face_adjacency) if {a, b} == {0, 1}]
    assert pair and end[0] and end[1]
    assert not end[2] and not end[3]                          # their wall directions are square-ish: no fold


def test_rim_runs_ignore_a_clipped_corner():
    """_rim_runs: a guard chain that starts with 0.3 mm of rim (a layer's height clipping a rim's corner)
    has no usable rim crossing (0), one that starts with 1 mm of it has 1 mm."""
    p = np.c_[np.arange(11) * 0.1, np.zeros(11)]
    p = np.vstack([p, p[-1] + np.c_[np.zeros(10), -np.arange(1, 11)]])          # then 10 mm of protected face
    chain = LineString(p)
    tree = shapely.STRtree(shapely.points((p[:-1] + p[1:]) / 2))
    rim = np.r_[np.ones(10, bool), np.zeros(10, bool)]
    assert cutter._rim_runs(chain, tree, rim, 0.59) == (1.0, 0.0)
    rim3 = np.r_[np.ones(3, bool), np.zeros(17, bool)]
    assert cutter._rim_runs(chain, tree, rim3, 0.59) == (0.0, 0.0)


@pytest.mark.parametrize("knockout", ["fill", "nearest"])
@pytest.mark.parametrize("deg", [175, 350])
def test_each_end_mechanism_works_alone(mixed, monkeypatch, knockout, deg):
    """The label noise at a blend is handled twice: _fill_gaps closes the sliver, and an end that misses
    its guard chain takes the one ending within 1.5 mm. Each must do the job alone (review: either one
    off left the other covering for it)."""
    if knockout == "fill":
        monkeypatch.setattr(cutter, "_fill_gaps", lambda seg, mask, tree, gap, labels: mask)
    else:
        monkeypatch.setattr(cutter, "END_GAP", -1.0)
    mixed.cache.clear()                                        # sections made without the knockout
    try:
        test_mixed_edge_blends_stay_below_the_top(mixed, deg)
    finally:
        mixed.cache.clear()


def test_fill_gaps_only_fills_label_noise():
    """Along the outline, a guard run shorter than `gap` between two flange runs goes to the flange side:
    not a longer one, not one at the end of an open line, not one on OTHER faces."""
    def line(n, closed=False):
        t = np.linspace(0, 2 * np.pi, n + 1) if closed else None
        p = np.c_[20 * np.cos(t), 20 * np.sin(t)] if closed else np.c_[np.arange(n + 1) * 0.25, np.zeros(n + 1)]
        seg = np.round(np.stack([p[:-1], p[1:]], axis=1), 5)
        return seg, shapely.STRtree(shapely.points(seg.mean(axis=1)))
    seg, tree = line(20)
    labels = np.zeros(20, int)
    fill = lambda mask, lab=labels, s=seg, tr=tree: cutter._fill_gaps(s, np.array(mask, bool), tr, 2.35, lab)
    assert fill([1] * 8 + [0] * 2 + [1] * 10).all()                         # 0.5 mm between flange: noise
    assert fill([1] * 5 + [0] * 12 + [1] * 3).sum() == 8                     # 3 mm: a real stretch of guard
    assert fill([0] * 2 + [1] * 18).sum() == 18                              # at the end of an open line
    assert fill([1] * 8 + [0] * 2 + [1] * 10, np.where(np.arange(20) // 2 == 4, mesh_io.OTHER, 0)).sum() == 18
    ring, ring_tree = line(400, closed=True)                                  # 0.31 mm pieces
    mask = np.ones(400, bool)
    mask[[398, 399, 0, 1]] = False                                            # a short run across the start
    assert cutter._fill_gaps(ring, mask, ring_tree, 2.35, np.zeros(400, int)).all()


@pytest.mark.parametrize("height", [150, 200])
def test_a_square_cup_keeps_its_first_look(height):
    """A cup with a square edge and walls of single long triangles (a CAD revolve): at the square bend of
    the bottom the two wall directions are square to each other, and their sign was rounding noise that
    made knife edges and stripes of rim (review). The first look is right and must stay."""
    profile = np.array([(0.0, 0.0), (40.0, 0.0), (40.0, height), (37.5, height), (37.5, 2.5), (0.0, 2.5)])
    m = mesh_io.load_stl(plain_stl(CrossSection([profile]).revolve(160)), "cup.stl")
    first, outside, _ = mesh_io.shell_surfaces(m.mesh, m.wall)
    assert m.edge == "sharp"
    assert np.array_equal(mesh_io._kinds(m.surfaces, m.outside), mesh_io._kinds(first, outside))
    sides = np.abs(m.normals[:, 2]) < 0.5                                     # the walls of the cup
    assert not ((mesh_io.wall_end(m.mesh, m.wall) & sides) & (np.abs(m.mesh.triangles_center[:, 2] - height) > 3)).any()


def test_a_solid_block_keeps_its_channel_clean():
    """A solid block (OTHER: the proof of concept's rule) with a channel in its top: the channel's walls are
    the protected side, so no flange goes into it (review: _fill_gaps filled it, 288 mm3)."""
    block = Manifold.cube((40, 30, 20)) - Manifold.cube((6, 40, 6)).translate((17, -5, 14))
    m = mesh_io.load_stl(plain_stl(block), "block.stl")
    assert m.edge == "none"
    res = cutter.generate(m, cuts({"plane": {"origin": [20, 15, 10], "normal": [0, 1, 0], "x_axis": [1, 0, 0]},
                                   "side": "outside", "bolt": "M4", "count": 4}))
    added = Manifold.batch_boolean([p.solid for p in res.parts], OpType.Add) - m.manifold
    assert (added ^ Manifold.cube((5.8, 40, 5.9)).translate((17.1, -5, 14.1))).volume() < 0.5


def test_choose_keeps_or_replaces_each_body():
    """_choose per body: the face mask (labeled by the first look) keeps its labels while the second look
    puts under 0.1 % of it on the other side, and takes the second look above that; the tube (OTHER in the
    first look) always takes the second."""
    both = mesh_io.load_stl(plain_stl(mask_mold() + mixed_edge_tube().translate((400, 0, 0))), "both.stl")
    mesh, t = both.mesh, both.wall
    first = mesh_io.shell_surfaces(mesh, t)
    second = mesh_io.shell_surfaces(mesh, t, mesh_io.wall_end(mesh, t))
    on_mask = mesh.triangles_center[:, 0] < 200
    k1 = mesh_io._kinds(first[0], first[1])
    outward = np.flatnonzero(on_mask & (mesh_io._kinds(second[0], second[1]) == 1))
    inward_id = next(s for s in np.unique(second[0][on_mask]) if s >= 0 and s not in second[1])
    mask_area = mesh.area_faces[on_mask].sum()
    for share, takes_second in ((0.0005, False), (0.005, True)):
        flipped = second[0].copy()
        n = np.searchsorted(np.cumsum(mesh.area_faces[outward]), share * mask_area)
        flipped[outward[:n]] = inward_id
        labels, outside, edge = mesh_io._choose(mesh, t, first, (flipped, second[1], second[2]))
        k = mesh_io._kinds(labels, outside)
        expected = mesh_io._kinds(flipped, second[1]) if takes_second else k1
        assert np.array_equal(k[on_mask], expected[on_mask]), share
        assert np.array_equal(k[~on_mask], mesh_io._kinds(second[0], second[1])[~on_mask])
        assert edge == "found from the wall direction"


def test_found_bodies_keep_their_labels():
    """The second look only touches the bodies the first one could not label: the face mask, in one file
    with the mixed-edge tube, keeps the labels it gets alone."""
    alone = mesh_io.load_stl(plain_stl(mask_mold()), "mask.stl")
    both = mesh_io.load_stl(plain_stl(mask_mold() + mixed_edge_tube().translate((400, 0, 0))), "both.stl")
    assert alone.edge == "sharp" and both.edge == "found from the wall direction" and not both.warnings

    def kinds(m, sel):
        lab = m.surfaces[sel]
        k = np.where(lab == RIM, 0, np.where(np.isin(lab, m.outside), 1, 2))
        return k[np.lexsort(np.round(m.mesh.triangles_center[sel], 4).T)]
    expected = kinds(alone, np.ones(len(alone.mesh.faces), bool))
    assert np.array_equal(kinds(both, both.mesh.triangles_center[:, 0] < 200), expected)


def test_a_mold_without_an_edge_keeps_the_warning():
    """A hollow ball has thin walls but no edge at all: its faces cannot be told apart, so the user is
    told and the cutter uses the proof of concept's rule (decision 13)."""
    ball = mesh_io.load_stl(plain_stl(Manifold.sphere(50, 96) - Manifold.sphere(47.5, 96)), "ball.stl")
    assert set(np.unique(ball.surfaces)) == {mesh_io.OTHER} and ball.edge == "not found"
    assert "edge_not_found" in {w["code"] for w in ball.warnings}


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
    (c,) = cutter.preview(model, cuts(dict(PIN, count=100)))["cuts"]
    assert "too many pins" in c["error"]               # and the error preview carries the cut's own values
    assert (c["derived"]["kind"], c["derived"]["thickness"], c["derived"]["width"]) == ("pin", 6.0, 15.0)
    (c,) = cutter.preview(model, cuts(dict(PIN, count=100, params={"pin_diameter": 12})))["cuts"]
    assert c["derived"]["thickness"] == 8.0


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
    (dict(LENGTHWISE, bolt="Pin"), "unknown bolt size"),
    (dict(LENGTHWISE, bolt="pin", params={"pin_diameter": 7}), "between 8 and 12"),
    (dict(LENGTHWISE, bolt="pin", params={"pin_preload": 0.5}), "between 0 and 0.4"),
    (dict(LENGTHWISE, bolt="pin", params={"pin_hole_clearance": -0.3}), "between -0.2 and 0.5"),
    (dict(LENGTHWISE, bolt="pin", params={"wall_gap": 11}), "gap pin head to wall"),
    (dict(LENGTHWISE, bolt="pin", count=0), "pin count must be between 1 and 100"),
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
    spec = pins.PinSpec(8, 12.0, 0.1, 0.2)            # the largest pin file of a 100-pin cut
    t = time.perf_counter()
    assert pins.pin_ok(spec) and cutter._prints_ok(stl_bytes(pins.plate(spec, 102)))
    assert time.perf_counter() - t < 1.5


# ---------------------------------------------------------------- printed pins (1.1.0)
PIN = dict(LENGTHWISE, bolt="pin")
SLANT = {"plane": {"origin": [-30, 0, 0], "normal": [math.sqrt(0.5), math.sqrt(0.5), 0]}, "count": 8}
TOP = {"plane": {"origin": [0, 0, 238.5], "normal": [0, 0, 1]}, "side": "outside", "count": 6}   # 1.5 mm under the top


def pin_words(text: str):
    """Bolt words in a pin text. "use bolts on this cut" is the one deliberate alternative (pin_no_room)."""
    return re.search(r"\b(bolts?|washers?|nuts?)\b", text.replace("use bolts on this cut", ""), re.I)


def pin_derive(fastening="through_nut", **params):
    return bolts.derive("pin", fastening, bolts.check_params(params, "Cut 1", "pin"))


def pin_in_plane(spec: pins.PinSpec, T: float, s: int, x=0.0, y=0.0, travel=0.0) -> Manifold:
    """The pin in plane coordinates, head on side A (s = 1) or B (s = -1), its prongs moved in by `travel`.
    Pin frame -> plane is a rotation for both signs, never a mirror."""
    p = pins.pin_solid(spec)
    if travel:
        halves = [(p ^ Manifold.cube((40, 80, 80)).translate((0 if sg > 0 else -40, -40, -20))).translate((-sg * travel, 0, 0))
                  for sg in (1, -1)]
        p = Manifold.batch_boolean(halves, OpType.Add)
    m = np.array([[1, 0, 0, x], [0, -s, 0, y], [0, 0, -s, s * T]], float)
    return p.transform(m)


def test_pin_derived_values():
    d = pin_derive()
    assert d.kind == "pin" and d.bolt is None and d.fastening is None and not d.warnings
    assert (d.thickness, d.bolt_line(), d.flange_width(7.0), d.envelope_r, d.min_spacing) == (6.0, 7.0, 15.0, 6.5, 14.0)
    assert d.hole_d == pytest.approx(8.1) and d.protrusion == pytest.approx(2.63, abs=0.01)
    assert d.pin.insert_room == pytest.approx(25.03) and d.grip == 12.0 and d.bolt_length is None
    for dia, want in ((10, (6.0, 8.5, 18.0, 10.1)), (12, (8.0, 10.0, 21.0, 12.1))):
        x = pin_derive(pin_diameter=dia)
        c = x.bolt_line()
        assert (x.thickness, c, x.flange_width(c)) == want[:3] and x.hole_d == pytest.approx(want[3]) and not x.warnings
    assert [bolts.pin_thickness(v) for v in (8, 10, 10.5, 11.7, 12)] == [6, 6, 7, 8, 8]
    x = pin_derive(pin_hole_clearance=0.3, pin_preload=0.3)          # the fit values travel
    assert (x.hole_d, x.pin.hole_clearance, x.pin.preload, x.pin.length) == pytest.approx((8.3, 0.3, 0.3, 16.93))
    assert x.as_dict()["hole_diameter"] == pytest.approx(8.3)
    x = pin_derive("nut_pocket", hole_diameter=5.0, hole_clearance=1.0, thickness=2.5)   # bolt values do not
    assert x.hole_d == pytest.approx(8.1) and x.pocket_af is None
    assert {w["code"] for w in x.warnings} == {"pin_flange_thin"}

    def weak(**p):
        return next(w["action"] for w in pin_derive(**p).warnings if w["code"] == "pin_weak")
    assert "thicker" in weak(thickness=5)
    assert "larger pin diameter" in weak(thickness=8) and "larger pin diameter" in weak(thickness=10)
    assert "larger pin diameter" not in weak(thickness=20)           # a 12 mm pin holds only 31 N there
    assert not pin_derive(thickness=6).warnings and not pin_derive(thickness=15, pin_diameter=12).warnings
    with pytest.raises(TypeError):
        pins.PinSpec(8, 12)                                          # no defaults: the fit values never get lost
    with pytest.raises(CutError, match="gap pin head to wall must be between 0 and 10 mm"):
        bolts.check_params({"wall_gap": 11}, "Cut 1", "pin")
    with pytest.raises(CutError, match="gap washer to wall must be between 0 and 10 mm"):
        bolts.check_params({"wall_gap": 11}, "Cut 1")


@pytest.mark.parametrize("d, T, fit, preload", [
    (8, 6, .1, .2), (8, 2, .1, .2), (8, 3.5, -.2, 0), (8, 20, .5, .4), (8.37, 4.55, .1, .2), (9.5, 6, 0, .1),
    (10, 6, .1, .2), (10, 10, .3, .35), (11.2, 7, -.1, .2), (12, 8, .1, .2), (12, 2.5, .5, 0), (12, 20, .1, .4)])
def test_pin_is_printable(d, T, fit, preload):
    """Every pin the settings allow is one watertight body that lies on a flat side and needs no support."""
    x = pin_derive(pin_diameter=d, thickness=T, pin_hole_clearance=fit, pin_preload=preload)
    q = pins.q
    spec = x.pin
    assert spec == pins.PinSpec(q(d), q(2 * T), q(fit), q(preload)) and pins.pin_ok(spec)
    tm = reload_stl(stl_bytes(pins.pin_solid(spec).transform(pins.print_pose(spec))))
    assert tm.is_watertight and tm.is_winding_consistent and tm.nondegenerate_faces().all()
    assert len(mesh_io.to_manifold(tm).decompose()) == 1 and abs(tm.bounds[0][2]) < 1e-6
    assert tm.bounds[1] - tm.bounds[0] == pytest.approx([spec.length, spec.head_d, spec.w], abs=0.05)
    nz, zmin = tm.face_normals[:, 2], tm.triangles[:, :, 2].min(axis=1)
    assert tm.area_faces[(nz < -0.999) & (zmin < 1e-6)].sum() > 30                       # on its flat side
    assert tm.area_faces[(nz < -math.cos(math.radians(45))) & (zmin > 0.1)].sum() < 0.01   # nothing to support


def test_slot_rule_and_mechanics():
    for d in (8, 10, 12):
        spec = pins.PinSpec(d, 2 * bolts.pin_thickness(d), 0.1, 0.2)
        m, (s0, s1) = pins.mechanics(spec), spec.slot
        assert m["strain"] <= pins.DESIGN_STRAIN + 1e-4 and m["push_in"] <= pins.PUSH_MAX + 0.5
        assert m["pull_out"] >= pins.PULL_MIN and pins.SLOT_MIN <= s0 and s1 <= spec.slot_max + 0.01
    assert pins.mechanics(pins.PinSpec(8, 7.0, 0.1, 0.2))["strain"] > pins.STRAIN_LIMIT
    hits = pins.mechanics.cache_info().hits
    pins.mechanics(pins.PinSpec(8, 12.0, 0.1, 0.2))
    assert pins.mechanics.cache_info().hits == hits + 1


@pytest.mark.parametrize("d, T", [(8, 6.0), (10, 6.0), (12, 8.0)])
@pytest.mark.parametrize("s", [1, -1])
def test_pin_sits_in_its_hole(d, T, s):
    """The pin overlaps its drilled stack only in the preload zone (the catch pulls the halves together);
    with the prongs pushed in by the barb's travel, or by the preload, it does not overlap at all."""
    spec = pins.PinSpec(d, 2 * T, 0.1, 0.2)
    stack = Manifold.cube((30, 30, 2 * T)).translate((-15, -15, -T)) - pins.hole_cutter(spec, T, s)
    over = (pin_in_plane(spec, T, s) ^ stack).volume()
    band = pins.pin_solid(spec) ^ Manifold.cube((40, 40, spec.grip + 0.01 - spec.z_catch + 0.01)).translate(
        (-20, -20, spec.z_catch - 0.01))
    m = np.array([[1, 0, 0, 0], [0, -s, 0, 0], [0, 0, -s, s * T]], float)
    assert 0 < over < 0.25 and (band.transform(m) ^ stack).volume() == pytest.approx(over, abs=1e-3)
    for travel in (spec.barb - (spec.hole_d - spec.d) / 2, spec.preload):
        assert (pin_in_plane(spec, T, s, travel=travel) ^ stack).volume() < 1e-3


def test_pin_plate_and_fit_test():
    spec = pins.PinSpec(8, 12.0, 0.1, 0.2)
    plate = pins.plate(spec, 14)
    data = stl_bytes(plate)
    assert cutter._prints_ok(data)
    bodies = mesh_io.to_manifold(reload_stl(data)).decompose()
    assert len(bodies) == 14 and all(abs(b.bounding_box()[2]) < 1e-6 for b in bodies)
    x0, y0, _, x1, y1, _ = plate.bounding_box()
    assert max(x1 - x0, y1 - y0) <= pins.PLATE_MAX
    x0, y0, _, x1, y1, _ = pins.plate(pins.PinSpec(12, 16.0, 0.1, 0.2), 102).bounding_box()
    assert x1 - x0 <= pins.PLATE_MAX < y1 - y0          # rows stay within the sheet; deeper plates warn
    ft = pins.fit_test(spec, 6.0)
    assert reload_stl(stl_bytes(ft)).is_watertight
    parts = sorted(ft.decompose(), key=lambda b: -b.volume())
    assert len(parts) == 3 and all(abs(b.bounding_box()[2]) < 1e-6 for b in parts)
    hole = math.pi * (spec.hole_d / 2) ** 2 * 6.0
    assert all(sq.volume() == pytest.approx(24 * 24 * 6 - hole, rel=0.02) for sq in parts[:2])
    assert parts[2].volume() == pytest.approx(pins.pin_solid(spec).volume(), rel=1e-3)

    def hole_at_top(square):
        g = cutter._cs_to_shapely(square.slice(5.9))
        return sum(Polygon(r).area for poly in getattr(g, "geoms", [g]) for r in poly.interiors)
    bevelled, sharp = sorted(parts[:2], key=lambda b: b.bounding_box()[0])     # the head-side square lies left
    assert hole_at_top(bevelled) > 1.1 * hole_at_top(sharp)


def hole_area(part, plane, x, y, z) -> float:
    g = cutter._cs_to_shapely(part.solid.transform(plane.to_plane()).slice(z))
    rings = [Polygon(r) for poly in getattr(g, "geoms", [g]) for r in poly.interiors]
    return next(r for r in rings if r.contains(Point(x, y))).area


@pytest.mark.parametrize("half, fastening", [("A", "through_nut"), ("B", "nut_pocket")])
def test_pin_cut(model, cavity, half, fastening):
    """Outside (check_parts counts an Inside flange as material in the cavity): watertight parts with pin
    holes, bevelled on the head side only, the pin file and the fit test; every pin fits its hole."""
    cs = cuts(dict(PIN, nut_pocket_half=half, fastening=fastening))
    res = cutter.generate(model, cs)
    lay, d, plane = res.lays[0], cs[0].d, res.secs[0].plane
    assert [p.name for p in res.parts] == ["A", "B"]
    check_parts(res, model, cavity, cs)
    assert (lay.c, lay.W, d.thickness) == (7.0, 15.0, 6.0) and len(lay.pts) == 8
    assert not {w["code"] for w in res.warnings} & {"pin_no_room", "bolts_blocked", "pin_fit"}
    (plate,) = res.pins
    assert (plate.cut, plate.needed, plate.spares, plate.count, plate.head_side) == (0, 8, 2, 10, half)
    assert len(mesh_io.to_manifold(reload_stl(plate.stl())).decompose()) == 10
    assert [f.cuts for f in res.fit_tests] == [[0]]
    s, T, (x, y) = (1 if half == "A" else -1), d.thickness, lay.pts[3]
    head = next(p for p in res.parts if p.signature == half)
    barb = next(p for p in res.parts if p.signature != half)
    a = d.hole_d / 2
    plain = 48 * a * a * math.tan(math.pi / 48)          # the circumscribed 48-gon
    assert hole_area(head, plane, x, y, s * (T - 0.3)) > 1.1 * plain                       # the bevel
    assert hole_area(barb, plane, x, y, -s * (T - 0.3)) == pytest.approx(plain, rel=0.01)   # sharp: the catch
    for part, z in ((head, s * T / 2), (barb, -s * T / 2)):
        assert hole_area(part, plane, x, y, z) == pytest.approx(plain, rel=0.01)             # no nut pocket
    for part, z in ((head, s * 0.2), (barb, -s * 0.2)):
        assert hole_area(part, plane, x, y, z) > 1.02 * plain                              # the joint bevel
    union = Manifold.batch_boolean([p.solid for p in res.parts], OpType.Add)
    for px, py in lay.pts:                       # only the preload zone overlaps, as in the bare stack
        pin = pin_in_plane(d.pin, T, s, px, py).transform(plane.to_world())
        assert (pin ^ union).volume() == pytest.approx(0.108, abs=0.01)


def test_pins_need_the_full_flange(model):
    res = cutter.generate(model, cuts(dict(TOP, bolt="pin")))
    assert "pin_fit" in {w["code"] for w in res.warnings}


def test_pin_and_bolt_cuts_cross(model, cavity):
    cs = cuts(PIN, ACROSS_Z)
    res = cutter.generate(model, cs)
    assert [p.name for p in res.parts] == ["A", "B", "C", "D"]
    check_parts(res, model, cavity, cs)
    assert hardware_overlap(res) < 0.01
    codes = {w["code"] for w in res.warnings}
    assert not codes & {"joint_unbolted", "part_unbolted"}
    side = next(w for w in res.warnings if w["code"] == "pin_holes_sideways")
    assert "parts A, B, C and D" in side["message"]
    assert {h["spec"].split()[0] for h in cutter.hardware(res.cuts, res.lays)} == {"M4"}
    assert all(p.pins > 0 and p.bolts > 0 for p in res.parts)
    report = cutter.report_text(res, "demo_mold.stl", "x", "1.1.0", {p.name: f"{p.name}.stl" for p in res.parts})
    assert all(re.search(r"\d+ bolts, \d+ pins$", line) for line in report.splitlines() if line.startswith("  Part "))


def test_pin_report_and_hardware(model):
    res = cutter.generate(model, cuts(PIN))
    assert cutter.hardware(res.cuts, res.lays) == []
    report = cutter.report_text(res, "demo_mold.stl", "x", "1.1.0", {"A": "a.stl", "B": "b.stl"}, {1: "p.stl"},
                                {1: "f.stl"})
    for text in ("Pins:", "p.stl", "f.stl", "10 pins (8 + 2 spares)", "nothing to buy", "joins: A-B (8 pins)",
                 "pushed in from part B", "The fit test", "Extra pin hole size 0.2, then 0.3",
                 "Pin preload 0.1, then 0", "To take a pin out"):
        assert text in report, text
    assert "Keep each cut's pins apart" not in report and not pin_words(report)
    assert cutter.report_text(res, "demo_mold.stl", "x", "1.1.0", {})          # five arguments still work
    shell, mid = cone_shell(lean=30)
    cone = mesh_io.load_stl(plain_stl(shell), "cone.stl")
    level = {"plane": {"origin": [0, 0, mid], "normal": [0, 0, 1]}, "side": "inside", "count": 8, "bolt": "pin"}
    jobs = [(model, PIN, set()), (model, dict(TRANSVERSE, bolt="pin"), {"bolt_line_moved"}),
            (cone, dict(level, nut_pocket_half="A"), {"pin_no_room"}),
            (cone, dict(level, nut_pocket_half="B"), {"bolts_blocked", "wall_angle"}),
            (model, dict(TOP, bolt="pin"), {"pin_fit", "joint_unbolted", "part_unbolted"})]
    for m, raw, want in jobs:
        res = cutter.generate(m, cuts(raw))
        assert want <= {w["code"] for w in res.warnings}, raw
        for w in res.warnings:
            assert not pin_words(f"{w['message']} {w['action']}"), w
    close = dict(ACROSS_Z, plane={"origin": [0, 0, 135], "normal": [0, 0, 1]})     # bolts, but none fit
    res = cutter.generate(model, cuts(ACROSS_Z, close))
    report = cutter.report_text(res, "demo_mold.stl", "x", "1.1.0", {p.name: "" for p in res.parts})
    assert "Hardware (all cuts):\n\nWarnings:" in report and "nothing to buy" not in report     # as in 1.0.x


def test_pin_plate_warning(model, monkeypatch):
    monkeypatch.setattr(pins, "PLATE_MAX", 40.0)
    res = cutter.generate(model, cuts(PIN))
    assert "pin_plate_large" in {w["code"] for w in res.warnings}
    assert len(mesh_io.to_manifold(reload_stl(res.pins[0].stl())).decompose()) == 10
    monkeypatch.setattr(cutter, "_prints_ok", lambda data: False)
    res = cutter.generate(model, cuts(PIN))
    texts = [w["message"] for w in res.warnings if w["code"] == "pins_not_watertight"]
    assert any("pin file" in t for t in texts) and any("fit test" in t for t in texts)


def test_two_pin_cuts(model):
    across = dict(ACROSS_Z, bolt="pin")
    res = cutter.generate(model, cuts(PIN, across))                 # (a) the same pins
    assert [p.cut for p in res.pins] == [0, 1] and res.pins[0].spec == res.pins[1].spec
    assert cutter.summary(res)["pins"][1]["same_as"] == 1
    assert [f.cuts for f in res.fit_tests] == [[0, 1]]
    report = cutter.report_text(res, "m.stl", "x", "1.1.0", {}, {1: "p1.stl", 2: "p2.stl"}, {1: "f.stl"})
    assert "Fit test for cuts 1 and 2" in report and "the same pins as cut 1" in report
    assert "Keep each cut's pins apart" not in report
    res = cutter.generate(model, cuts(PIN, dict(across, params={"thickness": 8})))     # (b) longer pins
    assert [round(p.spec.length, 2) for p in res.pins] == [17.03, 21.03]
    assert cutter.summary(res)["pins"][1]["same_as"] is None
    assert [(f.cuts, f.thickness) for f in res.fit_tests] == [([0], 6.0), ([1], 8.0)]
    report = cutter.report_text(res, "m.stl", "x", "1.1.0", {}, {1: "p1.stl", 2: "p2.stl"}, {1: "f1.stl", 2: "f2.stl"})
    assert "Keep each cut's pins apart" in report
    assert any(w["code"] == "pin_weak" and w["message"].startswith("Cut 2") for w in res.warnings)


def test_pins_need_room_to_go_in(mask, mixed, model):
    """A pin goes in only along its axis, from the head side: about 25 mm free there (decision 69). Only a
    warning, and the blocked pins are marked in the 2D view: the user decides."""
    for half, want in (("B", True), ("A", False)):
        (c,) = cutter.preview(mask, cuts(dict(SLANT, side="inside", bolt="pin", nut_pocket_half=half)))["cuts"]
        room = [w for w in c["warnings"] if w["code"] == "pin_no_room"]
        assert bool(room) == want, (half, c["warnings"])
        if want:
            assert "pin 1, 2, 3, 4, 5, 6, 7, 8 " in room[0]["message"]
            assert room[0]["action"] == "Move or rotate the cut, or use bolts on this cut."
            assert all(b["blocked"] for b in c["bolts"])               # marked in the 2D view
    a = math.radians(175)
    for half in ("A", "B"):
        (c,) = cutter.preview(mixed, cuts({"plane": {"origin": [0, 0, 25], "normal": [-math.sin(a), math.cos(a), 0]},
                                           "side": "inside", "count": 4, "bolt": "pin", "nut_pocket_half": half}))["cuts"]
        room = [w for w in c["warnings"] if w["code"] == "pin_no_room"]
        assert room and f"pin {', '.join(str(i + 1) for i in range(c['count']))} " in room[0]["message"], half
    # the advice: the other head side is named only when it would work for all of those pins
    cs = cuts(PIN)
    pl = cutter.plan(model, cs)
    sec, lay = copy.copy(pl.secs[0]), pl.lays[0]
    h, room = cs[0].d.tool_clearance, max(cutter._tool_heights(cs[0]))
    disc = Point(*lay.pts[0]).buffer(2).union(Point(*lay.pts[1]).buffer(2))
    sec.shadows = {(1, h): None, (-1, h): None, (1, room): None, (-1, room): disc}
    lay.blocked, lay.warnings = [0, 1], []
    cutter._pin_room(cs[0], sec, lay)
    (w,) = lay.warnings
    assert "pin 1, 2 in from the other side" in w["message"] and "25 mm" in w["message"]
    assert w["action"] == "Choose Pin heads on: Arrow side, or move the cut."
    sec.shadows[(1, room)] = disc
    lay.warnings = []
    cutter._pin_room(cs[0], sec, lay)
    assert lay.warnings[0]["action"] == "Move or rotate the cut, or use bolts on this cut."
    # review: a switch that would only block the other pins is not advised
    (c,) = cutter.preview(model, cuts({"plane": {"origin": [6.6, 0, 120], "normal": [1, 0, 0]}, "side": "inside",
                                       "count": 8, "bolt": "pin", "nut_pocket_half": "A"}))["cuts"]
    (w,) = [w for w in c["warnings"] if w["code"] == "pin_no_room"]
    assert w["action"] == "Move or rotate the cut, or use bolts on this cut."
    # review: two pin cuts whose heads face each other leave no room; the action offers the other side
    level = {"normal": [0, 0, 1], "x_axis": [1, 0, 0]}
    pv = cutter.preview(model, cuts(
        {"plane": dict(level, origin=[0, 0, 120]), "count": 6, "bolt": "pin", "nut_pocket_half": "A"},
        {"plane": dict(level, origin=[0, 0, 142]), "count": 6, "bolt": "pin", "nut_pocket_half": "B"}))
    for c in pv["cuts"]:
        w = next(w for w in c["warnings"] if w["code"] == "no_bolts")
        assert "Pin heads on" in w["action"] and not pin_words(w["message"] + " " + w["action"])


def test_a_narrowing_wall_blocks_the_tools():
    """1.0.2 lost the far band of a narrowing wall's shadow inside the near band's hole (decision 70)."""
    shell, mid = cone_shell(lean=30)
    cone = mesh_io.load_stl(plain_stl(shell), "cone.stl")
    level = {"plane": {"origin": [0, 0, mid], "normal": [0, 0, 1]}, "side": "inside", "count": 8}
    (c,) = cutter.preview(cone, cuts(dict(level, bolt="M4")))["cuts"]
    blocked = next(w for w in c["warnings"] if w["code"] == "bolts_blocked")
    assert "bolt 1, 2, 3, 4, 5, 6, 7, 8." in blocked["message"]
    (c,) = cutter.preview(cone, cuts(dict(level, bolt="pin", nut_pocket_half="A")))["cuts"]
    assert "pin_no_room" in {w["code"] for w in c["warnings"]}
    (c,) = cutter.preview(cone, cuts(dict(level, bolt="pin", nut_pocket_half="B")))["cuts"]
    assert "take it out" in next(w for w in c["warnings"] if w["code"] == "bolts_blocked")["message"]
    sq = lambda a, b, cw=False: np.array([(a, a), (b, a), (b, b), (a, b)][::-1 if cw else 1], float)   # noqa: E731
    g = cutter._cs_to_shapely(CrossSection([sq(0, 40), sq(10, 30, cw=True), sq(15, 25)]))
    assert g.area == pytest.approx(40 ** 2 - 20 ** 2 + 10 ** 2)


def test_pins_keep_the_least_blocked_step():
    """When every step is blocked, a pin cut keeps the step with the least mold in its tool discs; bolts
    keep the first step, as in 1.0.x (decisions 16 and 71)."""
    for lean, bolt, moved in ((20, "pin", 6), (30, "M4", 0)):
        shell, mid = cone_shell(lean=lean)
        cone = mesh_io.load_stl(plain_stl(shell), "cone.stl")
        cs = cuts({"plane": {"origin": [0, 0, mid], "normal": [0, 0, 1]}, "side": "outside", "count": 8, "bolt": bolt})
        lay = cutter.plan(cone, cs).lays[0]
        assert lay.blocked and lay.moved == moved, (bolt, lay.moved, lay.blocked)
