import json
from copy import deepcopy

from sqlalchemy import select

from app.core.enums import PIPELINE_STAGES, JobStatus, JobType, PipelineStage, StageStatus
from app.db.session import session_scope
from app.models import GenerationJob, JobStage

from .contracts import PipelineContext, PipelineRuntime
from .state import PipelineCancellationRequested, PipelineState


class PipelineOrchestrator:
    """Run a durable pipeline and return a detached ``GenerationJob`` snapshot.

    Scalar columns are available after return; ORM relationships are not loaded.
    """

    def __init__(self, database_url: str, runtime: PipelineRuntime):
        self.database_url = database_url
        self.runtime = runtime

    def _snapshot(self, job_id: str) -> tuple[GenerationJob, dict[PipelineStage, JobStage]]:
        with session_scope(self.database_url) as session:
            job = session.get(GenerationJob, job_id)
            if job is None:
                raise ValueError(f"job not found: {job_id}")
            stages = {
                row.stage: row
                for row in session.scalars(
                    select(JobStage).where(JobStage.job_id == job_id)
                )
            }
            return job, stages

    def _result(self, job_id: str) -> GenerationJob:
        job, _ = self._snapshot(job_id)
        return job

    @staticmethod
    def _context(database_url: str, job: GenerationJob) -> PipelineContext:
        input_json = job.input_json if job.input_json is not None else {}
        if not isinstance(input_json, dict):
            raise ValueError("job input_json must be a dict")
        return PipelineContext(
            database_url=database_url,
            job_id=job.id,
            project_id=job.project_id,
            input_json=deepcopy(input_json),
        )

    @staticmethod
    def _runtime_context(context: PipelineContext) -> PipelineContext:
        return PipelineContext(
            database_url=context.database_url,
            job_id=context.job_id,
            project_id=context.project_id,
            input_json=deepcopy(context.input_json),
        )

    @staticmethod
    def _stage_input(context: PipelineContext) -> dict:
        return {
            **deepcopy(context.input_json),
            "project_id": context.project_id,
        }

    @staticmethod
    def _output_error(output: object, subject: str) -> str | None:
        if not isinstance(output, dict):
            return f"{subject} must be a dict"
        try:
            json.dumps(output, allow_nan=False)
        except (TypeError, ValueError, OverflowError):
            return f"{subject} must be JSON serializable"
        return None

    @staticmethod
    def _start_or_cancel(
        state: PipelineState,
        stage: PipelineStage,
        input_json: dict,
    ) -> bool:
        if state.cancel_requested():
            state.mark_cancelled(stage)
            return False
        try:
            state.start(stage, input_json)
        except PipelineCancellationRequested:
            state.mark_cancelled(stage)
            return False
        return True

    async def run(self, job_id: str) -> GenerationJob:
        state = PipelineState(self.database_url, job_id)
        state.initialize()
        job, stages = self._snapshot(job_id)
        context = self._context(self.database_url, job)

        if job.status == JobStatus.COMPLETED:
            return job

        if job.type != JobType.FULL_DRAMA:
            first_stage = PIPELINE_STAGES[0]
            if not self._start_or_cancel(
                state,
                first_stage,
                self._stage_input(context),
            ):
                return self._result(job_id)
            state.fail(
                first_stage,
                "UNSUPPORTED_JOB_TYPE",
                f"unsupported job type: {job.type}",
            )
            return self._result(job_id)

        last_stage_output: dict = {}
        for stage in PIPELINE_STAGES:
            stage_row = stages[stage]
            if stage_row.status == StageStatus.COMPLETED:
                output_error = self._output_error(
                    stage_row.output_json,
                    "persisted stage output",
                )
                if output_error is not None:
                    state.fail_completed_output(
                        stage,
                        "INVALID_STAGE_OUTPUT",
                        output_error,
                    )
                    return self._result(job_id)
                last_stage_output = deepcopy(stage_row.output_json)
                continue

            if not self._start_or_cancel(state, stage, self._stage_input(context)):
                return self._result(job_id)

            try:
                output = await self.runtime.execute(
                    stage,
                    self._runtime_context(context),
                )
            except Exception as exc:
                state.fail_or_cancel(
                    stage,
                    getattr(exc, "code", "RUNTIME_ERROR"),
                    str(exc),
                )
                return self._result(job_id)

            output_error = self._output_error(output, "runtime output")
            if output_error is not None:
                state.fail_or_cancel(stage, "RUNTIME_ERROR", output_error)
                return self._result(job_id)

            state.complete(stage, output)
            last_stage_output = output

        state.finish({**last_stage_output, "project_id": context.project_id})
        return self._result(job_id)
