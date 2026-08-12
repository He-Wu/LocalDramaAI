from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Iterator

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.enums import PIPELINE_STAGES, JobStatus, PipelineStage, StageStatus
from app.db.session import session_scope
from app.models import GenerationJob, JobEvent, JobStage


class PipelineState:
    def __init__(self, database_url: str, job_id: str):
        self.database_url = database_url
        self.job_id = job_id

    @contextmanager
    def _write_session(self) -> Iterator[Session]:
        with session_scope(self.database_url) as session:
            if session.get_bind().dialect.name == "sqlite":
                session.connection().exec_driver_sql("BEGIN IMMEDIATE")
            yield session

    def _job(self, session: Session, *, for_update: bool = True) -> GenerationJob:
        statement = select(GenerationJob).where(GenerationJob.id == self.job_id)
        if for_update and session.get_bind().dialect.name != "sqlite":
            statement = statement.with_for_update()
        job = session.execute(statement).scalar_one_or_none()
        if job is None:
            raise ValueError(f"job not found: {self.job_id}")
        return job

    @staticmethod
    def _stage_index(stage: PipelineStage) -> int:
        if not isinstance(stage, PipelineStage):
            raise ValueError(f"unknown pipeline stage: {stage}")
        return PIPELINE_STAGES.index(stage)

    def _stage(self, session: Session, stage: PipelineStage) -> JobStage:
        self._stage_index(stage)
        row = session.execute(
            select(JobStage).where(
                JobStage.job_id == self.job_id,
                JobStage.stage == stage,
            )
        ).scalar_one_or_none()
        if row is None:
            raise ValueError(f"stage not initialized: {stage.value}")
        return row

    def _stages(self, session: Session) -> list[JobStage]:
        return list(
            session.scalars(
                select(JobStage).where(JobStage.job_id == self.job_id)
            )
        )

    @staticmethod
    def _require_status(
        row: JobStage,
        operation: str,
        allowed: tuple[StageStatus, ...],
    ) -> None:
        if row.status not in allowed:
            raise ValueError(
                f"cannot {operation} stage {row.stage.value} from {row.status.value}"
            )

    @staticmethod
    def _require_job_status(
        job: GenerationJob,
        operation: str,
        allowed: tuple[JobStatus, ...],
    ) -> None:
        if job.status not in allowed:
            raise ValueError(f"cannot {operation} job {job.id} from {job.status}")

    @staticmethod
    def _require_current_stage(job: GenerationJob, stage: PipelineStage) -> None:
        if job.current_stage != stage.value:
            raise ValueError(f"stage {stage.value} is not the current stage")

    @staticmethod
    def _require_completed_prefix(rows: list[JobStage], index: int) -> None:
        if any(
            PIPELINE_STAGES.index(row.stage) < index
            and row.status != StageStatus.COMPLETED
            for row in rows
        ):
            raise ValueError("preceding stages must be completed")

    def _event(
        self,
        session: Session,
        event_type: str,
        progress: float,
        message: str,
        payload: dict | None = None,
    ) -> None:
        latest = (
            session.query(func.max(JobEvent.sequence))
            .filter(JobEvent.job_id == self.job_id)
            .scalar()
        )
        session.add(
            JobEvent(
                job_id=self.job_id,
                sequence=(latest or 0) + 1,
                event_type=event_type,
                progress=progress,
                message=message,
                payload_json=payload,
            )
        )

    def initialize(self) -> None:
        with self._write_session() as session:
            self._job(session)
            existing = set(
                session.scalars(
                    select(JobStage.stage).where(JobStage.job_id == self.job_id)
                )
            )
            for stage in PIPELINE_STAGES:
                if stage not in existing:
                    session.add(
                        JobStage(
                            job_id=self.job_id,
                            stage=stage,
                            status=StageStatus.PENDING,
                        )
                    )

    def start(self, stage: PipelineStage, input_json: dict) -> None:
        index = self._stage_index(stage)
        with self._write_session() as session:
            job = self._job(session)
            self._require_job_status(
                job,
                "start",
                (
                    JobStatus.QUEUED,
                    JobStatus.CLAIMED,
                    JobStatus.PREPARING,
                    JobStatus.RUNNING,
                ),
            )
            if job.cancel_requested_at is not None:
                raise ValueError("cannot start stage while cancellation is requested")
            rows = self._stages(session)
            row = next((item for item in rows if item.stage == stage), None)
            if row is None:
                raise ValueError(f"stage not initialized: {stage.value}")
            self._require_status(row, "start", (StageStatus.PENDING,))
            self._require_completed_prefix(rows, index)
            if any(
                item.id != row.id and item.status == StageStatus.RUNNING
                for item in rows
            ):
                raise ValueError("another stage is already running")
            row.status = StageStatus.RUNNING
            row.attempt += 1
            row.input_json = input_json
            row.error_code = None
            row.error_message = None
            row.started_at = datetime.now(timezone.utc)
            row.completed_at = None
            job.status = JobStatus.RUNNING
            job.current_stage = stage.value
            job.progress = max(job.progress, index / len(PIPELINE_STAGES))
            self._event(
                session,
                "stage_started",
                job.progress,
                f"Started {stage.value}",
                {"stage": stage.value},
            )

    def complete(self, stage: PipelineStage, output_json: dict) -> None:
        index = self._stage_index(stage)
        with self._write_session() as session:
            job = self._job(session)
            self._require_job_status(job, "complete", (JobStatus.RUNNING,))
            row = self._stage(session, stage)
            self._require_status(row, "complete", (StageStatus.RUNNING,))
            self._require_current_stage(job, stage)
            row.status = StageStatus.COMPLETED
            row.output_json = output_json
            row.completed_at = datetime.now(timezone.utc)
            job.progress = max(job.progress, (index + 1) / len(PIPELINE_STAGES))
            self._event(
                session,
                "stage_completed",
                job.progress,
                f"Completed {stage.value}",
                {"stage": stage.value},
            )

    def fail(self, stage: PipelineStage, code: str, message: str) -> None:
        self._stage_index(stage)
        with self._write_session() as session:
            job = self._job(session)
            self._require_job_status(job, "fail", (JobStatus.RUNNING,))
            row = self._stage(session, stage)
            self._require_status(row, "fail", (StageStatus.RUNNING,))
            self._require_current_stage(job, stage)
            row.status = StageStatus.FAILED
            row.error_code = code
            row.error_message = message
            row.completed_at = datetime.now(timezone.utc)
            job.status = JobStatus.FAILED
            job.error_code = code
            job.error_message = message
            self._event(
                session,
                "stage_failed",
                job.progress,
                message,
                {"stage": stage.value, "code": code},
            )

    def finish(self, output_json: dict) -> None:
        with self._write_session() as session:
            job = self._job(session)
            self._require_job_status(job, "finish", (JobStatus.RUNNING,))
            statuses = list(
                session.scalars(
                    select(JobStage.status).where(JobStage.job_id == self.job_id)
                )
            )
            if len(statuses) != len(PIPELINE_STAGES) or any(
                status != StageStatus.COMPLETED for status in statuses
            ):
                raise ValueError("cannot finish pipeline before all stages are completed")
            if job.cancel_requested_at is not None:
                final_stage = PIPELINE_STAGES[-1]
                job.status = JobStatus.CANCELLED
                job.progress = 1.0
                job.current_stage = final_stage.value
                job.output_json = None
                job.completed_at = datetime.now(timezone.utc)
                self._event(
                    session,
                    "cancelled",
                    1.0,
                    "Cancelled before pipeline completion",
                    {"stage": final_stage.value},
                )
                return
            job.status = JobStatus.COMPLETED
            job.progress = 1.0
            job.current_stage = "complete"
            job.output_json = output_json
            job.completed_at = datetime.now(timezone.utc)
            self._event(session, "completed", 1.0, "Pipeline completed", output_json)

    def request_cancel(self) -> None:
        with self._write_session() as session:
            job = self._job(session)
            if job.cancel_requested_at is not None:
                return
            self._require_job_status(
                job,
                "request cancellation for",
                (
                    JobStatus.QUEUED,
                    JobStatus.CLAIMED,
                    JobStatus.RUNNING,
                    JobStatus.INTERRUPTED,
                ),
            )
            job.cancel_requested_at = datetime.now(timezone.utc)
            self._event(
                session,
                "cancel_requested",
                job.progress,
                "Cancellation requested",
            )

    def cancel_requested(self) -> bool:
        with session_scope(self.database_url) as session:
            job = self._job(session, for_update=False)
            return job.cancel_requested_at is not None

    def mark_cancelled(self, stage: PipelineStage) -> None:
        index = self._stage_index(stage)
        with self._write_session() as session:
            job = self._job(session)
            self._require_job_status(
                job,
                "cancel",
                (
                    JobStatus.QUEUED,
                    JobStatus.CLAIMED,
                    JobStatus.RUNNING,
                    JobStatus.INTERRUPTED,
                ),
            )
            rows = self._stages(session)
            row = next((item for item in rows if item.stage == stage), None)
            if row is None:
                raise ValueError(f"stage not initialized: {stage.value}")
            self._require_status(
                row,
                "cancel",
                (StageStatus.PENDING, StageStatus.RUNNING),
            )
            if row.status == StageStatus.RUNNING:
                self._require_current_stage(job, stage)
            else:
                self._require_completed_prefix(rows, index)
                if any(item.status == StageStatus.RUNNING for item in rows):
                    raise ValueError("another stage is already running")
            completed_at = datetime.now(timezone.utc)
            row.status = StageStatus.CANCELLED
            row.completed_at = completed_at
            job.status = JobStatus.CANCELLED
            job.current_stage = stage.value
            job.progress = max(job.progress, index / len(PIPELINE_STAGES))
            job.completed_at = completed_at
            self._event(
                session,
                "cancelled",
                job.progress,
                f"Cancelled before {stage.value}",
            )

    def retry_from(self, stage: PipelineStage) -> None:
        start = self._stage_index(stage)
        with self._write_session() as session:
            job = self._job(session)
            self._require_job_status(
                job,
                "retry",
                (
                    JobStatus.FAILED,
                    JobStatus.INTERRUPTED,
                    JobStatus.CANCELLED,
                ),
            )
            rows = self._stages(session)
            if not any(row.stage == stage for row in rows):
                raise ValueError(f"stage not initialized: {stage.value}")
            try:
                self._require_completed_prefix(rows, start)
            except ValueError as exc:
                raise ValueError(
                    "preceding stages must be completed before retry"
                ) from exc
            for row in rows:
                if PIPELINE_STAGES.index(row.stage) >= start:
                    row.status = StageStatus.PENDING
                    row.output_json = None
                    row.error_code = None
                    row.error_message = None
                    row.started_at = None
                    row.completed_at = None
            job.status = JobStatus.QUEUED
            job.current_stage = stage.value
            job.progress = max(job.progress, start / len(PIPELINE_STAGES))
            job.error_code = None
            job.error_message = None
            job.cancel_requested_at = None
            job.completed_at = None
            job.retry_count += 1
            self._event(
                session,
                "retry_queued",
                job.progress,
                f"Retry queued from {stage.value}",
            )
