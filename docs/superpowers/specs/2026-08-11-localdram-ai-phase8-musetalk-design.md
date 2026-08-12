# LocalDramaAI PHASE 8 MuseTalk Lip-Sync Design

## Scope

PHASE 8 converts one eligible dialogue Shot's immutable PHASE 7 video and one persisted Dialogue WAV into a real audiovisual lip-sync MP4 with MuseTalk 1.5. It does not implement subtitles, multi-shot rendering, quality gates, or the final timeline.

A Shot is eligible only when both `requires_lip_sync` and `speaker_visible` are true. False gates return a deliberate skip without starting MuseTalk or mutating the Shot.

## Chosen integration

Use the official MuseTalk repository in `E:/LocalDramaAI/MuseTalk`, a dedicated Python 3.10 environment at `E:/LocalDramaAI/env-musetalk`, and official MuseTalk 1.5 weights. A loopback-only FastAPI service listens on `127.0.0.1:8030`. Each generate request launches the official `scripts.inference` CLI as a child process, so the incompatible Torch/CUDA stack stays outside the application process and GPU memory is released when the child exits.

This is preferred over a persistent in-process model because predictable VRAM release is more important than warm-model latency on one 16 GB GPU. ComfyUI community MuseTalk nodes are not used because they are third-party and would expand the locked custom-node surface.

## Schema and migration

Add these Shot fields:

- `requires_lip_sync`: non-null Boolean, default false.
- `speaker_visible`: non-null Boolean, default false.
- `lipsync_asset_id`: nullable foreign key to `assets.id` with `ON DELETE SET NULL`.

`video_asset_id` always remains the immutable Wan source. `lipsync_asset_id` points to the PHASE 8 derivative. Add the two booleans to `ShotSpec`; automatic structured story generation may emit them, while missing fields remain safely false.

Alembic is already a declared dependency but has no environment. Add an Alembic configuration and an idempotent first revision that upgrades an existing pre-PHASE-8 `shots` table using SQLite batch mode. Existing rows are backfilled to false and their PHASE 7 links remain unchanged. A migration test must create an old schema, run the upgrade, and verify the data and new columns.

## Service and provider boundaries

`MuseTalkProvider` calls the local service and returns a published MP4 plus a filesystem manifest. It does not write application database state.

The MuseTalk service accepts absolute video/audio/output paths and a target duration. For each request it:

1. Creates a unique temporary job directory.
2. Validates the source MP4 and PCM WAV.
3. Converts a copy of the source to H.264/yuv420p, 640x368, constant 25 FPS.
4. Converts the WAV to PCM s16le mono 16 kHz and pads silence to the persisted Shot duration.
5. Writes a one-task YAML configuration.
6. Runs official MuseTalk 1.5 with FP16, GPU 0, batch size 4, `extra_margin=10`, `parsing_mode=jaw`, and cheek widths 90/90.
7. Treats CLI exit status as insufficient because the upstream script catches task exceptions; an expected output file must also exist and pass probing.
8. Canonicalizes the result to H.264/yuv420p plus AAC, `+faststart`, using a temporary path followed by atomic rename.
9. Returns measured metadata and deletes temporary frames.

The service holds an asynchronous single-job lock. `/health` reports repository, model, CUDA, and busy state. `/unload` reports whether a child is active; no model remains resident between jobs.

## Application data flow

`generate_shot_lipsync(database_url, project_id, shot_id, provider, output_dir)` performs this sequence:

1. Validate Shot ownership and both eligibility gates.
2. Require exactly one Dialogue for the first PHASE 8 MVP.
3. Resolve a project-owned PHASE 7 VIDEO Asset and Dialogue AUDIO Asset.
4. Probe both source files and require the video to cover the measured speech.
5. Snapshot the flags and source asset IDs, then call the provider outside a write transaction.
6. Probe the final A/V output. Require H.264/yuv420p at 640x368 and 25 FPS, at least one playable audio stream, duration covering the Shot within one 25 FPS frame, and audio duration aligned within 80 ms.
7. Reopen a short transaction and reject a stale result if flags or source links changed during generation.
8. Create an `Asset(kind="LIPSYNC", mime_type="video/mp4")`, a GenerationManifest with exactly the video/audio input asset IDs, set `Shot.lipsync_asset_id`, and set status `LIPSYNC_GENERATED` atomically.

Any validation, provider, timeout, media, stale-state, or database failure leaves the Shot and database without a PHASE 8 success record.

## Real smoke and acceptance

The real smoke reuses the verified PHASE 7 assets:

- Video: H.264 640x368, 16 FPS, 49 frames, 3.0625 seconds.
- Audio: PCM s16le mono 24 kHz, 2.48 seconds.
- Shot duration: 2.78 seconds.

The accepted output must:

- Decode fully with FFmpeg.
- Contain H.264/yuv420p video and AAC audio.
- Be 640x368 at 25 FPS and cover the 2.78-second Shot within one frame.
- Preserve identity, background, eyes, and hand placement in start/middle/end review.
- Show plausible mouth motion without a jaw/hand seam, mask edge, mouth tear, or severe flicker.
- Persist consistent Asset, Manifest, Shot link, hashes, runtime versions, generation time, peak VRAM/RAM, Windows commit, and temperature.
- Stop the MuseTalk service and leave ports 8030/8188/8020 free after verification.

## Official sources

- MuseTalk repository and Windows instructions: https://github.com/TMElyralab/MuseTalk
- Official MuseTalk weights: https://huggingface.co/TMElyralab/MuseTalk
- Official requirements: https://github.com/TMElyralab/MuseTalk/blob/main/requirements.txt
- Official inference implementation: https://github.com/TMElyralab/MuseTalk/blob/main/scripts/inference.py

The verified runtime will pin the exact repository commit, package versions, model revisions, and SHA256 hashes after the first successful local run. MuseTalk code is MIT-licensed; the official model repository identifies its model license separately, and all dependency-model licenses remain independently applicable.
