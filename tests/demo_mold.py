"""The proof-of-concept test mold (reference/flange_cut.py: demo_mold), with a resolution knob.

samples/demo_mold.stl was exported from the POC with the default resolution (about 32k triangles).
"""
import numpy as np
from manifold3d import CrossSection, Manifold

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


def rounded_cup(seg=160):
    """A cup 80 mm across and 60 mm high, 2.5 mm walls and bottom, whose top edge is rounded: no sharp
    edge separates its outer and inner faces, so they cannot be told apart."""
    profile = [(0.0, 0.0), (40.0, 0.0), (40.0, 60.0)]
    profile += [(38.75 + 1.25 * np.cos(a), 60 + 1.25 * np.sin(a)) for a in np.linspace(0, np.pi, 25)[1:-1]]
    profile += [(37.5, 60.0), (37.5, 2.5), (0.0, 2.5)]
    return CrossSection([np.array(profile)]).revolve(seg)


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
