from app.db.session import create_schema, session_scope
from app.models import Project, GenerationJob, JobEvent
from app.core.enums import JobStatus, JobType


def test_database_uses_wal_and_foreign_keys(tmp_path):
    db = tmp_path / "test.db"
    create_schema(str(db))
    with session_scope(str(db)) as session:
        assert session.execute(__import__('sqlalchemy').text("PRAGMA journal_mode")).scalar() == "wal"
        assert session.execute(__import__('sqlalchemy').text("PRAGMA foreign_keys")).scalar() == 1


def test_project_job_and_event_round_trip(tmp_path):
    db = tmp_path / "test.db"
    create_schema(str(db))
    with session_scope(str(db)) as session:
        project = Project(name="Demo", story="一个测试故事")
        session.add(project)
        session.flush()
        job = GenerationJob(project_id=project.id, type=JobType.STORY_GENERATION, status=JobStatus.QUEUED)
        session.add(job)
        session.flush()
        session.add(JobEvent(job_id=job.id, sequence=1, event_type="queued", progress=0, message="queued"))
        job_id = job.id
    with session_scope(str(db)) as session:
        job = session.get(GenerationJob, job_id)
        assert job.status == JobStatus.QUEUED
        assert job.events[0].message == "queued"
