import json
from copy import deepcopy

from sqlalchemy import select

from app.core.enums import PIPELINE_STAGES, JobStatus, JobType, PipelineStage, StageStatus
from app.db.session import session_scope
from app.models import GenerationJob, JobStage

from .contracts import PipelineContext, PipelineRuntime
from .state import PipelineState


class PipelineOrchestrator:
    """Run a durable pipeline and return the refreshed ``GenerationJob``."""

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
        except ValueError as exc:
            if str(exc) != "cannot start stage while cancellation is requested":
                raise
            state.mark_cancelled(stage)
            return False
        return True

    async def run(self, job_id: str) -> GenerationJob:
        state = PipelineState(self.database_url, job_id)
        state.initialize()
        job, stages = self._snapshot(job_id)
        context = self._context(self.database_url, job)
        stage_input = {**context.input_json, "project_id": context.project_id}

        if job.status == JobStatus.COMPLETED:
            return job

        if job.type != JobType.FULL_DRAMA:
            first_stage = PIPELINE_STAGES[0]
            if not self._start_or_cancel(state, first_stage, stage_input):
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
                last_stage_output = stage_row.output_json or {}
                continue

            if not self._start_or_cancel(state, stage, stage_input):
                return self._result(job_id)

            try:
                output = await self.runtime.execute(stage, context)
            except Exception as exc:
                state.fail(
                    stage,
                    getattr(exc, "code", "RUNTIME_ERROR"),
                    str(exc),
                )
                return self._result(job_id)

            if not isinstance(output, dict):
                state.fail(stage, "RUNTIME_ERROR", "runtime output must be a dict")
                return self._result(job_id)
            try:
                json.dumps(output, allow_nan=False)
            except (TypeError, ValueError, OverflowError):
                state.fail(
                    stage,
                    "RUNTIME_ERROR",
                    "runtime output must be JSON serializable",
                )
                return self._result(job_id)

            state.complete(stage, output)
            last_stage_output = output

        state.finish({"project_id": context.project_id, **last_stage_output})
        return self._result(job_id)
