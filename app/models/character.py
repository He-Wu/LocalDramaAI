import uuid
from sqlalchemy import String, Text, JSON, ForeignKey
from sqlalchemy.orm import Mapped, mapped_column, relationship
from .base import Base, TimestampMixin

class Character(Base, TimestampMixin):
    __tablename__ = "characters"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(200))
    visual_bible_json: Mapped[dict] = mapped_column(JSON)
    negative_prompt: Mapped[str | None] = mapped_column(Text, nullable=True)
    primary_reference_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    references = relationship("CharacterReference", back_populates="character", cascade="all, delete-orphan")
    voice_profiles = relationship("VoiceProfile", back_populates="character", cascade="all, delete-orphan")
