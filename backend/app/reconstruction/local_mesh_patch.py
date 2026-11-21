"""阶段 D3/D4：局部网格补丁与安全拼接（失败即拒绝发布）。

方法与诚实边界：
- 父侧保留 = 核心盒外三角面（按面中心选取，与区域提取同一套已验证逻辑），
  原 UV/材质/纹理字节随 trimesh visual 原样保留；
- 局部融合点云（D2 候选）在核心盒内 Poisson 网格化，再按顶点级 AABB 切割
  出补丁并保留边界环（面片级近似，非解析平面求交）；
- 父洞边界环与补丁边界环按弧长重采样对齐后缝合；缝隙超限、非流形边、
  区域外几何变化任一不过 → 只写诊断报告，不发布 GLB；
- 补丁面片顶点色来自融合点云多视角颜色（非完整纹理图集）；父侧贴图原样保留，
  结果里如实标注这是顶点色补丁而非图集补丁。
"""
from __future__ import annotations

import json
import shutil
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path

import numpy as np
from scipy.spatial import cKDTree

MODE = "local_mesh"
STAGE_ORIGIN = "LOCAL_MESH_PATCH"
POISSON_DEPTH = 8
# trim=10 会把开放小补丁裁成空（真实 Bridge 数据实测）；trim=7 覆盖整个核心盒。
POISSON_TRIM = 7
MIN_PATCH_POINTS = 300
SEAM_GAP_TOLERANCE_FACTOR = 3.0
COORD_DECIMALS = 6


@contextmanager
def scratch_dir(prefix: str, parent: Path):
    """NFS 安全临时目录：忽略清理失败，不让清理错误掩盖真实异常。"""
    path = Path(tempfile.mkdtemp(prefix=prefix, dir=parent))
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


def cut_to_box(mesh, lo: np.ndarray, hi: np.ndarray):
    """顶点级 AABB 切割：保留三个顶点都落在盒内的三角面。

    这是面片级近似（不是解析平面求交），用于无纹理要求的补丁网格；
    边界是三角形锯齿而非精确平面，缝合容差按网格边长给出。
    """
    inside = ((np.asarray(mesh.vertices) >= lo) & (np.asarray(mesh.vertices) <= hi)).all(axis=1)
    keep = inside[np.asarray(mesh.faces)].all(axis=1)
    if not keep.any():
        return None
    import trimesh

    result = mesh.submesh([np.where(keep)[0]], append=True)
    result.remove_unreferenced_vertices()
    return result


def boundary_loops(mesh) -> list[np.ndarray]:
    """返回开放边界环（有序顶点坐标），无边界时为空。"""
    edges = np.sort(mesh.edges_sorted, axis=1)
    unique, counts = np.unique(edges, axis=0, return_counts=True)
    boundary = unique[counts == 1]
    if not len(boundary):
        return []
    neighbors: dict[int, list[int]] = {}
    for a, b in boundary:
        neighbors.setdefault(int(a), []).append(int(b))
        neighbors.setdefault(int(b), []).append(int(a))
    tips = [v for v, nbrs in neighbors.items() if len(nbrs) != 2]
    loops: list[np.ndarray] = []
    visited: set[int] = set()
    for start in (tips or [min(neighbors)]):
        if start in visited:
            continue
        loop, previous, current = [start], None, start
        visited.add(start)
        while True:
            candidates = [n for n in neighbors.get(current, []) if n != previous]
            nxt = next((n for n in candidates if n not in visited), None)
            if nxt is None:
                break
            loop.append(nxt)
            visited.add(nxt)
            previous, current = current, nxt
            if nxt == start:
                break
        if len(loop) >= 3:
            loops.append(mesh.vertices[loop])
    return loops


def _resample_loop(points: np.ndarray, count: int) -> np.ndarray:
    segments = np.roll(points, -1, axis=0) - points
    lengths = np.linalg.norm(segments, axis=1)
    total = float(lengths.sum())
    if total <= 0:
        return np.repeat(points[:1], count, axis=0)
    targets = np.linspace(0.0, total, count, endpoint=False)
    cumulative = np.concatenate([[0.0], np.cumsum(lengths)])
    result = []
    for t in targets:
        index = min(int(np.searchsorted(cumulative, t, side="right") - 1), len(points) - 1)
        ratio = (t - cumulative[index]) / max(lengths[index], 1e-12)
        result.append(points[index] + ratio * segments[index])
    return np.asarray(result)


def stitch_loops(parent_loop: np.ndarray, patch_loop: np.ndarray,
                 gap_tolerance: float) -> tuple[np.ndarray, np.ndarray, dict]:
    """弧长重采样对齐两个边界环并生成封闭缝合三角带；缝隙超限则拒绝。"""
    count = max(len(parent_loop), len(patch_loop), 8)
    a = _resample_loop(parent_loop, count)
    raw = _resample_loop(patch_loop, count)
    # 两条环的遍历起点彼此独立：对补丁采样做相位搜索，取平均缝隙最小的起点与方向。
    best = None
    for flipped in (False, True):
        base = raw[::-1] if flipped else raw
        for shift in range(count):
            b = np.roll(base, shift, axis=0)
            gaps = np.linalg.norm(a - b, axis=1)
            score = float(gaps.mean())
            if best is None or score < best[0]:
                best = (score, b, gaps, flipped, shift)
    _, b, gaps, flipped, shift = best
    if float(gaps.max()) > gap_tolerance:
        raise ValueError(f"边界环缝隙超限：最大 {float(gaps.max()):.6f} > 容差 {gap_tolerance:.6f}")
    offset = count
    faces = []
    for i in range(count):
        j = (i + 1) % count
        faces.append([i, j, offset + j])
        faces.append([i, offset + j, offset + i])
    return (np.asarray(faces, dtype=np.int64), b,
            {"seam_samples": count, "mean_gap": float(gaps.mean()),
             "max_gap": float(gaps.max()), "gap_tolerance": gap_tolerance,
             "phase_aligned": True, "direction_flipped": bool(flipped), "start_shift": int(shift)})


def _principal_loop(loops: list[np.ndarray], center: np.ndarray,
                    box: tuple[np.ndarray, np.ndarray] | None = None) -> np.ndarray:
    """选择与切割区域对应的边界环。

    有参考盒时，先按“环包围盒与选区盒重叠”过滤，防止选中穿过全局的巨型
    天然洞环（其质心也可能靠近盒中心）；无匹配时回退到质心最近。
    """
    if not loops:
        raise ValueError("切割后没有开放边界环，无法缝合")
    if box is not None:
        lo, hi = box
        candidates = []
        for pts in loops:
            bmin, bmax = pts.min(axis=0), pts.max(axis=0)
            overlap_center = np.clip((bmin + bmax) / 2, lo, hi)
            if np.all(np.abs(overlap_center - (bmin + bmax) / 2) < 1e-9):
                candidates.append(pts)
        if candidates:
            loops = candidates
    return min(loops, key=lambda pts: float(np.linalg.norm(pts.mean(axis=0) - center)))


def _face_keys(faces: np.ndarray, vertices: np.ndarray) -> set[tuple]:
    coords = np.round(vertices, COORD_DECIMALS)
    return {tuple(sorted(tuple(coords[i]) for i in face)) for face in faces}


def mesh_invariants(parent, stitched, core_lo: np.ndarray, core_hi: np.ndarray) -> dict:
    """发布前不变量：核心盒外父三角面（按几何坐标）逐字保留、无非流形边。"""
    centers = parent.triangles_center
    outside = ~((centers >= core_lo).all(axis=1) & (centers <= core_hi).all(axis=1))
    reference = _face_keys(parent.faces[outside], parent.vertices)
    present = _face_keys(stitched.faces, stitched.vertices)
    edges = np.sort(stitched.edges_sorted, axis=1)
    _, counts = np.unique(edges, axis=0, return_counts=True)
    report = {
        "identity": "outside faces matched by rounded vertex coordinates",
        "outside_faces_expected": int(outside.sum()),
        "outside_faces_missing": int(len(reference - present)),
        "nonmanifold_edges": int((counts > 2).sum()),
        "boundary_edges": int((counts == 1).sum()),
    }
    report["pass"] = report["outside_faces_missing"] == 0 and report["nonmanifold_edges"] == 0
    return report


def patch_vertex_colors(patch_vertices: np.ndarray, patch_points, max_distance: float) -> tuple[np.ndarray, dict]:
    """D4：补丁顶点色取自融合点云的多视角融合颜色（最近点，距离门限）。"""
    xyz = np.column_stack([patch_points[n] for n in ("x", "y", "z")]).astype(float)
    rgb = np.column_stack([patch_points[n] for n in ("red", "green", "blue")])
    tree = cKDTree(xyz)
    distances, index = tree.query(patch_vertices)
    colors = np.full((len(patch_vertices), 3), 128, dtype=np.uint8)
    covered = distances <= max_distance
    colors[covered] = rgb[index[covered]].astype(np.uint8)
    return colors, {"vertices_total": int(len(patch_vertices)),
                    "vertices_colored": int(covered.sum()),
                    "color_source": "nearest fused point within tolerance; multi-view fused color, not a texture atlas"}


def gap_tolerance_for(mesh) -> float:
    """缝合缝隙容差：3× 补丁网格中位边长（下限 0.001 模型单位）。

    两侧边界都是三角形锯齿，缝隙量级由网格边长决定，不由点间距决定。
    """
    edges = np.asarray(mesh.edges)
    if not len(edges):
        return 0.001
    lengths = np.linalg.norm(mesh.vertices[edges[:, 0]] - mesh.vertices[edges[:, 1]], axis=1)
    return max(SEAM_GAP_TOLERANCE_FACTOR * float(np.median(lengths)), 0.001)


def _display_rotation(up):
    """与 photo_export.photo_mesh_to_glb 一致的正立旋转（front 投影规则相同）。"""
    from scipy.spatial.transform import Rotation

    direction = np.asarray(up, dtype=float)
    length = np.linalg.norm(direction)
    direction = direction / length
    front = np.array([0., 0., 1.]) - direction * direction[2]
    if np.linalg.norm(front) < 1e-6:
        front = np.array([1., 0., 0.]) - direction * direction[0]
    front /= np.linalg.norm(front)
    return Rotation.from_matrix(np.stack([np.cross(direction, front), direction, front]))


def recover_native_frame(baked_mesh, native_mesh, up, center):
    """把烘焙正立 GLB 的父网格恢复到原生坐标，并与父原生网格三角形核对。

    核对失败说明父网格/原生网格/显示元数据不一致，必须拒绝（诚实框架检查）。
    """
    import trimesh

    rotation = _display_rotation(up)
    native_vertices = rotation.inv().apply(np.asarray(baked_mesh.vertices, dtype=float)) + np.asarray(center)
    recovered = trimesh.Trimesh(vertices=native_vertices, faces=baked_mesh.faces, process=False)

    def face_keys(faces, vertices):
        coords = np.round(np.asarray(vertices), 5)
        return {tuple(sorted(tuple(coords[i]) for i in face)) for face in np.asarray(faces)}

    native_keys = face_keys(native_mesh.faces, native_mesh.vertices)
    recovered_keys = face_keys(recovered.faces, recovered.vertices)
    overlap = len(native_keys & recovered_keys) / max(len(native_keys), 1)
    if overlap < 0.999:
        raise ValueError(f"父网格原生坐标恢复失败：三角形重合率 {overlap:.4f} < 0.999；"
                         "父 GLB、原生网格与 display_up 不一致")
    return recovered


def run(ctx, config: dict) -> tuple[list[dict], dict]:
    """局部网格补丁：切割 → Poisson 补丁 → 缝合 → 不变量校验 → 独立 GLB。"""
    import trimesh
    from trimesh.exchange.gltf import export_glb

    from app.core.config import get_settings
    from app.reconstruction.adapter import ColmapAdapter, ColmapStageError
    from app.reconstruction.local_reconstruction import (_box, _checked_bytes, _local_points,
                                                         _read_ply, _stage, _verify_lineage,
                                                         _write_ply, _xyz)
    from app.reconstruction.pipeline import PipelineError

    with scratch_dir("local-mesh-", ctx.work_dir) as directory:
        root = Path(directory)
        baked_display = None
        with _stage(ctx, "IMAGE_VALIDATION") as cancel:
            lo, hi = _box(config["bounds"]["min"], config["bounds"]["max"])
            _verify_lineage(ctx, config["parent_model_id"], config["region_id"],
                            Path(config["parent_model"]["path"]).resolve(strict=True), lo, hi)
            parent_sha = _checked_bytes(Path(config["parent_model"]["path"]), cancel)
            cloud_sha = _checked_bytes(Path(config["source_cloud"]["path"]), cancel,
                                       expected=config["source_cloud"]["sha256"])
            parent = trimesh.load(config["parent_model"]["path"], force="mesh", process=False)
            cloud = _read_ply(Path(config["source_cloud"]["path"]))
            baked_display = config.get("parent_display")
            if baked_display:
                native_spec = config["parent_native_mesh"]
                native_sha = _checked_bytes(Path(native_spec["path"]), cancel, expected=native_spec["sha256"])
                native_mesh = trimesh.load(native_spec["path"], force="mesh", process=False)
                parent = recover_native_frame(parent, native_mesh, baked_display["up"], baked_display["center"])
                ctx.engine_identity["frame"] = "native（由烘焙 GLB 恢复并经三角形核对）"
            cancel()

        with _stage(ctx, "LOCAL_MESH_CUT"):
            centers = parent.triangles_center
            inside_core = ((centers >= lo).all(axis=1) & (centers <= hi).all(axis=1))
            kept_faces = np.where(~inside_core)[0]
            if not len(kept_faces):
                raise ValueError("核心盒覆盖了整个父网格，无法保留区域外几何")
            kept = parent.submesh([kept_faces], append=True)
            kept.remove_unreferenced_vertices()
            center = (lo + hi) / 2
            parent_loop = _principal_loop(boundary_loops(kept), center, box=(lo, hi))
            ctx.job_log(f"[LOCAL_MESH_CUT] 父网格保留 {len(kept.faces)} 面；洞边界 {len(parent_loop)} 点")

        adapter = ColmapAdapter(get_settings().colmap_bin, use_gpu=False, gpu_index=ctx.gpu_index)
        with _stage(ctx, "LOCAL_MESHING") as cancel:
            patch_points = _local_points(cloud, lo, hi, cancel)
            if len(patch_points) < MIN_PATCH_POINTS:
                raise ValueError(f"核心盒内融合点不足（{len(patch_points)} < {MIN_PATCH_POINTS}），无法生成补丁")
            patch_ply = root / "patch-input.ply"
            _write_ply(patch_ply, patch_points[:0], patch_points, cancel)
            patch_raw = root / "patch-mesh.ply"
            resolved = config["resolved"]
            try:
                adapter.poisson_meshing(patch_ply, patch_raw, ctx.log_file("LOCAL_MESHING"),
                                        lambda: ctx.check_cancel("LOCAL_MESHING"),
                                        quality=resolved["poisson_depth"], num_threads=4,
                                        trim=resolved["poisson_trim"])
            except ColmapStageError as exc:
                raise PipelineError("LOCAL_MESHING", exc.code, str(exc)) from exc
            patch_mesh = trimesh.load(patch_raw, force="mesh", process=False)
            patch = cut_to_box(patch_mesh, lo, hi)
            if patch is None or len(patch.faces) == 0:
                raise ValueError("补丁网格在核心盒内没有面片")
            patch_loop = _principal_loop(boundary_loops(patch), center)
            ctx.job_log(f"[LOCAL_MESHING] 补丁 {len(patch.faces)} 面；边界 {len(patch_loop)} 点")

        with _stage(ctx, "LOCAL_STITCH"):
            tolerance = gap_tolerance_for(patch)
            faces, seam_vertices, seam_report = stitch_loops(parent_loop, patch_loop, tolerance)
            ctx.job_log(f"[LOCAL_STITCH] 缝合 {seam_report['seam_samples']} 点；平均缝隙 "
                        f"{seam_report['mean_gap']:.6f}（容差 {tolerance:.6f}）")

        with _stage(ctx, "VALIDATION"):
            combined_vertices = np.vstack([kept.vertices, patch.vertices, seam_vertices])
            combined_faces = np.vstack([
                kept.faces,
                patch.faces + len(kept.vertices),
                faces + len(kept.vertices) + len(patch.vertices),
            ])
            stitched = trimesh.Trimesh(vertices=combined_vertices, faces=combined_faces, process=False)
            invariants = mesh_invariants(parent, stitched, lo, hi)
            if not invariants["pass"]:
                ctx.out_dir.mkdir(parents=True, exist_ok=True)
                (ctx.out_dir / "mesh-patch-report.json").write_text(json.dumps(
                    {"status": "rejected", "invariants": invariants, "seam": seam_report,
                     "note": "不变量未通过：不发布补丁网格；请使用局部点云候选。"},
                    ensure_ascii=False, indent=2), encoding="utf-8")
                raise PipelineError("VALIDATION", "MESH_PATCH_REJECTED",
                                    f"网格补丁不变量未通过：{invariants}；未发布任何网格")
            ctx.job_log(f"[VALIDATION] 不变量通过：{invariants}")

        with _stage(ctx, "EXPORTING"):
            patch_colored = trimesh.Trimesh(vertices=patch.vertices, faces=patch.faces, process=False)
            spacing = float(np.median(cKDTree(_xyz(patch_points)).query(_xyz(patch_points), k=2)[0][:, 1]))
            colors, color_report = patch_vertex_colors(
                np.asarray(patch.vertices, dtype=float), patch_points, max_distance=max(2.5 * spacing, 1e-6))
            patch_colored.visual = trimesh.visual.ColorVisuals(patch_colored, vertex_colors=colors)
            seam_mesh = trimesh.Trimesh(vertices=seam_vertices, faces=faces, process=False)
            seam_mesh.visual = trimesh.visual.ColorVisuals(
                seam_mesh, vertex_colors=np.full((len(seam_vertices), 4), 190, dtype=np.uint8))
            if baked_display:
                # 全部回到父 GLB 的正立显示坐标：刚性变换不改变任何几何关系
                rotation = _display_rotation(baked_display["up"])
                center_shift = np.asarray(baked_display["center"], dtype=float)
                kept_display = kept.copy()
                kept_display.vertices = rotation.apply(np.asarray(kept.vertices) - center_shift)
                patch_colored.vertices = rotation.apply(np.asarray(patch.vertices) - center_shift)
                seam_mesh.vertices = rotation.apply(seam_vertices - center_shift)
                scene = trimesh.Scene({"parent": kept_display, "patch": patch_colored, "seam": seam_mesh})
            else:
                scene = trimesh.Scene({"parent": kept, "patch": patch_colored, "seam": seam_mesh})
            out = ctx.out_dir / "local-patch.glb"
            out.write_bytes(export_glb(scene))
            size = out.stat().st_size
            ctx.job_log(f"[EXPORTING] 已导出 {out.name}（{size} 字节）")

    records = [{"model_type": "mesh", "format": "glb", "file_path": str(out), "file_size": size,
                "stage_origin": STAGE_ORIGIN, "vertex_count": int(len(stitched.vertices)),
                "face_count": int(len(stitched.faces)), "point_count": 0}]
    manifest = {
        "job_id": ctx.job_id, "pipeline_mode": MODE,
        "target_model_id": config["parent_model_id"], "parent_model_id": config["parent_model_id"],
        "region_id": config["region_id"], "source_model_id": config["source_model_id"],
        "geometry": "mesh_patch_with_fused_color_vertices",
        "note": "局部网格补丁：父侧三角面/UV/材质原样保留（原生坐标恢复经三角形核对）；"
                "补丁面片使用融合点云顶点色（非完整图集）；导出统一回到父 GLB 显示坐标。",
        "frame": ("display（父 GLB 正立坐标，由原生坐标经 display_up 旋转导出）"
                  if config.get("parent_display") else "native"),
        "parent_model_sha256": parent_sha, "source_cloud_sha256": cloud_sha,
        "resolved_params": {"bbox_min": np.asarray(lo).tolist(), "bbox_max": np.asarray(hi).tolist(),
                            "poisson_depth": resolved["poisson_depth"],
                            "poisson_trim": resolved["poisson_trim"],
                            "seam_gap_tolerance": tolerance},
        "seam": seam_report, "invariants": invariants, "colors": color_report,
        "counts": {"parent_faces_kept": int(len(kept.faces)), "patch_faces": int(len(patch.faces)),
                   "seam_faces": int(len(faces)), "total_faces": int(len(stitched.faces))},
        "outputs": [str(out)],
    }
    return records, manifest
