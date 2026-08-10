# LocalDramaAI Phase 6 TTS Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Generate a real Chinese cloned-voice WAV with Qwen3-TTS 0.6B Base, persist its provenance, and update Dialogue and Shot duration from the measured audio.

**Architecture:** Run the official Qwen3-TTS package in an isolated Python 3.12 environment behind a loopback-only FastAPI service. The application calls it through an asynchronous provider; a focused orchestration service validates the resulting WAV before atomically registering AUDIO provenance and updating Dialogue/Shot state.

**Tech Stack:** Python 3.12, FastAPI, Qwen3-TTS, PyTorch CUDA, httpx, SQLAlchemy, SQLite, soundfile/wave, pytest, PowerShell.

---

### Task 1: Add Dialogue and VoiceProfile persistence

**Files:**
- Create: `app/models/dialogue.py`
- Create: `app/models/voice_profile.py`
- Modify: `app/models/character.py`
- Modify: `app/models/shot.py`
- Modify: `app/models/__init__.py`
- Test: `tests/test_phase6.py`

- [ ] Write tests that create an ordered Dialogue and VoiceProfile and verify nullable audio linkage and timing fields.
- [ ] Run `python -m pytest tests/test_phase6.py -q` and verify failure because the models do not exist.
- [ ] Implement the two SQLAlchemy models and relationships with cascade-safe foreign keys.
- [ ] Run the focused test and verify it passes.

### Task 2: Add WAV validation and duration measurement

**Files:**
- Create: `app/services/audio_probe.py`
- Test: `tests/test_phase6.py`

- [ ] Write a test that creates a real mono PCM WAV and asserts sample rate, channel count, frame count, and exact duration.
- [ ] Run the focused test and verify failure because `probe_wav` does not exist.
- [ ] Implement `probe_wav(path)` with Python's `wave` module and reject missing, empty, compressed, or zero-duration files.
- [ ] Run the focused test and verify it passes.

### Task 3: Add the application Qwen3-TTS provider

**Files:**
- Create: `app/providers/qwen3_tts_provider.py`
- Test: `tests/test_phase6.py`

- [ ] Write provider tests for missing reference files, successful local-service responses, output existence, and unload responses.
- [ ] Run the focused tests and verify failure because the provider does not exist.
- [ ] Implement async `health`, `generate`, and `unload` methods with explicit timeouts and structured errors.
- [ ] Run the focused tests and verify they pass.

### Task 4: Persist successful TTS generation

**Files:**
- Create: `app/services/tts_generation.py`
- Test: `tests/test_phase6.py`

- [ ] Write a test using a local deterministic WAV-producing test provider to assert AUDIO Asset, GenerationManifest, Dialogue duration, and `Shot.duration == audio_duration + 0.3`.
- [ ] Write a failure-path test proving database state remains unchanged when generation fails.
- [ ] Run both tests and verify they fail because the orchestration function does not exist.
- [ ] Implement `generate_dialogue_audio` so file validation precedes its short database transaction.
- [ ] Run the focused tests and verify they pass.

### Task 5: Build the isolated local TTS service

**Files:**
- Create: `ai-services/qwen3-tts/service.py`
- Create: `ai-services/qwen3-tts/requirements.lock.txt`
- Create: `scripts/setup_qwen3_tts.ps1`
- Create: `scripts/start_ai_services.ps1`
- Test: `tests/test_phase6_service.py`

- [ ] Write tests for request validation, atomic WAV publication helper behavior, and health payload shape without loading model weights.
- [ ] Run them and verify failure because service helpers do not exist.
- [ ] Implement loopback service endpoints, lazy official model loading, reference cloning, atomic WAV output, and CUDA unload.
- [ ] Run service tests and verify they pass.
- [ ] Create `E:/LocalDramaAI/env-tts` with Python 3.12 and install the locked service runtime without altering the app or ComfyUI environments.

### Task 6: Download and lock official model/reference inputs

**Files:**
- Modify: `models/models.yaml`
- Modify: `runtime/runtime-lock.yaml`
- Create: `scripts/download_qwen3_tts_models.ps1`

- [ ] Download `Qwen/Qwen3-TTS-12Hz-0.6B-Base` and its tokenizer to NVMe storage using the official Hugging Face repository.
- [ ] Download the official short reference WAV and record its exact transcript.
- [ ] Record repository/package/model/runtime versions and model file hashes.
- [ ] Verify the reference WAV with `ffprobe` and verify model files are readable.

### Task 7: Run a real Chinese voice-clone smoke test

**Files:**
- Create: `scripts/smoke_phase6.py`
- Modify: `docs/BENCHMARKS.md`
- Modify: `docs/MODELS.md`
- Modify: `docs/ARCHITECTURE.md`
- Modify: `docs/INSTALL-WINDOWS.md`
- Modify: `docs/TROUBLESHOOTING.md`
- Modify: `README.md`

- [ ] Start the isolated TTS service and wait for health.
- [ ] Create a real project, character, voice profile, shot, dialogue, and reference AUDIO Asset in a clean SQLite database.
- [ ] Generate one Chinese cloned-voice WAV through the application provider.
- [ ] Verify the WAV with `ffprobe`, inspect/play the artifact, and confirm database linkage and duration math.
- [ ] Record generation time, peak VRAM, peak RAM, Windows commit memory when available, and post-unload GPU state.
- [ ] Run the complete test suite and report the exact result.

### Task 8: Phase boundary verification

**Files:**
- Review all Phase 6 files and artifacts.

- [ ] Confirm no Dialogue Video, LipSync, subtitle, or render implementation entered Phase 6.
- [ ] Confirm the generated WAV is real, non-empty, playable, and linked to an AUDIO Asset and GenerationManifest.
- [ ] Confirm the TTS model is unloaded and ComfyUI is not holding the GPU.
- [ ] Update documentation with actual evidence only.

Git commit steps are omitted because `E:/kang/github/Movie` is not currently a Git repository.
