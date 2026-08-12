# Phase Status

| Phase | Status | Verified result |
|---:|---|---|
| 0-4 | Complete | Local runtime, API/worker, structured generation, character identity, and storyboard evidence locked |
| 5 | Complete | Real Wan2.2 H.264 storyboard-to-video output |
| 6 | Complete | Real Qwen3-TTS voice-clone WAV with unload evidence |
| 7 | Complete | Dialogue-duration-driven Wan2.2 video linked in SQLite |
| 8 | Complete | Real official MuseTalk 1.5 audiovisual lip-sync derivative linked in SQLite |

## Phase 8 accepted evidence

- Run ID: `20260812-203657-29f959d5`
- Output: `E:\kang\github\Movie\artifacts\phase8\20260812-203657-29f959d5\output\musetalk-b9a88f6a1b1a4428965b79a737da58b3.mp4`
- Output SHA256: `73867115bff94ee05eadd31b5a2954eca4c0e1173ff30e95e9b55424d202ef27`
- Provider manifest SHA256: `7c90acc0710f4688eee35b38a73e0c5c60611c18b34668745669953d0b6bf940`
- Evidence SHA256: `a661e33eb1452ed110090dd86d7adb371c29cc965b5eccc06733c1db0190e912`
- Database: `E:\kang\github\Movie\.runtime\phase8\phase8-smoke-20260812-203657-29f959d5.db`
- Media: 640x368, 70 frames at 25 FPS, H.264/yuv420p video 2.80 s, AAC mono 16 kHz audio 2.78 s
- Timing: 32.8725 s provider generation, 35.976 s end to end
- Peaks: 9,219 MiB VRAM, 23,341 MiB RAM, 34,141 MiB Windows commit, 99% GPU, 67 C
- Visual review: approved; visibly varied plausible mouth shapes with no observed seam, mask edge, tearing, severe flicker, or identity change

Known limitation: the source video is slightly soft. This accepted output is not subtitle composition or a final multi-shot render.
