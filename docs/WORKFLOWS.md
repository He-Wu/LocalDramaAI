# ComfyUI Workflows

Workflow JSON must be a mapping of node IDs to objects containing `class_type` and `inputs`. Bindings replace placeholder values or input keys before validation. The Phase 3 image provider requires a completed history record containing at least one `images` output and atomically renames downloaded files from `.tmp`.

Phase 4 adds `phase4_character_storyboard.json` for MASTER generation and `phase4_storyboard_img2img.json`, which uses built-in `LoadImage` + `VAEEncode` to condition Storyboard First Frames on the approved MASTER reference.

Phase 5 adds the official Wan2.2 5B reference workflow plus the API-format `wan22_i2v_5b_api.json`. `bind_video_workflow` injects the approved first frame, source-aligned prompt, seed, resolution, and frame count. ComfyUI writes WebM; `ComfyUIVideoProvider` converts it atomically to H.264 MP4 and registers both the `VIDEO` asset and generation manifest.

Phase 7 reuses that locked workflow through `generate_dialogue_video`. The binding now also replaces `SaveWEBM.filename_prefix` with `LocalDramaAI/phase7/{shot_id}` and derives a `4n+1` frame count from measured Dialogue duration. The workflow input is always the already-persisted Storyboard Asset.

## Phase 8 MuseTalk lip sync

`generate_shot_lipsync` deliberately skips any Shot unless `requires_lip_sync=true` and `speaker_visible=true`. An eligible Shot must have exactly one Dialogue, a valid project-owned VIDEO Asset, and a valid Dialogue AUDIO Asset. The source links are snapshotted before provider execution and rechecked before the final atomic database write.

The loopback MuseTalk service normalizes video to 640x368 H.264/yuv420p at 25 FPS and audio to mono 16 kHz PCM padded to the 2.78-second target. It invokes official MuseTalk 1.5 with FP16, batch size 4, `extra_margin=10`, `parsing_mode=jaw`, and cheek widths 90/90. The published result must contain playable H.264/yuv420p video and AAC audio, cover the Shot within one frame, and keep A/V end times within 80 ms.

On success, the workflow preserves `video_asset_id`, creates a `LIPSYNC` Asset and GenerationManifest, and sets `lipsync_asset_id` plus `LIPSYNC_GENERATED`. The provider manifest binds project, Shot, ordered input Asset IDs, source/output paths and hashes, target duration, exact command, model, probes, and timing. This is a per-Shot mouth-sync derivative; subtitle composition and final multi-shot rendering remain later phases.
