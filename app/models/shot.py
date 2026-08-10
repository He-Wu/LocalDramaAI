import uuid
from sqlalchemy import String, Text, Integer, Float, Boolean, ForeignKey, false
from sqlalchemy.orm import Mapped, mapped_column, relationship
from .base import Base, TimestampMixin

class Shot(Base, TimestampMixin):
    __tablename__ = "shots"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    scene_id: Mapped[str] = mapped_column(ForeignKey("scenes.id", ondelete="CASCADE"), index=True)
    character_id: Mapped[str | None] = mapped_column(ForeignKey("characters.id", ondelete="SET NULL"), nullable=True, index=True)
    order: Mapped[int] = mapped_column(Integer)
    title: Mapped[str] = mapped_column(String(200))
    description: Mapped[str] = mapped_column(Text)
    shot_type: Mapped[str] = mapped_column(String(40), default="ACTION")
    duration: Mapped[float] = mapped_column(Float, default=3.0)
    image_prompt: Mapped[str | None] = mapped_column(Text, nullable=True)
    video_prompt: Mapped[str | None] = mapped_column(Text, nullable=True)
    negative_prompt: Mapped[str | None] = mapped_column(Text, nullable=True)
    storyboard_asset_id: Mapped[str | None] = mapped_column(ForeignKey("assets.id", ondelete="SET NULL"), nullable=True)
    video_asset_id: Mapped[str | None] = mapped_column(ForeignKey("assets.id", ondelete="SET NULL"), nullable=True)
    requires_lip_sync: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default=false())
    speaker_visible: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default=false())
    lipsync_asset_id: Mapped[str | None] = mapped_column(
        ForeignKey(
            "assets.id",
            name="fk_shots_lipsync_asset_id_assets",
            ondelete="SET NULL",
        ),
        nullable=True,
    )
    status: Mapped[str] = mapped_column(String(30), default="DRAFT")
    scene = relationship("Scene", back_populates="shots")
    dialogues = relationship("Dialogue", back_populates="shot", cascade="all, delete-orphan", order_by="Dialogue.order")
