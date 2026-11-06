"""Geometry tests: the proof-of-concept checks, every option, several cuts, bad inputs, speed."""
import math
import time

import numpy as np
import pytest
import trimesh
from shapely.geometry import Point, Polygon

from conftest import reload_stl
from demo_mold import demo_stl
from flangecutter.geometry import CutError, bolts, cutter, mesh_io

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
def test_cross_cut_gives_four_watertight_parts(model, cavity):
    lengthwise_mid = dict(LENGTHWISE, plane={"origin": [0, 0, 120], "normal": [0, 1, 0], "x_axis": [1, 0, 0]})
    cs = cuts(lengthwise_mid, ACROSS_Z)
    res = cutter.generate(model, cs)
    assert [p.signature for p in res.parts] == ["AA", "AB", "BA", "BB"]
    assert all(p.touches == [0, 1] for p in res.parts)
    check_parts(res, model, cavity, cs)
    for lay in res.lays:                       # no bolt inside a crossing band
        for band in lay.bands:
            assert np.all(np.abs(lay.pts @ band.a - band.c) >= band.half - 1e-6)


def test_three_crossing_cuts(model, cavity):
    cs = cuts(LENGTHWISE, ACROSS_Z, ACROSS_X)
    res = cutter.generate(model, cs)
    assert len(res.parts) == 8
    check_parts(res, model, cavity, cs)


def test_preview_and_generate_place_the_same_bolts(model):
    cs = cuts(LENGTHWISE, TRANSVERSE)
    pv = cutter.preview(model, cs)
    res = cutter.generate(model, cs)
    for cut_json, lay in zip(pv["cuts"], res.lays):
        assert np.allclose([b["xy"] for b in cut_json["bolts"]], lay.pts, atol=1e-3)
        assert cut_json["bands"] and cut_json["frame"]["normal"]


def test_each_cut_has_its_own_settings(model):
    cs = cuts(dict(LENGTHWISE, bolt="M3", side="outside"),
              dict(ACROSS_Z, bolt="M6", fastening="nut_pocket", distribution={"mode": "spacing", "spacing": 60}))
    res = cutter.generate(model, cs)
    assert [c.d.bolt.name for c in res.cuts] == ["M3", "M6"]
    assert res.lays[0].c == pytest.approx(4.5) and res.lays[1].c >= 7.0
    specs = {h["spec"] for h in cutter.hardware(res.cuts, res.lays) if h["item"] == "Bolt"}
    assert any(s.startswith("M3") for s in specs) and any(s.startswith("M6") for s in specs)


def test_parallel_cuts(model):
    near = dict(ACROSS_Z, plane={"origin": [0, 0, 125], "normal": [0, 0, 1]})
    with pytest.raises(CutError, match="too close together"):
        cutter.preview(model, cuts(ACROSS_Z, near))
    close = dict(ACROSS_Z, plane={"origin": [0, 0, 135], "normal": [0, 0, 1]})
    assert "cuts_close" in {w["code"] for w in cutter.preview(model, cuts(ACROSS_Z, close))["warnings"]}


# ---------------------------------------------------------------- bad inputs
def test_plane_misses_the_model(model):
    with pytest.raises(CutError, match="does not go through the model"):
        cutter.preview(model, cuts({"plane": {"origin": [0, 0, 500], "normal": [0, 0, 1]}}))


def test_too_many_bolts(model):
    with pytest.raises(CutError, match="too many bolts"):
        cutter.preview(model, cuts(dict(TRANSVERSE, count=100)))


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
    with pytest.raises(CutError, match="at most 6"):
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
