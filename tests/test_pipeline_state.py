from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest

from app.core.enums import PIPELINE_STAGES, JobStatus, JobType, StageStatus
from app.db.session import create_schema, session_scope
from app.models import GenerationJob, JobEvent, JobStage, Project
from app.pipeline import PipelineCancellationRequested, PipelineState


def seed(tmp_path):
    database = str(tmp_path / "state.db")
    create_schema(database)
    with session_scope(database) as session:
        project = Project(name="State")
        session.add(project)
        session.flush()
        job = GenerationJob(
            project_id=project.id,
            type=JobType.FULL_DRAMA,
            status=JobStatus.QUEUED,
        )
        session.add(job)
        session.flush()
        job_id = job.id
    return database, job_id


def persisted(database, job_id):
    with session_scope(database) as session:
        job = session.get(GenerationJob, job_id)
        stages = (
            session.query(JobStage)
            .filter_by(job_id=job_id)
            .order_by(JobStage.created_at)
            .all()
        )
        events = (
            session.query(JobEvent)
            .filter_by(job_id=job_id)
            .order_by(JobEvent.sequence)
            .all()
        )
        return job, stages, events


def test_initialize_creates_all_ordered_stages_once(tmp_path):
    database, job_id = seed(tmp_path)
    state = PipelineState(database, job_id)

    state.initialize()
    state.initialize()

    _, stages, events = persisted(database, job_id)
    assert [row.stage for row in stages] == PIPELINE_STAGES
    assert [row.status for row in stages] == [StageStatus.PENDING] * len(PIPELINE_STAGES)
    assert events == []


def test_start_and_complete_persist_snapshots_progress_and_ordered_events(tmp_path):
    database, job_id = seed(tmp_path)
    state = PipelineState(database, job_id)
    stage = PIPELINE_STAGES[0]
    state.initialize()

    input_json = {"services": ["ollama", "comfyui"]}
    state.start(stage, input_json)
    input_json["services"].append("mutated-after-start")
    state.complete(stage, {"available": 2})

    job, stages, events = persisted(database, job_id)
    assert stages[0].status == StageStatus.COMPLETED
    assert stages[0].attempt == 1
    assert stages[0].input_json == {"services": ["ollama", "comfyui"]}
    assert stages[0].output_json == {"available": 2}
    assert stages[0].started_at is not None
    assert stages[0].completed_at is not None
    assert job.status == JobStatus.RUNNING
    assert job.current_stage == stage.value
    assert job.progress == pytest.approx(1 / len(PIPELINE_STAGES))
    assert [event.sequence for event in events] == [1, 2]
    assert [event.event_type for event in events] == ["stage_started", "stage_completed"]
    assert [event.payload_json for event in events] == [
        {"stage": stage.value},
        {"stage": stage.value},
    ]


def test_fail_persists_stage_and_job_error_boundaries(tmp_path):
    database, job_id = seed(tmp_path)
    state = PipelineState(database, job_id)
    stage = PIPELINE_STAGES[0]
    state.initialize()
    state.start(stage, {})

    state.fail(stage, "SERVICE_UNAVAILABLE", "Ollama unavailable")

    job, stages, events = persisted(database, job_id)
    failed_stage = next(row for row in stages if row.stage == stage)
    assert failed_stage.status == StageStatus.FAILED
    assert failed_stage.error_code == "SERVICE_UNAVAILABLE"
    assert failed_stage.error_message == "Ollama unavailable"
    assert failed_stage.completed_at is not None
    assert job.status == JobStatus.FAILED
    assert job.error_code == "SERVICE_UNAVAILABLE"
    assert job.error_message == "Ollama unavailable"
    assert events[-1].event_type == "stage_failed"
    assert events[-1].payload_json == {"stage": stage.value, "code": "SERVICE_UNAVAILABLE"}


def test_fail_or_cancel_atomically_honors_persisted_cancellation(tmp_path):
    database, job_id = seed(tmp_path)
    state = PipelineState(database, job_id)
    stage = PIPELINE_STAGES[0]
    state.initialize()
    state.start(stage, {})
    state.request_cancel()

    outcome = state.fail_or_cancel(stage, "LATE_FAILURE", "must not win")

    job, stages, events = persisted(database, job_id)
    assert outcome == JobStatus.CANCELLED
    assert job.status == JobStatus.CANCELLED
    assert job.error_code is None
    assert stages[0].status == StageStatus.CANCELLED
    assert stages[0].error_code is None
    assert events[-1].event_type == "cancelled"


@pytest.mark.parametrize(
    "terminal_status",
    [JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELLED],
)
def test_fail_completed_output_rejects_terminal_jobs(tmp_path, terminal_status):
    database, job_id = seed(tmp_path)
    state = PipelineState(database, job_id)
    stage = PIPELINE_STAGES[0]
    state.initialize()
    state.start(stage, {})
    state.complete(stage, {"valid": True})
    with session_scope(database) as session:
        session.get(GenerationJob, job_id).status = terminal_status

    with pytest.raises(ValueError, match="cannot fail completed output for job"):
        state.fail_completed_output(
            stage,
            "INVALID_STAGE_OUTPUT",
            "must not mutate terminal job",
        )

    job, stages, events = persisted(database, job_id)
    assert job.status == terminal_status
    assert job.error_code is None
    assert stages[0].status == StageStatus.COMPLETED
    assert stages[0].error_code is None
    assert [event.event_type for event in events] == [
        "stage_started",
        "stage_completed",
    ]


def test_request_cancel_and_mark_cancelled_are_persisted(tmp_path):
    database, job_id = seed(tmp_path)
    state = PipelineState(database, job_id)
    stage = PIPELINE_STAGES[0]
    state.initialize()

    assert state.cancel_requested() is False
    state.request_cancel()
    assert state.cancel_requested() is True
    state.mark_cancelled(stage)

    job, stages, events = persisted(database, job_id)
    assert job.cancel_requested_at is not None
    assert job.status == JobStatus.CANCELLED
    assert job.completed_at is not None
    assert job.current_stage == stage.value
    assert stages[0].status == StageStatus.CANCELLED
    assert stages[0].completed_at is not None
    assert [event.event_type for event in events] == ["cancel_requested", "cancelled"]


def test_retry_from_resets_selected_and_downstream_only(tmp_path):
    database, job_id = seed(tmp_path)
    state = PipelineState(database, job_id)
    state.initialize()
    for stage in PIPELINE_STAGES[:3]:
        state.start(stage, {"stage": stage.value})
        state.complete(stage, {"done": stage.value})
    failed_stage = PIPELINE_STAGES[3]
    state.start(failed_stage, {"stage": failed_stage.value})
    state.request_cancel()
    state.fail(failed_stage, "RENDER_ERROR", "render failed")

    state.retry_from(PIPELINE_STAGES[2])

    job, stages, events = persisted(database, job_id)
    assert [row.status for row in stages[:2]] == [StageStatus.COMPLETED] * 2
    assert [row.output_json for row in stages[:2]] == [
        {"done": PIPELINE_STAGES[0].value},
        {"done": PIPELINE_STAGES[1].value},
    ]
    for row in stages[2:]:
        assert row.status == StageStatus.PENDING
        assert row.output_json is None
        assert row.error_code is None
        assert row.error_message is None
        assert row.started_at is None
        assert row.completed_at is None
    assert stages[2].attempt == 1
    assert job.status == JobStatus.QUEUED
    assert job.current_stage == PIPELINE_STAGES[2].value
    assert job.progress == pytest.approx(3 / len(PIPELINE_STAGES))
    assert job.retry_count == 1
    assert job.error_code is None
    assert job.error_message is None
    assert job.cancel_requested_at is None
    assert events[-1].event_type == "retry_queued"


def test_finish_persists_terminal_job_output(tmp_path):
    database, job_id = seed(tmp_path)
    state = PipelineState(database, job_id)
    state.initialize()
    for stage in PIPELINE_STAGES:
        state.start(stage, {})
        state.complete(stage, {"stage": stage.value})

    state.finish({"manifest": "manifest.json"})

    job, _, events = persisted(database, job_id)
    assert job.status == JobStatus.COMPLETED
    assert job.progress == 1.0
    assert job.current_stage == "complete"
    assert job.output_json == {"manifest": "manifest.json"}
    assert job.completed_at is not None
    assert events[-1].event_type == "completed"
    assert events[-1].payload_json == {"manifest": "manifest.json"}


@pytest.mark.parametrize(
    "operation",
    [
        lambda state: state.initialize(),
        lambda state: state.cancel_requested(),
        lambda state: state.request_cancel(),
        lambda state: state.finish({}),
    ],
)
def test_unknown_job_fails_clearly(tmp_path, operation):
    database = str(tmp_path / "unknown.db")
    create_schema(database)

    with pytest.raises(ValueError, match="job not found"):
        operation(PipelineState(database, "missing-job"))


def test_invalid_transitions_do_not_corrupt_a_completed_stage(tmp_path):
    database, job_id = seed(tmp_path)
    state = PipelineState(database, job_id)
    stage = PIPELINE_STAGES[0]
    state.initialize()
    state.start(stage, {"original": True})
    state.complete(stage, {"kept": True})

    with pytest.raises(ValueError, match="cannot start"):
        state.start(stage, {"replacement": True})
    with pytest.raises(ValueError, match="cannot fail"):
        state.fail(stage, "LATE_ERROR", "too late")
    with pytest.raises(ValueError, match="cannot cancel"):
        state.mark_cancelled(stage)
    with pytest.raises(ValueError, match="cannot complete"):
        state.complete(PIPELINE_STAGES[1], {})

    job, stages, events = persisted(database, job_id)
    assert stages[0].status == StageStatus.COMPLETED
    assert stages[0].attempt == 1
    assert stages[0].input_json == {"original": True}
    assert stages[0].output_json == {"kept": True}
    assert job.status == JobStatus.RUNNING
    assert [event.event_type for event in events] == ["stage_started", "stage_completed"]


def test_start_rejects_out_of_order_stage_without_mutation(tmp_path):
    database, job_id = seed(tmp_path)
    state = PipelineState(database, job_id)
    state.initialize()

    with pytest.raises(ValueError, match="preceding stages must be completed"):
        state.start(PIPELINE_STAGES[2], {"out_of_order": True})

    job, stages, events = persisted(database, job_id)
    assert job.status == JobStatus.QUEUED
    assert job.current_stage is None
    assert job.progress == 0
    assert [row.status for row in stages] == [StageStatus.PENDING] * len(PIPELINE_STAGES)
    assert [row.attempt for row in stages] == [0] * len(PIPELINE_STAGES)
    assert events == []


def test_start_rejects_when_another_stage_is_already_running(tmp_path):
    database, job_id = seed(tmp_path)
    state = PipelineState(database, job_id)
    state.initialize()
    with session_scope(database) as session:
        rogue = session.query(JobStage).filter_by(
            job_id=job_id,
            stage=PIPELINE_STAGES[-1],
        ).one()
        rogue.status = StageStatus.RUNNING

    with pytest.raises(ValueError, match="another stage is already running"):
        state.start(PIPELINE_STAGES[0], {})

    job, stages, events = persisted(database, job_id)
    assert job.status == JobStatus.QUEUED
    assert stages[0].status == StageStatus.PENDING
    assert stages[-1].status == StageStatus.RUNNING
    assert events == []


@pytest.mark.parametrize("operation", ["complete", "fail"])
def test_complete_and_fail_require_current_stage_agreement(tmp_path, operation):
    database, job_id = seed(tmp_path)
    state = PipelineState(database, job_id)
    stage = PIPELINE_STAGES[0]
    state.initialize()
    state.start(stage, {"original": True})
    with session_scope(database) as session:
        session.get(GenerationJob, job_id).current_stage = PIPELINE_STAGES[1].value

    with pytest.raises(ValueError, match="is not the current stage"):
        if operation == "complete":
            state.complete(stage, {"should_not": "persist"})
        else:
            state.fail(stage, "WRONG_STAGE", "should not persist")

    job, stages, events = persisted(database, job_id)
    assert job.status == JobStatus.RUNNING
    assert job.error_code is None
    assert stages[0].status == StageStatus.RUNNING
    assert stages[0].output_json is None
    assert stages[0].error_code is None
    assert [event.event_type for event in events] == ["stage_started"]


def test_progress_is_monotonic_across_ordered_stages(tmp_path):
    database, job_id = seed(tmp_path)
    state = PipelineState(database, job_id)
    state.initialize()
    observed = [0.0]

    for stage in PIPELINE_STAGES:
        state.start(stage, {})
        with session_scope(database) as session:
            observed.append(session.get(GenerationJob, job_id).progress)
        state.complete(stage, {})
        with session_scope(database) as session:
            observed.append(session.get(GenerationJob, job_id).progress)

    assert observed == sorted(observed)
    assert observed[-1] == 1.0


def test_retry_from_earlier_stage_does_not_move_progress_backwards(tmp_path):
    database, job_id = seed(tmp_path)
    state = PipelineState(database, job_id)
    state.initialize()
    for stage in PIPELINE_STAGES[:4]:
        state.start(stage, {})
        state.complete(stage, {})
    failed_stage = PIPELINE_STAGES[4]
    state.start(failed_stage, {})
    state.fail(failed_stage, "TRANSIENT", "retry earlier work")
    with session_scope(database) as session:
        progress_before_retry = session.get(GenerationJob, job_id).progress

    state.retry_from(PIPELINE_STAGES[2])

    job, _, events = persisted(database, job_id)
    assert job.current_stage == PIPELINE_STAGES[2].value
    assert job.progress == progress_before_retry
    assert events[-1].event_type == "retry_queued"
    assert events[-1].progress == progress_before_retry


def test_start_rejects_pending_cancellation_without_mutation(tmp_path):
    database, job_id = seed(tmp_path)
    state = PipelineState(database, job_id)
    state.initialize()
    state.request_cancel()

    with pytest.raises(PipelineCancellationRequested, match="cancellation is requested"):
        state.start(PIPELINE_STAGES[0], {})

    job, stages, events = persisted(database, job_id)
    assert job.status == JobStatus.QUEUED
    assert job.progress == 0
    assert stages[0].status == StageStatus.PENDING
    assert stages[0].attempt == 0
    assert [event.event_type for event in events] == ["cancel_requested"]


def test_finish_honors_cancellation_after_all_stages_complete(tmp_path):
    database, job_id = seed(tmp_path)
    state = PipelineState(database, job_id)
    state.initialize()
    for stage in PIPELINE_STAGES:
        state.start(stage, {})
        state.complete(stage, {})
    state.request_cancel()

    state.finish({"must_not": "publish"})

    job, stages, events = persisted(database, job_id)
    assert job.status == JobStatus.CANCELLED
    assert job.progress == 1.0
    assert job.current_stage == PIPELINE_STAGES[-1].value
    assert job.output_json is None
    assert job.completed_at is not None
    assert all(row.status == StageStatus.COMPLETED for row in stages)
    assert [event.event_type for event in events[-2:]] == ["cancel_requested", "cancelled"]
    assert all(event.event_type != "completed" for event in events)


def test_request_cancel_is_idempotent(tmp_path):
    database, job_id = seed(tmp_path)
    state = PipelineState(database, job_id)
    state.initialize()
    state.request_cancel()
    with session_scope(database) as session:
        first_requested_at = session.get(GenerationJob, job_id).cancel_requested_at

    state.request_cancel()

    job, _, events = persisted(database, job_id)
    assert job.cancel_requested_at == first_requested_at
    assert [event.event_type for event in events] == ["cancel_requested"]


def test_mark_cancelled_running_stage_requires_current_stage_agreement(tmp_path):
    database, job_id = seed(tmp_path)
    state = PipelineState(database, job_id)
    state.initialize()
    state.start(PIPELINE_STAGES[0], {})
    with session_scope(database) as session:
        session.get(GenerationJob, job_id).current_stage = PIPELINE_STAGES[1].value

    with pytest.raises(ValueError, match="is not the current stage"):
        state.mark_cancelled(PIPELINE_STAGES[0])

    job, stages, events = persisted(database, job_id)
    assert job.status == JobStatus.RUNNING
    assert stages[0].status == StageStatus.RUNNING
    assert [event.event_type for event in events] == ["stage_started"]


def test_terminal_job_guards_are_atomic_with_stage_transitions(tmp_path):
    database, job_id = seed(tmp_path)
    state = PipelineState(database, job_id)
    state.initialize()
    for stage in PIPELINE_STAGES:
        state.start(stage, {})
        state.complete(stage, {"stage": stage.value})
    state.finish({"manifest": "manifest.json"})

    with pytest.raises(ValueError, match="cannot start job"):
        state.start(PIPELINE_STAGES[-1], {})
    with pytest.raises(ValueError, match="cannot request cancellation"):
        state.request_cancel()
    with pytest.raises(ValueError, match="cannot finish job"):
        state.finish({"replacement": True})

    job, _, events = persisted(database, job_id)
    assert job.status == JobStatus.COMPLETED
    assert job.output_json == {"manifest": "manifest.json"}
    assert [event.event_type for event in events].count("completed") == 1
    assert [event.event_type for event in events].count("cancel_requested") == 0


def test_retry_rejects_an_incomplete_prefix_without_mutation(tmp_path):
    database, job_id = seed(tmp_path)
    state = PipelineState(database, job_id)
    state.initialize()
    state.start(PIPELINE_STAGES[0], {})
    state.fail(PIPELINE_STAGES[0], "FAILED_FIRST", "first stage failed")

    with pytest.raises(ValueError, match="preceding stages must be completed"):
        state.retry_from(PIPELINE_STAGES[3])

    job, stages, events = persisted(database, job_id)
    assert job.status == JobStatus.FAILED
    assert job.retry_count == 0
    assert stages[0].status == StageStatus.FAILED
    assert [event.event_type for event in events] == ["stage_started", "stage_failed"]


def test_only_one_concurrent_retry_can_requeue_a_failed_job(tmp_path):
    database, job_id = seed(tmp_path)
    state = PipelineState(database, job_id)
    state.initialize()
    state.start(PIPELINE_STAGES[0], {})
    state.fail(PIPELINE_STAGES[0], "TRANSIENT", "try again")
    barrier = Barrier(3)

    def retry_after_barrier():
        barrier.wait()
        try:
            state.retry_from(PIPELINE_STAGES[0])
        except ValueError as exc:
            return str(exc)
        return None

    with ThreadPoolExecutor(max_workers=2) as executor:
        retries = [executor.submit(retry_after_barrier) for _ in range(2)]
        barrier.wait()
        results = [retry.result(timeout=10) for retry in retries]

    job, stages, events = persisted(database, job_id)
    assert sum(result is None for result in results) == 1
    assert sum("cannot retry job" in result for result in results if result) == 1
    assert job.status == JobStatus.QUEUED
    assert job.retry_count == 1
    assert stages[0].status == StageStatus.PENDING
    assert [event.event_type for event in events].count("retry_queued") == 1


def test_concurrent_cancel_events_have_unique_ordered_sequences(tmp_path):
    database, job_id = seed(tmp_path)
    state = PipelineState(database, job_id)
    state.initialize()
    state.start(PIPELINE_STAGES[0], {})
    barrier = Barrier(3)

    def after_barrier(operation):
        barrier.wait()
        operation()

    with ThreadPoolExecutor(max_workers=2) as executor:
        cancel = executor.submit(after_barrier, state.request_cancel)
        second_cancel = executor.submit(after_barrier, state.request_cancel)
        barrier.wait()
        cancel.result(timeout=10)
        second_cancel.result(timeout=10)

    job, _, events = persisted(database, job_id)
    assert [event.sequence for event in events] == [1, 2]
    assert [event.event_type for event in events] == [
        "stage_started",
        "cancel_requested",
    ]
    assert job.cancel_requested_at is not None
