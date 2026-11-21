"""Package a COLMAP photo-textured PLY as a self-contained GLB."""
from pathlib import Path

import numpy as np
import trimesh
from PIL import Image
from scipy.spatial.transform import Rotation
from trimesh.exchange.gltf import export_glb
from trimesh.visual.material import PBRMaterial
from trimesh.visual.texture import TextureVisuals

PHOTO_STAGE = "PHOTO_TEXTURE_UPRIGHT"
PHOTO_NATIVE = "PHOTO_TEXTURE"
PHOTO_FILENAME = "model_photo.glb"


def photo_mesh_to_glb(source: Path, textured: Path, output: Path, up: list[float] | None) -> dict:
    if source.resolve() == output.resolve() or textured.resolve() == output.resolve():
        raise ValueError("Output must not overwrite an input mesh")
    # The COLMAP CLI declares a local PNG next to its PLY. Validate it before
    # letting the general PLY loader follow the texture reference.
    with textured.open("rb") as ply:
        comments = [line.strip() for line in ply.read(32768).split(b"end_header\n", 1)[0].splitlines()
                    if line.lower().startswith(b"comment texturefile ")]
    if comments != [b"comment TextureFile texture.png"]:
        raise ValueError("Unexpected COLMAP atlas reference")
    with Image.open(textured.with_name("texture.png")) as image:
        if image.format != "PNG" or not 0 < min(image.size) <= max(image.size) <= 8192:
            raise ValueError("Unsupported PNG atlas dimensions")
        if image.size[0] * image.size[1] > 180_000_000:
            raise ValueError("PNG atlas pixel count exceeds the safe single-image limit")
        image.verify()
    native = trimesh.load_mesh(source, process=False)
    baked = trimesh.load_mesh(textured, process=False)
    if not len(native.faces) or not np.isfinite(native.triangles).all() or not np.array_equal(native.triangles, baked.triangles):
        raise ValueError("Texturing changed triangle coordinates or winding, or mesh is empty/non-finite")
    if not isinstance(baked.visual, TextureVisuals) or baked.visual.material.image is None:
        raise ValueError("No photo atlas or UV coordinates in textured mesh")
    atlas = baked.visual.material.image
    uv = baked.visual.uv
    if uv is None or uv.shape != (len(baked.vertices), 2) or not np.all((uv >= 0) & (uv <= 1)):
        raise ValueError("Invalid UV coordinates")
    covered = np.any(uv[baked.faces] != 0, axis=(1, 2))
    if not covered.any():
        raise ValueError("No photo-covered faces")

    direction = None
    rotation = Rotation.identity()
    center = np.zeros(3)
    if up is not None:
        direction = np.array(up, dtype=float)
        length = np.linalg.norm(direction)
        if direction.shape != (3,) or not np.isfinite(length) or length < 1e-8:
            raise ValueError("Invalid up direction")
        direction /= length
        # Keep the native +Z heading projected onto the new horizontal plane.
        front = np.array([0., 0., 1.]) - direction * direction[2]
        if np.linalg.norm(front) < 1e-6:
            front = np.array([1., 0., 0.]) - direction * direction[0]
        front /= np.linalg.norm(front)
        rotation = Rotation.from_matrix(np.stack([np.cross(direction, front), direction, front]))
        center = native.bounds.mean(axis=0)
    normals = np.array(native.vertex_normals, copy=True)
    normals[np.linalg.norm(normals, axis=1) < 1e-8] = direction if direction is not None else [0, 1, 0]
    # UV seams duplicate vertices; reuse source normals there.
    original_index = np.empty(len(baked.vertices), dtype=int)
    original_index[baked.faces.ravel()] = native.faces.ravel()
    scene = trimesh.Scene()
    for name, mask in (("photo", covered), ("unobserved", ~covered)):
        if not mask.any():
            continue
        used, index = np.unique(baked.faces[mask], return_inverse=True)
        material = PBRMaterial(baseColorTexture=atlas if name == "photo" else None,
                               baseColorFactor=None if name == "photo" else [0.55, 0.55, 0.55, 1.0],
                               doubleSided=True, metallicFactor=0.0, roughnessFactor=1.0)
        geometry = trimesh.Trimesh(vertices=rotation.apply(baked.vertices[used] - center),
                                   faces=index.reshape(-1, 3), process=False,
                                   vertex_normals=rotation.apply(normals[original_index[used]]),
                                   visual=TextureVisuals(uv=uv[used] if name == "photo" else None,
                                                         material=material))
        scene.add_geometry(geometry, geom_name=name)
    output.write_bytes(export_glb(scene, include_normals=True))
    return {"vertex_count": sum(len(mesh.vertices) for mesh in scene.geometry.values()),
            "face_count": len(native.faces), "textured_faces": int(covered.sum()),
            "face_coverage": float(covered.mean()), "atlas_size": list(atlas.size),
            "orientation": "upright" if direction is not None else "native_manual",
            "color_space": "srgb",
            "rotation_xyzw": rotation.as_quat().tolist(), "rotation_center": center.tolist(),
            "source_up": direction.tolist() if direction is not None else None}
