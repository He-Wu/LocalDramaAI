# LocalDramaAI Phase 5 Implementation Plan

> **For agentic workers:** Use this plan task-by-task with test-first verification. Scope ends after one real 3–5 second video.

**Goal:** Turn an approved Storyboard First Frame into a real local Wan2.2 TI2V 5B video and register the resulting MP4, with Wan2.1 1.3B fallback diagnostics if 5B cannot run on the 16GB GPU.

**Architecture:** The video provider submits a validated ComfyUI API workflow, detects saved video outputs, downloads atomically, and uses local FFmpeg to produce MP4. The provider records model, resolution, frames, FPS, seed, runtime and output Asset/Manifest; no fake success is allowed.

**Tech Stack:** Existing ComfyUI client, Wan2.2 native nodes, FFmpeg 8.1, SQLAlchemy Asset/GenerationManifest.

---

### Task 1: Video provider contract

**Files:** `app/providers/comfyui_video_provider.py`, `app/providers/ffmpeg_provider.py`, `tests/test_phase5.py`

- [ ] Add failing tests for video output extraction, atomic MP4 conversion, and failure when no video output exists.
- [ ] Implement the minimal provider and diagnostics.

### Task 2: Official Wan2.2 workflow binding

**Files:** `comfyui/workflows/wan22_i2v_5b_api.json`, `app/comfyui/video_workflow.py`, `tests/test_phase5.py`

- [ ] Add failing validation tests for frame count, dimensions, prompt, model and input image bindings.
- [ ] Convert the official 5B Image-to-Video graph to API format and bind a Storyboard PNG.

### Task 3: Real Phase 5 smoke

**Files:** `scripts/download_wan_models.ps1`, `scripts/smoke_phase5.py`, `models/models.yaml`, `runtime/runtime-lock.yaml`, `docs/BENCHMARKS.md`

- [ ] Download only the official local Wan2.2 5B model set into the isolated ComfyUI runtime.
- [ ] Run one 3–5 second I2V generation, convert to MP4, verify duration/file existence, and capture VRAM/RAM/time.
- [ ] If 5B fails with OOM or unsupported runtime, run the same smoke with Wan2.1 1.3B and record the failure reason and fallback result.
