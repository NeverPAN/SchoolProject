"""集成验收驱动（第二阶段）：主链路验收 + 双卡 N≥3 冷跑矩阵。

前置：scripts/assemble_integrated_acceptance.py 已组装环境；服务已按
docs/集成系统验收标准.md §6 启动（隔离 Redis/API/Worker）。
所有结果写机器可读 JSON（--out），阶段耗时取任务事件真实起止，不做估算。

用法示例：
  python scripts/run_acceptance_benchmark.py --base-url http://127.0.0.1:18497 \
      --token-file <token> --scene south --images-dir /tmp/real-datasets/south-building/south-building/images \
      --main-count 36 --holdout-count 4 --out docs/integrated-acceptance-evidence/south-main.json

  python scripts/run_acceptance_benchmark.py --base-url ... --token-file ... \
      --cold-runs 3 --cold-images-dir <含 N 张照片的目录> --cold-out docs/.../bridge-cold-dual.json
"""
from __future__ import annotations

import argparse
import hashlib
import json
import statistics
import time
from pathlib import Path

import requests


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(4 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


class Client:
    def __init__(self, base: str, token: str):
        self.base = base.rstrip("/")
        self.headers = {"Authorization": f"Bearer {token}"}

    def get(self, path: str):
        r = requests.get(self.base + path, headers=self.headers, timeout=30)
        r.raise_for_status()
        return r.json()

    def post(self, path: str, body: dict | None = None):
        r = requests.post(self.base + path, headers=self.headers, json=body or {}, timeout=60)
        if r.status_code >= 400:
            raise SystemExit(f"POST {path} -> {r.status_code}: {r.text[:300]}")
        return r.json()

    def wait_job(self, job_id: int, timeout_s: int = 7200):
        started = time.time()
        while time.time() - started < timeout_s:
            job = self.get(f"/api/v1/jobs/{job_id}")
            if job["status"] in ("SUCCEEDED", "FAILED", "CANCELLED"):
                return job
            time.sleep(5)
        raise SystemExit(f"job {job_id} timeout")

    def upload(self, project_id: int, files: list[Path]):
        handles = [("files", (f.name, open(f, "rb"), "image/jpeg")) for f in files]
        r = requests.post(f"{self.base}/api/v1/projects/{project_id}/images",
                          headers=self.headers, files=handles, timeout=600)
        r.raise_for_status()
        return r.json()


def stage_timings(job: dict) -> dict:
    out = {}
    events = job.get("events") or []
    by_stage: dict[str, list] = {}
    for e in events:
        by_stage.setdefault(e["stage"], []).append(e)
    for stage, evs in by_stage.items():
        evs.sort(key=lambda e: e["started_at"] or "")
        running = next((e for e in evs if e["status"] == "running"), None)
        done = [e for e in evs if e.get("duration_ms") is not None]
        if done:
            out[stage] = round(sum(e["duration_ms"] for e in done) / 1000, 2)
        elif running and running.get("started_at"):
            pass
    return out


def run_main_scene(args, client: Client, out: dict) -> None:
    project = client.post("/api/v1/projects", {"name": f"集成验收-{args.scene}-{int(time.time())}", "description": ""})
    images = sorted(args.images_dir.iterdir())
    main_files = images[:args.main_count]
    holdout = images[args.main_count:args.main_count + args.holdout_count]
    uploaded = client.upload(project["id"], main_files)
    out["upload"] = {"count": len(uploaded), "bytes": sum(i["file_size"] for i in uploaded),
                     "sha256_head": [i["checksum"][:12] for i in uploaded[:3]]}
    job = client.post(f"/api/v1/projects/{project['id']}/jobs",
                      {"preset": "standard", "matcher": "auto", "enable_dense": True, "enable_mesh": True})
    job = client.wait_job(job["id"])
    out["main_job"] = {"id": job["id"], "status": job["status"], "error": job.get("error_message"),
                       "config": job["config"], "stages_s": stage_timings(job)}
    if job["status"] != "SUCCEEDED":
        raise SystemExit("main job failed; see main_job.error")
    models = client.get(f"/api/v1/projects/{project['id']}/models")
    web = next(m for m in models if m["format"] == "glb")
    out["main_models"] = [{"id": m["id"], "type": m["model_type"], "vertices": m["vertex_count"],
                           "faces": m["face_count"], "points": m["point_count"]} for m in models]

    # ---- 原色贴图资产实测（阶段 B 门槛） ----
    detail = client.get(f"/api/v1/models/{web['id']}")
    out["texture_assets"] = detail.get("texture_assets")

    # ---- D 链（场景 2）：留出照片注册 → 局部融合 → 网格补丁 ----
    held = client.upload(project["id"], holdout)
    out["holdout_upload"] = [{"id": i["id"], "name": i["original_filename"]} for i in held]
    dense = next(m for m in models if m["model_type"] == "dense_pointcloud")
    manifest = client.get(f"/api/v1/models/{dense['id']}/manifest")
    outputs = manifest.get("manifest", {}).get("outputs") or []
    ply = Path(outputs[0]) if outputs else None
    bounds = cloud_bounds_ply(ply) if ply and ply.is_file() else None
    if bounds is None:
        raise SystemExit("dense cloud path unavailable for region selection")
    lo, hi = central_third(bounds)
    region = client.post(f"/api/v1/models/{web['id']}/regions",
                         {"bbox_min": lo, "bbox_max": hi, "note": "集成验收自动选区（稠密点云中央三分之一）"})
    ljob = client.post(f"/api/v1/models/{web['id']}/local-reconstruction",
                       {"region_id": region["id"], "image_ids": [i["id"] for i in held]})
    ljob = client.wait_job(ljob["id"])
    out["local_general_job"] = {"id": ljob["id"], "status": ljob["status"], "error": ljob.get("error_message"),
                                "stages_s": stage_timings(ljob)}
    if ljob["status"] == "SUCCEEDED":
        candidate = next((m for m in client.get(f"/api/v1/projects/{project['id']}/models")
                          if m.get("stage_origin") == "LOCAL_POINT_FUSION"), None)
        out["local_candidate"] = {"id": candidate["id"], "points": candidate["point_count"]}
        mjob = client.post(f"/api/v1/models/{candidate['id']}/local-mesh")
        mjob = client.wait_job(mjob["id"])
        out["local_mesh_job"] = {"id": mjob["id"], "status": mjob["status"], "error": mjob.get("error_message"),
                                 "stages_s": stage_timings(mjob)}
    out["holdout_names"] = [f.name for f in holdout]


def cloud_bounds_ply(path: Path):
    import numpy as np

    from app.reconstruction.mesh_quality import read_binary_ply_xyz

    xyz = read_binary_ply_xyz(path)
    return xyz.min(axis=0).tolist(), xyz.max(axis=0).tolist()


def central_third(bounds):
    import numpy as np

    lo, hi = np.asarray(bounds[0]), np.asarray(bounds[1])
    size = (hi - lo) / 3
    return (lo + size * 0.8).tolist(), (hi - size * 0.8).tolist()


def run_cold_matrix(args, client: Client, out: dict) -> None:
    project = client.post("/api/v1/projects", {"name": f"冷跑-{args.scene}-{int(time.time())}", "description": ""})
    files = sorted(args.cold_images_dir.iterdir())[:args.cold_count]
    uploaded = client.upload(project["id"], files)
    image_ids = [i["id"] for i in uploaded]
    runs = []
    for index in range(args.cold_runs):
        job = client.post(f"/api/v1/projects/{project['id']}/jobs",
                          {"preset": args.preset, "matcher": "auto", "enable_dense": True, "enable_mesh": True})
        t0 = time.time()
        job = client.wait_job(job["id"])
        wall = time.time() - t0
        models = client.get(f"/api/v1/projects/{project['id']}/models")
        runs.append({
            "run": index + 1, "job_id": job["id"], "status": job["status"],
            "wall_s": round(wall, 2), "stages_s": stage_timings(job),
            "resolved": job["config"].get("resolved"), "capability": job["config"].get("capability"),
            "error": job.get("error_message"),
            "models": [{"type": m["model_type"], "faces": m["face_count"], "points": m["point_count"]}
                       for m in models if m["job_id"] == job["id"]],
        })
        print(f"[cold {index + 1}/{args.cold_runs}] {job['status']} wall={wall:.0f}s")
    out["cold_runs"] = runs
    ok = [r for r in runs if r["status"] == "SUCCEEDED"]
    if ok:
        dense_med = statistics.median([r["stages_s"]["DENSE_RECONSTRUCTION"] for r in ok if "DENSE_RECONSTRUCTION" in r["stages_s"]] or [None])
        wall_med = statistics.median([r["wall_s"] for r in ok])
        out["summary"] = {"median_wall_s": round(wall_med, 2), "median_dense_s": dense_med,
                          "gpu_index": ok[0]["resolved"].get("gpu_index"), "succeeded": len(ok), "total": len(runs)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--token-file", type=Path, required=True)
    parser.add_argument("--scene", required=True)
    parser.add_argument("--mode", choices=["main", "cold"], required=True)
    parser.add_argument("--images-dir", type=Path)
    parser.add_argument("--main-count", type=int, default=36)
    parser.add_argument("--holdout-count", type=int, default=4)
    parser.add_argument("--cold-images-dir", type=Path)
    parser.add_argument("--cold-count", type=int, default=40)
    parser.add_argument("--cold-runs", type=int, default=3)
    parser.add_argument("--preset", default="standard")
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    token = args.token_file.read_text().strip()
    client = Client(args.base_url, token)
    out: dict = {"scene": args.scene, "mode": args.mode, "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    if args.mode == "main":
        run_main_scene(args, client, out)
    else:
        run_cold_matrix(args, client, out)
    out["finished_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("x", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print("evidence:", args.out)


if __name__ == "__main__":
    main()
