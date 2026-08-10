import uuid
from sqlalchemy import String, Text, JSON, ForeignKey
from sqlalchemy.orm import Mapped, mapped_column, relationship
from .base import Base, TimestampMixin


class VoiceProfile(Base, TimestampMixin):
    __tablename__ = "voice_profiles"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    character_id: Mapped[str] = mapped_column(ForeignKey("characters.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(200))
    model_name: Mapped[str] = mapped_column(String(200), default="Qwen3-TTS-12Hz-0.6B-Base")
    language: Mapped[str] = mapped_column(String(40), default="Chinese")
    reference_asset_id: Mapped[str | None] = mapped_column(ForeignKey("assets.id", ondelete="SET NULL"), nullable=True)
    reference_transcript: Mapped[str | None] = mapped_column(Text, nullable=True)
    settings_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    character = relationship("Character", back_populates="voice_profiles")
