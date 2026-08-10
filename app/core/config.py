from pathlib import Path
from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    data_root: Path = Path("E:/LocalDramaAI")
    database_url: str = "sqlite:///E:/LocalDramaAI/localdrama.db"
    ollama_url: str = "http://127.0.0.1:11434"
    ollama_model: str = "qwen2.5:7b"
    comfyui_url: str = "http://127.0.0.1:8188"
    worker_poll_seconds: float = 1.0
    model_config = SettingsConfigDict(env_file=".env", env_prefix="LOCALDRAMA_", extra="ignore")

settings = Settings()
