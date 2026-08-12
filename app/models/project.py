import uuid
from sqlalchemy import ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship
from .base import Base, TimestampMixin

class Project(Base, TimestampMixin):
    __tablename__ = "projects"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    name: Mapped[str] = mapped_column(String(200))
    story: Mapped[str | None] = mapped_column(Text, nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    language: Mapped[str] = mapped_column(String(20), default="zh-CN")
    style: Mapped[str | None] = mapped_column(String(100), nullable=True)
    status: Mapped[str] = mapped_column(String(30), default="DRAFT")
    subtitle_asset_id: Mapped[str | None] = mapped_column(
        ForeignKey(
            "assets.id",
            name="fk_projects_subtitle_asset_id_assets",
            ondelete="SET NULL",
        ),
        nullable=True,
    )
    final_video_asset_id: Mapped[str | None] = mapped_column(
        ForeignKey(
            "assets.id",
            name="fk_projects_final_video_asset_id_assets",
            ondelete="SET NULL",
        ),
        nullable=True,
    )
    jobs = relationship("GenerationJob", back_populates="project", cascade="all, delete-orphan")
