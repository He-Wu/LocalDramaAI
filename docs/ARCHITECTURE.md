# Architecture

FastAPI (`python -m uvicorn app.main:app`) owns short REST requests and SQLite reads. `python -m app.worker_main` is a separate worker process. The worker atomically claims queued jobs inside `BEGIN IMMEDIATE`, writes progress to `job_events`, and never runs inside an API request.

Provider boundaries are deliberately small: `OllamaProvider` handles local structured text generation and unload, while `ComfyUIClient` handles health, prompt submission, history polling, and atomic output downloads. Generated files are referenced by `assets`; `generation_manifests` record the minimal provider/model/prompt/workflow provenance required in Phase 3.

Phase 4 separates character identity from shot descriptions. `VisualBible` is persisted in `characters.visual_bible_json`; MASTER references are stored in `character_references`, and storyboard images are linked from `shots.storyboard_asset_id`. Storyboard generation uses the approved MASTER image through ComfyUI's built-in `LoadImage`/`VAEEncode` path.

Phase 5 adds `ComfyUIVideoProvider` and `FFmpegProvider`. The video provider submits the bound Wan2.2 image-to-video workflow, downloads the real ComfyUI output, performs an atomic H.264 MP4 conversion, and persists a `VIDEO` `Asset` with a `GenerationManifest`. Video outputs remain immutable; source WebM and manifest paths are retained for auditability.

Phase 6 adds a loopback-only Qwen3-TTS service on port 8020, isolated from the application and ComfyUI Python environments. `Qwen3TTSProvider` calls the service, while `generate_dialogue_audio` owns the short persistence transaction: it validates the real WAV first, then creates an AUDIO Asset and GenerationManifest and updates Dialogue/Shot timing. The TTS model is explicitly unloaded after generation so Wan remains the exclusive GPU owner during video jobs.

Phase 7 adds `generate_dialogue_video`. It snapshots the persisted Dialogue audio durations and approved Storyboard Asset, rounds the required 16 FPS Wan length upward to a `4n+1` frame count, and binds the existing first frame without regenerating character art. Only after the real MP4 exists does it set `shots.video_asset_id` and `VIDEO_GENERATED`. Qwen3-TTS is unloaded and its process stopped before ComfyUI loads Wan2.2.

Phase 8 adds `generate_shot_lipsync` and a dedicated loopback MuseTalk service on `127.0.0.1:8030`. A Shot is processed only when both `requires_lip_sync` and `speaker_visible` are true. The application snapshots the immutable VIDEO and Dialogue AUDIO assets, invokes `MuseTalkProvider` outside the database transaction, independently probes the returned media and manifest, then uses a final SQLite write transaction with a stale-state recheck. The derivative is registered as a `LIPSYNC` Asset through `shots.lipsync_asset_id`; `shots.video_asset_id` is never replaced.

MuseTalk runs in `E:/LocalDramaAI/env-musetalk` with Python 3.10.11, Torch 2.0.1/cu118, and official repository commit `0a89dec45a0192b824e3cf4daf96c239440c5ed8`. Each request creates normalized 25 FPS video and 16 kHz mono audio, launches official `scripts.inference` as a child process, validates/canonicalizes the audiovisual MP4, publishes it atomically, and lets the child exit to release model VRAM. The application remains on Python 3.13 and does not import MuseTalk's incompatible runtime.
