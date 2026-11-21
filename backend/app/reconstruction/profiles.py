"""通用重建 profile 与任务配置解析。

设计目标（集成系统阶段 A1）：
- preset 是数据驱动的通用质量-速度画像，不绑定任何数据集或实验；
- 任务创建时一次性解析并冻结全部执行参数；Worker 恢复旧任务时
  不得被运行环境的新变量悄悄改变行为；
- ``speed`` 画像来自组合提速实测（PatchMatch 3 次、融合 16 线程、贴图
  从 0.125 档开始），作为通用可选画像而非场景专用路径；
- ``auto`` 匹配策略依据文件名连续性判断拍摄顺序，而不是固定映射 preset。
"""
from __future__ import annotations

import re
from pathlib import Path

from app.core.config import get_settings

# poisson_depth 与开发指南的预设表一致（fast=8 / standard=10 / high=12）。
# 第二轮在正式 Bridge 数据上的 depth×trim 对照表明：同一份 2000px 稠密点云，
# depth 10 的点支撑面积 74.3%、无支撑外插 11.1%，显著优于 depth 8 的 65.7%/27.8%。
# high 的 depth 12 网格量级可达数百万面，贴图与查看器开销大，未在本轮验收。
# speed 画像的迭代/线程/贴图档为显式画像参数，优先于全局环境变量。
# poisson_trim 是网格候选基准（COLMAP 默认 10）；当上部支撑不足时，
# 管线按预声明门槛自动生成 trim-1 候选并裁定，而不是固定使用 trim=9。
PROFILES: dict[str, dict] = {
    "fast": {"max_features": 4096, "poisson_depth": 8, "matcher_default": "sequential"},
    "standard": {"max_features": 8192, "poisson_depth": 10, "matcher_default": "exhaustive"},
    "high": {"max_features": 16384, "poisson_depth": 12, "matcher_default": "exhaustive"},
    "speed": {
        "max_features": 8192, "poisson_depth": 10, "matcher_default": "exhaustive",
        "patchmatch_iterations": 3, "fusion_threads": 16, "texture_start_scale": 0.125,
    },
}

POISSON_TRIM_DEFAULT = 10
MESH_UPGRADE_TOP_BAND_THRESHOLD = 0.7

SEQUENCE_MIN_IMAGES = 6
_NUMBER_RE = re.compile(r"(\d+)")


def auto_matcher_decision(filenames: list[str]) -> tuple[str, str]:
    """按文件名连续编号判断拍摄顺序；无序集合回退到穷举匹配。

    返回 (matcher, 决策说明)。连续编号（如 DSC_1174…DSC_1273）是顺序拍摄的
    强信号，可用 sequential 省时间；随机命名或编号断裂视为无序集合。
    """
    numbers: list[int] = []
    for name in filenames:
        found = _NUMBER_RE.findall(Path(name).stem)
        numbers.append(int(found[-1]) if found else -1)
    ordered = (
        len(filenames) >= SEQUENCE_MIN_IMAGES
        and all(n >= 0 for n in numbers)
        and len(set(numbers)) == len(numbers)
        and sorted(numbers)[-1] - sorted(numbers)[0] + 1 == len(numbers)
    )
    if ordered:
        return "sequential", f"auto→sequential：{len(filenames)} 张文件名连续编号，按拍摄顺序匹配"
    return "exhaustive", "auto→exhaustive：文件名无连续编号，按无序集合全匹配"


def _visible_gpu_count() -> int | None:
    """读取 nvidia-smi 可见 GPU 数；工具不可用时返回 None（不伪造结论）。"""
    import shutil
    import subprocess

    binary = shutil.which("nvidia-smi")
    if binary is None:
        return None
    try:
        out = subprocess.run([binary, "-L"], capture_output=True, text=True, timeout=15, check=True)
    except (OSError, subprocess.SubprocessError):
        return None
    return len([line for line in out.stdout.splitlines() if line.strip().startswith("GPU ")])


def parse_gpu_index(index: str) -> list[int]:
    return [int(part) for part in str(index).split(",") if part.strip() != ""]


def gpu_capability_report(colmap_bin: str) -> dict:
    """提交时的双卡能力核查报告（全部如实记录，缺失项标 unknown）。"""
    from app.reconstruction.adapter import colmap_has_cuda

    cuda = colmap_has_cuda(colmap_bin)
    requested = parse_gpu_index(get_settings().colmap_gpu_index)
    visible = _visible_gpu_count() if cuda else None
    report: dict = {
        "colmap_has_cuda": cuda,
        "requested_gpu_index": requested,
        "visible_gpu_count": visible,
        "all_requested_gpus_visible": None if visible is None else set(requested).issubset(range(visible)),
    }
    return report


def resolve_job_config(
    preset: str,
    matcher: str,
    enable_dense: bool,
    enable_mesh: bool,
    mode: str,
    filenames: list[str] | None = None,
) -> dict:
    """把请求 + 画像 + 环境一次性解析为冻结配置（含预检摘要与能力报告）。

    返回值整体写入任务的 config_json；执行阶段只读取这份解析结果。
    """
    if preset not in PROFILES:
        raise ValueError(f"未知 preset {preset}")
    settings = get_settings()
    profile = dict(PROFILES[preset])
    if matcher == "auto":
        resolved_matcher, decision = auto_matcher_decision(filenames or [])
        profile["matcher_decision"] = decision
    else:
        resolved_matcher = matcher
    resolved = {
        "max_features": profile["max_features"],
        "poisson_depth": profile["poisson_depth"],
        "matcher": resolved_matcher,
        "dense_max_image_size": settings.colmap_dense_max_image_size,
        "patchmatch_iterations": profile.get("patchmatch_iterations", settings.colmap_patchmatch_iterations),
        "fusion_threads": profile.get("fusion_threads", settings.colmap_fusion_threads),
        "texture_start_scale": profile.get("texture_start_scale", settings.colmap_texture_start_scale),
        "gpu_index": settings.colmap_gpu_index,
        "poisson_threads": 4,
        "poisson_trim": profile.get("poisson_trim", POISSON_TRIM_DEFAULT),
        "colmap_bin": settings.colmap_bin,
        "texturer_bin": settings.colmap_texturer_bin,
        "use_gpu": settings.use_gpu if mode == "colmap" else False,
    }
    return {"resolved": resolved, "matcher_decision": profile.get("matcher_decision")}
