# LocalDramaAI PHASE 7 Dialogue Video Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Generate a real dialogue Shot video whose duration is derived from persisted TTS audio and whose first frame is the existing Storyboard Asset.

**Architecture:** Add a small dialogue-video service around the existing ComfyUI Wan2.2 provider. The service validates all DB/file inputs before generation, copies the storyboard into ComfyUI input, binds frame count from audio duration, then atomically links the resulting VIDEO Asset and manifest to the Shot.

**Tech Stack:** Python, SQLAlchemy, existing ComfyUI client/provider, Wan2.2 TI2V 5B, FFmpeg, pytest.

---

### Task 1: Add frame-duration contract tests

**Files:**
- Modify: `tests/test_phase5.py`
- Modify: `app/comfyui/video_workflow.py`

- [ ] Write tests for 16 FPS duration conversion, minimum 49 frames, and `4n+1` rounding.
- [ ] Run `python -m pytest tests/test_phase5.py -q`; confirm the new import/function test fails before implementation.
- [ ] Implement `frame_count_for_duration(duration, fps=16, minimum=49, maximum=121)` with positive-duration validation.
- [ ] Run the focused tests and confirm they pass.

### Task 2: Persist Shot video linkage

**Files:**
- Modify: `app/models/shot.py`

- [ ] Add nullable `video_asset_id` foreign key to `assets.id` with `SET NULL` behavior.
- [ ] Run the model import and existing suite to ensure fresh SQLite schemas still create.

### Task 3: Add dialogue-video service tests first

**Files:**
- Create: `tests/test_phase7.py`
- Create: `app/services/dialogue_video_generation.py`

- [ ] Write a failing test that creates a project/scene/shot/dialogue, real WAV and storyboard files, invokes a deterministic provider, and asserts the returned VIDEO Asset, manifest inputs, Shot link, and non-shorter duration.
- [ ] Write a failing test that rejects a shot with no storyboard or audio without changing the database.
- [ ] Run `python -m pytest tests/test_phase7.py -q` and confirm the expected import/behavior failures.
- [ ] Implement input snapshot, atomic storyboard copy, frame calculation, provider call, real-file validation, and short persistence transaction.
- [ ] Run focused tests until green.

### Task 4: Build the real PHASE 7 smoke

**Files:**
- Create: `scripts/smoke_phase7.py`
- Modify: `scripts/start_all.ps1`, `scripts/stop_all.ps1` only if lifecycle gaps are found.

- [ ] Create fresh SQLite data with a real Phase 4 storyboard and VoiceProfile, run Qwen3-TTS to create the dialogue WAV, unload TTS, then run ComfyUI Wan2.2 through `generate_dialogue_video`.
- [ ] Probe MP4 codec, duration, dimensions, and frame rate; query the DB for VIDEO Asset, Shot link, and manifest.
- [ ] Record peak VRAM, GPU utilization, temperature, RAM, generation time, and Windows commit.
- [ ] Stop ComfyUI and TTS in `finally` and verify the TTS endpoint is offline/unloaded.

### Task 5: Document and verify

**Files:**
- Modify: `runtime/runtime-lock.yaml`, `docs/ARCHITECTURE.md`, `docs/BENCHMARKS.md`, `docs/TROUBLESHOOTING.md`, `README.md`

- [ ] Record the real Phase 7 artifact and runtime evidence without claiming lip sync/subtitles/rendering.
- [ ] Run `python -m pytest -q`, Python compilation, PowerShell parse checks, and inspect the generated MP4/DB evidence.
