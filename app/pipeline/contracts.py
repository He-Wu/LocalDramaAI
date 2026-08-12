from dataclasses import dataclass
from typing import Protocol

from app.core.enums import PipelineStage


@dataclass(frozen=True)
class PipelineContext:
    database_url: str
    job_id: str
    project_id: str
    input_json: dict


class PipelineRuntime(Protocol):
    async def execute(self, stage: PipelineStage, context: PipelineContext) -> dict: ...
