# ComfyUI Workflows

Workflow JSON must be a mapping of node IDs to objects containing `class_type` and `inputs`. Bindings replace placeholder values or input keys before validation. The Phase 3 image provider requires a completed history record containing at least one `images` output and atomically renames downloaded files from `.tmp`.

Phase 4 adds `phase4_character_storyboard.json` for MASTER generation and `phase4_storyboard_img2img.json`, which uses built-in `LoadImage` + `VAEEncode` to condition Storyboard First Frames on the approved MASTER reference.

Phase 5 adds the official Wan2.2 5B reference workflow plus the API-format `wan22_i2v_5b_api.json`. `bind_video_workflow` injects the approved first frame, source-aligned prompt, seed, resolution, and frame count. ComfyUI writes WebM; `ComfyUIVideoProvider` converts it atomically to H.264 MP4 and registers both the `VIDEO` asset and generation manifest.

Phase 7 reuses that locked workflow through `generate_dialogue_video`. The binding now also replaces `SaveWEBM.filename_prefix` with `LocalDramaAI/phase7/{shot_id}` and derives a `4n+1` frame count from measured Dialogue duration. The workflow input is always the already-persisted Storyboard Asset.
