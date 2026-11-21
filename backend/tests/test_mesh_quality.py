"""集成 A2：网格质量诊断、trim 候选裁定与顶部可观测性提示。"""
from __future__ import annotations

import struct
from pathlib import Path

import numpy as np
import pytest

from app.reconstruction.mesh_quality import (choose_mesh_candidate, read_binary_ply_xyz,
                                             read_camera_centers, support_metrics, top_view_warning)


def _write_cloud(path: Path, xyz: np.ndarray) -> None:
    n = len(xyz)
    props = "".join(f"property float {n_}\n" for n_ in ("x", "y", "z", "nx", "ny", "nz"))
    props += "".join(f"property uchar {c}\n" for c in ("red", "green", "blue"))
    header = (f"ply\nformat binary_little_endian 1.0\nelement vertex {n}\n{props}end_header\n").encode()
    body = np.zeros(n, dtype=[(k, "<f4") for k in ("x", "y", "z", "nx", "ny", "nz")]
                    + [(k, "u1") for k in ("red", "green", "blue")])
    body["x"], body["y"], body["z"] = xyz[:, 0], xyz[:, 1], xyz[:, 2]
    path.write_bytes(header + body.tobytes())


@pytest.fixture()
def cloud(tmp_path):
    """规则点阵：z ∈ [0, 10]，支撑半径 = 3× 采样间距（0.5 间距 → 1.5）。"""
    grid = np.stack(np.meshgrid(np.linspace(0, 10, 21), np.linspace(0, 10, 21), np.linspace(0, 10, 41), indexing="ij"),
                    axis=-1).reshape(-1, 3).astype(float)
    path = tmp_path / "fused.ply"
    _write_cloud(path, grid)
    return path, grid


def test_support_metrics_detects_mesh_on_cloud(cloud, tmp_path):
    import trimesh

    path, grid = cloud
    box = trimesh.creation.box(extents=[8, 8, 8])
    box.apply_translation([5, 5, 5])
    mesh_path = tmp_path / "mesh.ply"
    box.export(mesh_path)
    metrics = support_metrics(mesh_path, grid, up=[0, 0, 1])
    assert metrics["supported_face_fraction"] > 0.9
    assert metrics["height_axis_source"] == "display_up"
    assert set(metrics["bands"]) == {"bottom", "middle", "top"}


def test_support_metrics_marks_offset_mesh_unsupported(cloud, tmp_path):
    import trimesh

    path, grid = cloud
    box = trimesh.creation.box(extents=[4, 4, 4])
    box.apply_translation([40, 40, 40])  # 远离点云
    mesh_path = tmp_path / "mesh.ply"
    box.export(mesh_path)
    metrics = support_metrics(mesh_path, grid)
    assert metrics["supported_face_fraction"] < 0.05
    assert metrics["height_axis_source"] == "longest_axis_fallback"


def test_support_metrics_samples_huge_meshes(cloud, tmp_path):
    import trimesh

    path, grid = cloud
    sphere = trimesh.creation.icosphere(subdivisions=6)  # >20k faces
    sphere.apply_translation([5, 5, 5])
    mesh_path = tmp_path / "mesh.ply"
    sphere.export(mesh_path)
    metrics = support_metrics(mesh_path, grid, max_face_samples=5000)
    assert metrics["face_samples"] == 5000
    assert metrics["mesh_faces_total"] > 20000


def test_choose_mesh_candidate_prefers_support_without_extrapolation():
    conservative = {"supported_face_fraction": 0.80, "unsupported_face_fraction": 0.20}
    aggressive = {"supported_face_fraction": 0.85, "unsupported_face_fraction": 0.21}
    winner, _ = choose_mesh_candidate(conservative, aggressive)
    assert winner == "aggressive"


def test_choose_mesh_candidate_rejects_extrapolation_growth():
    conservative = {"supported_face_fraction": 0.80, "unsupported_face_fraction": 0.20}
    aggressive = {"supported_face_fraction": 0.86, "unsupported_face_fraction": 0.30}  # +10pp
    winner, reason = choose_mesh_candidate(conservative, aggressive)
    assert winner == "conservative" and "外插增长合格=False" in reason


def _camera_images_bin(path: Path, entries: list[tuple[tuple[float, float, float, float],
                                                       tuple[float, float, float], str]]):
    """手工构造 COLMAP images.bin（qvec wxyz + tvec + 名称 + 0 个观测）。"""
    with path.open("wb") as f:
        f.write(struct.pack("<Q", len(entries)))
        for i, (q, t, name) in enumerate(entries, start=1):
            f.write(struct.pack("<i7di", i, *q, *t, 1))
            f.write(name.encode() + b"\0")
            f.write(struct.pack("<Q", 0))


def test_read_camera_centers_recovers_translations(tmp_path):
    # 单位四元数 (1,0,0,0) → R=I → center = -t
    _camera_images_bin(tmp_path / "images.bin", [
        ((1, 0, 0, 0), (1, 2, 3), "a.jpg"),
        ((1, 0, 0, 0), (-4, 0, 5), "b.jpg"),
    ])
    centers = read_camera_centers(tmp_path / "images.bin")
    assert np.allclose(centers, [[-1, -2, -3], [4, 0, -5]], atol=1e-9)


def test_top_view_warning_flags_low_cameras(cloud):
    _, grid = cloud  # z ∈ [0, 10]，上部 20% 高度带从 z=8 开始
    centers = np.array([[0, 0, 5.0], [1, 1, 6.0]])  # 全部低于 z=8
    warning = top_view_warning(centers, grid, up=[0, 0, 1])
    assert warning is not None and "顶部视角不足" in warning
    assert top_view_warning(np.array([[0, 0, 9.0]]), grid, up=[0, 0, 1]) is None


class TestAdapterTrim:
    def test_poisson_meshing_passes_trim_option(self, tmp_path):
        from app.reconstruction.adapter import ColmapAdapter

        captured = {}

        def fake_run(self, args, log, cancel, timeout_s=None):
            captured["args"] = args
            return 0

        adapter = ColmapAdapter("colmap", use_gpu=False, version="COLMAP 3.13")
        adapter.run_command = fake_run.__get__(adapter)
        adapter.poisson_meshing(tmp_path / "in.ply", tmp_path / "out.ply", tmp_path / "log", trim=9)
        args = captured["args"]
        assert "--PoissonMeshing.trim" in args
        assert args[args.index("--PoissonMeshing.trim") + 1] == "9"
        assert args[args.index("--PoissonMeshing.depth") + 1] == "10"

    def test_profile_resolved_includes_trim(self):
        from app.reconstruction.profiles import resolve_job_config

        resolved = resolve_job_config("standard", "auto", True, True, "mock", [])["resolved"]
        assert resolved["poisson_trim"] == 10
