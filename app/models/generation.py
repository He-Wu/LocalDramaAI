import uuid
from sqlalchemy import String, Text, Float, JSON, ForeignKey
from sqlalchemy.orm import Mapped, mapped_column
from .base import Base, TimestampMixin

class GenerationManifest(Base, TimestampMixin):
    __tablename__ = "generation_manifests"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    asset_id: Mapped[str | None] = mapped_column(ForeignKey("assets.id", ondelete="SET NULL"), nullable=True)
    provider: Mapped[str] = mapped_column(String(80))
    provider_version: Mapped[str | None] = mapped_column(String(120), nullable=True)
    model_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    prompt: Mapped[str | None] = mapped_column(Text, nullable=True)
    negative_prompt: Mapped[str | None] = mapped_column(Text, nullable=True)
    seed: Mapped[int | None] = mapped_column(nullable=True)
    workflow_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    workflow_hash: Mapped[str | None] = mapped_column(String(128), nullable=True)
    binding_version: Mapped[str | None] = mapped_column(String(50), nullable=True)
    generation_time: Mapped[float | None] = mapped_column(Float, nullable=True)
    input_assets: Mapped[list | None] = mapped_column(JSON, nullable=True)
    output_asset: Mapped[str | None] = mapped_column(String(36), nullable=True)
