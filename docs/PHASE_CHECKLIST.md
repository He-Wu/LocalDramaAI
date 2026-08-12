# Phase Checklist

## Phases 0-7

- [x] Environment and runtime locks
- [x] FastAPI, SQLite WAL, worker claims, and JobEvents
- [x] Structured local text generation
- [x] Character MASTER and Storyboard First Frames
- [x] Wan2.2 storyboard-to-video generation
- [x] Qwen3-TTS voice cloning and explicit unload
- [x] Audio-duration-driven dialogue video persistence

## Phase 8 - MuseTalk lip sync

- [x] Add safe Shot eligibility defaults and Alembic upgrade
- [x] Require both `requires_lip_sync` and `speaker_visible`
- [x] Preserve the immutable Phase 7 `video_asset_id`
- [x] Run official MuseTalk commit `0a89dec45a0192b824e3cf4daf96c239440c5ed8` in an isolated Python 3.10/cu118 environment
- [x] Pin and hash all 11 official model/config files
- [x] Validate source and output media independently
- [x] Persist the LIPSYNC Asset, GenerationManifest, and `lipsync_asset_id` atomically after stale-state recheck
- [x] Complete a real 2.78-second eligible-Shot smoke
- [x] Decode the final 640x368 25 FPS H.264/yuv420p + AAC MP4
- [x] Verify database, provider manifest, output, evidence, and resource hashes
- [x] Visually approve start/middle/end frames and mouth contact sheet
- [x] Stop AI services and confirm ports 8020, 8030, and 8188 are free

Phase 8 scope ends at one eligible visible-speaker Shot. Subtitles, multi-shot editing, and the final renderer are not included.
