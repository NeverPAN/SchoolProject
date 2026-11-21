"""Local worker orchestration without CUDA; real GPU validation uses archived seed assets."""
import hashlib
import struct
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from app.models.entities import Model, ModelRegion
from app.reconstruction import local_reconstruction as local
from app.reconstruction.adapter import CancelledError
from app.reconstruction.pipeline import PipelineError, StageCancelled


def _vertices(xs):
    array = np.zeros(len(xs), dtype=local.VERTEX)
    array["x"], array["z"], array["nz"], array["red"] = xs, 1, 1, 23
    return array


def _spec(path):
    return {"path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}


@pytest.fixture
def worker(tmp_path, monkeypatch):
    assets = tmp_path / "assets"
    work = tmp_path / "work"
    out = tmp_path / "out"
    for p in (assets, work, out):
        p.mkdir()
    parent = assets / "parent.glb"
    parent.write_bytes(b"fixture: parent path and digest")
    cloud = assets / "parent.ply"
    cloud_data = _vertices([0, 0.5])
    local._write_ply(cloud, cloud_data, cloud_data[:0], lambda: None)
    lo, hi = [-0.2, -0.2, 0.8], [0.2, 0.2, 1.2]
    sparse = {}
    for name in local.SPARSE_FILES:
        p = assets / name
        p.write_bytes(name.encode())
        sparse[name] = _spec(p)
    names = [f"old-{i}.jpg" for i in range(2)] + [f"new-{i}.jpg" for i in range(3)]
    photos = {}
    for name in names:
        p = assets / name
        p.write_bytes(name.encode())
        photos[name] = _spec(p)
    old = {name: photos[name] for name in names[:2]}
    new = {name: i for i, name in enumerate(names[2:], 1)}
    seed = dict(version=1, target_model_id=24, parent_model=_spec(parent), parent_cloud=_spec(cloud),
                sparse=sparse, old_views=old, new_views=new, bbox_min=lo.copy(), bbox_max=hi.copy(),
                new_view_sha256={n: photos[n]["sha256"] for n in new})
    config = dict(seed=seed, target_model_id=24, region_id=1, bounds={"min": lo.copy(), "max": hi.copy()})
    inputs = {"images": [dict(image_id=i, stored_path=photos[n]["path"], checksum=photos[n]["sha256"])
                         for n, i in new.items()]}
    model = SimpleNamespace(project_id=1, job_id=7, file_path=str(parent))
    region = SimpleNamespace(model_id=24, user_id=2, **{
        f"bbox_{end}_{axis}": value for end, bounds in (("min", lo), ("max", hi)) for axis, value in zip("xyz", bounds)
    })

    @contextmanager
    def session():
        yield SimpleNamespace(get=lambda cls, id: {(Model, 24): model, (ModelRegion, 1): region}.get((cls, id)))

    ctx = SimpleNamespace(job_id=99, project_id=1, owner_id=2, work_dir=work, out_dir=out,
                          input_manifest=inputs, session_factory=session, gpu_index="0", fusion_threads=2,
                          log_file=lambda stage: work / stage, job_log=lambda _: None, events=[], cancelled=False)
    ctx.set_stage_running = lambda s: ctx.events.append((s, "running"))
    ctx.set_stage_result = lambda s, result: ctx.events.append((s, result))
    ctx.cancel_check = lambda: ctx.cancelled

    def check_cancel(stage):
        if ctx.cancel_check():
            raise StageCancelled(stage)

    ctx.check_cancel = check_cancel

    class FakeAdapter:
        version = "COLMAP 3.13.0"
        gpu_index = "0"
        engine_identity = {"version": version, "use_gpu": True}
        cancel_dense = False
        alter_parent = False

        def run_command(self, args, log, cancel):
            assert args[0] == "image_undistorter"
            images = Path(args[args.index("--image_path") + 1])
            assert set(p.name for p in images.iterdir()) == set(names)
            assert all((images / n).read_bytes() == n.encode() for n in names)
            dense = Path(args[args.index("--output_path") + 1])
            for p in (dense / "images", dense / "sparse", dense / "stereo" / "depth_maps"):
                p.mkdir(parents=True, exist_ok=True)
            (dense / "sparse" / "images.bin").write_bytes(struct.pack("<Q", len(names)) + b"".join(
                struct.pack("<i7di", i, 1, 0, 0, 0, 0, 0, 0, 1) + n.encode() + b"\0" + struct.pack("<Q", 0)
                for i, n in enumerate(names, 1)
            ))
            (dense / "sparse" / "cameras.bin").write_bytes(struct.pack("<QiiQQ4d", 1, 1, 1, 100, 100, 100, 100, 50, 50))
            for n in names:
                (dense / "images" / n).touch()

        def dense_stereo(self, dense, log, cancel, *, max_image_size, num_iterations):
            assert max_image_size == 1600 and num_iterations == 5
            lines = (dense / "stereo" / "patch-match.cfg").read_text().splitlines()
            assert set(lines[::2]) == set(names)
            for ref, sources in zip(lines[::2], lines[1::2]):
                assert set(sources.split(", ")) == set(names) - {ref}
            if self.cancel_dense:
                ctx.cancelled = True
                raise CancelledError("cancelled during GPU work")
            for n in new:
                data = np.zeros((100, 100), dtype="<f4")
                data[50, [50, 51, 59, 69]] = 1
                # Second plausible point (x=-.012) is not supported by any new depth map.
                (dense / "stereo" / "depth_maps" / f"{n}.geometric.bin").write_bytes(b"100&100&1&" + data.tobytes())

        def stereo_fusion(self, dense, fused, log, cancel, *, max_image_size, num_threads):
            assert (dense / "stereo" / "fusion.cfg").read_text().splitlines() == sorted(names)
            data = _vertices([.012, -.012, .002, .095, .195])
            local._write_ply(fused, data, data[:0], lambda: None)
            if self.alter_parent:
                parent.write_bytes(b"changed externally during processing")

    adapter = FakeAdapter()
    monkeypatch.setattr(local, "_adapter", lambda _: adapter)
    return SimpleNamespace(ctx=ctx, config=config, seed=seed, parent=parent, cloud=cloud,
                           adapter=adapter, cloud_data=cloud_data, region=region)


def test_local_reconstruction_appends_only_supported_guarded_points(worker):
    records, manifest = local.run(worker.ctx, worker.config)
    record, = records
    assert (record["model_type"], record["format"], record["stage_origin"]) == ("dense_pointcloud", "ply", "LOCAL_POINT_FUSION")
    assert record["point_count"] == 3 and record["face_count"] == 0
    out = Path(record["file_path"])
    child = local._read_ply(out)
    assert child[:2].tobytes() == worker.cloud_data.tobytes()
    assert child[2]["x"] == pytest.approx(.012)
    assert _spec(out)["sha256"] == manifest["output_sha256"]
    assert manifest["target_model_id"] == manifest["parent_model_id"] == 24
    assert manifest["geometry"] == "pointcloud_only"
    assert manifest["counts"] == dict(candidate_points_in_core=4, new_view_supported_points=3, added_points=1, parent_points=2)
    assert _spec(worker.parent) == worker.seed["parent_model"] and _spec(worker.cloud) == worker.seed["parent_cloud"]
    assert [(s, state) for s, state in worker.ctx.events if state == "completed"] == [
        (s, "completed") for s in ("IMAGE_VALIDATION", "IMAGE_UNDISTORTION", "DENSE_RECONSTRUCTION", "STEREO_FUSION", "EXPORTING")
    ]


@pytest.mark.parametrize("mutation", ["source", "parent", "sparse", "digest", "uploads", "region", "bounds"])
def test_worker_refuses_changed_or_unbound_frozen_inputs(worker, mutation):
    if mutation == "source":
        Path(worker.ctx.input_manifest["images"][0]["stored_path"]).write_bytes(b"replaced after submission")
    elif mutation == "parent":
        worker.parent.write_bytes(b"replaced after submission")
    elif mutation == "sparse":
        Path(worker.seed["sparse"]["points3D.bin"]["path"]).write_bytes(b"replaced after submission")
    elif mutation == "digest":
        worker.ctx.input_manifest["images"][0]["checksum"] = "0" * 64
    elif mutation == "uploads":
        worker.ctx.input_manifest["images"].pop()
    elif mutation == "region":
        worker.region.model_id = 23
    elif mutation == "bounds":
        worker.config["bounds"]["min"][0] -= 1
    with pytest.raises(PipelineError) as error:
        local.run(worker.ctx, worker.config)
    assert error.value.stage == "IMAGE_VALIDATION"
    assert not list(worker.ctx.out_dir.iterdir())
    assert worker.ctx.events == [("IMAGE_VALIDATION", "running")]


def test_worker_fails_if_parent_changes_after_validation(worker):
    worker.adapter.alter_parent = True
    with pytest.raises(PipelineError, match="SHA-256 mismatch") as error:
        local.run(worker.ctx, worker.config)
    assert error.value.stage == "EXPORTING"
    assert not list(worker.ctx.out_dir.iterdir())


@pytest.mark.parametrize("banner, cuda", [("with CUDA", True), ("without CUDA", False)])
def test_local_cuda_probe_checks_current_build_not_old_log(tmp_path, monkeypatch, banner, cuda):
    log = tmp_path / "validation.log"
    log.write_text("COLMAP 0.1\n(Commit Unknown on Unknown with CUDA)\n")
    ctx = SimpleNamespace(gpu_index="0", log_file=lambda _: log, cancel_check=lambda: False)
    monkeypatch.setattr(local, "get_settings", lambda: SimpleNamespace(colmap_bin="unused"))

    def run_help(self, args, output, cancel, *, timeout_s):
        assert args == ["help"] and timeout_s == 20 and not cancel()
        with output.open("a") as f:
            f.write(f"COLMAP 3.13.0 -- Structure-from-Motion and Multi-View Stereo\n(Commit Unknown on Unknown {banner})\n")

    monkeypatch.setattr(local.ColmapAdapter, "run_command", run_help)
    if cuda:
        assert local._adapter(ctx).engine_identity["version"].startswith("COLMAP 3.13.0")
    else:
        with pytest.raises(ValueError, match="CUDA-capable COLMAP"):
            local._adapter(ctx)


def test_worker_propagates_gpu_stage_cancellation(worker):
    worker.adapter.cancel_dense = True
    with pytest.raises(StageCancelled) as error:
        local.run(worker.ctx, worker.config)
    assert error.value.stage == "DENSE_RECONSTRUCTION"
    assert worker.ctx.events[-1] == ("DENSE_RECONSTRUCTION", "running")
    assert not list(worker.ctx.out_dir.iterdir()) and not list(worker.ctx.work_dir.glob("local-reconstruction-*"))
