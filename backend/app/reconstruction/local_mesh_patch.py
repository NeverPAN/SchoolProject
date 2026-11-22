"""阶段 D3/D4：局部网格补丁——重叠带拼接（失败即拒绝发布）。

方案演进（全部实测记录）：
- v1 环-弧长缝合：父洞边界与补丁边界形状不对应，拒绝发布（d-chain-results.json）；
  复盘另发现两处实现缺陷：GLB 顶点按面复制破坏索引级边界分析（已修复：
  boundary_loops 先按坐标焊接）；顶点级盒切割产生捏点（度数 4/6/8），简单环
  遍历失效（已修复：边界边集正规环分解）；
- v2 重叠带重网格化：预想"补丁与父侧都在外盒平面切割、边界同源同位"被实测
  证伪——Poisson 曲面在点云边缘因 trim 收缩，从不与切割平面形成完整同位边界
  （实测 trim 0/2/4/7 一致）；
- v2'（当前）**重叠拼接**：补丁 = 外盒内全部源点（含父点）的 Poisson 曲面
  （trim 7，与主管线同类插值），裁剪到核心盒；父侧 = 核心盒外三角面逐字保留，
  另将核心盒内"距补丁壳 ≤ d"的父面移除、由补丁取代——两曲面同源，交界带天然
  重合，无需边界环对应；补丁未覆盖的核心盒内父面原样保留，不制造假填充。
  诚实边界（不变量）：核心盒外父面逐字保留（UV/材质随 trimesh visual 保留）；
  非流形边 = 0；拼接缝（补丁边缘到存活父面的可视缝宽）实测并声明，超限拒绝；
  补丁面片顶点色来自融合点云多视角颜色（非完整纹理图集）。
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
# 产品决策（2026-11-22，负责人确认）：补丁一律低 trim 填洞——父网格天然洞区
# 由 Poisson 插值推测面覆盖，manifest 如实声明"洞区为推测插值面，非实测"；
# 高 trim（7）诚实但洞区残留缝隙，曾在 South 两选区触发量化拒绝。
# 核心盒外父面逐字保留的诚实边界不变。
PATCH_TRIM = 4
MIN_PATCH_POINTS = 300
SEAM_GAP_TOLERANCE_FACTOR = 3.0
BAND_EDGE_FACTOR = 3.0
REPLACE_FACTOR = 1.5
RIM_BUFFER_FACTOR = 2.0
GROUND_FACTOR = 3.0
COORD_DECIMALS = 6


@contextmanager
def scratch_dir(prefix: str, parent: Path):
    """NFS 安全临时目录：忽略清理失败，不让清理错误掩盖真实异常。"""
    path = Path(tempfile.mkdtemp(prefix=prefix, dir=parent))
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


def _median_edge_length(mesh) -> float:
    edges = np.asarray(mesh.edges)
    if not len(edges):
        return 0.0
    vertices = np.asarray(mesh.vertices)
    return float(np.median(np.linalg.norm(vertices[edges[:, 0]] - vertices[edges[:, 1]], axis=1)))


def outer_box(core_lo: np.ndarray, core_hi: np.ndarray, band: float):
    """外盒 = 核心盒向四周外扩 band（重网格化过渡带宽度）。"""
    return core_lo - band, core_hi + band


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
    """返回开放边界环（有序顶点坐标），无边界时为空。

    两处真实几何事实必须处理：
    - GLB 父网格顶点按面复制（UV/法线需要独立顶点），索引级共享边分析会把
      几何上共享的边判成边界——先按坐标焊接顶点再分析；焊接副本只用于拓扑，
      环坐标与原网格一致；
    - 顶点级盒切割会在表面折叠处产生"捏点"（一个顶点汇入 4/6/8 条边界边），
      简单环遍历假设失效——把边界边集分解为若干闭链（每条有向边恰用一次），
      捏点允许出现在多个环中。
    """
    work = mesh
    if len(mesh.vertices) != len(np.unique(np.round(np.asarray(mesh.vertices), 6), axis=0)):
        work = mesh.copy()
        work.merge_vertices()
    edges = np.sort(work.edges_sorted, axis=1)
    unique, counts = np.unique(edges, axis=0, return_counts=True)
    boundary = unique[counts == 1]
    if not len(boundary):
        return []
    adjacency: dict[int, list[int]] = {}
    for a, b in boundary:
        adjacency.setdefault(int(a), []).append(int(b))
        adjacency.setdefault(int(b), []).append(int(a))
    remaining = set(map(tuple, np.vstack([boundary, boundary[:, ::-1]])))
    remaining = {(int(a), int(b)) for a, b in remaining}
    loops: list[np.ndarray] = []
    while remaining:
        first = min(remaining)
        remaining.discard(first)
        start, begin = first
        loop = [start]
        prev, current = start, begin
        while True:
            loop.append(current)
            if current == start:
                break
            candidates = [n for n in adjacency.get(current, []) if (current, n) in remaining and n != prev]
            if not candidates:
                break  # 开放边界链（不应出现于闭合分解，防御性退出）
            nxt = min(candidates)
            remaining.discard((current, nxt))
            prev, current = current, nxt
        if loop[0] == loop[-1] and len(loop) >= 4:
            loops.append(work.vertices[np.asarray(loop[:-1], dtype=int)])
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
    """弧长重采样对齐两个边界环并生成封闭缝合三角带；缝隙超限则拒绝。

    v2 中两侧边界来自同一外盒平面上的两套三角化（同源同位），相位对齐后的
    缝隙由两套网格的边长尺度决定，不再依赖父洞形状。
    """
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


def mesh_invariants(parent, stitched, outer_lo: np.ndarray, outer_hi: np.ndarray) -> dict:
    """发布前不变量：核心盒外父三角面（按几何坐标）逐字保留、无非流形边。

    核心盒内的父面允许被补丁取代（重叠拼接），但补丁未覆盖处的父面保留；
    拼接缝宽单独实测并在发布门槛中裁定。
    """
    centers = parent.triangles_center
    outside = ~((centers >= outer_lo).all(axis=1) & (centers <= outer_hi).all(axis=1))
    reference = _face_keys(parent.faces[outside], parent.vertices)
    present = _face_keys(stitched.faces, stitched.vertices)
    edges = np.sort(stitched.edges_sorted, axis=1)
    _, counts = np.unique(edges, axis=0, return_counts=True)
    report = {
        "identity": "outside faces matched by rounded vertex coordinates",
        "box_semantics": "faces outside the CORE box must be verbatim; inside-core faces may be replaced by the patch only where the patch shell actually reaches",
        "outside_faces_expected": int(outside.sum()),
        "outside_faces_missing": int(len(reference - present)),
        "nonmanifold_edges": int((counts > 2).sum()),
        "boundary_edges": int((counts == 1).sum()),
    }
    report["pass"] = report["outside_faces_missing"] == 0 and report["nonmanifold_edges"] == 0
    return report


def grounded_shell_mask(patch, parent, ground_tolerance: float) -> np.ndarray:
    """补丁壳顶点的"接地"掩码：落在原始父表面 ground_tolerance 内的顶点。

    低 trim 填洞的产品决策下，壳面分两类：
    - 接地面：与父表面重合（同源点云重建的真实表面），其边缘与父面的缝隙
      是真实拼接缝，参与发布门槛；
    - 悬空面：父表面之外的推测插值面（洞区填充/边缘外伸），如实声明，不参与
      缝宽门槛——它可见、可解释，但不伪装成实测表面。
    """
    work = parent
    if len(parent.vertices) != len(np.unique(np.round(np.asarray(parent.vertices), 6), axis=0)):
        work = parent.copy()
        work.merge_vertices()
    tree = cKDTree(np.asarray(work.vertices))
    distances, _ = tree.query(np.asarray(patch.vertices))
    return distances <= ground_tolerance


def splice_slit(patch, kept, parent, gap_tolerance: float,
                ground_tolerance: float) -> dict:
    """重叠拼接缝宽（区分接地边缘与悬空推测面边缘）。

    - 接地边缘点（壳顶点落在父表面容差内）：到存活父面顶点的距离 = 真实可视
      裂缝，逐点实测；中位与最大值超限则拒绝发布；
    - 悬空边缘点（推测面在父表面之外收边）：数量与最大外伸量如实声明，不参与
      门槛——产品决策（低 trim 填洞）接受推测面，但不存在"伪装成实测"。
    """
    patch_loops = boundary_loops(patch)
    if not patch_loops:
        return {"patch_rim_points": 0, "crack_points": 0, "median_slit": 0.0, "max_slit": 0.0,
                "floating_points": 0, "max_overhang": 0.0,
                "gap_tolerance": gap_tolerance, "ground_tolerance": ground_tolerance, "pass": True}
    rim = np.vstack(patch_loops)
    kept_tree = cKDTree(np.asarray(kept.vertices))
    distances, _ = kept_tree.query(rim)
    grounded_idx = np.where(grounded_shell_mask(patch, parent, ground_tolerance))[0]
    grounded_tree = cKDTree(np.asarray(patch.vertices)[grounded_idx])
    d_grounded, _ = grounded_tree.query(rim)
    rim_grounded = d_grounded <= 1e-6
    crack = distances[rim_grounded]
    floating = distances[~rim_grounded]
    report = {
        "patch_rim_points": int(len(rim)),
        "crack_points": int(len(crack)),
        "median_slit": float(np.median(crack)) if len(crack) else 0.0,
        "max_slit": float(crack.max()) if len(crack) else 0.0,
        "floating_points": int(len(floating)),
        "max_overhang": float(floating.max()) if len(floating) else 0.0,
        "gap_tolerance": gap_tolerance,
        "ground_tolerance": ground_tolerance,
    }
    # 接地裂缝：中位必须达标，最大允许一个容差量级的局部最坏点（边缘重采样锯齿）
    report["pass"] = (not len(crack)) or (
        report["median_slit"] <= gap_tolerance and report["max_slit"] <= 2 * gap_tolerance)
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


def gap_tolerance_for(mesh, second=None) -> float:
    """缝合缝隙容差：3× 两侧网格中位边长的较大者（下限 0.001 模型单位）。

    v2 的两侧边界来自父网格与外盒重网格化补丁两套三角化，锯齿尺度由两者
    中较大的边长决定。
    """
    medians = [_median_edge_length(mesh)]
    if second is not None:
        medians.append(_median_edge_length(second))
    medians = [m for m in medians if m > 0]
    if not medians:
        return 0.001
    return max(SEAM_GAP_TOLERANCE_FACTOR * max(medians), 0.001)


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
    """局部网格补丁（v2）：外盒切割 → 外盒源点整体 Poisson → 平面缝合 → 不变量 → 独立 GLB。"""
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
            band = BAND_EDGE_FACTOR * _median_edge_length(parent)
            if band <= 0:
                raise ValueError("父网格没有可用的边长统计，无法确定点云选取外扩带")
            outer_lo, outer_hi = outer_box(lo, hi, band)
            centers = parent.triangles_center
            inside_core = ((centers >= lo).all(axis=1) & (centers <= hi).all(axis=1))
            outside_core_faces = np.where(~inside_core)[0]
            if not len(outside_core_faces):
                raise ValueError("核心盒覆盖了整个父网格，无法保留区域外几何")
            ctx.job_log(f"[LOCAL_MESH_CUT] 点云外扩带 {band:.6f}（3× 中位边长）；"
                        f"核心盒外父面 {len(outside_core_faces)}（逐字保留）")

        adapter = ColmapAdapter(get_settings().colmap_bin, use_gpu=False, gpu_index=ctx.gpu_index)
        with _stage(ctx, "LOCAL_MESHING") as cancel:
            # Poisson 输入是外盒内的全部源点（含父点与新增点）：补丁与父面同源，
            # 交界带两曲面天然重合——这是重叠拼接不需要边界环对应的原因。
            patch_points = _local_points(cloud, outer_lo, outer_hi, cancel)
            if len(patch_points) < MIN_PATCH_POINTS:
                raise ValueError(f"外盒内融合点不足（{len(patch_points)} < {MIN_PATCH_POINTS}），无法生成补丁")
            patch_ply = root / "patch-input.ply"
            _write_ply(patch_ply, patch_points[:0], patch_points, cancel)
            patch_raw = root / "patch-mesh.ply"
            resolved = config["resolved"]
            try:
                adapter.poisson_meshing(patch_ply, patch_raw, ctx.log_file("LOCAL_MESHING"),
                                        lambda: ctx.check_cancel("LOCAL_MESHING"),
                                        quality=resolved["poisson_depth"], num_threads=4,
                                        trim=PATCH_TRIM)
            except ColmapStageError as exc:
                raise PipelineError("LOCAL_MESHING", exc.code, str(exc)) from exc
            patch_mesh = trimesh.load(patch_raw, force="mesh", process=False)
            patch = cut_to_box(patch_mesh, lo, hi)
            if patch is None or len(patch.faces) == 0:
                raise ValueError("补丁网格在核心盒内没有面片")
            ctx.job_log(f"[LOCAL_MESHING] 外盒源点 {len(patch_points)}；核心盒内补丁 "
                        f"{len(patch.faces)} 面（{len(boundary_loops(patch))} 条开放边界）")

        with _stage(ctx, "LOCAL_STITCH"):
            # 重叠拼接：核心盒内"贴近接地壳面且远离补丁边缘"的父面由补丁取代；
            # 悬空推测面（低 trim 填洞的外伸/洞区插值）不取代父面——父面在其下
            # 原样保留，推测面悬空可见并如实声明（产品决策 2026-11-22）。
            tolerance = gap_tolerance_for(parent, patch)
            ground_tolerance = GROUND_FACTOR * _median_edge_length(parent)
            grounded = grounded_shell_mask(patch, parent, ground_tolerance)
            d_replace = REPLACE_FACTOR * max(_median_edge_length(parent), _median_edge_length(patch))
            grounded_tree = cKDTree(np.asarray(patch.vertices)[grounded])
            patch_rim = np.vstack(boundary_loops(patch)) if boundary_loops(patch) else np.zeros((0, 3))
            rim_tree = cKDTree(patch_rim) if len(patch_rim) else None
            inside_faces = np.where(inside_core)[0]
            if not len(inside_faces):
                raise ValueError("核心盒内没有父面，补丁无处拼接")
            replace_distances, _ = grounded_tree.query(centers[inside_faces])
            if rim_tree is not None and len(patch_rim):
                rim_distances, _ = rim_tree.query(centers[inside_faces])
                removable = (replace_distances <= d_replace) & (
                    rim_distances >= RIM_BUFFER_FACTOR * d_replace)
            else:
                removable = replace_distances <= d_replace
            replaced = inside_faces[removable]
            kept_faces = np.concatenate([outside_core_faces, inside_faces[~removable]])
            kept = parent.submesh([kept_faces], append=True)
            kept.remove_unreferenced_vertices()
            slit = splice_slit(patch, kept, parent, tolerance, ground_tolerance)
            ctx.job_log(f"[LOCAL_STITCH] 重叠拼接：取代 {len(replaced)} 父面（接地壳阈值 {d_replace:.6f}，"
                        f"边缘缓冲 {RIM_BUFFER_FACTOR * d_replace:.6f}）；接地裂缝中位 "
                        f"{slit['median_slit']:.6f} / 最大 {slit['max_slit']:.6f}（容差 {tolerance:.6f}，"
                        f"{slit['crack_points']} 点）；悬空推测边缘 {slit['floating_points']} 点，"
                        f"最大外伸 {slit['max_overhang']:.4f}")

        with _stage(ctx, "VALIDATION"):
            combined_vertices = np.vstack([kept.vertices, patch.vertices])
            combined_faces = np.vstack([
                kept.faces,
                patch.faces + len(kept.vertices),
            ])
            stitched = trimesh.Trimesh(vertices=combined_vertices, faces=combined_faces, process=False)
            invariants = mesh_invariants(parent, stitched, lo, hi)
            reject_reason = None
            if not invariants["pass"]:
                reject_reason = f"不变量未通过：{invariants}"
            elif not slit["pass"]:
                reject_reason = f"拼接缝宽超限：{slit}"
            if reject_reason:
                ctx.out_dir.mkdir(parents=True, exist_ok=True)
                (ctx.out_dir / "mesh-patch-report.json").write_text(json.dumps(
                    {"status": "rejected", "invariants": invariants, "slit": slit,
                     "replace_distance": d_replace, "replaced_parent_faces": int(len(replaced)),
                     "note": "重叠拼接未通过：不发布补丁网格；请使用局部点云候选。"},
                    ensure_ascii=False, indent=2), encoding="utf-8")
                raise PipelineError("VALIDATION", "MESH_PATCH_REJECTED",
                                    f"网格补丁拒绝发布：{reject_reason}；未发布任何网格")
            ctx.job_log(f"[VALIDATION] 不变量与缝宽通过：{invariants} | {slit}")

        with _stage(ctx, "EXPORTING"):
            patch_colored = trimesh.Trimesh(vertices=patch.vertices, faces=patch.faces, process=False)
            spacing = float(np.median(cKDTree(_xyz(patch_points)).query(_xyz(patch_points), k=2)[0][:, 1]))
            colors, color_report = patch_vertex_colors(
                np.asarray(patch.vertices, dtype=float), patch_points, max_distance=max(2.5 * spacing, 1e-6))
            patch_colored.visual = trimesh.visual.ColorVisuals(patch_colored, vertex_colors=colors)
            if baked_display:
                # 全部回到父 GLB 的正立显示坐标：刚性变换不改变任何几何关系
                rotation = _display_rotation(baked_display["up"])
                center_shift = np.asarray(baked_display["center"], dtype=float)
                kept_display = kept.copy()
                kept_display.vertices = rotation.apply(np.asarray(kept.vertices) - center_shift)
                patch_colored.vertices = rotation.apply(np.asarray(patch.vertices) - center_shift)
                scene = trimesh.Scene({"parent": kept_display, "patch": patch_colored})
            else:
                scene = trimesh.Scene({"parent": kept, "patch": patch_colored})
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
        "geometry": "mesh_patch_overlap_splice",
        "note": "局部网格补丁 v2'（重叠拼接 + 低 trim 填洞产品决策）：补丁为外盒内全部源点（含父点）的 Poisson "
                "曲面（trim=4），裁剪到核心盒；核心盒外父面逐字保留（原生坐标恢复经三角形核对）；核心盒内仅"
                "\"贴近接地壳面\"的父面由补丁取代；接地裂缝（补丁边缘与存活父面的可视缝）逐点实测并作为发布"
                "门槛；悬空推测边缘（洞区/边缘外伸的插值面）不参与门槛、数量与最大外伸如实声明——它们可见、"
                "可解释，不伪装成实测表面；补丁面片使用融合点云顶点色（非完整图集）；导出统一回到父 GLB 显示坐标。",
        "frame": ("display（父 GLB 正立坐标，由原生坐标经 display_up 旋转导出）"
                  if config.get("parent_display") else "native"),
        "parent_model_sha256": parent_sha, "source_cloud_sha256": cloud_sha,
        "resolved_params": {"bbox_min": np.asarray(lo).tolist(), "bbox_max": np.asarray(hi).tolist(),
                            "outer_bbox_min": np.asarray(outer_lo).tolist(),
                            "outer_bbox_max": np.asarray(outer_hi).tolist(),
                            "band_width": band, "band_definition": f"{BAND_EDGE_FACTOR}× 父网格中位边长（点云选取外扩）",
                            "replace_distance": d_replace,
                            "replace_definition": f"{REPLACE_FACTOR}× max(父/补丁中位边长)，核心盒内贴近补丁壳的父面由补丁取代",
                            "poisson_depth": resolved["poisson_depth"],
                            "poisson_trim": PATCH_TRIM,
                            "trim_policy": "产品决策（2026-11-22）：低 trim 填洞——核心盒内父洞/低密度区"
                                           "为 Poisson 插值推测面（如实声明，非实测）；核心盒外父面逐字保留",
                            "slit_gap_tolerance": tolerance},
        "slit": slit, "invariants": invariants, "colors": color_report,
        "counts": {"parent_faces_kept": int(len(kept.faces)), "parent_faces_replaced": int(len(replaced)),
                   "patch_faces": int(len(patch.faces)), "total_faces": int(len(stitched.faces)),
                   "outer_box_points": int(len(patch_points))},
        "outputs": [str(out)],
    }
    return records, manifest
