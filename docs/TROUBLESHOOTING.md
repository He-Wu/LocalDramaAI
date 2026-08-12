# Troubleshooting

- `503` or connection errors from Ollama mean the local Ollama HTTP server is not responding; start it and verify `GET /api/tags`.
- ComfyUI is intentionally not auto-installed. A health check failure is reported as unavailable, never as a successful image generation.
- PHASE 7 Wan offloading reached about 30.4 GB of 32 GB physical RAM and 51.5 GB Windows commit. Close browsers, D5, games, and other large applications before dialogue-video generation, and keep the page file enabled.
- A PHASE 7 video shorter than its Dialogue indicates an invalid custom profile. The locked service rounds upward to `4n+1` frames and rejects requests above 121 frames; split longer dialogue into multiple Shots instead of bypassing this guard.
- CUDA OOM diagnostics should include model, dimensions, frame count, and `nvidia-smi` output before retrying with a draft profile.
- If Qwen3-TTS reports `torch.cuda.is_available() == False`, rerun `scripts/setup_qwen3_tts.ps1`; a CPU-only Torch package may have replaced the verified CUDA junctions.
- The import-time `SoX could not be found` warning does not block the verified 12Hz voice-clone path. Install the SoX executable only if a future workflow actually needs its transforms.
- FlashAttention 2 is optional and was not used in the verified Windows Phase 6 runtime. Do not force-install an incompatible Windows build.
- Phase 9 rejects duplicate Scene/Shot/Dialogue order values, cross-project or wrong-kind Assets, materially short/corrupt media, stale timeline state, and eligible visible-speaker Shots without LIPSYNC. Fix the project graph instead of adding a fallback that would hide an upstream failure.
- If a render returns `alias_status=DEGRADED`, the immutable FINAL_VIDEO Asset and Project pointer are already committed; do not delete them. The next project render reconciles `output/final.mp4` under the project publication lock. `scripts.verify_phase9` intentionally rejects DEGRADED or stale aliases.
- FFmpeg failures should retain the exact native executable, stderr, input hashes, and private-job cleanup result. Do not remove `-xerror`, substitute `.cmd`/`.bat` wrappers, or point font configuration at an unlocked directory.
