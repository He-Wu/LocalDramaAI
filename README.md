# LocalDramaAI

LocalDramaAI is a Windows-first, local-only short-drama pipeline foundation. Phases 0–9 provide environment locking, FastAPI + SQLite WAL, an independent worker, structured local generation, character/storyboard assets, Qwen3-TTS voice cloning, Wan2.2 dialogue video, MuseTalk lip sync, deterministic SRT generation, and a real multi-shot FFmpeg final render.

## Verify

```powershell
python -m pip install -e .
python -m pytest -q
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
python -m scripts.smoke_phase5
python -m scripts.smoke_phase6
python -m scripts.smoke_phase7
python -m scripts.verify_phase9 E:\kang\github\Movie\artifacts\phase9\20260812-final-v2\evidence.json --evidence-root E:\kang\github\Movie
```

Create a project with `POST /api/projects`, then a queued job with `POST /api/jobs`. Start `python -m app.worker_main` in a second terminal to observe claim and event updates. Ollama and ComfyUI remain external local processes; their absence is surfaced as an unavailable health check.

The verified PHASE 8 MP4 is `artifacts/phase8/20260812-205621-fad213b5/output/musetalk-e0eac769dadc4242ae8b6ac2f9ea55ab.mp4`. It is a 640x368, 25 FPS H.264/yuv420p + AAC derivative of the immutable PHASE 7 video and Dialogue WAV. The SQLite link graph, provider manifest, review frames, resource samples, exact runtime, model revisions, byte sizes, and SHA256 hashes are locked in `runtime/runtime-lock.yaml`, `models/models.yaml`, and the Phase 8 evidence JSON.

The verified Phase 9 output is `artifacts/phase9/20260812-final-v2/.../output/final.mp4`: H.264/yuv420p 640x368 at 25 FPS with AAC stereo 48 kHz, 145 frames/5.8 seconds, burned Chinese subtitles, immutable Asset/Manifest provenance, and SHA256 `b5c2bb824e8485530191ec4daed59b80d8855c415678424a31f8558f9a7a0a45`. Phase 9 is manually seeded evidence; Phase 10 job orchestration, BGM, broader quality gates, reliability expansion, and UI are not included.
