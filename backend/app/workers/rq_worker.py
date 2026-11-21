"""RQ Worker 入口 — 单 Worker 串行执行 GPU 任务（指南建议）。

启动： python -m app.workers.rq_worker   或 scripts/run_worker.sh
"""
from __future__ import annotations

import os

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

    class ReconWorker(SimpleWorker):
        def run_maintenance_tasks(self):
            super().run_maintenance_tasks()
            requeue_stuck_jobs()

    requeue_stuck_jobs()

    # 唯一名称：进程被强杀后旧注册不会阻塞新实例
    worker = ReconWorker([queue], connection=conn, name=f"recon-worker-{os.getpid()}")
    worker.work(burst=False, with_scheduler=False)


if __name__ == "__main__":
    main()
