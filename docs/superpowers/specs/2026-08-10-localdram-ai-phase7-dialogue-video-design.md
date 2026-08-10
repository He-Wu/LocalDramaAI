# LocalDramaAI PHASE 7 Dialogue Video Design

## Scope

PHASE 7 turns an existing Shot with a Dialogue, synthesized audio, and a Storyboard Asset into a real Wan2.2 image-to-video output. It does not implement lip sync, subtitles, rendering, approvals, or multi-shot orchestration.

## Data flow

1. Read the Shot, ordered Dialogues, their AUDIO assets, and the Shot's Storyboard Asset.
2. Require every dialogue to have a real WAV and a positive measured duration. The required shot duration is the larger of the persisted shot duration and the sum of dialogue durations plus the 0.3 second tail buffer used by PHASE 6.
3. Copy the storyboard file into ComfyUI's input directory with an atomic temporary file, bind the existing Wan2.2 I2V workflow to that exact filename, prompt, seed, dimensions, and a frame count whose encoded duration is at least the required duration.
4. Release the Qwen3-TTS model before starting ComfyUI; the smoke script explicitly verifies the TTS service is unloaded before the video request.
5. Generate a real MP4 through `ComfyUIVideoProvider`, then persist a VIDEO Asset and GenerationManifest and link the Asset to `Shot.video_asset_id`.

## Interfaces and failure behavior

`generate_dialogue_video(...)` receives the database URL, project/shot identifiers, a video provider, a workflow template, ComfyUI input directory, output directory, and optional video profile. It performs all reads before provider execution and only writes database state after the MP4 exists. Missing shot, storyboard, dialogue audio, missing files, unsupported duration, or provider failure raises an error and leaves `Shot.video_asset_id` unchanged.

Frame calculation uses 16 FPS, a minimum of 49 frames for the locked Wan2.2 profile, and rounds upward to the model's `4n+1` frame cadence. The generated duration is therefore never shorter than dialogue audio.

## Verification

Unit tests cover frame calculation, missing-input rejection, workflow binding, and persistence/linking with a deterministic provider. A real smoke test runs Qwen3-TTS, unloads it, starts ComfyUI, generates the dialogue shot from a real storyboard, probes MP4 duration, checks the database link/manifest, records GPU/RAM/commit peaks, and stops both services.
