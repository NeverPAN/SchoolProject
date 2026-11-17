"""RQ Worker 入口 — 单 Worker 串行执行 GPU 任务（指南建议）。

启动： python -m app.workers.rq_worker   或 scripts/run_worker.sh
"""
from __future__ import annotations

from app.core.config import get_settings
from app.core.logging_setup import setup_logging
from app.db.session import create_all


def main() -> None:
    settings = get_settings()
    setup_logging(settings.log_level)
    create_all()

    from redis import Redis
    from rq import Queue, SimpleWorker

    from app.services.job_service import requeue_stuck_jobs

    conn = Redis.from_url(settings.redis_url)
    queue = Queue("recon", connection=conn)

    requeue_stuck_jobs()

    worker = SimpleWorker([queue], connection=conn, name=f"recon-worker-1")
    worker.work(burst=False, with_scheduler=False)


if __name__ == "__main__":
    main()
