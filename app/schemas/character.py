from pydantic import BaseModel, Field, field_validator

DEFAULT_FORBIDDEN = ["face structure", "age", "hair color", "eye shape", "clothing identity", "body type"]

class VisualBible(BaseModel):
    name: str
    age: str
    gender: str
    face: str
    eyes: str
    nose: str = ""
    mouth: str = ""
    hair: str
    body: str
    clothes: str
    accessories: str = ""
    visual_style: str
    forbidden_changes: list[str] = Field(default_factory=lambda: list(DEFAULT_FORBIDDEN))

    @field_validator("name", "age", "gender", "face", "eyes", "nose", "mouth", "hair", "body", "clothes", "accessories", "visual_style", mode="before")
    @classmethod
    def strip_text(cls, value): return value.strip() if isinstance(value, str) else value

    @field_validator("forbidden_changes")
    @classmethod
    def include_identity_locks(cls, value):
        return list(dict.fromkeys([*value, *DEFAULT_FORBIDDEN]))

class CharacterCreate(BaseModel):
    project_id: str
    name: str
    visual_bible: VisualBible
