# Windows Installation

```powershell
python -m pip install -e .
Copy-Item .env.example .env
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
python -m app.worker_main
```

Install ComfyUI separately in its own environment following the ComfyUI project’s current Windows instructions, then set `LOCALDRAMA_COMFYUI_URL`. Do not merge its PyTorch environment with this application environment.

Install and download the Phase 6 TTS runtime separately:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\setup_qwen3_tts.ps1
powershell -ExecutionPolicy Bypass -File scripts\download_qwen3_tts_models.ps1
powershell -ExecutionPolicy Bypass -File scripts\start_qwen3_tts.ps1
```

The service listens only on `127.0.0.1:8020`. The setup script keeps Python packages in `E:\LocalDramaAI\env-tts` and links only the already-verified CUDA Torch/Torchaudio directories from `env-comfyui`; it does not install a second CPU-only Torch runtime.
