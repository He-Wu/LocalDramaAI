# Models

No model is bundled. Configure the local Ollama model with `LOCALDRAMA_OLLAMA_MODEL`. ComfyUI workflows should refer to model filenames through bindings; license and hash metadata belongs in `models/models.yaml` when a model is installed.

Phase 4 uses the locked SD1.5 FP16 checkpoint as a baseline. It is adequate for validating the real generation and reference-image path, but its identity/wardrobe consistency is not production quality; later IPAdapter/ControlNet work should be evaluated separately.

Phase 5 uses Wan2.2 TI2V 5B FP16 with the Wan2.2 VAE and UMT5 XXL FP8 encoder. All three files are SHA256-locked in `runtime/runtime-lock.yaml`; ComfyUI loads the verified staging copies through `runtime/wan22-extra-model-paths.yaml`. The installer script downloads to staging, verifies hashes, then moves files into the standard ComfyUI model directories.

Phase 6 uses the official Qwen3-TTS 12Hz 0.6B Base checkpoint for local reference-audio voice cloning. It runs in `E:\LocalDramaAI\env-tts` with Python 3.12.4 and reuses the verified CUDA Torch 2.13.0+cu126 binaries through explicit environment junctions. The model, embedded 12Hz tokenizer, official reference audio, and accepted WAV hashes are locked in `runtime/runtime-lock.yaml`.
