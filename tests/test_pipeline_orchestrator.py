import json

import pytest

from app.core.enums import PIPELINE_STAGES, JobStatus, JobType, StageStatus
from app.db.session import create_schema, session_scope
from app.models import GenerationJob, JobStage, Project
from app.pipeline import PipelineContext, PipelineOrchestrator, PipelineState


class RecordingRuntime:
    def __init__(self, database_url=None, job_id=None, cancel_after=None):
        self.calls = []
        self.database_url = database_url
        self.job_id = job_id
        self.cancel_after = cancel_after

    async def execute(self, stage, context):
        self.calls.append((stage, context))
        if stage == self.cancel_after:
            PipelineState(self.database_url, self.job_id).request_cancel()
        return {"artifact": f"{stage.value}.json"}


class ProviderError(RuntimeError):
    code = "PROVIDER_DOWN"


class FailingRuntime(RecordingRuntime):
    def __init__(self, failed_stage):
        super().__init__()
        self.failed_stage = failed_stage

    async def execute(self, stage, context):
        self.calls.append((stage, context))
        if stage == self.failed_stage:
            raise ProviderError("provider unavailable")
        return {"artifact": f"{stage.value}.json"}


class InvalidOutputRuntime(RecordingRuntime):
    def __init__(self, invalid_output):
        super().__init__()
        self.invalid_output = invalid_output

    async def execute(self, stage, context):
        self.calls.append((stage, context))
        return self.invalid_output


def seed(tmp_path, *, job_type=JobType.FULL_DRAMA, input_json=None):
    database_url = str(tmp_path / "orchestrator.db")
    create_schema(database_url)
    with session_scope(database_url) as session:
        project = Project(name="Orchestrated drama")
        session.add(project)
        session.flush()
        job = GenerationJob(
            project_id=project.id,
            type=job_type,
            status=JobStatus.QUEUED,
            input_json=input_json,
        )
        session.add(job)
        session.flush()
        return database_url, job.id, project.id


def persisted(database_url, job_id):
    with session_scope(database_url) as session:
        job = session.get(GenerationJob, job_id)
        stages = {
            row.stage: row
            for row in session.query(JobStage).filter_by(job_id=job_id).all()
        }
        return job, stages


@pytest.mark.asyncio
async def test_success_runs_all_stages_in_order_and_returns_completed_job(tmp_path):
    database_url, job_id, project_id = seed(
        tmp_path,
        input_json={"title": "The Last Frame", "project_id": "malicious-override"},
    )
    runtime = RecordingRuntime()

    result = await PipelineOrchestrator(database_url, runtime).run(job_id)

    assert isinstance(result, GenerationJob)
    assert [stage for stage, _ in runtime.calls] == PIPELINE_STAGES
    assert all(
        context == PipelineContext(
            database_url=database_url,
            job_id=job_id,
            project_id=project_id,
            input_json={
                "title": "The Last Frame",
                "project_id": "malicious-override",
            },
        )
        for _, context in runtime.calls
    )
    job, stages = persisted(database_url, job_id)
    assert all(stages[stage].status == StageStatus.COMPLETED for stage in PIPELINE_STAGES)
    assert all(
        stages[stage].input_json
        == {"title": "The Last Frame", "project_id": project_id}
        for stage in PIPELINE_STAGES
    )
    assert job.status == JobStatus.COMPLETED
    assert job.current_stage == "complete"
    assert job.output_json == {
        "project_id": project_id,
        "artifact": "manifest_export.json",
    }
    assert result.output_json == job.output_json


@pytest.mark.asyncio
async def test_runtime_failure_is_persisted_and_stops_later_stages(tmp_path):
    database_url, job_id, _ = seed(tmp_path)
    failed_stage = PIPELINE_STAGES[3]
    runtime = FailingRuntime(failed_stage)

    result = await PipelineOrchestrator(database_url, runtime).run(job_id)

    assert [stage for stage, _ in runtime.calls] == PIPELINE_STAGES[:4]
    job, stages = persisted(database_url, job_id)
    assert result.status == JobStatus.FAILED
    assert job.status == JobStatus.FAILED
    assert job.current_stage == failed_stage.value
    assert job.error_code == "PROVIDER_DOWN"
    assert job.error_message == "provider unavailable"
    assert stages[failed_stage].status == StageStatus.FAILED
    assert stages[failed_stage].error_code == "PROVIDER_DOWN"
    assert all(stages[stage].status == StageStatus.PENDING for stage in PIPELINE_STAGES[4:])


@pytest.mark.asyncio
async def test_resume_skips_completed_prefix_without_changing_attempts(tmp_path):
    database_url, job_id, _ = seed(tmp_path)
    state = PipelineState(database_url, job_id)
    state.initialize()
    for stage in PIPELINE_STAGES[:3]:
        state.start(stage, {"seed": stage.value})
        state.complete(stage, {"seeded": stage.value})
    _, before = persisted(database_url, job_id)
    attempts_before = {stage: before[stage].attempt for stage in PIPELINE_STAGES[:3]}
    runtime = RecordingRuntime()

    result = await PipelineOrchestrator(database_url, runtime).run(job_id)

    assert [stage for stage, _ in runtime.calls] == PIPELINE_STAGES[3:]
    assert result.status == JobStatus.COMPLETED
    _, after = persisted(database_url, job_id)
    assert {stage: after[stage].attempt for stage in PIPELINE_STAGES[:3]} == attempts_before


@pytest.mark.asyncio
async def test_all_completed_resume_uses_persisted_manifest_output(tmp_path):
    database_url, job_id, project_id = seed(tmp_path)
    state = PipelineState(database_url, job_id)
    state.initialize()
    for stage in PIPELINE_STAGES:
        state.start(stage, {})
        output = (
            {"manifest": "persisted-manifest.json", "assets": 12}
            if stage == PIPELINE_STAGES[-1]
            else {"stage": stage.value}
        )
        state.complete(stage, output)
    runtime = RecordingRuntime()

    result = await PipelineOrchestrator(database_url, runtime).run(job_id)

    assert runtime.calls == []
    assert result.output_json == {
        "project_id": project_id,
        "manifest": "persisted-manifest.json",
        "assets": 12,
    }


@pytest.mark.asyncio
async def test_cancellation_before_first_stage_never_calls_runtime(tmp_path):
    database_url, job_id, _ = seed(tmp_path)
    state = PipelineState(database_url, job_id)
    state.initialize()
    state.request_cancel()
    runtime = RecordingRuntime()

    result = await PipelineOrchestrator(database_url, runtime).run(job_id)

    job, stages = persisted(database_url, job_id)
    assert runtime.calls == []
    assert result.status == job.status == JobStatus.CANCELLED
    assert stages[PIPELINE_STAGES[0]].status == StageStatus.CANCELLED


@pytest.mark.asyncio
async def test_cancellation_between_stages_stops_before_next_runtime_call(tmp_path):
    database_url, job_id, _ = seed(tmp_path)
    runtime = RecordingRuntime(
        database_url,
        job_id,
        cancel_after=PIPELINE_STAGES[0],
    )

    result = await PipelineOrchestrator(database_url, runtime).run(job_id)

    job, stages = persisted(database_url, job_id)
    assert [stage for stage, _ in runtime.calls] == PIPELINE_STAGES[:1]
    assert result.status == job.status == JobStatus.CANCELLED
    assert stages[PIPELINE_STAGES[0]].status == StageStatus.COMPLETED
    assert stages[PIPELINE_STAGES[1]].status == StageStatus.CANCELLED


@pytest.mark.asyncio
async def test_cancellation_race_during_start_is_cancelled_not_failed(tmp_path, monkeypatch):
    database_url, job_id, _ = seed(tmp_path)
    runtime = RecordingRuntime()
    real_start = PipelineState.start
    raced = False

    def cancel_then_start(state, stage, input_json):
        nonlocal raced
        if not raced:
            raced = True
            state.request_cancel()
        real_start(state, stage, input_json)

    monkeypatch.setattr(PipelineState, "start", cancel_then_start)

    result = await PipelineOrchestrator(database_url, runtime).run(job_id)

    job, stages = persisted(database_url, job_id)
    assert runtime.calls == []
    assert result.status == job.status == JobStatus.CANCELLED
    assert job.error_code is None
    assert stages[PIPELINE_STAGES[0]].status == StageStatus.CANCELLED


@pytest.mark.asyncio
async def test_unsupported_job_type_fails_without_calling_runtime(tmp_path):
    database_url, job_id, _ = seed(tmp_path, job_type=JobType.STORY_GENERATION)
    runtime = RecordingRuntime()

    result = await PipelineOrchestrator(database_url, runtime).run(job_id)

    job, stages = persisted(database_url, job_id)
    assert runtime.calls == []
    assert result.status == job.status == JobStatus.FAILED
    assert job.error_code == "UNSUPPORTED_JOB_TYPE"
    assert job.error_message == "unsupported job type: STORY_GENERATION"
    assert stages[PIPELINE_STAGES[0]].status == StageStatus.FAILED


@pytest.mark.asyncio
async def test_unknown_job_raises_a_clear_error(tmp_path):
    database_url = str(tmp_path / "unknown.db")
    create_schema(database_url)

    with pytest.raises(ValueError, match="job not found: missing-job"):
        await PipelineOrchestrator(database_url, RecordingRuntime()).run("missing-job")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("invalid_output", "message"),
    [
        (["not", "a", "dict"], "runtime output must be a dict"),
        ({"not_json": {1, 2}}, "runtime output must be JSON serializable"),
    ],
)
async def test_invalid_runtime_output_fails_current_stage_without_corruption(
    tmp_path,
    invalid_output,
    message,
):
    database_url, job_id, _ = seed(tmp_path)
    runtime = InvalidOutputRuntime(invalid_output)

    result = await PipelineOrchestrator(database_url, runtime).run(job_id)

    job, stages = persisted(database_url, job_id)
    first_stage = PIPELINE_STAGES[0]
    assert result.status == job.status == JobStatus.FAILED
    assert job.current_stage == first_stage.value
    assert job.error_code == "RUNTIME_ERROR"
    assert job.error_message == message
    assert stages[first_stage].status == StageStatus.FAILED
    assert stages[first_stage].output_json is None
    assert len(runtime.calls) == 1
    json.dumps(job.error_message)
