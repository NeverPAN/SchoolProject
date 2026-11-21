import json
import struct

import numpy as np
import trimesh

from app.reconstruction.export import mesh_to_glb
from app.reconstruction.orientation import estimate_display_up


def test_web_mesh_preserves_geometry(tmp_path):
    mesh = trimesh.Trimesh(vertices=[[0, 0, 0], [1, 0, 0], [0, 1, 0], [0, 0, 1]],
                           faces=[[0, 1, 2], [1, 0, 3]], process=False)
    mesh.visual.vertex_colors = [[128, 64, 255, 255]] * 4
    ply = tmp_path / 'mesh.ply'
    glb = tmp_path / 'web.glb'
    mesh.export(ply)
    assert mesh_to_glb(ply, glb)['face_count'] == 2
    data = glb.read_bytes()
    length, = struct.unpack_from('<I', data, 12)
    tree = json.loads(data[20:20 + length])
    prim = tree['meshes'][0]['primitives'][0]

    def read(index, dtype, width):
        accessor = tree['accessors'][index]
        view = tree['bufferViews'][accessor['bufferView']]
        offset = 28 + length + view.get('byteOffset', 0) + accessor.get('byteOffset', 0)
        return np.frombuffer(data, dtype, accessor['count'] * width, offset).reshape(-1, width)

    attrs = prim['attributes']
    np.testing.assert_array_equal(read(attrs['POSITION'], '<f4', 3), mesh.vertices)
    np.testing.assert_array_equal(read(prim['indices'], '<u4', 1).reshape(-1, 3), mesh.faces)
    np.testing.assert_allclose(np.linalg.norm(read(attrs['NORMAL'], '<f4', 3), axis=1), 1)
    np.testing.assert_allclose(read(attrs['COLOR_0'], '<f4', 4)[0], [0.215861, 0.0512695, 1, 1], atol=1e-6)
    assert tree['materials'][prim['material']]['doubleSided'] is True


def test_display_up_requires_consensus(tmp_path):
    images = tmp_path / 'images.bin'

    def write(quaternions):
        images.write_bytes(struct.pack('<Q', len(quaternions)) + b''.join(
            struct.pack('<i7di', i, *q, 0, 0, 0, 1) + b'view.jpg\0' + struct.pack('<Q', 0)
            for i, q in enumerate(quaternions)
        ))

    identity = (1, 0, 0, 0)
    write([identity] * 4)
    assert estimate_display_up(images) == [0, -1, 0]
    write([identity] * 2 + [(0, 0, 0, 1)] * 2)
    assert estimate_display_up(images) is None
    images.write_bytes(b'broken')
    assert estimate_display_up(images) is None
