"""GPU MVS and append-only point fusion for a trusted, pre-registered ROI seed.

The server freezes parent artifacts, a native-frame sparse reconstruction and
its allowed old/new views at submission. This worker does not estimate new
poses, update any parent artifact, or construct a mesh. Dense processing is
bounded by the selected view set, not pixel crops within the selected AABB.
"""
from __future__ import annotations

import hashlib
import os
import re
import shutil
import struct
import tempfile
import time
from contextlib import contextmanager, nullcontext
from pathlib import Path

import numpy as np
from scipy.spatial import cKDTree
from scipy.spatial.transform import Rotation

from app.core.config import get_settings
from app.models.entities import Model, ModelRegion
from app.reconstruction.adapter import CancelledError, ColmapAdapter
from app.reconstruction.manifest import compute_sha_of, input_manifest_sha
from app.reconstruction.pipeline import PipelineContext, PipelineError, StageCancelled

MODE = "local_reconstruct"
STAGE_ORIGIN = "LOCAL_POINT_FUSION"
SPARSE_FILES = {"cameras.bin", "images.bin", "points3D.bin"}
RIG_FILES = {"rigs.bin", "frames.bin"}
MAX_OLD_VIEWS = 16
MAX_IMAGE_SIZE = 1600
PATCHMATCH_ITERATIONS = 5
GUARD = 0.03
MIN_DISTANCE = 0.004
MAX_DISTANCE = 0.025
DEPTH_TOLERANCE = 0.012
MIN_NEW_VOTES = 2
VERTEX = np.dtype([(name, '<f4') for name in ("x", "y", "z", "nx", "ny", "nz")]
                  + [(name, 'u1') for name in ("red", "green", "blue")])
PROPERTIES = tuple(f"property {'float' if VERTEX[name].kind == 'f' else 'uchar'} {name}" for name in VERTEX.names)


@contextmanager
def scratch_dir(prefix: str, parent: Path):
    """NFS 安全临时目录：忽略清理失败，不让清理错误掩盖真实异常。"""
    path = Path(tempfile.mkdtemp(prefix=prefix, dir=parent))
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


@contextmanager
def _stage(ctx, stage):
    ctx.check_cancel(stage)
    ctx.set_stage_running(stage)
    ctx.job_log(f"[{stage}] 本地重建开始")
    started = time.monotonic()
    try:
        yield lambda: ctx.check_cancel(stage)
        ctx.check_cancel(stage)
    except CancelledError as exc:
        raise StageCancelled(stage) from exc
    except (OSError, ValueError, KeyError, TypeError, OverflowError, struct.error) as exc:
        raise PipelineError(stage, "LOCAL_RECONSTRUCTION_FAILED", str(exc)) from exc
    ctx.job_log(f"[{stage}] 完成（{time.monotonic() - started:.2f}s）")
    ctx.set_stage_result(stage, "completed")


def _digest(value):
    if not isinstance(value, str) or not re.fullmatch(r"[a-fA-F0-9]{64}", value):
        raise ValueError("Expected a frozen SHA-256 digest")
    return value.lower()


def _file(spec, roots):
    path = Path(spec["path"])
    if not path.is_absolute():
        raise ValueError("Seed file paths must be absolute")
    path = path.resolve(strict=True)
    if not path.is_file() or any(path.is_relative_to(root) for root in roots):
        raise ValueError(f"Not an independent input file: {path.name}")
    return path, _digest(spec["sha256"])


def _checked_bytes(source, cancel, dest=None, expected=None):
    """Hash exactly the bytes staged, with no hard links to the source."""
    sha = hashlib.sha256()
    with source.open("rb") as inp, (dest.open("xb") if dest else nullcontext()) as out:
        while chunk := inp.read(4 * 1024 * 1024):
            cancel()
            sha.update(chunk)
            if out is not None:
                out.write(chunk)
    actual = sha.hexdigest()
    if expected is not None and actual != expected:
        raise ValueError(f"Frozen input SHA-256 mismatch: {source.name}")
    return actual


def _check_file(asset, cancel, dest=None):
    return _checked_bytes(asset[0], cancel, dest, asset[1])


def _box(lo, hi):
    if any(not isinstance(v, (tuple, list)) or len(v) != 3 or
           any(type(n) not in (int, float) for n in v) for v in (lo, hi)):
        raise ValueError("Expected two finite 3D bounds")
    a, b = np.asarray(lo, dtype=float), np.asarray(hi, dtype=float)
    if not np.isfinite([a, b]).all() or not (a < b).all():
        raise ValueError("Invalid or empty AABB")
    return a, b


def _verify_lineage(ctx, target_id, region_id, model_path, lo, hi):
    with ctx.session_factory() as db:
        parent = db.get(Model, target_id)
        region = db.get(ModelRegion, region_id)
        if (parent is None or parent.project_id != ctx.project_id or parent.job_id == ctx.job_id or
                Path(parent.file_path).resolve() != model_path):
            raise ValueError("Seed does not match the explicit parent model in this project")
        if region is None or region.model_id != target_id or region.user_id != ctx.owner_id:
            raise ValueError("Region does not belong to the explicit parent model")
        db_bounds = [[getattr(region, f"bbox_{end}_{axis}") for axis in "xyz"] for end in ("min", "max")]
        if not np.array_equal(db_bounds, [lo, hi]):
            raise ValueError("Frozen bounds do not match the parent region")


def _input_specs(ctx, config):
    seed = config["seed"]
    target_id, region_id = config["target_model_id"], config["region_id"]
    if (type(target_id) is not int or target_id < 1 or type(region_id) is not int or region_id < 1 or
            type(seed["version"]) is not int or seed["version"] != 1 or seed["target_model_id"] != target_id):
        raise ValueError("Invalid local seed version or explicit parent/region")
    lo, hi = _box(config["bounds"]["min"], config["bounds"]["max"])
    allowed_lo, allowed_hi = _box(seed["bbox_min"], seed["bbox_max"])
    if not ((lo >= allowed_lo).all() and (hi <= allowed_hi).all() and (lo + GUARD < hi - GUARD).all()):
        raise ValueError("Bounds are outside the verified seed envelope or too small for the guard band")

    roots = (ctx.work_dir.resolve(), ctx.out_dir.resolve())
    parent = _file(seed["parent_model"], roots)
    cloud = _file(seed["parent_cloud"], roots)
    _verify_lineage(ctx, target_id, region_id, parent[0], lo, hi)
    sparse_specs = seed["sparse"]
    if set(sparse_specs) not in (SPARSE_FILES, SPARSE_FILES | RIG_FILES):
        raise ValueError("Expected a COLMAP binary sparse model (optional paired rigs/frames)")
    sparse = {name: _file(spec, roots) for name, spec in sparse_specs.items()}
    new = seed["new_views"]
    old_specs = seed["old_views"]
    names = set(old_specs) | set(new)
    if (not 2 <= len(old_specs) <= MAX_OLD_VIEWS or len(new) != 3 or set(old_specs) & set(new) or
            any(not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,199}", name) for name in names) or
            len({name.casefold() for name in names}) != len(names)):
        raise ValueError("Invalid frozen old/new view names or counts")
    if any(type(i) is not int or i <= 0 for i in new.values()) or len(set(new.values())) != len(new):
        raise ValueError("New views must map to distinct uploaded image IDs")
    entries = ctx.input_manifest["images"]
    if any(type(entry["image_id"]) is not int for entry in entries):
        raise ValueError("Expected frozen uploaded image IDs")
    by_id = {entry["image_id"]: entry for entry in entries}
    if len(entries) != len(new) or by_id.keys() != set(new.values()):
        raise ValueError("Frozen uploads must exactly match the registered new views")
    if set(seed["new_view_sha256"]) != set(new):
        raise ValueError("Missing trusted new-view digests")
    images = {name: _file(spec, roots) for name, spec in old_specs.items()}
    for name, image_id in new.items():
        entry = by_id[image_id]
        sha = _digest(entry["checksum"])
        if sha != _digest(seed["new_view_sha256"][name]):
            raise ValueError(f"Frozen upload does not match the registered seed photo: {name}")
        images[name] = _file({"path": entry["stored_path"], "sha256": sha}, roots)
    if len({sha for _, sha in images.values()}) != len(images):
        raise ValueError("Each view must be a distinct photograph")
    return parent, cloud, sparse, images, sorted(new), lo, hi


def _read_ply(path):
    with path.open("rb") as inp:
        header = []
        for _ in range(32):
            line = inp.readline(256).decode("ascii").strip()
            if line == "end_header":
                break
            if not line:
                raise ValueError(f"Invalid PLY header: {path.name}")
            if not line.startswith("comment "):
                header.append(line)
        else:
            raise ValueError(f"Invalid PLY header: {path.name}")
        if (len(header) != 3 + len(PROPERTIES) or header[:2] != ["ply", "format binary_little_endian 1.0"] or
                not re.fullmatch(r"element vertex [1-9][0-9]*", header[2]) or tuple(header[3:]) != PROPERTIES):
            raise ValueError(f"Unexpected point-cloud PLY layout: {path.name}")
        count = int(header[2].split()[-1])
        if os.fstat(inp.fileno()).st_size - inp.tell() != count * VERTEX.itemsize:
            raise ValueError(f"Unexpected PLY point data size: {path.name}")
        return np.memmap(inp, dtype=VERTEX, mode="r", offset=inp.tell(), shape=(count,))


def _write_ply(path, parent, patch, cancel):
    with path.open("xb") as out:
        header = ["ply", "format binary_little_endian 1.0", f"element vertex {len(parent) + len(patch)}",
                  *PROPERTIES, "end_header", ""]
        out.write("\n".join(header).encode("ascii"))
        for vertices in (parent, patch):
            for start in range(0, len(vertices), 131072):
                cancel()
                out.write(vertices[start:start + 131072].tobytes())
        out.flush()
        os.fsync(out.fileno())


def _xyz(vertices):
    return np.column_stack([vertices[name] for name in ("x", "y", "z")]).astype(float)


def _inside(xyz, lo, hi):
    return ((xyz >= lo) & (xyz <= hi)).all(axis=1)


def _local_points(vertices, lo, hi, cancel):
    parts = []
    for start in range(0, len(vertices), 131072):
        cancel()
        part = vertices[start:start + 131072]
        points = _xyz(part)
        if not np.isfinite(points).all():
            raise ValueError("Non-finite coordinates in a point cloud")
        parts.append(part[_inside(points, lo, hi)])
    return np.concatenate(parts)


def _poses(path):
    """Read COLMAP's images.bin records without loading all track observations."""
    poses = {}
    with path.open("rb") as f:
        count, = struct.unpack("<Q", f.read(8))
        for _ in range(count):
            _, qw, qx, qy, qz, tx, ty, tz, camera_id = struct.unpack("<i7di", f.read(64))
            name = bytearray()
            while (b := f.read(1)) != b"\0":
                if not b or len(name) >= 4096:
                    raise ValueError("Invalid COLMAP image name")
                name.extend(b)
            text = name.decode("utf-8")
            if text in poses or not np.isfinite([qw, qx, qy, qz, tx, ty, tz]).all():
                raise ValueError("Invalid or duplicate COLMAP image pose")
            poses[text] = (Rotation.from_quat([qx, qy, qz, qw]).as_matrix(), np.array([tx, ty, tz]), camera_id)
            observations, = struct.unpack("<Q", f.read(8))
            f.seek(24 * observations, os.SEEK_CUR)
        if f.tell() != os.fstat(f.fileno()).st_size:
            raise ValueError("Invalid COLMAP images.bin size")
    return poses


def _cameras(path):
    cameras = {}
    with path.open("rb") as f:
        count, = struct.unpack("<Q", f.read(8))
        for _ in range(count):
            camera_id, model, w, h = struct.unpack("<iiQQ", f.read(24))
            if model == 0:
                fx, cx, cy = struct.unpack("<3d", f.read(24))
                fy = fx
            elif model in (1, 2):
                p = struct.unpack("<4d", f.read(32))
                if model == 1:
                    fx, fy, cx, cy = p
                else:
                    fx, cx, cy, distortion = p
                    fy = fx
                    if distortion != 0:
                        raise ValueError("Depth support requires an undistorted camera")
            else:
                raise ValueError("Unsupported undistorted camera model")
            if (camera_id in cameras or not w or not h or min(fx, fy) <= 0 or
                    not np.isfinite([fx, fy, cx, cy]).all()):
                raise ValueError("Invalid COLMAP camera")
            cameras[camera_id] = (w, h, fx, fy, cx, cy)
        if f.read(1):
            raise ValueError("Invalid COLMAP cameras.bin size")
    return cameras


def _depth(path):
    with path.open("rb") as f:
        header = bytearray()
        while header.count(b"&") < 3:
            ch = f.read(1)
            if not ch or len(header) >= 100:
                raise ValueError(f"Invalid depth-map header: {path.name}")
            header.extend(ch)
        w, h, channels = map(int, header[:-1].split(b"&"))
        if (channels != 1 or not 0 < w <= MAX_IMAGE_SIZE or not 0 < h <= MAX_IMAGE_SIZE or
                os.fstat(f.fileno()).st_size - f.tell() != 4 * w * h):
            raise ValueError(f"Unexpected geometric depth-map dimensions or size: {path.name}")
        return np.fromfile(f, dtype="<f4", count=w * h).reshape(h, w)


def _supported(points, pose, camera, depth):
    w, h, fx, fy, cx, cy = camera
    rot, trans, _ = pose
    dh, dw = depth.shape
    pc = points @ rot.T + trans
    z = pc[:, 2]
    safe = np.where(z > 0, z, 1)
    with np.errstate(over="ignore", divide="ignore", invalid="ignore"):
        u = (fx * pc[:, 0] / safe + cx) * dw / w
        v = (fy * pc[:, 1] / safe + cy) * dh / h
    ok = (z > 0) & (u >= 0) & (v >= 0) & (u < dw) & (v < dh)
    measured = depth[v[ok].astype(int), u[ok].astype(int)]
    ok[ok] = np.isfinite(measured) & (measured > 0) & (abs(measured - z[ok]) <= DEPTH_TOLERANCE * z[ok])
    return ok


def _select_patch(parent, candidate, dense, names, lo, hi, cancel, *, min_votes=MIN_NEW_VOTES):
    local_parent = _local_points(parent, lo, hi, cancel)
    local = _local_points(candidate, lo, hi, cancel)
    if not len(local_parent) or not len(local):
        raise ValueError("The guarded ROI does not contain both parent and reconstructed points")
    poses = _poses(dense / "sparse" / "images.bin")
    cameras = _cameras(dense / "sparse" / "cameras.bin")
    points = _xyz(local)
    votes = np.zeros(len(local), dtype=np.uint8)
    for name in names:
        cancel()
        pose = poses[name]
        depth = _depth(dense / "stereo" / "depth_maps" / (name + ".geometric.bin"))
        for start in range(0, len(local), 131072):
            cancel()
            sl = slice(start, start + 131072)
            votes[sl] += _supported(points[sl], pose, cameras[pose[2]], depth)
    supported = votes >= min_votes
    points, eligible = points[supported], local[supported]
    tree = cKDTree(_xyz(local_parent))
    keep = np.zeros(len(points), dtype=bool)
    for start in range(0, len(points), 131072):
        cancel()
        sl = slice(start, start + 131072)
        distances = tree.query(points[sl])[0]
        keep[sl] = (distances >= MIN_DISTANCE) & (distances <= MAX_DISTANCE)
    patch = eligible[keep]
    if not len(patch):
        raise ValueError("No supported, non-duplicate points to append inside the guarded ROI")
    if any(not np.isfinite(patch[name]).all() for name in ("nx", "ny", "nz")):
        raise ValueError("Invalid normals on reconstructed points")
    return patch, {"candidate_points_in_core": len(local), "new_view_supported_points": len(eligible),
                   "added_points": len(patch), "parent_points": len(parent)}


def _adapter(ctx):
    executable = get_settings().colmap_bin
    probe = ColmapAdapter(executable, use_gpu=True, gpu_index=ctx.gpu_index)
    # Parse only this invocation; prior attempts may have used another binary.
    log = ctx.log_file("IMAGE_VALIDATION")
    start = log.stat().st_size if log.exists() else 0
    probe.run_command(["help"], log, ctx.cancel_check, timeout_s=20)
    with log.open("rb") as inp:
        inp.seek(start)
        text = inp.read().decode("utf-8", errors="replace")
    version = re.search(r"(?m)^COLMAP\s+\d[^\r\n]*", text)
    if version is None or not re.search(r"\bwith CUDA\b", text):
        raise ValueError("A CUDA-capable COLMAP executable is required for local reconstruction")
    probe.reapply_version(version[0])
    return probe


def _prepare_dense(adapter, images, sparse, dense, names, log_file, cancel_check):
    image_list = dense.parent / "views.txt"
    image_list.write_text("\n".join(names) + "\n", encoding="ascii")
    adapter.run_command([
        "image_undistorter", "--image_path", str(images), "--input_path", str(sparse),
        "--output_path", str(dense), "--output_type", "COLMAP", "--image_list_path", str(image_list),
    ], log_file, cancel_check)
    poses = _poses(dense / "sparse" / "images.bin")
    if any(name not in poses or not (dense / "images" / name).is_file() for name in names):
        raise ValueError("A selected view is missing from the undistorted COLMAP workspace")
    stereo = dense / "stereo"
    (stereo / "patch-match.cfg").write_text("".join(
        f"{name}\n{', '.join(n for n in names if n != name)}\n" for name in names), encoding="ascii")
    (stereo / "fusion.cfg").write_text("\n".join(names) + "\n", encoding="ascii")


def run(ctx: PipelineContext, config: dict) -> tuple[list[dict], dict]:
    """Build a separate point cloud and return publication records/manifest.

    Safe retries rebuild in fresh scratch space. No artifacts are registered
    here; the caller owns the last cancellation check and DB transaction.
    """
    with scratch_dir("local-reconstruction-", ctx.work_dir) as directory:
        root = Path(directory)
        images, sparse, dense = (root / name for name in ("images", "sparse", "dense"))
        copied_cloud = root / "parent.ply"
        with _stage(ctx, "IMAGE_VALIDATION") as cancel:
            parent, cloud, sparse_files, photos, new_views, lo, hi = _input_specs(ctx, config)
            _check_file(parent, cancel)
            _check_file(cloud, cancel, copied_cloud)
            parent_points = _read_ply(copied_cloud)
            images.mkdir()
            sparse.mkdir()
            for name, asset in sparse_files.items():
                _check_file(asset, cancel, sparse / name)
            for name, asset in photos.items():
                _check_file(asset, cancel, images / name)
            adapter = _adapter(ctx)
            ctx.engine_identity = adapter.engine_identity
            names = sorted(photos)
            ctx.job_log(f"[IMAGE_VALIDATION] 已验证父版本 #{config['target_model_id']}、{len(names)} 个注册视角")

        with _stage(ctx, "IMAGE_UNDISTORTION"):
            _prepare_dense(adapter, images, sparse, dense, names, ctx.log_file("IMAGE_UNDISTORTION"), ctx.cancel_check)

        with _stage(ctx, "DENSE_RECONSTRUCTION"):
            adapter.dense_stereo(dense, ctx.log_file("DENSE_RECONSTRUCTION"), ctx.cancel_check,
                                 max_image_size=MAX_IMAGE_SIZE, num_iterations=PATCHMATCH_ITERATIONS)

        with _stage(ctx, "STEREO_FUSION") as cancel:
            fused = dense / "fused.ply"
            adapter.stereo_fusion(dense, fused, ctx.log_file("STEREO_FUSION"), ctx.cancel_check,
                                  max_image_size=MAX_IMAGE_SIZE, num_threads=ctx.fusion_threads)
            patch, counts = _select_patch(parent_points, _read_ply(fused), dense, new_views, lo + GUARD, hi - GUARD, cancel)
            ctx.job_log(f"[STEREO_FUSION] 核心区新增 {counts['added_points']} 个支持点")

        with _stage(ctx, "EXPORTING") as cancel:
            out = ctx.out_dir / "local-fused.ply"
            total = len(parent_points) + len(patch)
            # Stage on the destination filesystem, then publish by atomic rename.
            staging = Path(tempfile.mkdtemp(prefix=".local-export-", dir=ctx.out_dir))
            try:
                temp = staging / out.name
                _write_ply(temp, parent_points, patch, cancel)
                sha = _checked_bytes(temp, cancel)
                staged = _read_ply(temp)
                if len(staged) != total or not _inside(_xyz(patch), lo + GUARD, hi - GUARD).all():
                    raise ValueError("Local point-cloud output does not satisfy the guarded bounds")
                for start in range(0, len(parent_points), 131072):
                    cancel()
                    sl = slice(start, min(start + 131072, len(parent_points)))
                    if not np.array_equal(staged[sl].view("u1"), parent_points[sl].view("u1")):
                        raise ValueError("Parent point records changed in the child point cloud")
                # The child was computed from staged bytes; verify the authoritative sources still match.
                _verify_lineage(ctx, config["target_model_id"], config["region_id"], parent[0], lo, hi)
                for asset in (parent, cloud):
                    _check_file(asset, cancel)
                size = temp.stat().st_size
                del staged
                cancel()
                os.replace(temp, out)
            finally:
                shutil.rmtree(staging, ignore_errors=True)
        del parent_points

    records = [{"model_type": "dense_pointcloud", "format": "ply", "file_path": str(out),
                "file_size": size, "stage_origin": STAGE_ORIGIN, "vertex_count": total,
                "face_count": 0, "point_count": total}]
    manifest = {
        "job_id": ctx.job_id, "pipeline_mode": MODE, "engine_version": adapter.version, "engine": ctx.engine_identity,
        "target_model_id": config["target_model_id"], "parent_model_id": config["target_model_id"],
        "region_id": config["region_id"], "geometry": "pointcloud_only",
        "note": "预注册相机、整幅视图 GPU 重建；只补充核心区点云，不替换父模型或生成网格。",
        "inputs_sha": input_manifest_sha(ctx.input_manifest), "seed_sha": compute_sha_of(config["seed"]),
        "parent_model_sha256": parent[1], "parent_cloud_sha256": cloud[1],
        "sparse_sha256": {name: asset[1] for name, asset in sparse_files.items()},
        "view_sha256": {name: asset[1] for name, asset in photos.items()},
        "new_views": {name: config["seed"]["new_views"][name] for name in new_views},
        "resolved_params": {"bbox_min": lo.tolist(), "bbox_max": hi.tolist(),
                            "core_min": (lo + GUARD).tolist(), "core_max": (hi - GUARD).tolist(),
                            "guard_band": GUARD, "min_parent_distance": MIN_DISTANCE, "max_parent_distance": MAX_DISTANCE,
                            "min_new_view_votes": MIN_NEW_VOTES, "relative_depth_tolerance": DEPTH_TOLERANCE,
                            "dense_max_image_size": MAX_IMAGE_SIZE, "patchmatch_iterations": PATCHMATCH_ITERATIONS,
                            "geom_consistency": True, "gpu_index": adapter.gpu_index, "fusion_threads": ctx.fusion_threads},
        "counts": counts, "output_sha256": sha, "outputs": [str(out)],
    }
    return records, manifest
