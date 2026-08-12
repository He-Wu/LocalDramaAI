# Benchmarks

## Phase 3 smoke (2026-08-09)

| Provider | Model | Resolution | Steps | VRAM observed | Result |
|---|---|---:|---:|---:|---|
| ComfyUI 0.31.0 | v1-5-pruned-emaonly-fp16.safetensors | 512×512 | 12 | 3,012 MiB used / 16,380 MiB total during run | Success |

Output: `E:\LocalDramaAI\Storage\phase3\phase3_smoke_00002_.png`. The benchmark used a fixed seed (`20260809`) and the project workflow `comfyui/workflows/sd15_smoke.json`.

## Phase 4 Character + Storyboard smoke (2026-08-09)

| Asset | Workflow | Resolution | Seed | Generation time | VRAM after run |
|---|---|---:|---:|---:|---:|
| Character MASTER | `phase4_character_master` | 512×512 | 823412302 | 9.63 s | 2,517 MiB |
| Storyboard 01 | `phase4_storyboard` + MASTER img2img | 512×512 | 1237058366 | 3.44 s | 2,517 MiB |
| Storyboard 02 | `phase4_storyboard` + MASTER img2img | 512×512 | 921602733 | 2.40 s | 2,517 MiB |

All three files exist and are linked in the Phase 4 smoke SQLite database. Visual review shows stable face shape, hair silhouette, eye placement, and framing across the MASTER and both Storyboard First Frames. The SD1.5 baseline does not perfectly preserve the requested blue wardrobe, so wardrobe consistency remains a quality limitation for a later IPAdapter/ControlNet refinement.

## Phase 5 Storyboard-to-Video smoke (2026-08-09)

| Model | Profile | Steps | Generation time | Peak VRAM | Peak system RAM | Result |
|---|---|---:|---:|---:|---:|---|
| Wan2.2 TI2V 5B FP16 | 640×368, 49 frames, 16 fps | 20 | 71.45 s | 15,807 MiB | 30,465 MiB used | Success |

Final output: `E:\kang\github\Movie\artifacts\phase5\1786277293\phase5_00002_.mp4` (H.264, 3.0625 s, 49 frames). GPU utilization peaked at 100% and temperature at 74°C. The first/middle/last-frame review shows strong facial identity retention and subtle motion, with a visible duplicated-edge artifact on the left/bottom boundary; this is an MVP baseline rather than final production quality.

An earlier mismatched action prompt also completed in 74.04 s but caused severe subject morphing. The accepted run therefore uses a source-aligned portrait-motion prompt, demonstrating that Phase 5 prompt binding materially affects temporal consistency.

## Phase 6 Qwen3-TTS voice-clone smoke (2026-08-10)

| Model | Output | Generation time | Peak VRAM | Peak RAM | Peak Windows commit | Result |
|---|---|---:|---:|---:|---:|---|
| Qwen3-TTS 12Hz 0.6B Base, BF16 | 24 kHz mono PCM WAV, 2.48 s | 27.16 s | 5,813 MiB | 23,835 MiB used | 35,398 MiB | Success |

Output: `E:\kang\github\Movie\artifacts\phase6\1786296246\2c52e8d6-5b98-4c42-86fa-42831efdb38f.wav`. The source was the official 8.08-second clone reference. The generated file is 119,084 bytes, has a mean level of -21.4 dB and peak of -4.8 dB, and contains one 0.38-second natural pause. The persisted Dialogue duration is 2.48 seconds and the owning Shot duration is 2.78 seconds. Model unload returned `UNLOADED`; observed VRAM fell from the 5,813 MiB peak to 3,812 MiB after release.

FlashAttention 2 is not installed on this Windows runtime, so the verified smoke uses the official eager PyTorch path. The Python `sox` package emits a missing executable warning during import, but the 12Hz clone path completed successfully without SoX.

## Phase 7 Dialogue-to-Video smoke (2026-08-10)

| Pipeline | Audio / required shot | Video profile | Video generation | Peak VRAM | Peak RAM | Peak Windows commit | Result |
|---|---:|---:|---:|---:|---:|---:|---|
| Qwen3-TTS 0.6B → unload → Wan2.2 TI2V 5B | 2.48 s / 2.78 s | 640×368, 49 frames, 16 FPS | 80.51 s | 15,615 MiB | 30,442 MiB used | 52,179 MiB | Success |

Output: `E:\kang\github\Movie\artifacts\phase7\1786342910\video\0753ed98-8ccc-4c4c-99dc-f9fc96b822ac_00001_.mp4` (H.264, 3.0625 s, 72,039 bytes, SHA256 `009cf772e2ba8f36604cc7f72c6799c15f093699aecb51c8cbea89f1db9a95f6`). The source is the existing Phase 4 Storyboard Asset; it was copied into ComfyUI input rather than regenerated. The MP4 is 0.2825 seconds longer than the required Shot duration and 0.5825 seconds longer than the synthesized Dialogue, so the duration contract is satisfied. Database inspection confirms `Shot.video_asset_id`, VIDEO Asset, GenerationManifest input assets, and `VIDEO_GENERATED` status agree.

The complete smoke peaked close to the 32 GB physical-RAM limit and used a 52.2 GB Windows commit charge while Wan offloading was active. Closing unrelated applications remains important. Contact-sheet review shows stable face, hair, hand placement, and a natural blink; precise speech mouth shapes are intentionally deferred to PHASE 8 MuseTalk.

## Phase 8 MuseTalk lip-sync smoke (2026-08-12)

| Pipeline | Output profile | Service generation | End-to-end elapsed | Peak VRAM | Peak RAM | Peak Windows commit | Peak GPU / temperature | Result |
|---|---|---:|---:|---:|---:|---:|---:|---|
| Official MuseTalk 1.5, FP16, batch 4 | 640x368, 70 frames, 25 FPS, H.264/yuv420p + AAC mono 16 kHz | 32.8725 s | 35.976 s | 9,219 MiB | 23,341 MiB | 34,141 MiB | 99% / 67 C | Success |

Output: `E:\kang\github\Movie\artifacts\phase8\20260812-203657-29f959d5\output\musetalk-b9a88f6a1b1a4428965b79a737da58b3.mp4` (video 2.80 s, audio 2.78 s, A/V end delta 20 ms, SHA256 `73867115bff94ee05eadd31b5a2954eca4c0e1173ff30e95e9b55424d202ef27`). Full FFmpeg decode and database/provider-manifest link validation passed; ports 8020, 8030, and 8188 were free after verification.

Start/middle/end frames and the mouth contact sheet were visually approved: mouth shapes vary plausibly without a visible jaw/hand seam, mask edge, mouth tearing, severe flicker, or identity change. The Wan source is slightly soft, so the output is an accepted lip-sync baseline rather than a final-quality render. Phase 8 does not generate subtitles or a final timeline.
