from pydantic import BaseModel, Field

class CharacterSpec(BaseModel):
    name: str
    gender: str = "未知"
    appearance: str = ""
    personality: str = ""

class DialogueSpec(BaseModel):
    shot_order: int
    character_name: str | None = None
    text: str
    emotion: str = "平静"

class ShotSpec(BaseModel):
    order: int
    title: str
    description: str
    shot_type: str = "ACTION"
    duration: float = Field(default=3.0, ge=0.1)
    requires_lip_sync: bool = False
    speaker_visible: bool = False
    camera_angle: str = "平视"
    camera_movement: str = "固定"
    image_prompt: str = ""
    video_prompt: str = ""
    negative_prompt: str = ""

class SceneSpec(BaseModel):
    order: int
    title: str
    description: str
    location: str = ""
    time_of_day: str = "白天"
    mood: str = ""
    shots: list[ShotSpec] = Field(default_factory=list)

class StructuredDrama(BaseModel):
    title: str
    characters: list[CharacterSpec] = Field(min_length=1)
    scenes: list[SceneSpec] = Field(min_length=1)
    shots: list[ShotSpec] = Field(min_length=1)
    dialogues: list[DialogueSpec] = Field(min_length=1)
