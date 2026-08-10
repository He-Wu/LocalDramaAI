import uuid
from datetime import datetime

from sqlalchemy import JSON, DateTime, Enum, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.enums import PipelineStage, StageStatus
from .base import Base, TimestampMixin


class JobStage(Base, TimestampMixin):
    __tablename__ = "job_stages"
    __table_args__ = (UniqueConstraint("job_id", "stage", name="uq_job_stage"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    job_id: Mapped[str] = mapped_column(ForeignKey("generation_jobs.id", ondelete="CASCADE"), index=True)
    stage: Mapped[PipelineStage] = mapped_column(
        Enum(
            PipelineStage,
            values_callable=lambda enum: [member.value for member in enum],
            native_enum=False,
            validate_strings=True,
            create_constraint=True,
            name="pipeline_stage",
        )
    )
    status: Mapped[StageStatus] = mapped_column(
        Enum(
            StageStatus,
            values_callable=lambda enum: [member.value for member in enum],
            native_enum=False,
            validate_strings=True,
            create_constraint=True,
            name="stage_status",
        ),
        default=StageStatus.PENDING,
        index=True,
    )
    attempt: Mapped[int] = mapped_column(Integer, default=0)
    input_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    output_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(80), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    job = relationship("GenerationJob", back_populates="stages")
