# Phase Status

| Phase | Status | Verified result |
|---:|---|---|
| 0-4 | Complete | Local runtime, API/worker, structured generation, character identity, and storyboard evidence locked |
| 5 | Complete | Real Wan2.2 H.264 storyboard-to-video output |
| 6 | Complete | Real Qwen3-TTS voice-clone WAV with unload evidence |
| 7 | Complete | Dialogue-duration-driven Wan2.2 video linked in SQLite |
| 8 | Complete | Real official MuseTalk 1.5 audiovisual lip-sync derivative linked in SQLite |
| 9 | Complete | Deterministic Chinese SRT and real two-Shot H.264/AAC final render linked in SQLite |

## Phase 9 accepted evidence

- Run ID: `20260812-final-v3`
- Canonical output: `E:\kang\github\Movie\artifacts\phase9\20260812-final-v3\storage\projects\7700eb1d-9798-4960-9f13-c79ca30b2dba\output\final.mp4`
- Output SHA256: `b5c2bb824e8485530191ec4daed59b80d8855c415678424a31f8558f9a7a0a45`
- Media: 640x368, 145 frames at 25 FPS, H.264/yuv420p + AAC stereo 48 kHz, 5.8 seconds
- Subtitle: one UTF-8 Chinese cue from 0.000 to 2.480 seconds; SHA256 `6bf1a9593614ea9dba466b055f457eb3533e9497e7ab3ab4d851b0782194f828`
- Alias status: `READY`; immutable render, canonical alias, database pointers, Assets, manifests, and hashes agree
- Visual review: approved; readable safe-margin glyphs, clean subtitle removal, undistorted framing, and a clean non-black Shot transition

Known limitation: this is a manually seeded Phase 9 renderer acceptance using locked Phase 5/7/8 media. Phase 10 orchestration, BGM, automated perceptual quality gates, reliability expansion, and UI remain future work.

## Phase 8 accepted evidence

- Run ID: `20260812-205621-fad213b5`
- Output: `E:\kang\github\Movie\artifacts\phase8\20260812-205621-fad213b5\output\musetalk-e0eac769dadc4242ae8b6ac2f9ea55ab.mp4`
- Output SHA256: `58029ed0c9f539daed13faf643fba3c03f0c93e23d2814bfd36a44c144d09f98`
- Provider manifest SHA256: `28018ec070920c7e51b19ff840a0622200d5f6f26b3d3152ae727648e10c6777`
- Evidence SHA256: `2a1ed6908ed16ac95eb062a1e0208f964045bc921f7b00343015f9901f9fecaf`
- Database: `E:\kang\github\Movie\.runtime\phase8\phase8-smoke-20260812-205621-fad213b5.db`
- Media: 640x368, 70 frames at 25 FPS, H.264/yuv420p video 2.80 s, AAC mono 16 kHz audio 2.78 s
- Timing: 35.8198 s provider generation, 39.139 s end to end
- Peaks: 9,199 MiB VRAM, 23,548 MiB RAM, 34,272 MiB Windows commit, 97% GPU, 75 C
- Visual review: approved; visibly varied plausible mouth shapes with no observed seam, mask edge, tearing, severe flicker, or identity change

Known limitation: the source video is slightly soft. This accepted output is not subtitle composition or a final multi-shot render.
