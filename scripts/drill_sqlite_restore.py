"""Run the SQLite backup/restore drill against synthetic data in a new temp dir."""

from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / "scripts" / "sqlite_backup.py"


def run(*args, env=None, cwd=ROOT):
    return subprocess.run(args, text=True, capture_output=True, env=env, cwd=cwd, timeout=180)


def checked(result):
    result.check_returncode()
    return result.stdout.strip()


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def main():
    work = Path(tempfile.mkdtemp(prefix="recon-sqlite-drill-"))
    source = work / "live source.db"
    destination = work / "restored"
    # Override configuration inherited from the shell, including case variants.
    overrides = {"RECON_ENV_FILE": str(work / "unused.env"), "DATABASE_URL": f"sqlite:///{source}",
                 "DATA_DIR": str(work / "data files"), "SCRATCH_DIR": str(work / "scratch")}
    env = {key: value for key, value in os.environ.items() if key.upper() not in overrides}
    env.update(overrides, RECON_PYTHON=sys.executable)
    try:
        migration = run(sys.executable, "-m", "alembic", "upgrade", "head", env=env, cwd=ROOT / "backend")
        checked(migration)
        with closing(sqlite3.connect(source)) as live:
            require(live.execute("PRAGMA journal_mode=WAL").fetchone() == ("wal",), "WAL setup failed")
            live.execute("PRAGMA wal_autocheckpoint=0")
            insert = ("INSERT INTO users (username, password_hash, role, created_at) "
                      "VALUES (?, 'drill-not-a-credential', 'user', CURRENT_TIMESTAMP)")
            live.execute(insert, ("committed-in-wal",))
            live.commit()
            live.execute(insert, ("uncommitted",))
            require(Path(f"{source}-wal").stat().st_size > 0, "Expected an active WAL")
            with closing(sqlite3.connect(source.as_uri() + "?mode=ro&immutable=1", uri=True)) as main_only:
                require(main_only.execute("SELECT count(*) FROM users").fetchone() == (0,),
                        "Committed fixture row was not isolated to the WAL")
            revision = live.execute("SELECT version_num FROM alembic_version").fetchone()[0]
            backup = json.loads(checked(run("bash", str(ROOT / "scripts" / "backup_db.sh"), env=env)))
            bundle = Path(backup["bundle"])
            require(bundle.parent == work / "data files" / "backups", "Backup ignored isolated configuration")
            restored = json.loads(checked(run(sys.executable, "-I", str(HELPER), "restore",
                                              "--backup", str(bundle), "--destination", str(destination))))
            with closing(sqlite3.connect((destination / "recon.db").as_uri() + "?mode=ro", uri=True)) as db:
                require(db.execute("SELECT username FROM users").fetchall() == [("committed-in-wal",)],
                        "Restored DB must include committed WAL rows only")
                require(db.execute("SELECT version_num FROM alembic_version").fetchone() == (revision,),
                        "Alembic revision changed")
                require(db.execute("PRAGMA foreign_key_check").fetchall() == [], "Foreign-key check failed")
            live.rollback()
        require(backup["sha256"] == restored["sha256"], "Snapshot and restored SHA-256 differ")
        require(backup["integrity_check"] == restored["integrity_check"] == "ok", "Integrity check not recorded")

        restore = (sys.executable, "-I", str(HELPER), "restore")
        refused = run(*restore, "--backup", str(bundle), "--destination", str(destination))
        require(refused.returncode != 0 and "File exists" in refused.stderr, "Existing destination was not rejected")
        tampered = work / "tampered-backup"
        tampered.mkdir()
        (tampered / "manifest.json").write_bytes((bundle / "manifest.json").read_bytes())
        (tampered / "recon.db").write_bytes((bundle / "recon.db").read_bytes() + b"tampered")
        rejected_dst = work / "must-not-exist"
        rejected = run(*restore, "--backup", str(tampered), "--destination", str(rejected_dst))
        require(rejected.returncode != 0 and "SHA-256 or size" in rejected.stderr and not rejected_dst.exists(),
                "Tampered backup was not cleanly rejected")
        (tampered / "recon.db-wal").touch()
        sidecar = run(*restore, "--backup", str(tampered), "--destination", str(rejected_dst))
        require(sidecar.returncode != 0 and "sidecar files" in sidecar.stderr and not rejected_dst.exists(),
                "Unexpected SQLite sidecar was not rejected")
        missing = run(sys.executable, "-I", str(HELPER), "backup", "--source", str(work / "missing.db"),
                      "--output-dir", str(work / "must-not-be-created"))
        require(missing.returncode != 0 and not (work / "must-not-be-created").exists(), "Missing source was accepted")
        evidence = {"work_dir": str(work), "alembic_revision": revision, "backup": backup, "restore": restored,
                    "checks": {"committed_wal_row": True, "uncommitted_row_excluded": True,
                               "foreign_keys_ok": True, "existing_destination_refused": True,
                               "tampered_backup_refused": True, "unexpected_sidecar_refused": True,
                               "missing_source_refused": True}}
        text = json.dumps(evidence, indent=2, sort_keys=True)
        (work / "drill-evidence.json").write_text(text + "\n", encoding="utf-8")
        print(text)
    except (OSError, RuntimeError, sqlite3.Error, subprocess.SubprocessError, ValueError) as exc:
        details = getattr(exc, "stderr", None)
        sys.exit(f"SQLite drill failed in {work}: {exc}\n{details or ''}")


if __name__ == "__main__":
    main()
