# LocalDramaAI Phases 0-3 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Build a runnable Windows-first FastAPI/SQLite/Worker foundation with real Ollama structured generation and a validated ComfyUI client/workflow boundary, stopping before Phase 4.

**Architecture:** FastAPI owns short REST requests and reads SQLite state. A separate worker claims queued jobs atomically and records JobEvents. Providers are replaceable adapters; Ollama is called over HTTP and ComfyUI is integrated through a validated workflow client. Generated files are stored on disk and linked through minimal manifests.

**Tech Stack:** Python 3.13, FastAPI, Uvicorn, SQLAlchemy, SQLite WAL, Pydantic v2, httpx, PyYAML, pytest.

---

### Task 1: Environment and project skeleton

**Files:** `pyproject.toml`, `.env.example`, `runtime/runtime-lock.yaml`, `docs/ENVIRONMENT.md`, `app/core/config.py`

- [ ] Record detected tools and hardware in `docs/ENVIRONMENT.md` and initialize the runtime lock.
- [ ] Add installable application dependencies and configurable storage/service URLs.

### Task 2: Core persistence and API

**Files:** `app/db/*`, `app/models/*`, `app/schemas/*`, `app/api/*`, `app/main.py`, `tests/test_core.py`

- [ ] Add failing tests for WAL setup, project/job creation, and job event retrieval.
- [ ] Implement SQLAlchemy models, short transactions, and REST endpoints.

### Task 3: Worker and GPU/system services

**Files:** `app/worker_main.py`, `app/workers/*`, `app/services/*`, `tests/test_worker.py`

- [ ] Add failing tests for atomic claim and event emission.
- [ ] Implement single-worker polling, crash recovery, GPU status, and bounded provider lifecycle hooks.

### Task 4: Ollama structured provider

**Files:** `app/providers/ollama_provider.py`, `app/schemas/drama.py`, `tests/test_ollama_provider.py`

- [ ] Add failing tests for schema-constrained JSON parsing and unload behavior.
- [ ] Implement real HTTP calls to Ollama `/api/chat` and `/api/generate` with `format` set to the Pydantic JSON schema and `keep_alive=0`.

### Task 5: ComfyUI client, bindings, and manifest

**Files:** `app/comfyui/*`, `app/providers/comfyui_image_provider.py`, `app/models/generation.py`, `tests/test_comfyui.py`

- [ ] Add failing tests for workflow binding/validation and output registration.
- [ ] Implement health check, prompt submission, progress polling, output download, atomic asset registration, and minimal manifest creation.

### Task 6: Verification and operator scripts

**Files:** `scripts/*`, `README.md`, `docs/ARCHITECTURE.md`, `docs/MODELS.md`, `docs/WORKFLOWS.md`, `docs/INSTALL-WINDOWS.md`, `docs/TROUBLESHOOTING.md`, `docs/BENCHMARKS.md`

- [ ] Add Windows start/stop/check scripts and document exact commands.
- [ ] Run unit tests, API smoke test, worker smoke test, Ollama health/model check, and ComfyUI availability check; report any unavailable external service without fake success.
