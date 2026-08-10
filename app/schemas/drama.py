from pydantic import BaseModel, Field

class CharacterSpec(BaseModel):
    name: str
    age: str = "成年"
    gender: str = "未知"
    face: str = "自然脸型"
    eyes: str = "自然眼型"
    nose: str = ""
    mouth: str = ""
    hair: str = "自然发型"
    body: str = "自然体型"
    clothes: str = "日常服装"
    accessories: str = ""
    visual_style: str = "写实"
    personality: str = ""

class DialogueSpec(BaseModel):
    shot_order: int
    character_name: str | None = None
    text: str
    emotion: str = "平静"

class ShotSpec(BaseModel):
    order: int
    scene_order: int = Field(default=1, ge=1)
    character_name: str | None = None
    title: str
    description: str
    shot_type: str = "ACTION"
    duration: float = Field(default=3.0, ge=0.1)
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
    estimated_duration: float | None = Field(default=None, ge=0)
    shots: list[ShotSpec] = Field(default_factory=list)

class StructuredDrama(BaseModel):
    title: str
    characters: list[CharacterSpec] = Field(min_length=1)
    scenes: list[SceneSpec] = Field(min_length=1)
    shots: list[ShotSpec] = Field(min_length=1)
    dialogues: list[DialogueSpec] = Field(min_length=1)
