# LocalDramaAI Environment

Checked through Phase 8 on 2026-08-12 on Windows.

| Component | Detected |
|---|---|
| OS | Windows |
| CPU | Intel Core i5-12600KF |
| GPU | NVIDIA GeForce RTX 4060 Ti, 16380 MiB |
| NVIDIA driver | 576.02 |
| Python | 3.13.3 (`C:\Users\Juan He\AppData\Local\Programs\Python\Python313\python.exe`) |
| Git | 2.46.0.windows.1 |
| FFmpeg | 8.1-full_build-www.gyan.dev |
| Ollama | 0.32.3, API online, `qwen2.5:0.5b` installed and unloaded after smoke call |
| Node/npm | 24.14.1 / 11.12.1 |
| ComfyUI | 0.31.0 at `E:\LocalDramaAI\ComfyUI`, commit `cbbc9dab1f03d0d9a6caa8a8be7d77a7e37e1e44` |
| MuseTalk | Official 1.5 code at `E:\LocalDramaAI\MuseTalk`, commit `0a89dec45a0192b824e3cf4daf96c239440c5ed8` |
| MuseTalk Python | 3.10.11 at `E:\LocalDramaAI\env-musetalk\Scripts\python.exe` |
| MuseTalk Torch/CUDA | Torch 2.0.1+cu118, torchvision 0.15.2+cu118, torchaudio 2.0.2+cu118, CUDA runtime 11.8 |
| MuseTalk OpenMMLab | mmcv 2.0.1, mmdet 3.1.0, mmengine 0.10.7, mmpose 1.1.0 |
| MuseTalk service | FastAPI 0.133.1, Pydantic 2.11.10, PyYAML 6.0.3, Rich 13.4.2, Typer 0.15.4, Uvicorn 0.41.0 |

The application defaults to `E:/LocalDramaAI` for data, but every path is configurable through `.env`.

MuseTalk is intentionally isolated from the application, Qwen3-TTS, and ComfyUI environments. `scripts/start_musetalk.ps1` starts Uvicorn on loopback port 8030 using `-m uvicorn ai_services.musetalk.service:app --app-dir <project-root> --host 127.0.0.1 --port 8030 --no-access-log`. Inference uses official `scripts.inference` with V1.5 weights, FP16, batch size 4, margin 10, jaw parsing, cheek widths 90/90, and the resolved FFmpeg directory. All 11 expected model files are pinned by repository revision, byte size, and SHA256 in `models/models.yaml`.
