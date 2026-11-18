#!/usr/bin/env python3
"""Linux Redis/RQ fault drill: backend/.venv/bin/python scripts/drill_worker_faults.py.

Runs FastAPI routes in-process and uses a real Redis and separate RQ worker.
Redis listens only on a private Unix socket; no application env file is loaded.
"""
from __future__ import annotations

import argparse
import ctypes
import io
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from contextlib import ExitStack
from datetime import datetime, timezone
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1] / "backend"
ENGINE = """\
import json
import os
import signal
import sqlite3
import struct
import sys
import time
from pathlib import Path

command, *args = sys.argv[1:]
def option(name):
    return Path(args[args.index(name) + 1])

if command in ('--version', 'help'):
    print('COLMAP 3.13.0 synthetic worker-fault fixture')
elif command == 'feature_extractor':
    with sqlite3.connect(option('--database_path')) as db:
        db.execute('CREATE TABLE synthetic_feature (id INTEGER)')
elif command == 'exhaustive_matcher':
    control = Path(os.environ['DRILL_CONTROL'])
    if (control / 'pause').exists():
        # Stay silent and force cancellation to escalate past SIGTERM.
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        proc = Path('/proc/self/stat').read_text().rsplit(')', 1)[1].split()
        marker = control / 'engine.json'
        tmp = marker.with_suffix('.tmp')
        tmp.write_text(json.dumps([os.getpid(), int(proc[19])]))
        tmp.replace(marker)
        time.sleep(120)
        raise SystemExit('fixture timed out')
elif command == 'mapper':
    model = option('--output_path') / '0'
    model.mkdir(parents=True)
    for filename in ('cameras.bin', 'images.bin'):
        (model / filename).write_bytes(struct.pack('<Q', 0))
    points = [struct.pack('<QdddBBBdQ', i + 1, i % 4, i // 4, i % 3, 20, 90, 160, 0, 0)
              for i in range(16)]
    (model / 'points3D.bin').write_bytes(struct.pack('<Q', len(points)) + b''.join(points))
else:
    raise SystemExit(f'unexpected command: {command}')
"""


def wait_for(description, probe, timeout=20):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = probe()
        if result:
            return result
        time.sleep(0.05)
    raise RuntimeError(f"Timed out waiting for {description}")


def engine_alive(identity):
    pid, start_ticks = identity
    try:
        fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
        return int(fields[19]) == start_ticks and fields[0] not in ("Z", "X")
    except FileNotFoundError:
        return False


def clean_engine(marker):
    if not marker.exists():
        return
    identity = json.loads(marker.read_text())
    pid = identity[0]
    if engine_alive(identity):
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    try:
        os.waitpid(pid, 0)
    except ChildProcessError:
        pass


def stop_process(process):
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=4)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=4)


def run_drill(root, redis_bin, evidence):
    control = root / "control"
    control.mkdir()
    pause, marker = control / "pause", control / "engine.json"
    executable = root / "fake-colmap"
    executable.write_text(f"#!{sys.executable}\n{ENGINE}")
    executable.chmod(0o700)
    redis_socket = root / "redis.sock"
    os.environ.update(
        RECON_ENV_FILE=str(root / "not-loaded.env"), DATABASE_URL=f"sqlite:///{root / 'app.db'}",
        DATA_DIR=str(root / "data"), SCRATCH_DIR=str(root / "scratch"),
        REDIS_URL=f"unix://{redis_socket}?db=0", API_V1_PREFIX="/api/v1", SECRET_KEY="isolated-fixture",
        RECON_MODE="colmap", RECON_USE_GPU="0", COLMAP_BIN=str(executable), COLMAP_DENSE_MAX_IMAGE_SIZE="-1",
        DRILL_CONTROL=str(control),
    )
    os.environ.pop("COLMAP_TEXTURER_BIN", None)
    sys.path.insert(0, str(BACKEND))
    import redis
    import rq
    from fastapi.testclient import TestClient
    from PIL import Image, ImageDraw
    from app.main import app

    evidence["isolation"] = "Dedicated Redis Unix socket (TCP disabled), temporary SQLite and storage"
    with ExitStack() as stack:
        def start(name, argv):
            log = stack.enter_context((root / f"{name}.log").open("wb"))
            process = subprocess.Popen(argv, cwd=BACKEND, stdout=log, stderr=subprocess.STDOUT)
            stack.callback(stop_process, process)
            return process

        redis_proc = start("redis", [redis_bin, "--port", "0", "--unixsocket", str(redis_socket),
                                    "--unixsocketperm", "700", "--dir", str(root),
                                    "--save", "", "--appendonly", "no"])
        conn = redis.Redis.from_url(os.environ["REDIS_URL"], socket_timeout=1)
        stack.callback(conn.close)

        def redis_ready():
            if redis_proc.poll() is not None:
                raise RuntimeError(f"Private Redis exited: {redis_proc.returncode}")
            try:
                return conn.ping()
            except redis.ConnectionError:
                return False

        wait_for("private Redis", redis_ready)
        evidence["versions"] = {"redis_server": conn.info("server")["redis_version"],
                                "redis_py": redis.__version__, "rq": rq.__version__}
        queue = rq.Queue("recon", connection=conn)
        client = stack.enter_context(TestClient(app))
        stack.callback(clean_engine, marker)  # after both workers stop

        def api(method, path, expected=200, **kwargs):
            response = client.request(method, f"/api/v1{path}", **kwargs)
            assert response.status_code == expected, (method, path, response.status_code, response.text)
            return response.json()

        credentials = {"username": "drill", "password": "fixture-password"}
        api("POST", "/auth/register", 201, json=credentials)
        login = api("POST", "/auth/login", json=credentials)
        client.headers["Authorization"] = f"Bearer {login['access_token']}"

        def start_worker(name):
            process = start(name, [sys.executable, "-m", "app.workers.rq_worker"])

            def registered():
                assert process.poll() is None, (name, process.returncode)
                return conn.exists(f"rq:worker:recon-worker-{process.pid}")

            wait_for(f"{name} registration", registered)
            return process

        def submit(name):
            pause.touch()
            marker.unlink(missing_ok=True)
            project = api("POST", "/projects", 201, json={"name": name})["id"]
            files = []
            for i in range(3):
                image = Image.new("RGB", (320, 240), (i * 60 + 32, 100, 140))
                ImageDraw.Draw(image).rectangle((i * 20, 30, 180, 160), fill="white")
                buf = io.BytesIO()
                image.save(buf, "JPEG")
                files.append(("files", (f"image_{i}.jpg", buf.getvalue(), "image/jpeg")))
            api("POST", f"/projects/{project}/images", 201, files=files)
            jid = api("POST", f"/projects/{project}/jobs", 202,
                      json={"matcher": "exhaustive", "enable_dense": False, "enable_mesh": False})["id"]
            wait_for("synthetic matcher PID", marker.exists)
            identity = json.loads(marker.read_text())
            live = api("GET", f"/jobs/{jid}")
            assert live["status"] == "RUNNING" and live["current_stage"] == "FEATURE_MATCHING", live
            assert engine_alive(identity), identity
            registry = queue.started_job_registry
            delivery, = [rid for rid in registry.get_job_ids(cleanup=False)
                         if rq.job.Job.fetch(rid, connection=conn).args == (jid,)]
            assert delivery not in registry.get_expired_job_ids()
            return jid, project, identity, delivery

        def await_terminal(jid):
            def probe():
                job = api("GET", f"/jobs/{jid}")
                return job if job["status"] in ("FAILED", "CANCELLED", "SUCCEEDED") else None

            return wait_for(f"job {jid} terminal state", probe)

        def assert_closed(job, expected, code):
            assert job["status"] == expected and job["error_code"] == code, job
            assert job["error_stage"] == "FEATURE_MATCHING" and job["finished_at"], job
            assert all(e["status"] != "running" for e in job["events"]), job
            event, = [e for e in job["events"] if e["stage"] == "FEATURE_MATCHING"]
            assert event["status"] == expected.lower() and event["finished_at"], event
            return event["status"]

        worker = start_worker("worker-first")
        jid, project, identity, delivery = submit("Silent cancellation")
        t0 = time.monotonic()
        requested = api("POST", f"/jobs/{jid}/cancel")["status"]
        cancelled = await_terminal(jid)
        elapsed = round(time.monotonic() - t0, 3)
        event = assert_closed(cancelled, "CANCELLED", "CANCELLED")
        assert requested == "CANCEL_REQUESTED" and not engine_alive(identity), (requested, identity)
        assert elapsed < 5, elapsed
        assert api("GET", f"/projects/{project}/models") == []
        wait_for("cancelled delivery to finish in RQ", lambda: (
            rq.job.Job.fetch(delivery, connection=conn).return_value() == {"ok": True, "cancelled": True}))
        evidence["live_cancellation"] = {
            "job_id": jid, "requested": requested, "status": cancelled["status"],
            "error_code": cancelled["error_code"], "stage_event": event,
            "elapsed_seconds": elapsed, "synthetic_matcher_alive": False, "published_models": 0,
        }

        jid, project, identity, delivery = submit("SIGKILL and resume")
        t0 = time.monotonic()
        worker.kill()  # Exact Popen child, never an existing worker.
        killed_exit = worker.wait(timeout=5)
        assert killed_exit == -signal.SIGKILL, killed_exit
        assert api("GET", f"/jobs/{jid}")["status"] == "RUNNING"
        orphan_after_kill = engine_alive(identity)
        stale_delivery = delivery in queue.started_job_registry.get_job_ids(cleanup=False)
        assert stale_delivery, delivery
        replacement = start_worker("worker-replacement")
        failed = await_terminal(jid)
        elapsed = round(time.monotonic() - t0, 3)
        event = assert_closed(failed, "FAILED", "WORKER_LOST")
        assert api("GET", f"/projects/{project}/models") == []
        orphan_after_reconciliation = engine_alive(identity)
        evidence["sigkill_recovery"] = {
            "job_id": jid, "worker_returncode": killed_exit, "worker_pid": worker.pid,
            "replacement_pid": replacement.pid, "status": failed["status"], "error_code": failed["error_code"],
            "stage_event": event, "elapsed_seconds": elapsed, "published_models_before_resume": 0,
            "stale_rq_started_entry_after_kill": stale_delivery,
            "stale_rq_started_entry_after_reconciliation": (
                delivery in queue.started_job_registry.get_job_ids(cleanup=False)),
            "synthetic_matcher_alive_after_worker_kill": orphan_after_kill,
            "synthetic_matcher_alive_after_reconciliation": orphan_after_reconciliation,
        }
        # Avoid letting an orphan touch the job's scratch files during recovery.
        clean_engine(marker)
        evidence["sigkill_recovery"]["harness_required_orphan_cleanup"] = orphan_after_reconciliation
        pause.unlink()
        requested = api("POST", f"/jobs/{jid}/resume")["status"]
        resumed = await_terminal(jid)
        assert requested == "RESUMING" and resumed["status"] == "SUCCEEDED", resumed
        skipped = [e["stage"] for e in resumed["events"] if e["status"] == "skipped"]
        assert {"IMAGE_VALIDATION", "FEATURE_EXTRACTION"} <= set(skipped), resumed
        matching = [e["status"] for e in resumed["events"] if e["stage"] == "FEATURE_MATCHING"]
        assert matching == ["failed", "completed"], matching
        models = api("GET", f"/projects/{project}/models")
        pointcloud, = [m for m in models if m["model_type"] == "sparse_pointcloud"]
        detail = api("GET", f"/models/{pointcloud['id']}")
        assert [v["version_number"] for v in detail["versions"]] == [1], detail
        evidence["resume"] = {
            "job_id": jid, "requested": requested, "status": resumed["status"],
            "reused_checkpoints": [s for s in skipped if s in ("IMAGE_VALIDATION", "FEATURE_EXTRACTION")],
            "matching_events": matching, "published_models": len(models), "pointcloud_version": 1,
        }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evidence", type=Path, help="write observations to a new JSON file")
    args = parser.parse_args()
    redis_bin = shutil.which("redis-server")
    if sys.platform != "linux" or not redis_bin or not __debug__:
        parser.error("requires Linux, redis-server, and Python with assertions enabled")
    if args.evidence and args.evidence.exists():
        parser.error("evidence file already exists")
    # Adopt the fixture after SIGKILL so we can reap it even under an init that does not.
    if ctypes.CDLL(None, use_errno=True).prctl(36, 1, 0, 0, 0) != 0:  # PR_SET_CHILD_SUBREAPER
        raise OSError(ctypes.get_errno(), "PR_SET_CHILD_SUBREAPER failed")
    evidence = {"recorded_at_utc": datetime.now(timezone.utc).isoformat()}
    with tempfile.TemporaryDirectory(prefix="recon-fault-", dir="/tmp") as path:
        root = Path(path)
        try:
            run_drill(root, redis_bin, evidence)
        except Exception:
            for log in sorted(root.glob("*.log")):
                print(f"\n{log.name}:\n{log.read_text(errors='replace')[-6000:]}", file=sys.stderr)
            print(json.dumps(evidence, indent=2), file=sys.stderr)
            raise
    print(json.dumps(evidence, indent=2))
    if args.evidence:
        with args.evidence.open("x") as output:
            json.dump(evidence, output, indent=2)
            output.write("\n")


if __name__ == "__main__":
    main()
