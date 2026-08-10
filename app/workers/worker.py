import asyncio, os, socket
from datetime import datetime, timezone
from app.core.config import settings
from app.core.enums import JobStatus
from app.db.session import session_scope
from app.models import GenerationJob, JobEvent
from app.workers.job_claim import claim_next_job, recover_orphaned_jobs

class LocalDramaWorker:
    def __init__(self, database_url=settings.database_url, worker_id=None):
        self.database_url = database_url
        self.worker_id = worker_id or f"{socket.gethostname()}-{os.getpid()}"
        self.running = True

    def process_one(self):
        job = claim_next_job(self.database_url, self.worker_id)
        if not job: return False
        with session_scope(self.database_url) as session:
            db_job = session.get(GenerationJob, job.id)
            db_job.status = JobStatus.RUNNING; db_job.current_stage = "worker_started"; db_job.started_at = datetime.now(timezone.utc)
            session.add(JobEvent(job_id=job.id, sequence=2, event_type="running", progress=0.05, message="Worker claimed job"))
            # Phase 1 deliberately proves orchestration and event persistence only.
            db_job.status = JobStatus.COMPLETED; db_job.progress = 1.0; db_job.current_stage = "complete"; db_job.completed_at = datetime.now(timezone.utc)
            session.add(JobEvent(job_id=job.id, sequence=3, event_type="completed", progress=1, message="Job completed"))
        return True

    async def run(self):
        recover_orphaned_jobs(self.database_url, {self.worker_id})
        while self.running:
            if not self.process_one(): await asyncio.sleep(settings.worker_poll_seconds)
