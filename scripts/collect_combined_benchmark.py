"""Collect two completed isolated Bridge runs, evaluate identical views, and prepare an archive catalog."""
from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from hole_diagnosis import read_images_bin

EXPECTED_IMAGES = Path(__file__).resolve().parents[1] / "docs/phase2-evidence/bridge100.sha256"


def read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def write(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def digest(path):
    with path.open("rb") as stream:
        h = hashlib.sha256()
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(chunk)
        return h.hexdigest()


def collect(root: Path, out: Path):
    evidence, catalog, names, inputs = {}, [], [], []
    with sqlite3.connect(f"file:{root / 'app.db'}?mode=ro", uri=True) as db:
        db.row_factory = sqlite3.Row
        for arm, code, expected in (("baseline", "C00", (5, -1, 0.5)), ("optimized", "C01", (3, 16, 0.125))):
            summary_path, = (root / arm).glob("e2e-summary-job*.json")
            summary = read(summary_path)
            job = db.execute("SELECT * FROM reconstruction_jobs WHERE id=?", (summary["job_id"],)).fetchone()
            project = db.execute("SELECT * FROM projects WHERE id=?", (job["project_id"],)).fetchone()
            if job["status"] != "SUCCEEDED" or not summary["all_stages_ok"]:
                raise ValueError(f"Incomplete run: {arm}")
            key = Path("users") / str(project["owner_id"]) / "projects" / str(project["id"])
            work = root / "scratch" / key / "jobs" / str(job["id"])
            output = root / "data" / key / "outputs" / f"job-{job['id']}"
            manifest = read(output / "job_manifest.json")
            params = manifest["resolved_params"]
            if tuple(params[k] for k in ("patchmatch_iterations", "fusion_threads", "texture_start_scale")) != expected:
                raise ValueError(f"Unexpected parameters: {arm}")
            inputs.append([(v["filename"], v["checksum"]) for v in json.loads(job["input_manifest"])["images"]])
            registered = sorted(x["name"] for x in read_images_bin(work / "work/dense/sparse/images.bin"))
            names.append(registered)
            photo = manifest["photo_texture"]
            if photo["status"] != "completed":
                raise ValueError(f"No successful photo texture: {arm}")
            files = []
            for m in db.execute("SELECT * FROM models WHERE job_id=? ORDER BY id", (job["id"],)):
                path = Path(m["file_path"])
                if path.parent != output or path.stat().st_size != m["file_size"]:
                    raise ValueError(f"Unexpected artifact: {path}")
                files.append(dict(path=str(path.relative_to(root)), type=m["model_type"], origin=m["stage_origin"],
                                  sha256=digest(path), source_model_id=m["id"]))
            textured, = (x for x in files if x["origin"] in ("PHOTO_TEXTURE", "PHOTO_TEXTURE_UPRIGHT"))
            if textured["sha256"] != digest(Path(summary["download_file"])):
                raise ValueError("Download does not match worker output")
            stages = [dict(row) for row in db.execute("SELECT stage,status,duration_ms,message FROM job_stage_events WHERE job_id=? ORDER BY id", (job["id"],))]
            wall = (datetime.fromisoformat(job["finished_at"]) - datetime.fromisoformat(job["started_at"])).total_seconds()
            queued = (datetime.fromisoformat(job["started_at"]) - datetime.fromisoformat(job["created_at"])).total_seconds()
            evidence[arm] = dict(job=job["id"], parameters=params, wall_seconds=wall, queue_seconds=queued,
                                 stages=stages, registered=len(registered), photo_texture=photo,
                                 photo_sha256=textured["sha256"], work=work, output=output)
            catalog.append(dict(code=code, name=job["experiment_name"].split(" · ", 1)[-1],
                                parent="B00" if arm == "baseline" else "C00", scope="Bridge 100 张完整冷跑",
                                source={"service": "isolated combined benchmark", "job_id": job["id"], "input_sha256": manifest["inputs_sha"]},
                                metrics={"wall_seconds": wall, "atlas_size": photo["atlas_size"], "textured_faces": photo["textured_faces"]}, parameters=params,
                                display={k: manifest[k] for k in ("display_up", "mesh_glb_color_space")}, files=files))
    original = {name: sha for sha, name in (line.split(maxsplit=1) for line in EXPECTED_IMAGES.read_text().splitlines())}
    if (len(inputs[0]) != 100 or dict(inputs[0]) != original or inputs[0] != inputs[1]
            or len(names[0]) != 100 or names[0] != names[1]):
        raise ValueError("The runs do not have matching images and registrations")
    out.mkdir(parents=True, exist_ok=True)
    views = out / "views.json"
    write(views, [names[0][i * 100 // 12] for i in range(12)])
    for arm, record in evidence.items():
        work, output = record.pop("work"), record.pop("output")
        mesh, cloud = output / "mesh.ply", work / "checkpoints/MESHING/fused-filtered.ply"
        if not cloud.is_file():
            cloud = output / "dense_pointcloud.ply"
        target = out / arm
        subprocess.run([sys.executable, str(Path(__file__).with_name("hole_diagnosis.py")),
                        "--job-dir", str(work), "--cloud", str(cloud), "--mesh-default", str(mesh),
                        "--mesh-candidate", str(mesh), "--camera-list", str(views), "--work-scale", "1000",
                        "--point-radius", "2", "--support-dilate", "5", "--strips", "4", "--out", str(target)], check=True)
        record["projection"] = read(target / "diagnosis.json")
        samples = [json.loads(line) for line in (root / arm / "resources.jsonl").read_text().splitlines()]
        record["resources"] = dict(samples=len(samples), first_status=samples[0]["job_status"], last_status=samples[-1]["job_status"],
                                   max_rss_mib=max(s["colmap_rss_mib"] for s in samples),
                                   max_work_bytes=max(s["work_dir_bytes"] for s in samples),
                                   gpu_max_mib={str(i): max(g["mem_mib"] for s in samples for g in s["gpus"] if g["index"] == i) for i in (0, 1)},
                                   gpu_mean_percent={str(i): sum(g["util_pct"] for s in samples for g in s["gpus"] if g["index"] == i) / len(samples) for i in (0, 1)})
    write(out / "results.json", evidence)
    write(out / "catalog.json", catalog)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args()
    collect(args.runtime.resolve(), args.out.resolve())
