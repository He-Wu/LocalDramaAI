# LocalDramaAI

LocalDramaAI is a Windows-first, local-only short-drama pipeline foundation. Phases 0–7 provide environment locking, FastAPI + SQLite WAL, an independent worker with atomic job claims and JobEvents, structured Ollama generation, character/Storyboard First Frame generation, local Qwen3-TTS voice cloning, and a real audio-duration-driven Wan2.2 dialogue-video output.

## Verify

```powershell
python -m pip install -e .
python -m pytest -q
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
python -m scripts.smoke_phase5
python -m scripts.smoke_phase6
python -m scripts.smoke_phase7
```

Create a project with `POST /api/projects`, then a queued job with `POST /api/jobs`. Start `python -m app.worker_main` in a second terminal to observe claim and event updates. Ollama and ComfyUI remain external local processes; their absence is surfaced as an unavailable health check.

The verified PHASE 7 MP4 is `artifacts/phase7/1786342910/video/0753ed98-8ccc-4c4c-99dc-f9fc96b822ac_00001_.mp4`. It uses the persisted Storyboard First Frame and is longer than the measured Dialogue audio. Model hashes, runtime versions, and exact verification artifacts are locked in `runtime/runtime-lock.yaml`.
