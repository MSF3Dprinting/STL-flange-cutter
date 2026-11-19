"""The proof-of-concept test mold (reference/flange_cut.py: demo_mold), with a resolution knob.

samples/demo_mold.stl was exported from the POC with the default resolution (about 32k triangles).
"""
import math

import numpy as np
from manifold3d import CrossSection, Manifold, Mesh

from flangecutter.geometry.mesh_io import stl_bytes


def demo_mold(H=240.0, a=60.0, b=42.0, wall=2.5, taper=0.72, seg=160, bend=30.0, n_div=48):
    """Tapered, bent elliptical cup (open top), a stand-in for a limb mold. Returns (shell, cavity)."""
    th = np.linspace(0, 2 * np.pi, seg, endpoint=False)

    def ell(ax, by):
        return CrossSection([np.c_[ax * np.cos(th), by * np.sin(th)]])

    outer = ell(a, b).extrude(H, n_divisions=n_div, scale_top=(taper, taper))
    z0, z1 = wall, H + 1.0

    def ai(z, s):
        return s * (1 - (1 - taper) * z / H) - wall

    inner = ell(ai(z0, a), ai(z0, b)).extrude(
        z1 - z0, n_divisions=n_div,
        scale_top=(ai(z1, a) / ai(z0, a), ai(z1, b) / ai(z0, b))).translate((0, 0, z0))

    def warp(v):
        return (v[0] + bend * (v[2] / H) ** 2, v[1], v[2])

    shell = (outer - inner).warp(warp)
    cavity = (inner ^ Manifold.cube((400, 400, H - 0.01)).translate((-200, -200, 0))).warp(warp)
    return shell, cavity


def demo_stl(**kw) -> bytes:
    return stl_bytes(demo_mold(**kw)[0])


def mask_mold(wall=2.35, seg=128):
    """A face-mask-like open shell (like the TFO molds): the front of an ellipsoid with a nose,
    2.35 mm thick, cut flat at the back. The face looks toward -x, z is up; the rim is the flat
    cut at x = 10. A cut through the nose meets a wavy profile, where the POC's rule failed."""
    def ellipsoid(a, b, c, at=(0, 0, 0)):
        return Manifold.sphere(1.0, seg).scale((a, b, c)).translate(at)

    # nose, upper lip, lower lip and chin along the midline: a wavy profile like a real face
    bumps = [((-60.0, 0, 8), (20, 12, 24)), ((-62.0, 0, -30), (10, 18, 7)), ((-58.0, 0, -46), (9, 16, 6)),
             ((-50.0, 0, -66), (14, 22, 12))]
    outer = ellipsoid(70, 55, 90)
    inner = ellipsoid(70 - wall, 55 - wall, 90 - wall)
    for at, (a, b, c) in bumps:
        outer += ellipsoid(a, b, c, at)
        inner += ellipsoid(a - wall, b - wall, c - wall, at)
    front = Manifold.cube((200, 300, 300)).translate((-190, -150, -150))      # keeps x < 10
    return (outer - inner) ^ front


def mask_stl() -> bytes:
    return stl_bytes(mask_mold())


def mask_with_marks(seg=128):
    """The face mask with two small marks on its outer face, as real molds have: a raised pad (a label or
    strap boss, 12 x 12 mm, about 1 mm high) on the cheek, and an engraved line (1 mm wide, 0.8 mm deep)
    across the forehead, where the cut through the nose crosses it. Both belong to the outer face."""
    def ellipsoid(a, b, c):
        return Manifold.sphere(1.0, seg).scale((a, b, c))

    pad = Manifold.cube((12, 2, 12)).translate((-6, -56, -6))
    skin = ellipsoid(71, 56, 91) - ellipsoid(69.2, 54.2, 89.2)              # the outer 0.8 mm, and beyond
    line = skin ^ Manifold.cube((60, 30, 1.0)).translate((-90, -15, 49.5))
    return mask_mold(seg=seg) + pad - line


def rounded_cup(seg=160, height=60.0):
    """A cup 80 mm across and `height` high (60 mm), 2.5 mm walls and bottom, whose top edge is rounded:
    no sharp edge separates its outer and inner faces."""
    profile = [(0.0, 0.0), (40.0, 0.0), (40.0, height)]
    profile += [(38.75 + 1.25 * np.cos(a), height + 1.25 * np.sin(a)) for a in np.linspace(0, np.pi, 25)[1:-1]]
    profile += [(37.5, height), (37.5, 2.5), (0.0, 2.5)]
    return CrossSection([np.array(profile)]).revolve(seg)


MIXED_EDGE = {"square": (0, 160), "rounded": (180, 250), "knife": (270, 340)}   # degrees around the tube


def mixed_edge_tube(r=60.0, wall=2.35, height=50.0, rings=180, cap=9, step=2.0, spans=None, lean=30.0):
    """A tube (open at both ends) whose top edge changes its shape around the tube, like the edge of
    the real molds that 1.0.1 could not label: square from 0 to 160 deg, rounded in 22.5 deg steps (a
    half-round of the wall's thickness) from 180 to 250 deg, a knife edge from 270 to 340 deg (both
    faces lean in over the last 4 mm and meet at a sharp ridge), with smooth blends between. The
    bottom edge is square. Every top shape reaches z = height. Outer radius r, inner r - wall.
    `spans` ({kind: (from, to) deg}) changes the layout; the kind "bevel" is a flat rim that leans
    `lean` deg from square toward the inner face."""
    spans = spans or MIXED_EDGE

    def top(kind):
        """(cap points from the outer face to the inner face, height where the walls stop)."""
        u = np.linspace(0.0, 1.0, cap)
        if kind == "square":
            return np.c_[r - wall * u, np.full(cap, height)], height
        if kind == "bevel":
            drop = wall * math.tan(math.radians(lean))
            return np.c_[r - wall * u, height - drop * u], height - drop
        if kind == "rounded":
            a = np.pi * u
            return np.c_[r - wall / 2 + wall / 2 * np.cos(a), height - wall / 2 + wall / 2 * np.sin(a)], height - wall / 2
        tip, half = np.array([r - wall / 2, height]), cap // 2       # knife: up to the tip, then down
        lo_o, lo_i = np.array([r, height - 4.0]), np.array([r - wall, height - 4.0])
        up = lo_o + np.linspace(0, 1, half + 1)[:, None] * (tip - lo_o)
        down = tip + np.linspace(0, 1, cap - half)[1:, None] * (lo_i - tip)
        return np.vstack([up, down]), height - 4.0

    def weights(deg):
        """How much of each top shape a ring at this angle has (linear blends in the gaps)."""
        order = list(spans.items())
        for kind, (a0, a1) in order:
            if a0 <= deg < a1:
                return {kind: 1.0}
        for (k0, (_, a1)), (k1, (b0, _)) in zip(order, order[1:] + order[:1]):
            gap_end = b0 if b0 >= a1 else b0 + 360
            d = deg if deg >= a1 else deg + 360
            if a1 <= d < gap_end:
                f = (d - a1) / (gap_end - a1)
                w = {k0: 1.0 - f}
                w[k1] = w.get(k1, 0.0) + f
                return w
        return {"square": 1.0}

    n_wall = int(round(height / step))
    profiles = []
    for deg in np.arange(rings) * 360.0 / rings:
        pts, z_top = 0.0, 0.0
        for kind, w in weights(deg).items():
            p, z = top(kind)
            pts, z_top = pts + w * p, z_top + w * z
        z = np.linspace(0.0, z_top, n_wall + 1)[:-1]
        bottom = np.c_[r - wall + wall * np.array([1, 2]) / 3, [0.0, 0.0]]
        profiles.append(np.vstack([np.c_[np.full(n_wall, r), z], pts, np.c_[np.full(n_wall, r - wall), z[::-1]][:-1],
                                   [[r - wall, 0.0]], bottom]))
    prof = np.array(profiles)                                            # (rings, points, 2): (radius, z)
    th = np.arange(rings) * 2 * np.pi / rings
    v = np.stack([prof[..., 0] * np.cos(th)[:, None], prof[..., 0] * np.sin(th)[:, None], prof[..., 1]], axis=-1)
    k, i = np.meshgrid(np.arange(rings), np.arange(prof.shape[1]), indexing="ij")
    idx = lambda kk, ii: (kk % rings) * prof.shape[1] + ii % prof.shape[1]
    a, b, c, d = idx(k, i), idx(k, i + 1), idx(k + 1, i + 1), idx(k + 1, i)
    f = np.r_[np.c_[a.ravel(), b.ravel(), c.ravel()], np.c_[a.ravel(), c.ravel(), d.ravel()]]
    m = Manifold(Mesh(vert_properties=v.reshape(-1, 3).astype(np.float32), tri_verts=f.astype(np.uint32)))
    if m.volume() < 0:
        m = Manifold(Mesh(vert_properties=v.reshape(-1, 3).astype(np.float32), tri_verts=f[:, ::-1].astype(np.uint32)))
    return m


def cone_shell(lean=50.0, wall=2.35, length=45.0, r=70.0, seg=128):
    """A lampshade: a shell whose wall leans `lean` degrees from the vertical, open at both ends,
    with square edges like a real mold's. A level cut meets the wall `lean` degrees from square
    all the way round. Returns (shell, height of the middle of the wall)."""
    a = np.radians(lean)
    up, out = np.array([-np.sin(a), np.cos(a)]), np.array([np.cos(a), np.sin(a)])
    s = np.linspace(0, length, int(length) + 1)[:, None]                # a ring of triangles every mm
    p0 = np.array([r, 0.0])
    profile = np.vstack([p0 + s * up, (p0 + s * up - wall * out)[::-1]])   # (radius, z), outer then inner
    (ax, ay), (bx, by) = profile[1] - profile[0], profile[-1] - profile[0]
    if ax * by - ay * bx < 0:                                           # counter-clockwise
        profile = profile[::-1]
    return CrossSection([profile]).revolve(seg), length * np.cos(a) / 2
