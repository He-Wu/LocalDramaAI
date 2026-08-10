from datetime import datetime
from pydantic import BaseModel, ConfigDict
from app.core.enums import JobStatus, JobType

class ProjectCreate(BaseModel):
    name: str
    story: str | None = None
    description: str | None = None

class ProjectRead(ProjectCreate):
    model_config = ConfigDict(from_attributes=True)
    id: str
    status: str
    created_at: datetime

class JobCreate(BaseModel):
    project_id: str
    type: JobType
    input_json: dict | None = None

class JobRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    project_id: str
    type: JobType
    status: JobStatus
    progress: float
    current_stage: str | None
    output_json: dict | None
    error_message: str | None
