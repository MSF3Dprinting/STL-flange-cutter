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
    return Model(name=name, sha256=sha, mesh=mesh, manifold=manifold, repaired=repaired, warnings=warnings,
                 normals=np.array(mesh.face_normals), bounds=np.array(mesh.bounds))


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
    """Binary STL of a solid."""
    return to_trimesh(m).export(file_type="stl")


def zip_bytes(files: dict[str, bytes | str]) -> bytes:
    """A ZIP archive from {name: content}; text is stored as UTF-8."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name, content in files.items():
            z.writestr(name, content.encode("utf-8") if isinstance(content, str) else content)
    return buf.getvalue()
