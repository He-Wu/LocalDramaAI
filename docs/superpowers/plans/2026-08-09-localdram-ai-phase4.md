# LocalDramaAI Phase 4 Implementation Plan

> **For agentic workers:** Use this plan task-by-task with test-first verification. Scope ends after Character MASTER and Storyboard image generation.

**Goal:** Add a persistent Character Visual Bible and real ComfyUI-powered MASTER/Storyboard image generation with asset/reference/shot registration.

**Architecture:** Character identity is stored separately from shot descriptions in a JSON Visual Bible. Prompt builders always prepend the immutable identity and forbidden-change constraints. A Phase 4 service uses the existing ComfyUI image provider and creates CharacterReference/Shot records only after real files exist.

**Tech Stack:** SQLAlchemy, Pydantic, FastAPI-compatible service classes, existing ComfyUI SD1.5 provider.

---

### Task 1: Visual Bible and domain persistence

**Files:** `app/schemas/character.py`, `app/models/character.py`, `app/models/scene.py`, `app/models/shot.py`, `app/models/character_reference.py`, `app/models/__init__.py`, `tests/test_phase4.py`

- [ ] Add failing tests for Visual Bible normalization, identity locking, and Character/Scene/Shot persistence.
- [ ] Implement models and schema creation compatible with the existing SQLite database.

### Task 2: Prompt builders and registration service

**Files:** `app/services/character_generation.py`, `app/services/storyboard_generation.py`, `tests/test_phase4.py`

- [ ] Add failing tests proving MASTER and storyboard prompts contain the same identity text and deterministic seed derivation.
- [ ] Implement real-provider orchestration, Asset/CharacterReference/Shot registration, and manifest linkage.

### Task 3: Real Phase 4 smoke workflow

**Files:** `comfyui/workflows/phase4_character_storyboard.json`, `scripts/smoke_phase4.py`, `docs/BENCHMARKS.md`, `runtime/runtime-lock.yaml`

- [ ] Generate one MASTER and two storyboard PNGs through ComfyUI using the locked SD1.5 checkpoint.
- [ ] Verify files, dimensions, database links, VRAM/RAM observations, and run the full unit suite.
