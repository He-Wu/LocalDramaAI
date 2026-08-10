import uuid
from sqlalchemy import String, Text, Integer, ForeignKey
from sqlalchemy.orm import Mapped, mapped_column, relationship
from .base import Base, TimestampMixin

class CharacterReference(Base, TimestampMixin):
    __tablename__ = "character_references"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    character_id: Mapped[str] = mapped_column(ForeignKey("characters.id", ondelete="CASCADE"), index=True)
    asset_id: Mapped[str] = mapped_column(ForeignKey("assets.id", ondelete="CASCADE"))
    type: Mapped[str] = mapped_column(String(30), default="MASTER")
    prompt: Mapped[str] = mapped_column(Text)
    seed: Mapped[int] = mapped_column(Integer)
    model: Mapped[str | None] = mapped_column(String(200), nullable=True)
    workflow: Mapped[str | None] = mapped_column(String(200), nullable=True)
    manifest_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    character = relationship("Character", back_populates="references")
