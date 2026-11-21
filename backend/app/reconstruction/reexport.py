"""Create the job #7 display derivative without replacing native artifacts."""
from __future__ import annotations

import json
from pathlib import Path
from tempfile import TemporaryDirectory

from sqlalchemy import select

from app.db.session import SessionLocal
from app.models.entities import Model, ModelVersion
from app.reconstruction.export import mesh_to_glb
from app.reconstruction.orientation import estimate_display_up


def main() -> None:
    with SessionLocal() as db:
        old = db.get(Model, 4)
        if old is None or (old.project_id, old.job_id, old.model_type) != (7, 7, "web"):
            raise ValueError("演示项目 #7 的原始 GLB 不存在")
        out = Path(old.file_path).parent
        destination = out / "model-web-v2.glb"
        existing = db.scalar(select(Model).where(Model.job_id == 7, Model.file_path == str(destination)))
        if existing and destination.is_file():
            print(existing.id)
            return
        if existing or destination.exists():
            raise RuntimeError("修正版文件或登记不完整，请检查现有产物")
        manifest_path = out / "job_manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if manifest.get("job_id") != 7:
            raise ValueError("任务清单与示范任务不一致")
        from app.services.image_service import project_job_dir

        checkpoint = project_job_dir(old.project, 7) / "checkpoints" / "SPARSE_RECONSTRUCTION" / "model"
        up = estimate_display_up(checkpoint / "images.bin")
        if up is None:
            raise ValueError("未取得一致的相机上方向")
        manifest.update(display_up=up, mesh_glb_color_space="srgb")
        manifest.setdefault("display_derivatives", {})[destination.name] = {
            "source": "mesh.ply", "vertex_color_space": "linear", "geometry_unchanged": True,
        }
        with TemporaryDirectory(dir=out) as temp:
            staged = Path(temp) / destination.name
            stats = mesh_to_glb(out / "mesh.ply", staged)
            staged_manifest = Path(temp) / manifest_path.name
            staged_manifest.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
            model = Model(project_id=7, job_id=7, model_type="web", format="glb", file_path=str(destination),
                          file_size=staged.stat().st_size, stage_origin="WEB_REEXPORT", **stats)
            db.add(model)
            db.flush()
            old_version = db.scalar(select(ModelVersion).where(ModelVersion.model_id == old.id)
                                    .order_by(ModelVersion.version_number.desc()))
            db.add(ModelVersion(project_id=7, model_id=model.id, version_number=2,
                                parent_version_id=old_version.id if old_version else None,
                                change_type="manual", change_description="从 mesh.ply 重新导出：平滑法线、双面材质、线性顶点色"))
            staged.rename(destination)
            try:
                db.commit()
            except Exception:
                destination.unlink()
                raise
            staged_manifest.replace(manifest_path)
        print(model.id)


if __name__ == "__main__":
    main()
