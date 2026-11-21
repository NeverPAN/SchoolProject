#!/usr/bin/env python
"""全程资源采样：从任务提交到终态，覆盖阶段 6 要求的完整过程（非中途采样）。

每 15s 采样：各 GPU 显存/利用率、colmap 子进程 RSS 总和与 CPU、任务工作目录大小（60s 一次）。
输出 JSONL；任务进入终态（SUCCEEDED/FAILED/CANCELLED）后停止。
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path


def gpu_stats() -> list[dict]:
    out = subprocess.run(
        ["nvidia-smi", "--query-gpu=index,memory.used,utilization.gpu",
         "--format=csv,noheader,nounits"], capture_output=True, text=True)
    rows = []
    for line in out.stdout.strip().splitlines():
        idx, mem, util = [x.strip() for x in line.split(",")]
        rows.append({"index": int(idx), "mem_mib": int(mem), "util_pct": int(util)})
    return rows


def colmap_procs() -> dict:
    out = subprocess.run(["ps", "-eo", "pid,comm,rss,pcpu", "--no-headers"],
                         capture_output=True, text=True)
    procs = [p for p in out.stdout.splitlines() if "colmap" in p.lower()]
    rss = sum(int(p.split()[2]) for p in procs if len(p.split()) >= 3)
    cpu = sum(float(p.split()[3]) for p in procs if len(p.split()) >= 4)
    return {"colmap_proc_count": len(procs), "colmap_rss_mib": round(rss / 1024, 1),
            "colmap_cpu_pct": round(cpu, 1)}


def dir_size(path: Path) -> int:
    out = subprocess.run(["du", "-sb", str(path)], capture_output=True, text=True)
    return int(out.stdout.split()[0]) if out.stdout.strip() else 0


def job_status(db: str, job_id: int) -> str | None:
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=5)
        row = con.execute("SELECT status FROM reconstruction_jobs WHERE id=?", (job_id,)).fetchone()
        con.close()
        return row[0] if row else None
    except sqlite3.OperationalError:
        return None


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True)
    ap.add_argument("--job-id", type=int, required=True)
    ap.add_argument("--work-dir", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--interval", type=float, default=15.0)
    args = ap.parse_args()

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    work = Path(args.work_dir)
    last_dir_size = 0
    with out.open("a") as f:
        while True:
            rec = {"ts": datetime.now(timezone.utc).isoformat(), "job_status": job_status(args.db, args.job_id),
                   "gpus": gpu_stats(), **colmap_procs()}
            if rec["job_status"] in ("SUCCEEDED", "FAILED", "CANCELLED", None):
                rec["work_dir_bytes"] = dir_size(work) if work.exists() else 0
                f.write(json.dumps(rec) + "\n")
                f.flush()
                break
            if time.time() - getattr(main, "_last_du", 0) >= 60:
                last_dir_size = dir_size(work) if work.exists() else 0
                main._last_du = time.time()
            rec["work_dir_bytes"] = last_dir_size
            f.write(json.dumps(rec) + "\n")
            f.flush()
            time.sleep(args.interval)
    print("sampling finished ->", out)


if __name__ == "__main__":
    main()
