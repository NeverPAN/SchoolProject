"""集成验收环境组装（受信任操作员工具，一次性组装 + 哈希钉扎）。

用途：按 docs/集成系统验收标准.md 在隔离目录装配真实 GPU 验收环境。
父工件（Bridge R01/#24、C01 检查点）来自既有验收目录，全部 SHA-256 钉扎；
任何哈希不符立即拒绝。父任务/模型/选区行是操作员登记（带 provenance manifest），
不冒充正常运行；此后的一切验收走真实 HTTP + Redis/RQ + GPU。

用法：python scripts/assemble_integrated_acceptance.py --target <新目录> --scene bridge
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

PINS = {
    # 父模型/父点云沿用 #28 发布清单的哈希（同一字节）；稀疏检查点钉到 C01 任务
    # #2 自身快照的实际内容（#28 的 seed 是含 110 视图的实验副本，不是通用父工件）。
    "parent_model": "a5d4ca3a5c87c9deeb3bc78c72554bf652474887adb324c8d231857696964f9d",
    "parent_cloud": "f96be7d6b2e9c9a0c8d7c9839672bb71f8294109db2124e44cdeb46739460f37",
    "sparse": None,  # 组装时按 C01 检查点实测哈希钉扎
}
SOURCES = {
    "parent_model": Path("/data/home/2024090904028/reconstration/local-review.YNQSrQ/data/users/1/projects/1/outputs/job-11/model_photo.glb"),
    "parent_cloud": Path("/tmp/recon-combined.NRRNmN/scratch/users/1/projects/2/jobs/2/checkpoints/MESHING/fused-filtered.ply"),
    "sparse_dir": Path("/tmp/recon-combined.NRRNmN/scratch/users/1/projects/2/jobs/2/checkpoints/SPARSE_RECONSTRUCTION/model"),
    "combined_db": Path("/tmp/recon-combined.NRRNmN/app.db"),
    "combined_input_original": Path("/tmp/recon-combined.NRRNmN/data/users/1/projects/2/input/original"),
    "new_photos_dir": Path("/data/home/2024090904028/reconstration/local-review.YNQSrQ/data/users/1/projects/1/input/original"),
    "new_view_sha256": {  # 来自 #28 job manifest：三张已校验新增原片
        "DSC_1290.JPG": None, "DSC_1291.JPG": None, "DSC_1292.JPG": None,
    },
}
REGION_BOUNDS = {"min": [6.48, -0.75, 2.46], "max": [7.95, -0.38, 3.1700000000000004]}


def sha256_file(path: Path) -> str:
    import hashlib

    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(4 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def require(condition: bool, message: str) -> None:
    if not condition:
        raise SystemExit(f"REFUSED: {message}")


def resolve_new_photo_pins() -> dict[str, str]:
    manifest = json.loads((Path("/data/home/2024090904028/reconstration/local-review.YNQSrQ")
                           / "data/users/1/projects/1/outputs/job-15/job_manifest.json").read_text())
    views = manifest["view_sha256"]
    out = {}
    for name, digest in views.items():
        if name.startswith("new_"):
            original = name.removeprefix("new_")
            out[original] = digest
    require(len(out) == 3, f"expected 3 pinned new photos, got {sorted(out)}")
    return out


def assemble(target: Path) -> dict:
    # 安全栅栏：在导入任何 app/alembic 模块之前钉死数据库与存储路径。
    # migrations/env.py 用 get_settings().database_url；漏设会作用到默认历史库。
    os.environ["DATABASE_URL"] = f"sqlite:///{target.resolve() / 'app.db'}"
    os.environ["DATA_DIR"] = str(target.resolve() / "data")
    os.environ["SCRATCH_DIR"] = str(target.resolve() / "scratch")
    os.environ["RECON_ENV_FILE"] = str(target.resolve() / "empty.env")
    require(not target.exists(), f"target {target} already exists")
    require(SOURCES["parent_model"].is_file() and SOURCES["parent_cloud"].is_file(), "parent artifacts missing")
    require(SOURCES["sparse_dir"].is_dir(), "parent sparse checkpoints missing")
    (target / "data").mkdir(parents=True)
    (target / "scratch").mkdir()
    pins = {"new_photos": resolve_new_photo_pins()}
    pins.update({k: v for k, v in PINS.items() if k != "sparse"})
    pins["sparse"] = {}

    # ---- 父工件复制 + 钉扎 ----
    outputs = target / "data" / "users" / "1" / "projects" / "1" / "outputs" / "job-1"
    outputs.mkdir(parents=True)
    parent_glb = outputs / "model_photo.glb"
    shutil.copyfile(SOURCES["parent_model"], parent_glb)
    require(sha256_file(parent_glb) == PINS["parent_model"], "parent GLB hash mismatch")
    cloud = target / "scratch" / "users" / "1" / "projects" / "1" / "jobs" / "1" / "checkpoints" / "MESHING" / "fused-filtered.ply"
    cloud.parent.mkdir(parents=True)
    shutil.copyfile(SOURCES["parent_cloud"], cloud)
    require(sha256_file(cloud) == PINS["parent_cloud"], "parent fused cloud hash mismatch")
    sparse_dir = cloud.parents[1] / "SPARSE_RECONSTRUCTION" / "model"
    sparse_dir.mkdir(parents=True)
    for name in ("cameras.bin", "images.bin", "points3D.bin", "rigs.bin", "frames.bin"):
        source = SOURCES["sparse_dir"] / name
        require(source.is_file(), f"sparse checkpoint {name} missing")
        shutil.copyfile(source, sparse_dir / name)
        pins["sparse"][name] = sha256_file(sparse_dir / name)

    # ---- 父任务输入清单（来自 C01 任务 #2 的冻结清单，重定向到本环境并核对哈希） ----
    combined = sqlite3.connect(f"file:{SOURCES['combined_db']}?mode=ro", uri=True)
    row = combined.execute("select input_manifest from reconstruction_jobs where id=2").fetchone()
    require(row is not None, "combined C01 job #2 not found")
    manifest = json.loads(row[0])
    images = manifest["images"]
    require(len(images) == 100, f"expected 100 frozen parent entries, got {len(images)}")
    originals = target / "data" / "users" / "1" / "projects" / "1" / "input" / "original"
    originals.mkdir(parents=True)
    for index, entry in enumerate(images):
        source = Path(entry["stored_path"])
        require(source.is_file(), f"parent original missing: {source.name}")
        require(sha256_file(source) == entry["checksum"], f"parent original hash mismatch: {source.name}")
        dest = originals / source.name
        shutil.copyfile(source, dest)
        entry["stored_path"] = str(dest.resolve())
        entry["image_id"] = index + 1  # 本环境登记 ID 从 1 起，顺序不变
    combined.close()
    pins["parent_manifest_entries"] = len(images)

    # ---- 数据库行：操作员登记父任务/模型/选区（provenance 写入 task） ----
    db_path = target / "app.db"
    conn = sqlite3.connect(db_path)
    sys.path.insert(0, str(ROOT / "backend"))
    from alembic.config import Config
    from alembic import command

    alembic_cfg = Config(str(ROOT / "backend" / "alembic.ini"))
    alembic_cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    alembic_cfg.set_main_option("script_location", str(ROOT / "backend" / "migrations"))
    assert alembic_cfg.get_main_option("sqlalchemy.url") == f"sqlite:///{db_path}"
    command.upgrade(alembic_cfg, "head")
    conn.close()

    from app.db.session import SessionLocal
    from app.models.entities import (Image, Model, ModelRegion, ModelVersion, Project,
                                     ReconstructionJob, User)
    from app.core.security import hash_password
    from app.db.base import utcnow_db

    now = utcnow_db()
    with SessionLocal() as db:
        db.add(User(id=1, username="acceptance", password_hash=hash_password("acceptance-bridge-2026"), role="user",
                    created_at=now))
        db.flush()
        db.add(Project(id=1, name="集成验收-Bridge", owner_id=1, created_at=now, updated_at=now))
        db.flush()
        for entry in images:
            db.add(Image(project_id=1, original_filename=entry["filename"],
                         stored_path=entry["stored_path"], file_size=Path(entry["stored_path"]).stat().st_size,
                         width=entry["width"], height=entry["height"], format="jpg",
                         checksum=entry["checksum"], quality_status="ok", created_at=now))
        db.flush()
        job = ReconstructionJob(id=1, project_id=1, status="SUCCEEDED", pipeline_mode="colmap",
                                started_at=now, finished_at=now,
                                config_json=json.dumps({"preset": "standard", "matcher": "auto", "enable_dense": True,
                                                        "enable_mesh": True, "use_gpu": True, "mode": "colmap",
                                                        "provenance": "operator-registered C01 job #2 snapshot for acceptance"}),
                                input_manifest=json.dumps({"images": images, "image_count": len(images)}),
                                engine_version="COLMAP 3.13.0 -- Structure-from-Motion and Multi-View Stereo",
                                created_at=now)
        db.add(job)
        db.flush()
        db.add(Model(id=1, project_id=1, job_id=1, model_type="web", format="glb",
                     file_path=str(parent_glb.resolve()), file_size=parent_glb.stat().st_size,
                     vertex_count=307895, face_count=449617, point_count=0,
                     stage_origin="PHOTO_TEXTURE_UPRIGHT", created_at=now))
        db.flush()
        db.add(ModelVersion(project_id=1, model_id=1, version_number=1, parent_version_id=None,
                            change_type="manual", change_description="操作员登记：C01/R01 检查点快照，用于集成验收",
                            created_at=now))
        db.flush()
        db.add(ModelRegion(model_id=1, user_id=1, bbox_min_x=REGION_BOUNDS["min"][0], bbox_min_y=REGION_BOUNDS["min"][1],
                           bbox_min_z=REGION_BOUNDS["min"][2], bbox_max_x=REGION_BOUNDS["max"][0],
                           bbox_max_y=REGION_BOUNDS["max"][1], bbox_max_z=REGION_BOUNDS["max"][2],
                           note="继承 #28 验证选区（哈希钉扎）", created_at=now))
        db.commit()
        parent_model_id = 1

    # ---- 三张新增原片候选文件（提交时经 API 真实上传） ----
    uploads = target / "upload-candidates"
    uploads.mkdir()
    by_digest = {sha256_file(f): f for f in SOURCES["new_photos_dir"].iterdir() if f.is_file()}
    for name, digest in pins["new_photos"].items():
        source = by_digest.get(digest)
        require(source is not None, f"new photo {name} source missing (digest {digest[:12]})")
        require(sha256_file(source) == digest, f"new photo {name} hash mismatch")
        shutil.copyfile(source, uploads / name)

    (target / "empty.env").write_text("")
    pins_path = target / "acceptance-pins.json"
    pins_path.write_text(json.dumps({"pins": pins, "region": REGION_BOUNDS, "parent_model_id": parent_model_id,
                                     "manifest_entries": len(images)}, ensure_ascii=False, indent=2))
    (target / "provenance.json").write_text(json.dumps({
        "assembler": "scripts/assemble_integrated_acceptance.py",
        "parent_provenance": "C01 job #2 / R01 #24 snapshots; all hashes pinned in acceptance-pins.json",
        "operator_rows": "parent job/model/region registered by operator; later acceptance runs are real HTTP+RQ+GPU",
    }, ensure_ascii=False, indent=2))
    return {"target": str(target), "pins": str(pins_path), "parent_model_id": parent_model_id,
            "uploads": str(uploads), "db": str(db_path)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target", type=Path, required=True)
    args = parser.parse_args()
    result = assemble(args.target.resolve())
    print(json.dumps(result, ensure_ascii=False, indent=2))
    print(f"环境就绪：{time.strftime('%Y-%m-%d %H:%M:%S')}")


if __name__ == "__main__":
    main()
