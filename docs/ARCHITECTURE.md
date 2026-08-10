# Architecture

FastAPI (`python -m uvicorn app.main:app`) owns short REST requests and SQLite reads. `python -m app.worker_main` is a separate worker process. The worker atomically claims queued jobs inside `BEGIN IMMEDIATE`, writes progress to `job_events`, and never runs inside an API request.

Provider boundaries are deliberately small: `OllamaProvider` handles local structured text generation and unload, while `ComfyUIClient` handles health, prompt submission, history polling, and atomic output downloads. Generated files are referenced by `assets`; `generation_manifests` record the minimal provider/model/prompt/workflow provenance required in Phase 3.

Phase 4 separates character identity from shot descriptions. `VisualBible` is persisted in `characters.visual_bible_json`; MASTER references are stored in `character_references`, and storyboard images are linked from `shots.storyboard_asset_id`. Storyboard generation uses the approved MASTER image through ComfyUI's built-in `LoadImage`/`VAEEncode` path.

Phase 5 adds `ComfyUIVideoProvider` and `FFmpegProvider`. The video provider submits the bound Wan2.2 image-to-video workflow, downloads the real ComfyUI output, performs an atomic H.264 MP4 conversion, and persists a `VIDEO` `Asset` with a `GenerationManifest`. Video outputs remain immutable; source WebM and manifest paths are retained for auditability.

Phase 6 adds a loopback-only Qwen3-TTS service on port 8020, isolated from the application and ComfyUI Python environments. `Qwen3TTSProvider` calls the service, while `generate_dialogue_audio` owns the short persistence transaction: it validates the real WAV first, then creates an AUDIO Asset and GenerationManifest and updates Dialogue/Shot timing. The TTS model is explicitly unloaded after generation so Wan remains the exclusive GPU owner during video jobs.

Phase 7 adds `generate_dialogue_video`. It snapshots the persisted Dialogue audio durations and approved Storyboard Asset, rounds the required 16 FPS Wan length upward to a `4n+1` frame count, and binds the existing first frame without regenerating character art. Only after the real MP4 exists does it set `shots.video_asset_id` and `VIDEO_GENERATED`. Qwen3-TTS is unloaded and its process stopped before ComfyUI loads Wan2.2.
