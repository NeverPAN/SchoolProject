"""阶段 D3/D4：局部网格补丁几何与提交校验。"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest
import trimesh

from app.reconstruction.local_mesh_patch import (boundary_loops, cut_to_box, gap_tolerance_for,
                                                 mesh_invariants, outer_box, patch_vertex_colors,
                                                 stitch_loops)


def _box_mesh(extents=(4, 4, 4), center=(0, 0, 0)):
    mesh = trimesh.creation.box(extents=list(extents))
    mesh.apply_translation(list(center))
    return mesh


def _grid_plane(spacing=0.25, extent=6.0):
    xs = np.arange(-extent / 2, extent / 2 + 1e-9, spacing)
    verts = np.asarray([(x, y, 0.0) for y in xs for x in xs])
    faces, n = [], len(xs)
    for j in range(n - 1):
        for i in range(n - 1):
            a = j * n + i
            faces += [[a, a + 1, a + n], [a + 1, a + n + 1, a + n]]
    return trimesh.Trimesh(vertices=verts, faces=np.asarray(faces, dtype=np.int64), process=False)


def test_cut_to_box_keeps_faces_fully_inside():
    plane = _grid_plane()
    cut = cut_to_box(plane, np.array([-1.0, -1, -1]), np.array([1.0, 1, 1]))
    assert cut is not None and len(cut.faces) > 0
    assert (np.abs(np.asarray(cut.vertices)) <= 1.0 + 1e-9).all()
    assert boundary_loops(cut), "顶点级切割应产生边界环"


def test_cut_to_box_returns_none_for_empty_region():
    plane = _grid_plane()
    assert cut_to_box(plane, np.array([10.0, 10, 10]), np.array([11.0, 11, 11])) is None


def test_boundary_loops_empty_for_closed_mesh():
    assert boundary_loops(_box_mesh()) == []


def test_boundary_loops_weld_glb_style_duplicated_vertices():
    """GLB 顶点按面复制时，几何共享边不能被误判为边界（真实父网格事故）。"""
    plane = _grid_plane()
    duplicated = plane.submesh([np.arange(len(plane.faces))], append=True)  # 每面独立顶点
    loop_candidates = boundary_loops(cut_to_box(duplicated, np.array([-1.0, -1, -1]), np.array([1.0, 1, 1])))
    assert loop_candidates, "切割后应有边界环"
    longest = max(loop_candidates, key=len)
    assert len(longest) >= 24, f"焊接后主边界环应覆盖完整周界（实得最长环 {len(longest)} 点）"


def test_stitch_loops_matches_resampled_boundaries():
    parent_loop = np.asarray([[np.cos(t), np.sin(t), 0.0] for t in np.linspace(0, 2 * np.pi, 64, endpoint=False)])
    patch_loop = parent_loop * 0.98 + np.array([0.0, 0.0, 0.02])
    faces, seam_vertices, report = stitch_loops(parent_loop, patch_loop, gap_tolerance=0.2)
    assert report["max_gap"] <= 0.2
    assert len(faces) == 2 * report["seam_samples"]
    assert len(seam_vertices) == report["seam_samples"]


def test_stitch_loops_rejects_large_gap():
    parent_loop = np.asarray([[np.cos(t), np.sin(t), 0.0] for t in np.linspace(0, 2 * np.pi, 32, endpoint=False)])
    patch_loop = parent_loop + np.array([5.0, 0.0, 0.0])
    with pytest.raises(ValueError, match="缝隙超限"):
        stitch_loops(parent_loop, patch_loop, gap_tolerance=0.1)


def test_mesh_invariants_pass_when_outside_faces_kept():
    mesh = _box_mesh((6, 6, 6))
    centers = mesh.triangles_center
    inside = (np.abs(centers) <= 1.0).all(axis=1)
    kept = mesh.submesh([np.where(~inside)[0]], append=True)
    kept.remove_unreferenced_vertices()
    report = mesh_invariants(mesh, kept, np.array([-1.0, -1, -1]), np.array([1.0, 1, 1]))
    assert report["pass"] is True


def test_mesh_invariants_fail_when_outside_faces_missing():
    mesh = _box_mesh((6, 6, 6))
    report = mesh_invariants(mesh, _box_mesh((2, 2, 2)), np.array([-1.0, -1, -1]), np.array([1.0, 1, 1]))
    assert report["pass"] is False and report["outside_faces_missing"] > 0


def test_patch_vertex_colors_from_fused_points():
    points = np.zeros(64, dtype=[(n, "<f4") for n in ("x", "y", "z", "nx", "ny", "nz")]
                      + [(n, "u1") for n in ("red", "green", "blue")])
    grid = np.stack(np.meshgrid(np.linspace(0, 1, 4), np.linspace(0, 1, 4), np.linspace(0, 1, 4), indexing="ij"),
                    axis=-1).reshape(-1, 3)
    points["x"], points["y"], points["z"] = grid[:, 0], grid[:, 1], grid[:, 2]
    points["red"], points["green"], points["blue"] = 200, 40, 30
    colors, report = patch_vertex_colors(grid, points, max_distance=0.1)
    assert (colors[:, 0] == 200).all() and report["vertices_colored"] == len(grid)


def test_patch_vertex_colors_falls_back_to_grey_when_far():
    points = np.zeros(1, dtype=[(n, "<f4") for n in ("x", "y", "z", "nx", "ny", "nz")]
                      + [(n, "u1") for n in ("red", "green", "blue")])
    points["x"], points["red"] = 50.0, 10
    colors, report = patch_vertex_colors(np.array([[0.0, 0.0, 0.0]]), points, max_distance=0.1)
    assert colors[0].tolist() == [128, 128, 128] and report["vertices_colored"] == 0


def test_gap_tolerance_scales_with_mesh_edge_length():
    fine = trimesh.creation.icosphere(subdivisions=4, radius=1.0)
    coarse = trimesh.creation.icosphere(subdivisions=0, radius=1.0)
    assert gap_tolerance_for(coarse) > gap_tolerance_for(fine)


def test_outer_box_expands_by_band():
    lo, hi = outer_box(np.array([1.0, 2.0, 3.0]), np.array([4.0, 5.0, 6.0]), 0.5)
    assert np.allclose(lo, [0.5, 1.5, 2.5]) and np.allclose(hi, [4.5, 5.5, 6.5])


def test_gap_tolerance_uses_max_of_both_meshes():
    fine = trimesh.creation.icosphere(subdivisions=4, radius=1.0)
    coarse = trimesh.creation.icosphere(subdivisions=0, radius=1.0)
    assert gap_tolerance_for(fine, coarse) == pytest.approx(gap_tolerance_for(coarse))
    assert gap_tolerance_for(fine, coarse) > gap_tolerance_for(fine)


def test_mesh_invariants_allow_remeshed_band_interior():
    """v2 语义：外盒内的父面可被过渡带重网格化替换，只要外盒外逐字保留。"""
    mesh = _box_mesh((6, 6, 6))
    centers = mesh.triangles_center
    inside = (np.abs(centers) <= 1.5).all(axis=1)
    kept = mesh.submesh([np.where(~inside)[0]], append=True)
    kept.remove_unreferenced_vertices()
    # stitched 只含外盒外面 + 一个不相干的补丁面（过渡带替换物）
    patch_face = np.asarray([[0.0, 0.0, 0.0], [0.1, 0.0, 0.0], [0.0, 0.1, 0.0]])
    stitched = trimesh.Trimesh(
        vertices=np.vstack([kept.vertices, patch_face]),
        faces=np.vstack([kept.faces, [[len(kept.vertices), len(kept.vertices) + 1, len(kept.vertices) + 2]]]),
        process=False)
    report = mesh_invariants(mesh, stitched, np.array([-1.5, -1.5, -1.5]), np.array([1.5, 1.5, 1.5]))
    assert report["pass"] is True
    assert report["outside_faces_missing"] == 0


class TestLocalMeshSubmission:
    def _make_candidate(self, client, auth_headers, project, upload_images, monkeypatch):
        """伪造一个 local_general 点云候选（发布记录 + 清单）供补丁任务使用。"""
        from app.core.config import get_settings
        from app.db.session import SessionLocal
        from app.models.entities import Model, ModelRegion, ModelVersion, ReconstructionJob
        from tests.test_jobs import wait_job

        upload_images(4)
        job = client.post(f"/api/v1/projects/{project['id']}/jobs", json={"preset": "fast"}, headers=auth_headers).json()
        wait_job(client, auth_headers, job["id"])
        import time as _time

        _time.sleep(0.2)
        monkeypatch.setattr(__import__("app.services.job_service", fromlist=["enqueue_job"]),
                            "enqueue_job", lambda job_id: None)
        with SessionLocal() as db:
            parent = db.query(Model).filter_by(project_id=project["id"], model_type="web").first()
            assert parent is not None
            region = ModelRegion(model_id=parent.id, user_id=project["owner_id"],
                                 bbox_min_x=0.0, bbox_min_y=0.0, bbox_min_z=0.0,
                                 bbox_max_x=2.0, bbox_max_y=2.0, bbox_max_z=2.0, note="补丁选区")
            db.add(region)
            db.commit()
            db.refresh(region)
            parent_id, region_id = parent.id, region.id
        # 发布一份 local_general 风格的点云候选
        cloud = (Path(get_settings().data_dir) / "users" / str(project["owner_id"]) / "projects"
                 / str(project["id"]) / "outputs" / f"job-{job['id']}")
        cloud.mkdir(parents=True, exist_ok=True)
        ply = cloud / "local-fused.ply"
        ply.write_bytes(b"ply\nfake fused cloud")
        source_manifest = {
            "job_id": job["id"], "pipeline_mode": "local_general",
            "parent_model_id": parent_id, "region_id": region_id,
            "resolved_params": {"bbox_min": [0.0, 0.0, 0.0], "bbox_max": [2.0, 2.0, 2.0]},
            "outputs": [str(ply)],
        }
        (cloud / "job_manifest.json").write_text(json.dumps(source_manifest), encoding="utf-8")
        return job["id"], ply, source_manifest

    def test_local_mesh_submission_freezes_source_and_parent(self, client, auth_headers, project, upload_images, monkeypatch):
        import hashlib

        from app.core.config import get_settings
        from app.db.session import SessionLocal
        from app.models.entities import Model, ModelVersion, ReconstructionJob

        job_id, ply, source_manifest = self._make_candidate(client, auth_headers, project, upload_images, monkeypatch)
        from app.reconstruction.manifest import write_manifest

        write_manifest(Path(source_manifest["outputs"][0]).parent, "FAKE", {}, {}, [ply])
        # 用真实发布通道登记点云候选（绕过 worker，仅登记 DB 与文件）
        records = [{"model_type": "dense_pointcloud", "format": "ply", "file_path": str(ply),
                    "file_size": ply.stat().st_size, "stage_origin": "LOCAL_POINT_FUSION",
                    "vertex_count": 10, "face_count": 0, "point_count": 10}]
        with SessionLocal() as db:
            candidate = Model(project_id=project["id"], job_id=job_id, model_type="dense_pointcloud",
                              format="ply", file_path=str(ply), file_size=ply.stat().st_size,
                              vertex_count=10, face_count=0, point_count=10,
                              stage_origin="LOCAL_POINT_FUSION")
            db.add(candidate)
            db.commit()
            db.refresh(candidate)
            candidate_id = candidate.id
            db.add(ModelVersion(project_id=project["id"], model_id=candidate_id, version_number=1,
                                parent_version_id=None, change_type="initial", change_description="测试点云候选登记"))
            db.commit()
        r = client.post(f"/api/v1/models/{candidate_id}/local-mesh", json={}, headers=auth_headers)
        assert r.status_code == 202, r.text
        out = r.json()
        assert out["pipeline_mode"] == "local_mesh"
        with SessionLocal() as db:
            config = json.loads(db.get(ReconstructionJob, out["id"]).config_json)
        assert config["source_model_id"] == candidate_id
        assert config["source_cloud"]["sha256"] == hashlib.sha256(ply.read_bytes()).hexdigest()
        assert config["parent_model"]["sha256"]
        assert config["bounds"]["min"] == [0.0, 0.0, 0.0]

    def test_local_mesh_rejects_non_fusion_source(self, client, auth_headers, project, upload_images, monkeypatch):
        from app.db.session import SessionLocal
        from app.models.entities import Model

        upload_images(4)
        job = client.post(f"/api/v1/projects/{project['id']}/jobs", json={"preset": "fast"}, headers=auth_headers).json()
        from tests.test_jobs import wait_job

        wait_job(client, auth_headers, job["id"])
        import time as _time

        _time.sleep(0.2)
        monkeypatch.setattr(__import__("app.services.job_service", fromlist=["enqueue_job"]),
                            "enqueue_job", lambda job_id: None)
        with SessionLocal() as db:
            web = db.query(Model).filter_by(project_id=project["id"], model_type="web").first()
            web_id = web.id
        r = client.post(f"/api/v1/models/{web_id}/local-mesh", json={}, headers=auth_headers)
        assert r.status_code in (400, 422), r.text
        if r.status_code == 400:
            assert "局部融合点云" in r.json()["message"]
