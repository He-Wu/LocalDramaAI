import sqlite3
import subprocess
import sys
import time
from datetime import datetime

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app.core.enums import PIPELINE_STAGES, JobStatus, JobType, PipelineStage, StageStatus
from app.db.session import create_schema, session_scope
from app.models import GenerationJob, JobStage, Project


def _create_legacy_database(database):
    with sqlite3.connect(database) as connection:
        connection.executescript(
            """
            CREATE TABLE generation_jobs (
                id VARCHAR(36) PRIMARY KEY,
                project_id VARCHAR(36) NOT NULL,
                type VARCHAR(40) NOT NULL,
                status VARCHAR(20) NOT NULL,
                progress FLOAT NOT NULL,
                current_stage VARCHAR(100),
                input_json JSON,
                output_json JSON,
                error_code VARCHAR(80),
                error_message TEXT,
                retry_count INTEGER NOT NULL,
                worker_id VARCHAR(100),
                claimed_at DATETIME,
                started_at DATETIME,
                completed_at DATETIME,
                created_at DATETIME NOT NULL,
                updated_at DATETIME NOT NULL
            );
            INSERT INTO generation_jobs (
                id, project_id, type, status, progress, retry_count, created_at, updated_at
            ) VALUES (
                'legacy-job', 'legacy-project', 'FULL_DRAMA', 'QUEUED', 0, 0,
                '2026-01-01 00:00:00', '2026-01-01 00:00:00'
            );
            """
        )


def test_create_schema_upgrades_existing_generation_jobs_table(tmp_path):
    database = tmp_path / "legacy.db"
    _create_legacy_database(database)

    create_schema(str(database))
    create_schema(str(database))

    with sqlite3.connect(database) as connection:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(generation_jobs)")}
    assert "cancel_requested_at" in columns

    with session_scope(str(database)) as session:
        job = session.get(GenerationJob, "legacy-job")
        assert job.cancel_requested_at is None


def test_create_schema_keeps_in_memory_sqlite_schema_alive():
    database = "sqlite:///:memory:"
    create_schema(database)

    with session_scope(database) as session:
        project = Project(name="In memory")
        session.add(project)
        session.flush()
        assert project.id is not None


def test_concurrent_create_schema_serializes_sqlite_upgrade(tmp_path):
    database = tmp_path / "concurrent-legacy.db"
    _create_legacy_database(database)
    go = tmp_path / "go"
    ready_files = [tmp_path / "ready-1", tmp_path / "ready-2"]
    script = """
import sys
import time
from pathlib import Path

from app.db.session import create_schema

database, ready_path, go_path = sys.argv[1:]
Path(ready_path).touch()
deadline = time.monotonic() + 10
while not Path(go_path).exists():
    if time.monotonic() >= deadline:
        raise TimeoutError("parent did not release schema creation barrier")
    time.sleep(0.001)
create_schema(database)
"""
    processes = [
        subprocess.Popen(
            [sys.executable, "-c", script, str(database), str(ready), str(go)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for ready in ready_files
    ]
    deadline = time.monotonic() + 10
    while not all(ready.exists() for ready in ready_files):
        if time.monotonic() >= deadline:
            for process in processes:
                process.kill()
            pytest.fail("schema creation subprocesses did not reach the barrier")
        time.sleep(0.001)
    go.touch()

    results = [process.communicate(timeout=15) for process in processes]
    for process, (stdout, stderr) in zip(processes, results):
        assert process.returncode == 0, f"stdout:\n{stdout}\nstderr:\n{stderr}"

    with sqlite3.connect(database) as connection:
        tables = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        columns = {row[1] for row in connection.execute("PRAGMA table_info(generation_jobs)")}
        legacy_row = connection.execute(
            "SELECT id, status FROM generation_jobs WHERE id = 'legacy-job'"
        ).fetchone()
    assert "job_stages" in tables
    assert "cancel_requested_at" in columns
    assert legacy_row == ("legacy-job", "QUEUED")

    with session_scope(str(database)) as session:
        assert session.get(GenerationJob, "legacy-job").status == JobStatus.QUEUED


def test_full_drama_stage_order_is_stable():
    assert JobType.FULL_DRAMA == "FULL_DRAMA"
    assert PIPELINE_STAGES == [
        PipelineStage.ENVIRONMENT_CHECK,
        PipelineStage.SCRIPT_STRUCTURE,
        PipelineStage.CHARACTER_MASTER,
        PipelineStage.STORYBOARD,
        PipelineStage.DIALOGUE_AUDIO,
        PipelineStage.RELEASE_TTS_GPU,
        PipelineStage.SHOT_VIDEO,
        PipelineStage.SHOT_MUX,
        PipelineStage.FINAL_CONCAT,
        PipelineStage.SUBTITLE_EXPORT,
        PipelineStage.MANIFEST_EXPORT,
    ]


def test_job_stage_round_trip(tmp_path):
    database = str(tmp_path / "pipeline.db")
    create_schema(database)
    with session_scope(database) as session:
        project = Project(name="Pipeline")
        session.add(project)
        session.flush()
        job = GenerationJob(project_id=project.id, type=JobType.FULL_DRAMA, status=JobStatus.QUEUED)
        session.add(job)
        session.flush()
        stage = JobStage(job_id=job.id, stage=PipelineStage.ENVIRONMENT_CHECK, status=StageStatus.PENDING)
        session.add(stage)
        session.flush()
        stage_id = stage.id

    with sqlite3.connect(database) as connection:
        persisted_stage, persisted_status = connection.execute(
            "SELECT stage, status FROM job_stages WHERE id = ?", (stage_id,)
        ).fetchone()
    assert persisted_stage == PipelineStage.ENVIRONMENT_CHECK.value
    assert persisted_status == StageStatus.PENDING.value

    with session_scope(database) as session:
        stage = session.get(JobStage, stage_id)
        assert stage.job.type == JobType.FULL_DRAMA
        assert isinstance(stage.stage, PipelineStage)
        assert isinstance(stage.status, StageStatus)
        assert stage.status == StageStatus.PENDING


@pytest.mark.parametrize(
    ("stage_value", "status_value"),
    [("not_a_stage", StageStatus.PENDING.value), (PipelineStage.STORYBOARD.value, "NOT_A_STATUS")],
)
def test_job_stage_rejects_invalid_persisted_enum_values(tmp_path, stage_value, status_value):
    database = str(tmp_path / "invalid-enum.db")
    create_schema(database)
    with session_scope(database) as session:
        project = Project(name="Pipeline")
        session.add(project)
        session.flush()
        job = GenerationJob(project_id=project.id, type=JobType.FULL_DRAMA, status=JobStatus.QUEUED)
        session.add(job)
        session.flush()

        with pytest.raises(IntegrityError):
            session.execute(
                text(
                    """
                    INSERT INTO job_stages (
                        id, job_id, stage, status, attempt, created_at, updated_at
                    ) VALUES (
                        :id, :job_id, :stage, :status, 0, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
                    )
                    """
                ),
                {
                    "id": f"invalid-{stage_value}-{status_value}",
                    "job_id": job.id,
                    "stage": stage_value,
                    "status": status_value,
                },
            )


def test_cancel_requested_at_round_trip(tmp_path):
    database = str(tmp_path / "cancel.db")
    cancel_requested_at = datetime(2026, 8, 11, 12, 30)
    create_schema(database)
    with session_scope(database) as session:
        project = Project(name="Pipeline")
        session.add(project)
        session.flush()
        job = GenerationJob(
            project_id=project.id,
            type=JobType.FULL_DRAMA,
            status=JobStatus.QUEUED,
            cancel_requested_at=cancel_requested_at,
        )
        session.add(job)
        session.flush()
        job_id = job.id

    with session_scope(database) as session:
        job = session.get(GenerationJob, job_id)
        assert job.cancel_requested_at == cancel_requested_at


def test_job_stage_is_unique_per_job_and_stage(tmp_path):
    database = str(tmp_path / "unique.db")
    create_schema(database)

    with pytest.raises(IntegrityError):
        with session_scope(database) as session:
            project = Project(name="Pipeline")
            session.add(project)
            session.flush()
            job = GenerationJob(
                project_id=project.id,
                type=JobType.FULL_DRAMA,
                status=JobStatus.QUEUED,
            )
            session.add(job)
            session.flush()
            session.add_all(
                [
                    JobStage(job_id=job.id, stage=PipelineStage.STORYBOARD),
                    JobStage(job_id=job.id, stage=PipelineStage.STORYBOARD),
                ]
            )
            session.flush()


def test_job_stages_are_ordered_and_deleted_with_job(tmp_path):
    database = str(tmp_path / "relationship.db")
    create_schema(database)
    with session_scope(database) as session:
        project = Project(name="Pipeline")
        session.add(project)
        session.flush()
        job = GenerationJob(
            project_id=project.id,
            type=JobType.FULL_DRAMA,
            status=JobStatus.QUEUED,
        )
        session.add(job)
        session.flush()
        later_stage = JobStage(
            job_id=job.id,
            stage=PipelineStage.STORYBOARD,
            created_at=datetime(2026, 8, 11, 12, 31),
        )
        earlier_stage = JobStage(
            job_id=job.id,
            stage=PipelineStage.ENVIRONMENT_CHECK,
            created_at=datetime(2026, 8, 11, 12, 30),
        )
        session.add_all([later_stage, earlier_stage])
        session.flush()
        job_id = job.id
        stage_ids = [earlier_stage.id, later_stage.id]

    with session_scope(database) as session:
        job = session.get(GenerationJob, job_id)
        assert [stage.stage for stage in job.stages] == [
            PipelineStage.ENVIRONMENT_CHECK,
            PipelineStage.STORYBOARD,
        ]
        session.delete(job)

    with session_scope(database) as session:
        assert [session.get(JobStage, stage_id) for stage_id in stage_ids] == [None, None]
