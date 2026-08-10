from app.core.enums import PIPELINE_STAGES, JobStatus, JobType, PipelineStage, StageStatus
from app.db.session import create_schema, session_scope
from app.models import GenerationJob, JobStage, Project


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
    with session_scope(database) as session:
        stage = session.get(JobStage, stage_id)
        assert stage.job.type == JobType.FULL_DRAMA
        assert stage.status == StageStatus.PENDING
