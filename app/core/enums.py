from enum import StrEnum

class JobStatus(StrEnum):
    PENDING = "PENDING"; QUEUED = "QUEUED"; CLAIMED = "CLAIMED"; PREPARING = "PREPARING"; RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"; FAILED = "FAILED"; CANCELLED = "CANCELLED"; INTERRUPTED = "INTERRUPTED"

class JobType(StrEnum):
    STORY_GENERATION = "STORY_GENERATION"; CHARACTER_GENERATION = "CHARACTER_GENERATION"; CHARACTER_IMAGE = "CHARACTER_IMAGE"
    STORYBOARD = "STORYBOARD"; TTS = "TTS"; VIDEO = "VIDEO"; LIPSYNC = "LIPSYNC"; SUBTITLE = "SUBTITLE"; UPSCALE = "UPSCALE"; RENDER = "RENDER"
    FULL_DRAMA = "FULL_DRAMA"

class StageStatus(StrEnum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"

class PipelineStage(StrEnum):
    ENVIRONMENT_CHECK = "environment_check"
    SCRIPT_STRUCTURE = "script_structure"
    CHARACTER_MASTER = "character_master"
    STORYBOARD = "storyboard"
    DIALOGUE_AUDIO = "dialogue_audio"
    RELEASE_TTS_GPU = "release_tts_gpu"
    SHOT_VIDEO = "shot_video"
    SHOT_MUX = "shot_mux"
    FINAL_CONCAT = "final_concat"
    SUBTITLE_EXPORT = "subtitle_export"
    MANIFEST_EXPORT = "manifest_export"

PIPELINE_STAGES = list(PipelineStage)
