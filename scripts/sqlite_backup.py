"""Create and validate standalone SQLite backups; never restore in place."""

import argparse
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import shutil
import sqlite3
import sys
import tempfile
import time

DATABASE = "recon.db"
MANIFEST = "manifest.json"


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect_readonly(path, immutable=False):
    uri = path.resolve(strict=True).as_uri() + "?mode=ro"
    if immutable:
        uri += "&immutable=1"
    return sqlite3.connect(uri, uri=True, timeout=5)


def file_evidence(path):
    sha256 = hashlib.sha256()
    size = 0
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            sha256.update(block)
            size += len(block)
    return {"sha256": sha256.hexdigest(), "bytes": size}


def require_no_sidecars(path):
    for suffix in ("-wal", "-shm", "-journal"):
        sidecar = Path(f"{path}{suffix}")
        if sidecar.exists() or sidecar.is_symlink():
            raise ValueError("Snapshot must not have SQLite sidecar files")


def integrity(path):
    require_no_sidecars(path)
    with closing(connect_readonly(path, immutable=True)) as db:
        rows = db.execute("PRAGMA integrity_check").fetchall()
    if rows != [("ok",)]:
        raise ValueError(f"SQLite integrity_check failed: {rows[:3]}")
    return "ok"


def write_evidence(path, evidence):
    with path.open("x", encoding="utf-8") as stream:
        json.dump(evidence, stream, indent=2, sort_keys=True)
        stream.write("\n")


def backup(source, output_dir):
    source = source.resolve(strict=True)
    if not source.is_file() or source.stat().st_size == 0:
        raise ValueError("Source must be a non-empty SQLite database file")
    deadline = time.monotonic() + 120

    def check_deadline(status, remaining, total):
        if time.monotonic() > deadline:
            raise TimeoutError("SQLite online backup exceeded 120 seconds")

    output_dir.mkdir(parents=True, exist_ok=True)
    bundle = Path(tempfile.mkdtemp(prefix="recon-", dir=output_dir.resolve()))
    try:
        snapshot = bundle / DATABASE
        with closing(connect_readonly(source)) as live, closing(sqlite3.connect(snapshot)) as dest:
            live.backup(dest, pages=256, progress=check_deadline, sleep=0.1)
            if dest.execute("PRAGMA journal_mode=DELETE").fetchone() != ("delete",):
                raise ValueError("Could not produce a standalone SQLite snapshot")
        evidence = {
            "format": 1,
            "sqlite_version": sqlite3.sqlite_version,
            "created_at_utc": utc_now(),
            "database": DATABASE,
            "integrity_check": integrity(snapshot),
            **file_evidence(snapshot),
        }
        write_evidence(bundle / MANIFEST, evidence)
    except BaseException:
        shutil.rmtree(bundle)
        raise
    return {"bundle": str(bundle), **evidence}


def restore(bundle, destination):
    bundle = bundle.resolve(strict=True)
    with (bundle / MANIFEST).open(encoding="utf-8") as stream:
        manifest = json.load(stream)
    if not isinstance(manifest, dict) or manifest.get("format") != 1 or manifest.get("database") != DATABASE:
        raise ValueError("Unrecognized SQLite backup manifest")
    snapshot = bundle / DATABASE
    if not snapshot.is_file() or snapshot.is_symlink():
        raise ValueError("Missing or non-regular SQLite backup file")
    require_no_sidecars(snapshot)
    expected = {key: manifest.get(key) for key in ("sha256", "bytes")}

    # Reserve the exact destination first. Never replace an existing path.
    destination.mkdir(mode=0o700)
    try:
        restored = destination / DATABASE
        with snapshot.open("rb") as src, restored.open("xb") as out:
            shutil.copyfileobj(src, out)
        observed = file_evidence(restored)
        if observed != expected:
            raise ValueError("Restored database does not match backup manifest (SHA-256 or size)")
        evidence = {
            "backup_created_at_utc": manifest.get("created_at_utc"),
            "restored_at_utc": utc_now(),
            "sqlite_version": sqlite3.sqlite_version,
            "database": DATABASE,
            "integrity_check": integrity(restored),
            **observed,
        }
        write_evidence(destination / "restore-evidence.json", evidence)
    except BaseException:
        shutil.rmtree(destination)
        raise
    return {"destination": str(destination.resolve()), **evidence}


def configured_paths():
    # Match the service's working directory and dotenv/environment precedence.
    backend = Path(__file__).resolve().parents[1] / "backend"
    sys.path.insert(0, str(backend))
    from app.core.config import Settings
    from sqlalchemy.engine import make_url

    env_file = backend / Settings.model_config["env_file"]
    settings = Settings(_env_file=env_file)
    db = make_url(settings.database_url)
    if (
        db.drivername not in {"sqlite", "sqlite+pysqlite"}
        or db.query
        or not db.database
        or db.database == ":memory:"
    ):
        raise ValueError("Configured database must be a file-backed SQLite URL without URI parameters")
    return backend / db.database, backend / settings.data_dir / "backups"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    backup_parser = subparsers.add_parser("backup", help="create a standalone SQLite backup")
    backup_parser.add_argument("--source", type=Path)
    backup_parser.add_argument("--output-dir", type=Path)
    restore_parser = subparsers.add_parser("restore", help="verify and copy to a new directory")
    restore_parser.add_argument("--backup", required=True, type=Path)
    restore_parser.add_argument("--destination", required=True, type=Path)
    args = parser.parse_args()
    try:
        if args.command == "backup":
            if (args.source is None) != (args.output_dir is None):
                parser.error("provide both --source and --output-dir, or neither for app configuration")
            evidence = backup(*(configured_paths() if args.source is None else (args.source, args.output_dir)))
        else:
            evidence = restore(args.backup, args.destination)
    except (OSError, sqlite3.Error, ValueError, ImportError) as exc:
        parser.exit(1, f"SQLite {args.command} failed: {exc}\n")
    print(json.dumps(evidence, sort_keys=True))


if __name__ == "__main__":
    main()
