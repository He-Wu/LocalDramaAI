# LocalDramaAI Phase 6 TTS Design

## Scope

Phase 6 adds real, fully local Chinese voice cloning and dialogue audio generation. It stops after producing a verified WAV, measuring its duration, registering the audio provenance, and updating the owning Shot duration. Dialogue-driven video generation, lip sync, subtitles, and final rendering remain outside this phase.

## Selected Runtime

- Official repository/package: `QwenLM/Qwen3-TTS` / `qwen-tts`
- Model: `Qwen/Qwen3-TTS-12Hz-0.6B-Base`
- Voice method: reference-audio cloning
- Reference input: one clean 5–10 second WAV plus its exact transcript
- Runtime: isolated Python 3.12 environment at `E:/LocalDramaAI/env-tts`
- Service: local-only HTTP process bound to `127.0.0.1`
- GPU policy: load on demand, expose explicit unload, and release the model after the Phase 6 smoke request

The 0.6B Base model is selected over the 1.7B variant to minimize VRAM and RAM pressure on the 16 GB RTX 4060 Ti while retaining the official voice-cloning path. A persistent GPU-resident service and per-line CLI subprocesses are rejected: the former conflicts with Wan GPU ownership, while the latter repeatedly pays model startup cost and has a weaker provider boundary.

## Components

### TTS service

`ai-services/qwen3-tts/` owns the model runtime and has no dependency on the application environment. It provides:

- `GET /health`: process, CUDA, model-load, and current-device status.
- `POST /generate`: text, language, reference WAV path, reference transcript, output path, and optional generation settings.
- `POST /unload`: releases the model, runs Python garbage collection, clears the CUDA cache, and returns post-release memory status.

The service accepts only local filesystem paths and listens on loopback. It writes to a temporary WAV next to the requested output, validates that the result is non-empty PCM audio, and atomically replaces the final path.

### Application provider

`Qwen3TTSProvider` is an asynchronous HTTP client. It owns service health checks, generation requests, error translation, and unload requests. The provider rejects empty dialogue text, missing reference audio, missing reference transcripts, unsuccessful responses, absent files, and non-positive durations.

### Domain persistence

Phase 6 adds the minimal missing persistence models required by the existing specification:

- `VoiceProfile`: character ownership, model, reference asset, reference transcript, language, and generation settings.
- `Dialogue`: shot, character, ordered text, emotion, audio asset, timing fields, and measured duration.

Successful generation creates an `Asset(kind="AUDIO", mime_type="audio/wav")` and a `GenerationManifest` linked to that asset. It then updates `Dialogue.audio_asset_id` and `Dialogue.duration`. Database mutation occurs only after the final WAV exists and passes validation.

## Data Flow

1. Load the Dialogue, Character VoiceProfile, Shot, and reference-audio Asset.
2. Validate that dialogue text, reference WAV, and exact reference transcript are present.
3. Enter the single GPU-heavy-job boundary and record preflight GPU/RAM status.
4. Ask the local TTS service to clone the reference voice and synthesize the Chinese dialogue.
5. The service generates a temporary WAV, validates it, and atomically publishes it.
6. Measure duration from the WAV container rather than estimating from text length.
7. Create the AUDIO Asset and GenerationManifest.
8. Update Dialogue audio linkage and duration.
9. For the Phase 6 single-dialogue shot, set `Shot.duration` to `audio_duration + 0.3` seconds.
10. Request TTS model unload and record post-release GPU status.

## Duration Rule

Phase 6 supports one synthesized Dialogue for its real smoke Shot. The final Shot duration is the measured WAV duration plus a fixed 0.3-second tail buffer. The duration is not rounded down and is never inferred from character count. Multi-dialogue timeline composition is deferred to the later pipeline/render phases.

## File Safety and Provenance

- Output layout: `storage/projects/{project_id}/audio/{dialogue_id}.wav`.
- Temporary output: `{dialogue_id}.tmp.wav` in the same directory.
- Only an atomic replacement publishes the final WAV.
- Manifest fields include provider/version, model, dialogue text, language, voice-profile identifier, reference audio asset, measured generation time, and output asset.
- Runtime and model versions are locked in `runtime/runtime-lock.yaml`; model metadata is added to `models/models.yaml`.

## Failure Handling

- Service unavailable: keep Dialogue and Shot unchanged; no Asset is created.
- Model load or CUDA failure: return a structured local error with model and GPU status.
- Invalid or missing reference input: fail before GPU work.
- Empty/corrupt WAV: remove the temporary output and do not write database success records.
- Database failure after file generation: preserve the unregistered WAV for diagnosis and report the exact path; do not claim job success.
- Unload failure: report the generation as produced but record the lifecycle failure separately; do not claim VRAM was released.

## Testing and Acceptance

Automated tests are written before production code and cover:

- WAV duration measurement using a real generated PCM fixture.
- VoiceProfile and Dialogue persistence/order fields.
- Provider validation for missing inputs and invalid service results.
- Successful AUDIO Asset and GenerationManifest registration.
- Dialogue duration and Shot `audio_duration + 0.3` update.
- No database mutation on generation failure.

The real smoke acceptance test must:

- use the official 0.6B Base model locally;
- clone a supplied 5–10 second reference voice;
- synthesize one non-empty Chinese Dialogue into a playable WAV;
- confirm sample rate, channel count, file size, and measured duration;
- confirm the database Asset, GenerationManifest, Dialogue, and Shot updates;
- record generation time, peak VRAM, peak RAM, and Windows commit memory when available;
- unload the TTS model and verify post-run GPU status;
- contain no mocked AI audio and no fake success records.

Phase 6 ends after these checks and documentation updates. Phase 7 work is not authorized by this design.
