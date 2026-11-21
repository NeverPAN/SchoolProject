"""GLB 纹理资产校验（阶段 B）：原色贴图可用性以产物内容为准。

只读取 GLB 的 JSON chunk（体积小），不解析整个 BIN chunk：
- 材质是否带 baseColorTexture、图元是否带 TEXCOORD_0；
- 纹理图像必须内嵌（bufferView），存在外部 URI 时如实标记；
- 校验失败/文件缺失返回 None，不阻塞模型详情。
"""
from __future__ import annotations

import json
import struct
from pathlib import Path


def inspect_glb(path: Path, *, max_file_size: int = 512 * 1024 * 1024) -> dict | None:
    """解析 GLB 头与 JSON chunk，返回纹理资产摘要；不适合或损坏时返回 None。"""
    try:
        size = path.stat().st_size
        if size > max_file_size:
            return None
        with path.open("rb") as f:
            magic, version, length = struct.unpack("<III", f.read(12))
            if magic != 0x46546C67 or version < 2 or length != size:
                return None
            chunk_length, chunk_type = struct.unpack("<II", f.read(8))
            if chunk_type != 0x4E4F534A:  # 'JSON'
                return None
            doc = json.loads(f.read(chunk_length).decode("utf-8"))
    except (OSError, ValueError, struct.error):
        return None

    meshes = doc.get("meshes") or []
    primitives = [prim for mesh in meshes for prim in mesh.get("primitives", [])]
    materials = doc.get("materials") or []
    textures = doc.get("textures") or []
    images = doc.get("images") or []
    external = [img.get("uri") for img in images if img.get("uri")]
    return {
        "primitives_total": len(primitives),
        "primitives_with_uv": sum(1 for p in primitives if "TEXCOORD_0" in (p.get("attributes") or {})),
        "materials_total": len(materials),
        "materials_with_base_color_texture": sum(
            1 for m in materials if ((m.get("pbrMetallicRoughness") or {}).get("baseColorTexture") is not None
                                     or m.get("unlit") is not None or m.get("emissiveTexture") is not None)
        ),
        "texture_count": len(textures),
        "embedded_textures": bool(textures) and not external,
        "external_texture_uris": external,
        "double_sided_materials": sum(1 for m in materials if m.get("doubleSided")),
    }
