from datetime import datetime, timezone
from sqlalchemy import text, select, update
from app.db.session import get_engine, session_scope
from app.models import GenerationJob
from app.core.enums import JobStatus

def claim_next_job(database_url: str, worker_id: str):
    engine = get_engine(database_url)
    with engine.begin() as conn:
        conn.exec_driver_sql("BEGIN IMMEDIATE")
        row = conn.execute(text("SELECT id FROM generation_jobs WHERE status = :status ORDER BY created_at LIMIT 1"), {"status": JobStatus.QUEUED}).first()
        if not row:
            conn.commit(); return None
        result = conn.execute(text("UPDATE generation_jobs SET status=:claimed, worker_id=:worker, claimed_at=:now WHERE id=:id AND status=:queued"), {"claimed": JobStatus.CLAIMED, "worker": worker_id, "now": datetime.now(timezone.utc).isoformat(), "id": row.id, "queued": JobStatus.QUEUED})
        if result.rowcount != 1:
            conn.rollback(); return None
        conn.commit()
    with session_scope(database_url) as session:
        return session.get(GenerationJob, row.id)

def recover_orphaned_jobs(database_url: str, live_worker_ids: set[str]):
    count = 0
    with session_scope(database_url) as session:
        jobs = session.query(GenerationJob).filter(GenerationJob.status.in_([JobStatus.CLAIMED, JobStatus.PREPARING, JobStatus.RUNNING])).all()
        for job in jobs:
            if job.worker_id not in live_worker_ids:
                job.status = JobStatus.INTERRUPTED; count += 1
    return count
