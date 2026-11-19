"""Load, check and repair STL files; write STL and ZIP files."""
from __future__ import annotations

import hashlib
import io
import math
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
WALL_CELL = 2.0                       # wall directions are read in cells this many wall thicknesses wide
END_COS = math.cos(math.radians(60))  # a face more than 60 deg off the wall direction is the wall's end
SIDE_RATIO = 2.0                      # the two sides of a thin shell differ in area by at most this much
CHANGED = 0.001                       # the second look replaces a first-look shell that flips more area
MAX_PIECES = 2_000_000                # wall directions: at most about this many triangle pieces
PAIRS = ((0, 0), (1, 1), (2, 2), (0, 1), (0, 2), (1, 2))      # the 6 entries of a symmetric 3 x 3 matrix
SYMMETRIC = [0, 3, 4, 3, 1, 5, 4, 5, 2]                         # ... and where they go in it
BLOCK = np.array([(i, j, k) for i in (0, 1) for j in (0, 1) for k in (0, 1)])   # a 2 x 2 x 2 block of cells


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
    edge: str = ""                               # how the mold's edge was found, for the log (see load_stl)
    wall: float = 0.0                            # wall thickness estimate (volume / half the area), mm
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
    thickness = manifold.volume() / max(mesh.area / 2, 1e-9)
    # Two looks at the faces: by sharp edges, and with the wall's end found from the wall direction
    # too (rounded and knife edges, decision 54). Each body keeps the first look unless that failed
    # or was fooled (decision 56).
    first = shell_surfaces(mesh, thickness)
    second = shell_surfaces(mesh, thickness, wall_end(mesh, thickness))
    surfaces, outside, edge = _choose(mesh, thickness, first, second)
    if edge == "not found":
        warnings.append(warning("edge_not_found", "Flange Cutter could not find the edge of this mold, so it "
                                                  "cannot tell the two faces apart. It chooses the flange side "
                                                  "piece by piece along each cut.",
                                "Check the flange in the preview, especially near the edge of the mold."))
    return Model(name=name, sha256=sha, mesh=mesh, manifold=manifold, repaired=repaired, warnings=warnings,
                 normals=np.array(mesh.face_normals), bounds=np.array(mesh.bounds),
                 surfaces=surfaces, outside=outside, edge=edge, wall=thickness)


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


def shell_surfaces(mesh: trimesh.Trimesh, thickness: float,
                   end: np.ndarray | None = None) -> tuple[np.ndarray, tuple, set]:
    """Which surface of a thin-walled mold each face belongs to.

    A thin-walled mold is a thickened surface: an outer and an inner surface, both smooth, joined
    along the mold's edge by a narrow band (the rim) that meets them at sharp edges. Faces are
    grouped into patches across smooth edges (under 45 deg). With `end` (the faces at the wall's end,
    from wall_end) an edge between a face of the wall's end and one that is not also parts patches:
    so a rounded or knife-edged stretch of the edge still parts the two faces of the wall. A patch is
    thin when its area divided by its boundary length is under the wall thickness (a strip up to
    about twice the wall wide).
    Thick patches joined across sharp edges make a surface; thin patches joined across sharp edges
    make a thin region. A surface whose normals point away from its body's centre (area-weighted)
    faces outward, else inward. The outward surfaces are the outer side of the wall, with every
    raised pad or label on it; the inward ones are the inner side (several of them where two cups
    are joined). A thin region that touches surfaces of one side only belongs to the largest of them
    (the walls of a pad, the walls and floor of an engraved line); one that touches both sides is the
    rim (also when it is made of several facets). A body with surfaces on both sides joined by rim,
    whose two sides differ in area by at most 2 times, is a thin shell; any other body gets OTHER, and
    the cutter uses the proof of concept's rule there.

    Returns (label per face, ids of the outward surfaces, notes). Notes: "edge_not_found" when a body
    with thin walls came out OTHER (where its edge is rounded, its two sides are one surface unless
    `end` parts them).
    """
    n = len(mesh.faces)
    adj = mesh.face_adjacency
    if n == 0 or len(adj) == 0:
        return np.full(n, OTHER), (), set()
    smooth = mesh.face_adjacency_angles < np.radians(CREASE_DEG)
    if end is not None:
        smooth &= end[adj[:, 0]] == end[adj[:, 1]]
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
    # a thin shell: surfaces on both sides of the wall, joined by rim, and about the same area on both
    # sides (the two faces of a thin wall): where a rounded stretch of the edge joined the outer face to
    # most of the inner one, the rest of the inner side (a pad on it, a second cavity) is far smaller
    is_rim = thin & (plabel == RIM)
    has_rim = np.bincount(body[is_rim], minlength=k) > 0
    thick = ~thin
    a_out = np.bincount(body[thick], area[thick] * (side[surface[thick]] > 0), minlength=k)
    a_in = np.bincount(body[thick], area[thick] * (side[surface[thick]] < 0), minlength=k)
    shell = has_rim & (a_out > 0) & (a_in > 0) & (np.maximum(a_out, a_in) <= SIDE_RATIO * np.minimum(a_out, a_in))
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


def _choose(mesh: trimesh.Trimesh, thickness: float, first: tuple, second: tuple) -> tuple[np.ndarray, tuple, str]:
    """Per body (connected part of the mesh), the labels of the first look (sharp edges) or the second
    (with the wall's end).

    The first look stays where it found a thin shell and the second agrees with it, or finds none: so
    every mold that the first look labeled keeps exactly its labels. The second look is taken where
    the first found no shell (a rounded or knife edge joined the two faces), and where both found one
    but they put more than 0.1 % of the body's area on opposite sides (outward in one, inward in the
    other): then a rounded stretch joined part of one face to the other, and a pad or an engraved
    line made the rest still look like a shell. On every mold that the first look labels right, the
    two looks put no face on opposite sides at all. Faces that only change between a surface and
    the rim do not count: the second look's wall directions are less sure at sharp bends of the wall.
    Returns (labels, ids of the outward surfaces, how the mold's edge was found: "sharp", "found from
    the wall direction", "not found" (a thin-walled body that neither look could label) or "none").
    """
    (l1, o1, _), (l2, o2, _) = first, second
    n = len(mesh.faces)
    if n == 0 or len(mesh.face_adjacency) == 0:
        return l1, o1, "none"
    body = np.unique(_components(n, mesh.face_adjacency), return_inverse=True)[1]
    nb = int(body.max()) + 1
    area = mesh.area_faces
    total = np.bincount(body, area, minlength=nb)
    other1 = np.bincount(body, area * (l1 == OTHER), minlength=nb) > 0          # a body is OTHER as a whole
    other2 = np.bincount(body, area * (l2 == OTHER), minlength=nb) > 0
    k1, k2 = _kinds(l1, o1), _kinds(l2, o2)
    flips = ((k1 == 1) & (k2 == 2)) | ((k1 == 2) & (k2 == 1))                  # outward in one look, inward in the other
    differ = np.bincount(body, area * flips, minlength=nb) > CHANGED * total
    take = (other1 | (~other2 & differ))[body]                                 # per face: from the second look
    labels = np.where(take, np.where(l2 >= 0, l2 + max(int(l1.max()) + 1, 0), l2), l1)
    out_ids = [o for o in o1] + [o + max(int(l1.max()) + 1, 0) for o in o2]
    keep = labels >= 0
    ids, labels[keep] = np.unique(labels[keep], return_inverse=True)           # surfaces 0..m-1 again
    outside = tuple(int(i) for i, s in enumerate(ids) if s in set(out_ids))
    lost = other1 & other2
    centre = mesh.triangles_center
    lo = np.full((nb, 3), np.inf)
    hi = np.full((nb, 3), -np.inf)
    np.minimum.at(lo, body, centre)
    np.maximum.at(hi, body, centre)
    if (lost & (thickness < 0.1 * (hi - lo).min(axis=1))).any():               # thin walls, no shell found
        return labels, outside, "not found"
    if not outside:
        return labels, outside, "none"
    return labels, outside, "found from the wall direction" if (take & (labels >= RIM)).any() else "sharp"


def _kinds(labels: np.ndarray, outside: tuple) -> np.ndarray:
    """Per face: 0 rim, 1 outward surface, 2 inward surface, 3 OTHER."""
    return np.where(labels == RIM, 0, np.where(np.isin(labels, outside), 1, np.where(labels >= 0, 2, 3)))


def wall_end(mesh: trimesh.Trimesh, thickness: float) -> np.ndarray:
    """The faces at the end of the wall (the mold's edge), found without sharp edges.

    Both faces of a thin wall point along the wall's direction, one each way. A face whose normal is
    more than 60 deg off it belongs to the wall's end: the rim, or the middle of a rounded edge. So do
    both faces of an edge where the normal crosses over from one side of the wall to the other, both
    within 60 deg of the wall direction (a knife edge: the two faces meet without a rim between them).
    Real face-mask molds have both (decision 54). Rim faces are square to the wall direction, so the
    sign of n . m is noise there: they never count for the knife-edge rule. The second face's side is
    read against both faces' wall directions, which differ a little where the edge changes its shape.
    The edge need not be convex: a third of the knife-edge folds of one real mold were concave (its
    edge curls there).
    """
    m = _wall_directions(mesh, WALL_CELL * thickness)
    normals = mesh.face_normals
    a, b = mesh.face_adjacency.T
    dot = np.einsum("ij,ij->i", normals, m)
    end = np.abs(dot) < END_COS
    # b's side of the wall, against a's wall direction and against its own (turned to match a's); a
    # sharp edge that crosses over by either one, both faces clearly on a side, is a knife edge
    da = dot[a]
    mab = np.einsum("ij,ij->i", m[a], m[b])         # turned to match only where the two are clearly one axis:
    db = np.c_[np.einsum("ij,ij->i", normals[b], m[a]),      # at a square bend they are square to each other
               np.where(np.abs(mab) >= END_COS, dot[b] * np.sign(mab), 0.0)]   # and the sign is rounding noise
    crosses = ((da[:, None] * db < 0) & (np.abs(db) >= END_COS)).any(axis=1)
    fold = crosses & (np.abs(da) >= END_COS)
    end[a[fold]] = True
    end[b[fold]] = True
    return end


def _wall_directions(mesh: trimesh.Trimesh, h: float) -> np.ndarray:
    """Per face: the wall's direction (a unit vector, either way round) near its centre.

    It is the main axis of the face normals, weighted by area, in the 2 x 2 x 2 cells of size h nearest
    to the face's centre (2 h wide: both faces of the wall, and what is left of it at its end). Long
    triangles are cut into pieces of at most one cell first, so that each cell sees the surface in it
    (pieces of half a cell gave the same labels on every test and real mold, 1.6 to 4 times slower).
    No scipy: a sparse grid of cells, numpy only.
    """
    o = mesh.bounds[0] - h                                   # every cell index is positive
    size = max(h, math.sqrt(2 * mesh.area / MAX_PIECES))
    tri = _split_long(mesh.triangles - o, size)
    cross = np.cross(tri[:, 1] - tri[:, 0], tri[:, 2] - tri[:, 0])
    w = np.linalg.norm(cross, axis=1)                         # twice the area
    keep = w > 1e-12
    if not keep.any():                                        # nothing to read a direction from
        return np.array(mesh.face_normals)
    n, w = cross[keep] / w[keep, None], w[keep]
    cell = np.floor(tri[keep].mean(axis=1) / h).astype(np.int64)
    corner = np.floor((mesh.triangles_center - o) / h - 0.5).astype(np.int64)   # of each face's block
    dims = np.maximum(cell.max(axis=0), corner.max(axis=0) + 1) + 1

    def key(c):
        return (c[:, 0] * dims[1] + c[:, 1]) * dims[2] + c[:, 2]
    cells, inv = np.unique(key(cell), return_inverse=True)
    tensor = np.stack([np.bincount(inv, w * n[:, i] * n[:, j], len(cells)) for i, j in PAIRS], axis=1)
    blocks, back = np.unique(key(corner), return_inverse=True)
    start = np.stack([blocks // (dims[1] * dims[2]), blocks // dims[2] % dims[1], blocks % dims[2]], axis=1)
    total = np.zeros((len(blocks), 6))
    for step in BLOCK:
        k = key(start + step)
        j = np.minimum(np.searchsorted(cells, k), len(cells) - 1)
        hit = cells[j] == k
        total[hit] += tensor[j[hit]]
    axis = np.linalg.eigh(total[:, SYMMETRIC].reshape(-1, 3, 3))[1][:, :, 2]   # largest eigenvalue's vector
    m = axis[back]
    empty = total[back, :3].sum(axis=1) <= 0                  # no surface around (should not happen)
    m[empty] = mesh.face_normals[empty]
    return m


def _split_long(tri: np.ndarray, size: float) -> np.ndarray:
    """Triangles (n, 3, 3) halved across their longest edge until no edge is longer than `size`."""
    done = []
    for _ in range(200):                                     # each round halves the longest edges
        edge = np.linalg.norm(tri[:, [1, 2, 0]] - tri, axis=2)           # edge k: point k to point k + 1
        long = edge.max(axis=1) > size
        done.append(tri[~long])
        tri = tri[long]
        if not len(tri):
            break
        k = edge[long].argmax(axis=1)
        i = np.arange(len(tri))
        a, b, c = tri[i, k], tri[i, (k + 1) % 3], tri[i, (k + 2) % 3]
        mid = (a + b) / 2
        tri = np.concatenate([np.stack([a, mid, c], axis=1), np.stack([mid, b, c], axis=1)])
    return np.concatenate(done + [tri])


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
