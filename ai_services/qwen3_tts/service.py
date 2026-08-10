import gc
import os
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from app.services.audio_probe import probe_wav


class GenerateRequest(BaseModel):
    text: str
    language: str = "Chinese"
    reference_audio: str
    reference_transcript: str
    output_path: str
    model_path: str | None = None
    instruct: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class RuntimeState:
    model: Any = None
    model_path: str | None = None
    loaded: bool = False


runtime = RuntimeState()
app = FastAPI(title="LocalDramaAI Qwen3-TTS")


def validate_generate_request(request: GenerateRequest) -> None:
    if not request.text.strip(): raise ValueError("text must not be empty")
    if not request.reference_transcript.strip(): raise ValueError("reference transcript must not be empty")
    reference = Path(request.reference_audio)
    if not reference.is_file(): raise FileNotFoundError(reference)
    if reference.stat().st_size == 0: raise ValueError("reference audio is empty")
    if not request.output_path.strip(): raise ValueError("output path must not be empty")


def atomic_publish_wav(source: Path, destination: Path) -> Path:
    info = probe_wav(source)
    if info.duration <= 0: raise ValueError("generated WAV has no duration")
    destination.parent.mkdir(parents=True, exist_ok=True)
    source.replace(destination)
    return destination


def _load_model(model_path: str | None):
    if runtime.loaded and runtime.model is not None and runtime.model_path == model_path:
        return runtime.model
    import torch
    from qwen_tts import Qwen3TTSModel
    target = model_path or os.environ.get("LOCALDRAMA_QWEN3_TTS_MODEL", "Qwen/Qwen3-TTS-12Hz-0.6B-Base")
    kwargs = {"device_map": "cuda:0" if torch.cuda.is_available() else "cpu",
              "dtype": torch.bfloat16 if torch.cuda.is_available() else torch.float32}
    runtime.model = Qwen3TTSModel.from_pretrained(target, **kwargs)
    runtime.model_path = target; runtime.loaded = True
    return runtime.model


@app.get("/health")
def health():
    try:
        import torch
        cuda = torch.cuda.is_available()
        device = torch.cuda.get_device_name(0) if cuda else "cpu"
    except Exception as exc:
        cuda = False; device = f"error:{exc}"
    return {"status": "ONLINE", "loaded": runtime.loaded, "model": runtime.model_path,
            "cuda": cuda, "device": device}


@app.post("/generate")
def generate(request: GenerateRequest):
    try:
        validate_generate_request(request)
        import soundfile as sf
        model = _load_model(request.model_path)
        kwargs = {"text": request.text, "language": request.language,
                  "ref_audio": request.reference_audio, "ref_text": request.reference_transcript}
        if request.instruct: kwargs["instruct"] = request.instruct
        wavs, sample_rate = model.generate_voice_clone(**kwargs)
        temp = Path(request.output_path).with_name(Path(request.output_path).stem + ".tmp.wav")
        sf.write(str(temp), wavs[0], sample_rate, subtype="PCM_16")
        published = atomic_publish_wav(temp, Path(request.output_path))
        info = probe_wav(published)
        return {"output_path": str(published), "duration": info.duration, "sample_rate": info.sample_rate,
                "channels": info.channels, "model": runtime.model_path}
    except (ValueError, FileNotFoundError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Qwen3-TTS generation failed: {exc}") from exc


@app.post("/unload")
def unload():
    runtime.model = None; runtime.model_path = None; runtime.loaded = False
    gc.collect()
    try:
        import torch
        if torch.cuda.is_available(): torch.cuda.empty_cache(); torch.cuda.synchronize()
        cuda = torch.cuda.is_available()
    except Exception:
        cuda = False
    return {"status": "UNLOADED", "cuda": cuda, "loaded": runtime.loaded}
