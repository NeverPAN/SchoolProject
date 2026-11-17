"""COLMAP 命令适配层。

安全约束（指南 §7.4）：
- subprocess 参数数组调用，禁止 shell=True；
- 可执行文件路径来自配置；
- stdout/stderr 流式写入阶段日志；
- 支持取消（进程组 SIGTERM）与超时；
- 退出码 + 预期输出检查。
"""
from __future__ import annotations

import os
import re
import signal
import subprocess
import time
from collections.abc import Callable
from pathlib import Path

from app.core.errors import AppError
from app.core.logging_setup import get_logger

logger = get_logger("colmap")

# 每个阶段完成后必须存在的输出（相对输出目录）
DEFAULT_TIMEOUT_SECONDS = 6 * 3600


class ColmapNotFoundError(AppError):
    code = "COLMAP_NOT_FOUND"
    status_code = 500


class ColmapStageError(AppError):
    code = "COLMAP_STAGE_FAILED"
    status_code = 500


class CancelledError(AppError):
    code = "CANCELLED"
    status_code = 499


class ColmapAdapter:
    def __init__(self, colmap_bin: str, use_gpu: bool, version: str = "unknown"):
        self.colmap_bin = colmap_bin
        self.use_gpu = use_gpu
        self.version = version
        # 3.10+ 将 SiftExtraction/SiftMatching 选项改名为 FeatureExtraction/FeatureMatching
        self._new_option_names = _parse_major_minor(version) >= (3, 10)

    @property
    def _extraction_prefix(self) -> str:
        return "FeatureExtraction" if self._new_option_names else "SiftExtraction"

    @property
    def _matching_prefix(self) -> str:
        return "FeatureMatching" if self._new_option_names else "SiftMatching"

    # ------------------------------------------------------------------
    # 进程执行
    # ------------------------------------------------------------------

    def run_command(
        self,
        args: list[str],
        log_file: str | Path,
        cancel_check: Callable[[], bool] | None = None,
        timeout_s: int = DEFAULT_TIMEOUT_SECONDS,
        success_hint: str | None = None,
    ) -> None:
        """执行 COLMAP 子命令，流式记录日志，支持取消/超时。

        :param success_hint: 输出中必须出现的关键字（可选，用于更严格校验）
        """
        cmd = [self.colmap_bin, *args]
        logger.info("执行: %s", " ".join(cmd))
        Path(log_file).parent.mkdir(parents=True, exist_ok=True)

        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            errors="replace",
            start_new_session=True,  # 独立进程组，便于整组终止
        )
        start = time.time()
        killed = False
        with open(log_file, "a", encoding="utf-8") as log:
            log.write(f"\n$ {' '.join(cmd)}\n")
            log.flush()
            assert proc.stdout is not None
            for line in proc.stdout:
                log.write(line)
                log.flush()
                now = time.time()
                if now - start > timeout_s:
                    killed = True
                    _kill_group(proc)
                    raise ColmapStageError(f"阶段超时（>{timeout_s}s）: {args[0]}")
                if cancel_check and (now - (getattr(self, "_last_cancel_check", 0)) > 1.0):
                    self._last_cancel_check = now
                    if cancel_check():
                        killed = True
                        _kill_group(proc)
                        raise CancelledError("任务已被用户取消")
            code = proc.wait()
        if killed:
            raise CancelledError("任务已被用户取消")
        if code != 0:
            raise ColmapStageError(f"COLMAP 命令失败（exit={code}）: {args[0]}，详见阶段日志")
        if success_hint:
            text = Path(log_file).read_text(encoding="utf-8", errors="replace")
            if success_hint not in text:
                raise ColmapStageError(f"COLMAP 命令输出异常（未出现 '{success_hint}'）: {args[0]}")

    # ------------------------------------------------------------------
    # 阶段命令构建
    # ------------------------------------------------------------------

    def feature_extraction(self, db_path: Path, image_dir: Path, log_file: Path, cancel_check=None) -> None:
        args = [
            "feature_extractor",
            "--database_path", str(db_path),
            "--image_path", str(image_dir),
            "--ImageReader.camera_model", "SIMPLE_RADIAL",
            f"--{self._extraction_prefix}.use_gpu", "1" if self.use_gpu else "0",
            # SIFT 算法参数在所有版本均使用 SiftExtraction 前缀
            "--SiftExtraction.max_num_features", "8192",
        ]
        self.run_command(args, log_file, cancel_check)

    def feature_matching(
        self, db_path: Path, matcher: str, log_file: Path, cancel_check=None
    ) -> None:
        sub = {"exhaustive": "exhaustive_matcher", "sequential": "sequential_matcher"}.get(
            matcher, "exhaustive_matcher"
        )
        args = [
            sub,
            "--database_path", str(db_path),
            f"--{self._matching_prefix}.use_gpu", "1" if self.use_gpu else "0",
        ]
        if sub == "sequential_matcher":
            args += ["--SequentialMatching.overlap", "10", "--SequentialMatching.quadratic_overlap", "1"]
        self.run_command(args, log_file, cancel_check)

    def sparse_mapping(self, db_path: Path, image_dir: Path, out_dir: Path, log_file: Path, cancel_check=None) -> Path:
        out_dir.mkdir(parents=True, exist_ok=True)
        args = [
            "mapper",
            "--database_path", str(db_path),
            "--image_path", str(image_dir),
            "--output_path", str(out_dir),
        ]
        self.run_command(args, log_file, cancel_check)
        model_dir = out_dir / "0"
        if not (model_dir / "cameras.bin").is_file():
            # COLMAP 3.7 可能输出到不同子目录编号
            candidates = sorted(p for p in out_dir.iterdir() if (p / "cameras.bin").is_file())
            if not candidates:
                raise ColmapStageError("稀疏重建未产出任何模型（相机注册可能全部失败）")
            model_dir = candidates[0]
        return model_dir

    def undistort(self, image_dir: Path, sparse_dir: Path, out_dir: Path, log_file: Path, cancel_check=None) -> None:
        # 注意：不预创建 out_dir，COLMAP 内部使用 std::filesystem 创建目录，
        # 预创建的空目录在某些场景会触发 create_directories 异常
        out_dir.parent.mkdir(parents=True, exist_ok=True)
        args = [
            "image_undistorter",
            "--image_path", str(image_dir),
            "--input_path", str(sparse_dir),
            "--output_path", str(out_dir),
            "--output_type", "COLMAP",
        ]
        self.run_command(args, log_file, cancel_check)

    def dense_stereo(self, dense_dir: Path, log_file: Path, cancel_check=None) -> None:
        args = [
            "patch_match_stereo",
            "--workspace_path", str(dense_dir),
            "--workspace_format", "COLMAP",
            "--PatchMatchStereo.geom_consistency", "true",
        ]
        self.run_command(args, log_file, cancel_check, success_hint=None)

    def stereo_fusion(self, dense_dir: Path, out_ply: Path, log_file: Path, cancel_check=None) -> None:
        out_ply.parent.mkdir(parents=True, exist_ok=True)
        args = [
            "stereo_fusion",
            "--workspace_path", str(dense_dir),
            "--workspace_format", "COLMAP",
            "--input_type", "geometric",
            "--output_path", str(out_ply),
        ]
        self.run_command(args, log_file, cancel_check)

    def poisson_meshing(self, in_ply: Path, out_ply: Path, log_file: Path, cancel_check=None, quality: int = 10) -> None:
        out_ply.parent.mkdir(parents=True, exist_ok=True)
        args = [
            "poisson_mesher",
            "--input_path", str(in_ply),
            "--output_path", str(out_ply),
            "--PoissonMeshing.depth", str(quality),
        ]
        self.run_command(args, log_file, cancel_check)


def _kill_group(proc: subprocess.Popen) -> None:
    """终止整个进程组（COLMAP 可能有子进程）。"""
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        try:
            proc.terminate()
        except Exception:
            pass
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except Exception:
            proc.kill()


def get_colmap_version(colmap_bin: str) -> str | None:
    """获取 COLMAP 版本串（3.13 起不支持 --version，改用 help）。"""
    import re

    for args in (["--version"], ["help"]):
        try:
            out = subprocess.run(
                [colmap_bin, *args], capture_output=True, text=True, timeout=20
            )
            first = (out.stdout or out.stderr).strip().splitlines()
            if first and "COLMAP" in first[0]:
                return first[0].strip()
        except Exception:
            continue
    return None


def _parse_major_minor(version: str) -> tuple[int, int]:
    m = re.search(r"COLMAP\s+(\d+)\.(\d+)", version or "")
    if not m:
        return (0, 0)
    return (int(m.group(1)), int(m.group(2)))


def colmap_has_cuda(colmap_bin: str) -> bool:
    try:
        out = subprocess.run([colmap_bin, "-h"], capture_output=True, text=True, timeout=20)
        return "CUDA" in (out.stdout + out.stderr)
    except Exception:
        return False
