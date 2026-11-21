"""双卡/单卡 N≥3 冷跑矩阵（集成验收 §3 协议）。

每个臂：重启 API/Worker（冻结 COLMAP_GPU_INDEX 到该臂）→ 新项目上传同一组照片 →
standard 画像、auto 匹配、稠密+网格 → 记录阶段耗时（任务事件真实起止）、
产物计数与下载哈希。结果写机器可读 JSON，不做任何估算。

用法：
  python scripts/run_cold_matrix.py --env <环境目录> --base-url http://127.0.0.1:PORT \
      --token-file <token> --scene bridge --images-dir <照片目录> --count 100 \
      --arms dual:3,single:3 --port 18497 --redis 18498 --out <目录>
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import signal
import subprocess
import time
from pathlib import Path

import requests

REPO = Path(__file__).resolve().parents[1]
COLMAP_GPU = "/data/home/2024090904028/.conda/envs/colmap-gpu/bin/colmap"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(4 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _retry(fn, tries=4, backoff=3):
    import time as _time

    for attempt in range(tries):
        try:
            return fn()
        except requests.RequestException as exc:
            if attempt == tries - 1:
                raise
            print(f"  [retry {attempt + 1}/{tries}] {exc}", flush=True)
            _time.sleep(backoff)


class Driver:
    def __init__(self, env: Path, base: str, port: int, redis_port: int, dense_size: int, log: Path):
        self.env = env
        self.base = base.rstrip("/")
        self.port = port
        self.redis_port = redis_port
        self.dense_size = dense_size
        self.log = log

    def _service_env(self, gpu_index: str) -> dict:
        e = os.environ.copy()
        e.update({
            "RECON_ENV_FILE": str(self.env / "empty.env"),
            "DATABASE_URL": f"sqlite:///{self.env / 'app.db'}",
            "DATA_DIR": str(self.env / "data"),
            "SCRATCH_DIR": str(self.env / "scratch"),
            "RECON_MODE": "colmap", "RECON_USE_GPU": "1",
            "COLMAP_BIN": COLMAP_GPU, "COLMAP_GPU_INDEX": gpu_index,
            "COLMAP_DENSE_MAX_IMAGE_SIZE": str(self.dense_size),
            "COLMAP_TEXTURER_BIN": "/tmp/recon-colmap.oUTYXW/env/bin/colmap",
            "REDIS_URL": f"redis://127.0.0.1:{self.redis_port}/0",
            "LOG_LEVEL": "INFO",
        })
        return e

    def restart(self, gpu_index: str) -> None:
        if getattr(self, "no_restart", False):
            return
        for name in ("api.pid", "worker.pid"):
            pid_file = self.env / name
            if pid_file.exists():
                try:
                    os.kill(int(pid_file.read_text().strip()), signal.SIGKILL)
                except (ProcessLookupError, ValueError):
                    pass
        # 臂切换必须确认旧 Worker 真正退出，否则残留的双卡 Worker 会抢走
        # 单卡任务（South 矩阵实测发生过）；循环等待直至进程消失。
        deadline = time.time() + 30
        while time.time() < deadline:
            alive = [pid for pid in os.listdir("/proc") if pid.isdigit()
                     and self._proc_is_worker(pid)]
            if not alive:
                break
            time.sleep(1)
        else:
            raise SystemExit(f"refusing to start: stale workers remain: {alive}")

    def _proc_is_worker(self, pid: str) -> bool:
        """只认本验收环境（REDIS_URL 指向本环境 Redis）的 Worker。

        历史 18082 等服务的 Worker 模块名相同但环境不同，绝不能误杀。
        """
        try:
            with open(f"/proc/{pid}/cmdline", "rb") as f:
                cmd = f.read().decode(errors="replace")
            if "rq_worker" not in cmd and ("rq" not in cmd or "worker" not in cmd):
                return False
            with open(f"/proc/{pid}/environ", "rb") as f:
                env = f.read().decode(errors="replace")
            return f"REDIS_URL=redis://127.0.0.1:{self.redis_port}/0" in env
        except OSError:
            return False
        e = self._service_env(gpu_index)
        with (self.env / "api.log").open("ab") as out:
            api = subprocess.Popen([".venv/bin/uvicorn", "app.main:app", "--port", str(self.port)],
                                   cwd=REPO / "backend", env=e, stdout=out, stderr=subprocess.STDOUT)
        (self.env / "api.pid").write_text(str(api.pid))
        with (self.env / "worker.log").open("ab") as out:
            worker = subprocess.Popen([".venv/bin/rq", "worker", "-u", f"redis://127.0.0.1:{self.redis_port}/0", "recon"],
                                      cwd=REPO / "backend", env=e, stdout=out, stderr=subprocess.STDOUT)
        (self.env / "worker.pid").write_text(str(worker.pid))
        # 启动后轮询健康检查，避免 uvicorn 尚未完成绑定就继续执行
        deadline = time.time() + 30
        last_error = None
        while time.time() < deadline:
            try:
                health = requests.get(f"{self.base}/api/v1/health", timeout=5).json()
                if health.get("status") == "ok":
                    break
            except requests.RequestException as exc:
                last_error = exc
            time.sleep(2)
        else:
            raise SystemExit(f"API did not become healthy: {last_error}")

    def stage_timings(self, job: dict) -> dict:
        out, by_stage = {}, {}
        for e in job.get("events") or []:
            by_stage.setdefault(e["stage"], []).append(e)
        for stage, evs in by_stage.items():
            done = [e for e in evs if e.get("duration_ms") is not None]
            if done:
                out[stage] = round(sum(e["duration_ms"] for e in done) / 1000, 2)
        return out

    def run_arm(self, token: str, gpu_index: str, runs: int, images: list[Path], scene: str,
                preset: str = "standard") -> list[dict]:
        headers = {"Authorization": f"Bearer {token}"}
        self.restart(gpu_index)
        results = []
        for index in range(runs):
            r = _retry(lambda: requests.post(f"{self.base}/api/v1/projects", headers=headers,
                           json={"name": f"冷跑-{scene}-{gpu_index}-{index}-{int(time.time())}"}, timeout=30))
            r.raise_for_status()
            project = r.json()
            handles = [("files", (f.name, open(f, "rb"), "image/jpeg")) for f in images]
            r = requests.post(f"{self.base}/api/v1/projects/{project['id']}/images",
                              headers=headers, files=handles, timeout=1800)
            r.raise_for_status()
            uploaded = r.json()
            t0 = time.time()
            r = requests.post(f"{self.base}/api/v1/projects/{project['id']}/jobs", headers=headers,
                              json={"preset": preset, "matcher": "auto", "enable_dense": True, "enable_mesh": True},
                              timeout=60)
            r.raise_for_status()
            job = r.json()
            job_id = job["id"]
            while True:
                j = _retry(lambda: requests.get(f"{self.base}/api/v1/jobs/{job_id}", headers=headers, timeout=30).json())
                if j["status"] in ("SUCCEEDED", "FAILED", "CANCELLED"):
                    break
                time.sleep(10)
            wall = round(time.time() - t0, 2)
            models = requests.get(f"{self.base}/api/v1/projects/{project['id']}/models",
                                  headers=headers, timeout=30).json()
            artifacts = []
            for m in models:
                if m.get("job_id") != job_id:
                    continue
                dl = requests.get(f"{self.base}/api/v1/models/{m['id']}/download", headers=headers, timeout=600)
                artifacts.append({"model_id": m["id"], "type": m["model_type"], "format": m["format"],
                                  "vertices": m["vertex_count"], "faces": m["face_count"], "points": m["point_count"],
                                  "sha256": hashlib.sha256(dl.content).hexdigest()})
            detail = requests.get(f"{self.base}/api/v1/jobs/{job_id}", headers=headers, timeout=30).json()
            results.append({"run": index + 1, "gpu_index": gpu_index, "job_id": job_id, "status": j["status"],
                            "wall_s": wall, "stages_s": self.stage_timings(j),
                            "resolved": j["config"].get("resolved"), "capability": j["config"].get("capability"),
                            "error": j.get("error_message"), "artifacts": artifacts,
                            "uploaded": {"count": len(uploaded), "bytes": sum(i["file_size"] for i in uploaded)}})
            print(f"[{scene} {gpu_index} run {index + 1}/{runs}] {j['status']} wall={wall:.0f}s", flush=True)
        return results


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env", type=Path, required=True)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--redis", type=int, required=True)
    parser.add_argument("--token-file", type=Path, required=True)
    parser.add_argument("--scene", required=True)
    parser.add_argument("--images-dir", type=Path, required=True)
    parser.add_argument("--count", type=int, required=True)
    parser.add_argument("--arms", default="dual:3,single:3")
    parser.add_argument("--dense-size", type=int, default=2000)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--no-restart", action="store_true", help="服务已就绪时跳过臂间重启")
    parser.add_argument("--preset", default="standard")
    args = parser.parse_args()

    token = args.token_file.read_text().strip()
    images = sorted(args.images_dir.iterdir())[:args.count]
    driver = Driver(args.env.resolve(), args.base_url, args.port, args.redis, args.dense_size, args.out)
    driver.no_restart = args.no_restart
    out: dict = {"scene": args.scene, "images": len(images), "total_bytes": sum(f.stat().st_size for f in images),
                 "preset": args.preset, "matcher": "auto", "dense_max_image_size": args.dense_size,
                 "arms": {}}
    for arm in args.arms.split(","):
        name, runs = arm.split(":")
        gpu_index = "0,1" if name == "dual" else "0"
        out["arms"][name] = driver.run_arm(token, gpu_index, int(runs), images, args.scene, preset=args.preset)
    # 汇总：中位稠密耗时与加速比
    summary = {}
    medians = {}
    for name, runs in out["arms"].items():
        ok = [r for r in runs if r["status"] == "SUCCEEDED"]
        dense = [r["stages_s"]["DENSE_RECONSTRUCTION"] for r in ok if "DENSE_RECONSTRUCTION" in r["stages_s"]]
        wall = [r["wall_s"] for r in ok]
        medians[name] = {"median_dense_s": statistics.median(dense) if dense else None,
                         "median_wall_s": statistics.median(wall) if wall else None,
                         "succeeded": len(ok), "total": len(runs)}
    if medians.get("dual", {}).get("median_dense_s") and medians.get("single", {}).get("median_dense_s"):
        medians["dense_speedup"] = round(medians["single"]["median_dense_s"] / medians["dual"]["median_dense_s"], 3)
    out["summary"] = medians
    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("x", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print("evidence:", args.out)


import statistics  # noqa: E402

if __name__ == "__main__":
    main()
