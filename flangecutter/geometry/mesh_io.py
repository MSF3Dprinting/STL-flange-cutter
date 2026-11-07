"""Load, check and repair STL files; write STL and ZIP files."""
from __future__ import annotations

import hashlib
import io
import zipfile
from dataclasses import dataclass, field

import numpy as np
import trimesh
from manifold3d import Error, Manifold, Mesh

from . import CutError, warning

HOLES_MESSAGE = ("This model has holes. Repair it first, for example with your slicer's repair function, "
                 "and try again.")
SMALL_MM, LARGE_MM = 10.0, 2000.0     # probably the wrong unit outside this range
CREASE_DEG = 45.0                     # edges sharper than this separate surface patches
RIM, OTHER = -1, -2                   # face labels: the mold's edge band; not part of a thin shell
MIN_EDGE = 1e-4                       # mm: slivers thinner than this are removed when writing an STL


@dataclass
class Model:
    """A checked model: the repaired mesh (world coordinates, mm) and its Manifold twin."""

    name: str
    sha256: str
    mesh: trimesh.Trimesh
    manifold: Manifold
    repaired: bool
    warnings: list[dict] = field(default_factory=list)
    normals: np.ndarray | None = None            # plain copies: trimesh re-hashes the mesh on every
    bounds: np.ndarray | None = None             # access to face_normals and bounds (20-50 ms at 100k)
    surfaces: np.ndarray | None = None           # per face: surface id (>= 0), RIM or OTHER (see shell_surfaces)
    outside: tuple = ()                          # ids of the surfaces that face outward
    cache: dict = field(default_factory=dict)    # cutter sections, keyed by plane and settings

    def info(self) -> dict:
        """Summary for the web page."""
        lo, hi = self.bounds
        return {"name": self.name, "sha256": self.sha256, "triangles": int(len(self.mesh.faces)),
                "bbox": {"min": lo.tolist(), "max": hi.tolist(), "size": (hi - lo).tolist(),
                         "center": ((lo + hi) / 2).tolist()},
                "watertight": True, "repaired": self.repaired, "warnings": self.warnings}


def load_stl(data: bytes, name: str) -> Model:
    """Read an STL (binary or ASCII), repair small problems and check that it is a closed solid."""
    sha = hashlib.sha256(data).hexdigest()
    try:
        mesh = trimesh.load_mesh(io.BytesIO(data), file_type="stl", process=True)
    except Exception as exc:  # trimesh raises many types for bad files
        raise CutError("This file could not be read as an STL file. Export it again as STL and try again.",
                       repr(exc)) from exc
    if not isinstance(mesh, trimesh.Trimesh) or len(mesh.faces) == 0:
        raise CutError("This STL file contains no triangles.", f"loaded {type(mesh).__name__}")

    # Basic repair: degenerate and duplicate faces, winding, inside-out bodies.
    # (Vertices were merged by process=True.) We call the repair steps one by one, because
    # trimesh's fix_normals() silently needs scipy, which this app does not ship.
    n_faces = len(mesh.faces)
    mesh.update_faces(mesh.nondegenerate_faces())
    mesh.update_faces(mesh.unique_faces())
    mesh.remove_unreferenced_vertices()
    repaired = len(mesh.faces) != n_faces
    if not mesh.is_winding_consistent:
        trimesh.repair.fix_winding(mesh)     # uses networkx
        repaired = True
    if not mesh.is_watertight:
        raise CutError(HOLES_MESSAGE, f"{len(mesh.faces)} faces, not watertight after repair")
    if mesh.volume < 0:
        mesh.invert()
        repaired = True
    manifold = to_manifold(mesh)

    warnings = []
    size = float(np.max(mesh.extents))
    if size < SMALL_MM:
        warnings.append(warning("units_small", f"The model is only {size:.2f} mm across. It may be in "
                                               "centimeters, inches or meters.",
                                "Scale the model to millimeters in your slicer or CAD program, then open it again."))
    elif size > LARGE_MM:
        warnings.append(warning("units_large", f"The model is {size:.0f} mm across. It may be in a "
                                               "unit smaller than millimeters.",
                                "Scale the model to millimeters in your slicer or CAD program, then open it again."))
    if repaired:
        warnings.append(warning("repaired", "The model had small errors. They were fixed automatically.",
                                "No action needed. Check the parts in your slicer."))
    surfaces, outside, notes = shell_surfaces(mesh, manifold.volume() / max(mesh.area / 2, 1e-9))
    if "edge_not_found" in notes:
        warnings.append(warning("edge_not_found", "Flange Cutter could not find the edge of this mold (it may "
                                                  "be rounded). It chooses the flange side piece by piece along "
                                                  "each cut.",
                                "Check the flange in the preview, especially near the edge of the mold."))
    return Model(name=name, sha256=sha, mesh=mesh, manifold=manifold, repaired=repaired, warnings=warnings,
                 normals=np.array(mesh.face_normals), bounds=np.array(mesh.bounds),
                 surfaces=surfaces, outside=outside)


def _components(n: int, pairs: np.ndarray) -> np.ndarray:
    """Connected components of a graph with nodes 0..n-1 and edges `pairs` (k, 2): the smallest node
    of its component, for every node (hook roots together, then jump pointers; no scipy needed)."""
    parent = np.arange(n)
    a, b = pairs[:, 0], pairs[:, 1]
    while True:
        pa, pb = parent[a], parent[b]
        lo, hi = np.minimum(pa, pb), np.maximum(pa, pb)
        todo = lo != hi
        if not todo.any():
            return parent
        np.minimum.at(parent, hi[todo], lo[todo])
        while True:
            up = parent[parent]
            if np.array_equal(up, parent):
                break
            parent = up


def shell_surfaces(mesh: trimesh.Trimesh, thickness: float) -> tuple[np.ndarray, tuple, set]:
    """Which surface of a thin-walled mold each face belongs to.

    A thin-walled mold is a thickened surface: an outer and an inner surface, both smooth, joined
    along the mold's edge by a narrow band (the rim) that meets them at sharp edges. Faces are
    grouped into patches across smooth edges (under 45 deg). A patch is thin when its area divided
    by its boundary length is under the wall thickness (a strip up to about twice the wall wide).
    Thick patches joined across sharp edges make a surface; thin patches joined across sharp edges
    make a thin region. A surface whose normals point away from its body's centre (area-weighted)
    faces outward, else inward. The outward surfaces are the outer side of the wall, with every
    raised pad or label on it; the inward ones are the inner side (several of them where two cups
    are joined). A thin region that touches surfaces of one side only belongs to the largest of them
    (the walls of a pad, the walls and floor of an engraved line); one that touches both sides is the
    rim (also when it is made of several facets). A body with surfaces on both sides joined by rim is
    a thin shell; any other body gets OTHER, and the cutter uses the proof of concept's rule there.

    Returns (label per face, ids of the outward surfaces, notes). Notes: "edge_not_found" when a body
    with thin walls came out OTHER (its edge may be rounded, so its two sides are one surface).
    """
    n = len(mesh.faces)
    adj = mesh.face_adjacency
    if n == 0 or len(adj) == 0:
        return np.full(n, OTHER), (), set()
    smooth = mesh.face_adjacency_angles < np.radians(CREASE_DEG)
    _, patch = np.unique(_components(n, adj[smooth]), return_inverse=True)       # face -> patch
    k = patch.max() + 1
    area = np.bincount(patch, mesh.area_faces, minlength=k)
    edge = mesh.vertices[mesh.face_adjacency_edges[~smooth]]
    length = np.linalg.norm(edge[:, 0] - edge[:, 1], axis=1)
    pa, pb = patch[adj[~smooth, 0]], patch[adj[~smooth, 1]]                       # patches across sharp edges
    border = np.bincount(pa, length, minlength=k) + np.bincount(pb, length, minlength=k)
    thin = area / np.maximum(border, 1e-9) < thickness                            # per patch
    links = np.unique(np.sort(np.c_[pa, pb], axis=1), axis=0)
    links = links[links[:, 0] != links[:, 1]].reshape(-1, 2)
    t0, t1 = thin[links[:, 0]], thin[links[:, 1]]
    surface = _components(k, links[~t0 & ~t1])        # per patch: its surface (thick patches joined)
    region = _components(k, links[t0 & t1])           # per patch: its thin region (thin patches joined)
    body = _components(k, links)
    # which way each surface faces: away from its body's centre (+1) or towards it (-1)
    face_body = body[patch]
    centre, weight, normals = mesh.triangles_center, mesh.area_faces, mesh.face_normals
    w_sum = np.bincount(face_body, weight, minlength=k)
    mid = np.stack([np.bincount(face_body, weight * centre[:, i], minlength=k) for i in range(3)], axis=1)
    mid /= np.maximum(w_sum, 1e-30)[:, None]
    away = np.sign(np.einsum("ij,ij->i", normals, centre - mid[face_body])) * weight
    face_surface = surface[patch]
    side = np.where(np.bincount(face_surface, away, minlength=k) > 0, 1, -1)       # per surface
    s_area = np.bincount(surface[~thin], area[~thin], minlength=k)
    # thin regions: part of the side they touch, or rim where they touch both sides
    mixed = links[t0 != t1]
    t_p = np.where(thin[mixed[:, 0]], mixed[:, 0], mixed[:, 1])
    s_p = np.where(thin[mixed[:, 0]], mixed[:, 1], mixed[:, 0])
    touches = {}
    for r, s in np.unique(np.c_[region[t_p], surface[s_p]], axis=0).reshape(-1, 2).tolist():
        touches.setdefault(r, set()).add(s)
    region_label = {r: (max(ss, key=lambda s: s_area[s]) if len({side[s] for s in ss}) == 1 else RIM)
                    for r, ss in touches.items()}
    plabel = np.where(thin, RIM, surface)
    thin_patches = np.flatnonzero(thin)
    plabel[thin_patches] = [region_label.get(int(r), RIM) for r in region[thin_patches]]
    # a thin shell: surfaces on both sides of the wall, joined by rim
    is_rim = thin & (plabel == RIM)
    has_rim = np.bincount(body[is_rim], minlength=k) > 0
    thick = ~thin
    outward = np.bincount(body[thick], side[surface[thick]] > 0, minlength=k) > 0
    inward = np.bincount(body[thick], side[surface[thick]] < 0, minlength=k) > 0
    shell = has_rim & outward & inward                                              # per body
    not_shell = ~shell[body]                                                        # per patch
    plabel[not_shell] = OTHER
    keep = plabel >= 0
    ids, plabel[keep] = np.unique(plabel[keep], return_inverse=True)               # surfaces 0..m-1
    labels = plabel[patch]
    notes = set()
    lo = np.full((k, 3), np.inf); hi = np.full((k, 3), -np.inf)
    np.minimum.at(lo, face_body, centre); np.maximum.at(hi, face_body, centre)
    for b in np.unique(body[not_shell]).tolist():          # thin walls, yet no shell: the edge may be rounded
        if thickness < 0.1 * float(np.min(hi[b] - lo[b])):
            notes.add("edge_not_found")
    return labels, tuple(int(i) for i, s in enumerate(ids) if side[s] > 0), notes


def to_manifold(mesh: trimesh.Trimesh) -> Manifold:
    m = Manifold(Mesh(vert_properties=np.asarray(mesh.vertices, np.float32),
                      tri_verts=np.asarray(mesh.faces, np.uint32)))
    if m.status() != Error.NoError:
        raise CutError(HOLES_MESSAGE, f"Manifold status {m.status()}")
    return m


def to_trimesh(m: Manifold) -> trimesh.Trimesh:
    mm = m.to_mesh()
    return trimesh.Trimesh(np.asarray(mm.vert_properties)[:, :3], np.asarray(mm.tri_verts), process=False)


def stl_bytes(m: Manifold) -> bytes:
    """Binary STL of a solid.

    Booleans can leave sliver triangles without area (a point on the straight edge between two
    others where flange layers meet; a needle where a mold face grazes a flange face), which slicers
    report as errors. Manifold's simplify removes most of them; no surface moves by more than 0.1 um.
    A needle that only flattens when rounded to float32, as STL stores it, then loses its shortest
    edge if that is under 0.1 um: the two points merge and the triangles on that edge are dropped.
    Every step is checked on the mesh as an STL reader sees it (float32 points, equal points merged):
    a step that would leave an open or non-manifold edge is skipped (a needle is a warning in a
    slicer, a broken surface is an error).
    """
    for solid in (m.simplify(MIN_EDGE), m):
        v, f = _as_stored(solid)
        if _closed(f):
            break
    fixed = _merge_needles(v, f)
    if fixed is not f and _closed(fixed):
        f = fixed
    return trimesh.Trimesh(v, f, process=False).export(file_type="stl")


def _as_stored(solid: Manifold) -> tuple[np.ndarray, np.ndarray]:
    """Points and triangles as an STL stores them: float32, with equal points merged (as every reader
    merges them); triangles that become a line or a point are dropped."""
    mm = solid.to_mesh()
    p = np.asarray(mm.vert_properties, np.float32)[:, :3] + np.float32(0)   # -0.0 becomes 0.0
    bits = np.ascontiguousarray(p).view(np.uint32)
    order = np.lexsort(bits.T[::-1])
    new = np.r_[True, (bits[order][1:] != bits[order][:-1]).any(axis=1)]
    inv = np.empty(len(p), np.int64)
    inv[order] = np.cumsum(new) - 1
    f = inv[np.asarray(mm.tri_verts, np.int64)]
    f = f[(f[:, 0] != f[:, 1]) & (f[:, 1] != f[:, 2]) & (f[:, 2] != f[:, 0])]
    return p[order][new].astype(np.float64), f


def _merge_needles(v: np.ndarray, f: np.ndarray) -> np.ndarray:
    """Triangles after merging the ends of every flat triangle's shortest edge under MIN_EDGE."""
    for _ in range(4):
        edge = v[f[:, [1, 2, 0]]] - v[f]                                 # edges 0-1, 1-2, 2-0
        length = np.linalg.norm(edge, axis=2)
        height = np.linalg.norm(np.cross(edge[:, 0], edge[:, 1]), axis=1) / np.maximum(length.max(axis=1), 1e-30)
        flat = np.flatnonzero(height <= 1e-8)                             # trimesh's test for degenerate faces
        k = length[flat].argmin(axis=1)
        flat, k = flat[length[flat, k] < MIN_EDGE], k[length[flat, k] < MIN_EDGE]
        if not len(flat):
            break
        f = _components(len(v), np.c_[f[flat, k], f[flat, (k + 1) % 3]])[f]
        f = f[(f[:, 0] != f[:, 1]) & (f[:, 1] != f[:, 2]) & (f[:, 2] != f[:, 0])]
        _, idx, count = np.unique(np.sort(f, axis=1), axis=0, return_index=True, return_counts=True)
        f = f[np.sort(idx[count == 1])]          # two triangles folded onto each other: drop both
    return f


def _closed(f: np.ndarray) -> bool:
    """Every edge has exactly two triangles, one in each direction (a closed, oriented surface): the
    directed edges, sorted, are the reversed edges, sorted, and none comes twice."""
    if len(f) == 0:
        return False
    n = np.int64(f.max()) + 1
    a, b = f.reshape(-1).astype(np.int64), f[:, [1, 2, 0]].reshape(-1).astype(np.int64)
    forward, backward = np.sort(a * n + b), np.sort(b * n + a)
    return bool(np.array_equal(forward, backward) and (forward[1:] != forward[:-1]).all())


def zip_bytes(files: dict[str, bytes | str]) -> bytes:
    """A ZIP archive from {name: content}; text is stored as UTF-8."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name, content in files.items():
            z.writestr(name, content.encode("utf-8") if isinstance(content, str) else content)
    return buf.getvalue()
