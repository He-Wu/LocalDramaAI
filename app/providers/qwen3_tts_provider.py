from pathlib import Path
import httpx
from app.services.audio_probe import probe_wav


class Qwen3TTSProvider:
    def __init__(self, base_url: str = "http://127.0.0.1:8020", timeout: float = 600):
        self.base_url = base_url.rstrip("/"); self.timeout = timeout

    async def health(self) -> dict:
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.get(f"{self.base_url}/health"); response.raise_for_status(); return response.json()

    async def generate(self, text: str, output_path: Path, reference_path: Path, reference_transcript: str,
                       language: str = "Chinese", metadata: dict | None = None) -> Path:
        if not text.strip(): raise ValueError("dialogue text must not be empty")
        if not Path(reference_path).is_file(): raise FileNotFoundError(reference_path)
        if not reference_transcript.strip(): raise ValueError("reference transcript must not be empty")
        output_path = Path(output_path); output_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"text": text, "language": language, "reference_audio": str(reference_path),
                   "reference_transcript": reference_transcript, "output_path": str(output_path), "metadata": metadata or {}}
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            response = await client.post(f"{self.base_url}/generate", json=payload); response.raise_for_status()
        result = response.json(); result_path = Path(result.get("output_path", output_path))
        if not result_path.is_file(): raise RuntimeError("TTS service returned without a WAV output")
        probe_wav(result_path)
        return result_path

    async def unload(self) -> dict:
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.post(f"{self.base_url}/unload"); response.raise_for_status(); return response.json()
