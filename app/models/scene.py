import uuid
from sqlalchemy import String, Text, Integer, Float, ForeignKey
from sqlalchemy.orm import Mapped, mapped_column, relationship
from .base import Base, TimestampMixin

class Scene(Base, TimestampMixin):
    __tablename__ = "scenes"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    order: Mapped[int] = mapped_column(Integer)
    title: Mapped[str] = mapped_column(String(200))
    description: Mapped[str] = mapped_column(Text)
    location: Mapped[str | None] = mapped_column(String(200), nullable=True)
    time_of_day: Mapped[str | None] = mapped_column(String(80), nullable=True)
    mood: Mapped[str | None] = mapped_column(String(100), nullable=True)
    estimated_duration: Mapped[float | None] = mapped_column(Float, nullable=True)
    shots = relationship("Shot", back_populates="scene", cascade="all, delete-orphan", order_by="Shot.order")
