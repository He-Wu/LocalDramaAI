from app.db.session import create_schema, session_scope
from app.models import Project, GenerationJob
from app.core.enums import JobStatus, JobType
from app.workers.job_claim import claim_next_job, recover_orphaned_jobs


def test_claim_is_atomic_and_sets_worker(tmp_path):
    db = tmp_path / "test.db"
    create_schema(str(db))
    with session_scope(str(db)) as session:
        project = Project(name="Demo")
        session.add(project); session.flush()
        session.add(GenerationJob(project_id=project.id, type=JobType.STORY_GENERATION, status=JobStatus.QUEUED))
    job = claim_next_job(str(db), "worker-a")
    assert job is not None
    assert job.status == JobStatus.CLAIMED
    assert claim_next_job(str(db), "worker-b") is None


def test_recover_orphaned_jobs(tmp_path):
    db = tmp_path / "test.db"
    create_schema(str(db))
    with session_scope(str(db)) as session:
        project = Project(name="Demo"); session.add(project); session.flush()
        session.add(GenerationJob(project_id=project.id, type=JobType.STORY_GENERATION, status=JobStatus.RUNNING, worker_id="dead-worker"))
    assert recover_orphaned_jobs(str(db), {"live-worker"}) == 1
    with session_scope(str(db)) as session:
        assert session.query(GenerationJob).one().status == JobStatus.INTERRUPTED
