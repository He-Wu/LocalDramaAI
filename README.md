# LocalDramaAI

LocalDramaAI is a Windows-first, local-only short-drama pipeline foundation. Phases 0–8 provide environment locking, FastAPI + SQLite WAL, an independent worker with atomic job claims and JobEvents, structured Ollama generation, character/Storyboard First Frame generation, local Qwen3-TTS voice cloning, a real audio-duration-driven Wan2.2 dialogue-video output, and eligible-shot lip sync with official MuseTalk 1.5.

## Verify

```powershell
python -m pip install -e .
python -m pytest -q
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
python -m scripts.smoke_phase5
python -m scripts.smoke_phase6
python -m scripts.smoke_phase7
python -m scripts.verify_phase8
```

Create a project with `POST /api/projects`, then a queued job with `POST /api/jobs`. Start `python -m app.worker_main` in a second terminal to observe claim and event updates. Ollama and ComfyUI remain external local processes; their absence is surfaced as an unavailable health check.

The verified PHASE 8 MP4 is `artifacts/phase8/20260812-205621-fad213b5/output/musetalk-e0eac769dadc4242ae8b6ac2f9ea55ab.mp4`. It is a 640x368, 25 FPS H.264/yuv420p + AAC derivative of the immutable PHASE 7 video and Dialogue WAV. The SQLite link graph, provider manifest, review frames, resource samples, exact runtime, model revisions, byte sizes, and SHA256 hashes are locked in `runtime/runtime-lock.yaml`, `models/models.yaml`, and the Phase 8 evidence JSON.

Phase 8 only changes the mouth region for a single eligible visible-speaker Shot. The accepted source remains slightly soft, and this phase does not add subtitles, edit multiple shots, or perform final timeline rendering.
