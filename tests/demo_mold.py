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
