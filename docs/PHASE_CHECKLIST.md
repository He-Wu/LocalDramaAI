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

## Phase 9 - subtitles and final render

- [x] Snapshot scenes, shots, dialogues, links, flags, durations, paths, hashes, and project output pointers deterministically
- [x] Select required LIPSYNC for eligible visible-speaker Shots and VIDEO otherwise
- [x] Generate deterministic UTF-8 CRLF SRT from measured Dialogue WAV timing without ASR
- [x] Normalize heterogeneous source media to 640x368 25 FPS and rebuild AAC stereo audio from Dialogue assets
- [x] Burn locked Microsoft YaHei subtitles and fully decode the H.264/AAC result with strict FFmpeg errors
- [x] Persist immutable SUBTITLE and FINAL_VIDEO Assets plus exact GenerationManifests after stale-state recheck
- [x] Serialize concurrent project renders and keep exactly one coherent winner
- [x] Publish and reconcile `output/final.mp4` as a recoverable alias with READY/DEGRADED semantics
- [x] Run a real two-Shot smoke, verify all hashes/database links, and visually approve frames/contact sheet

Phase 9 does not add Phase 10 orchestration, BGM, automated quality scoring, broad reliability operations, or UI.
