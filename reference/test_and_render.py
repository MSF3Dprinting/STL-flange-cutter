"""Run the flange cutter on the demo mold (two cut cases), verify, render."""
import json, time
import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from mpl_toolkits.mplot3d.art3d import Poly3DCollection
from shapely.geometry import Point

from flange_cut import demo_mold, flange_cut, plane_frame, manifold_to_tm, export

mold, cavity = demo_mold()
zc = 140.0
cases = {
    "A_lengthwise": dict(origin=[0, 0, 0], normal=[0, 1, 0], count=8),
    "B_transverse_tilted": dict(origin=[30.0 * (zc / 240) ** 2, 0, zc],
                                normal=[np.sin(np.radians(10)), 0, np.cos(np.radians(10))],
                                count=6),
}
results, summary = {}, {}
for name, cfg in cases.items():
    t = time.time()
    r = flange_cut(mold, cfg["origin"], cfg["normal"], bolt="M4", count=cfg["count"])
    to_plane, _ = plane_frame(cfg["origin"], cfg["normal"])
    cav = cavity.transform(to_plane)
    a, b = r["half_a"], r["half_b"]
    ta, tb = manifold_to_tm(a), manifold_to_tm(b)
    summary[name] = dict(
        report=r["report"],
        watertight=[bool(ta.is_watertight), bool(tb.is_watertight)],
        genus=[a.genus(), b.genus()],
        volume_cm3=[round(a.volume() / 1000, 1), round(b.volume() / 1000, 1)],
        halves_overlap_mm3=round((a ^ b).volume(), 4),
        cavity_intrusion_mm3=[round((a ^ cav).volume(), 4), round((b ^ cav).volume(), 4)],
        total_seconds=round(time.time() - t, 2),
        files=export(r, "out", name),
    )
    results[name] = (r, cfg)
print(json.dumps(summary, indent=1))
json.dump(summary, open("out/verification.json", "w"), indent=1)

# ------------------------------------------------------------------ render
def draw_section(ax, r, title):
    def fill(g, **kw):
        for P in getattr(g, "geoms", [g]):
            x, y = P.exterior.xy; ax.fill(x, y, **kw)
            for h in P.interiors:
                x, y = h.xy; ax.fill(x, y, color="white", zorder=kw.get("zorder", 1) + 0.1)
    fill(r["flange2d"], color="#e8a33d", alpha=0.85, zorder=1, lw=0)
    fill(r["section"], color="#3b4252", zorder=3, lw=0)
    for x, y in r["bolts_xy"]:
        ax.add_patch(plt.Circle((x, y), r["washer"] / 2, fill=False, ls="--", lw=0.8,
                                color="#5e3a00", zorder=4))
        ax.add_patch(plt.Circle((x, y), r["hole_r"], color="white", zorder=5))
    cx, cy = r["cavity_ref"]
    ax.text(cx, cy, "mold cavity\n(kept clear)", ha="center", va="center", fontsize=9,
            color="#2e7d32", weight="bold")
    ax.set_aspect("equal"); ax.set_title(title, fontsize=11); ax.axis("off")


def draw_3d(ax, r, cfg, title, gap=18.0):
    n = np.asarray(cfg["normal"], float); n /= np.linalg.norm(n)
    light = np.array([0.4, -0.5, 0.75]); light /= np.linalg.norm(light)
    allv = []
    for m, sgn, base in ((r["half_a"], +1, np.array([0.55, 0.66, 0.80])),
                         (r["half_b"], -1, np.array([0.86, 0.62, 0.42]))):
        tm = manifold_to_tm(m.transform(r["to_world"]))
        tm.apply_translation(sgn * gap / 2 * n)
        shade = np.clip(0.35 + 0.65 * np.abs(tm.face_normals @ light), 0, 1)
        col = np.c_[base * shade[:, None], np.ones(len(shade))]
        ax.add_collection3d(Poly3DCollection(tm.triangles, facecolors=col, linewidths=0))
        allv.append(tm.vertices)
    v = np.vstack(allv); c = (v.max(0) + v.min(0)) / 2; s = (v.max(0) - v.min(0)).max() / 2
    ax.set_xlim(c[0] - s, c[0] + s); ax.set_ylim(c[1] - s, c[1] + s); ax.set_zlim(c[2] - s, c[2] + s)
    ax.set_box_aspect((1, 1, 1), zoom=1.55); ax.axis("off"); ax.set_title(title, fontsize=11)


fig = plt.figure(figsize=(14, 12))
(rA, cA), (rB, cB) = results["A_lengthwise"], results["B_transverse_tilted"]
draw_section(fig.add_subplot(2, 2, 1), rA,
             "Lengthwise cut: flange (orange) on the outside only,\n8 x M4 spaced along the wall")
draw_section(fig.add_subplot(2, 2, 2), rB,
             "Tilted transverse cut: ring flange outside,\n6 x M4 evenly around")
axA = fig.add_subplot(2, 2, 3, projection="3d"); axA.view_init(elev=18, azim=-58)
draw_3d(axA, rA, cA, "Exploded halves (lengthwise)")
axB = fig.add_subplot(2, 2, 4, projection="3d"); axB.view_init(elev=24, azim=-50)
draw_3d(axB, rB, cB, "Exploded halves (transverse)")
fig.suptitle("Flange cutter proof of concept: 2.5 mm test mold, M4 bolts", fontsize=13)
plt.tight_layout()
plt.savefig("out/preview.png", dpi=110)
print("rendered")
