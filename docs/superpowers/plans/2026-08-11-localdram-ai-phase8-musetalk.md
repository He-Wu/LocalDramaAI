# LocalDramaAI PHASE 8 MuseTalk Lip-Sync Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Produce and persist one real MuseTalk 1.5 audiovisual lip-sync MP4 from the verified PHASE 7 dialogue video and WAV, only when both Shot eligibility gates are true.

**Architecture:** A Python 3.13 application provider calls a loopback FastAPI service on port 8030. The service runs the official MuseTalk CLI in an isolated Python 3.10/Torch 2.0.1 environment per request, normalizes inputs to the official 25 FPS profile, validates the output, and exits the child process to release VRAM. Application persistence happens only after independent A/V probing and a stale-state recheck.

**Tech Stack:** Python 3.13 application, FastAPI/httpx, SQLAlchemy/Alembic/SQLite, Python 3.10 MuseTalk environment, PyTorch 2.0.1 cu118, MuseTalk 1.5, FFmpeg/ffprobe, pytest.

---

### Task 1: Add Shot eligibility fields and an upgrade path

**Files:**
- Modify: `app/models/shot.py`
- Modify: `app/schemas/drama.py`
- Create: `alembic.ini`
- Create: `migrations/env.py`
- Create: `migrations/script.py.mako`
- Create: `migrations/versions/0001_phase8_shot_lipsync.py`
- Create: `app/db/migrations.py`
- Create: `tests/test_phase8_migration.py`

- [ ] **Step 1: Write a failing model/default test and old-database upgrade test.**

```python
def test_phase8_shot_defaults_are_false(tmp_path):
    database = str(tmp_path / "new.db")
    create_schema(database)
    # Seed Project -> Scene -> Shot, reload it, and assert both flags are False
    # and lipsync_asset_id is None.

def test_phase7_database_upgrades_without_losing_video_link(tmp_path):
    database = str(tmp_path / "old.db")
    # Create a literal pre-PHASE-8 shots table and one row with video_asset_id.
    upgrade_schema(database)
    # Inspect columns and row values; expect requires_lip_sync=0,
    # speaker_visible=0, lipsync_asset_id=NULL, original video_asset_id unchanged.
```

- [ ] **Step 2: Run the focused tests and confirm RED.**

Run: `python -m pytest tests/test_phase8_migration.py -q`

Expected: collection/import failure for `upgrade_schema` or missing Shot fields.

- [ ] **Step 3: Add fields, Alembic environment, and SQLite batch migration.**

The migration must inspect `shots`, then add only missing columns with:

```python
with op.batch_alter_table("shots", recreate="always") as batch:
    batch.add_column(sa.Column("requires_lip_sync", sa.Boolean(), nullable=False, server_default=sa.false()))
    batch.add_column(sa.Column("speaker_visible", sa.Boolean(), nullable=False, server_default=sa.false()))
    batch.add_column(sa.Column("lipsync_asset_id", sa.String(length=36), nullable=True))
    batch.create_foreign_key("fk_shots_lipsync_asset_id_assets", "assets", ["lipsync_asset_id"], ["id"], ondelete="SET NULL")
```

`upgrade_schema(database_url)` must construct an Alembic `Config`, set `sqlalchemy.url` to the normalized URL, and call `command.upgrade(config, "head")`.

- [ ] **Step 4: Run focused tests and the existing suite.**

Run: `python -m pytest tests/test_phase8_migration.py tests/test_core.py tests/test_phase7.py -q`

Expected: all selected tests pass.

- [ ] **Step 5: Commit.**

```powershell
git add app/models/shot.py app/schemas/drama.py app/db/migrations.py alembic.ini migrations tests/test_phase8_migration.py
git commit -m "feat: migrate phase 8 shot fields"
```

### Task 2: Probe real audiovisual media

**Files:**
- Create: `app/services/media_probe.py`
- Create: `tests/test_media_probe.py`

- [ ] **Step 1: Write failing tests using real FFmpeg-generated media.**

```python
def test_probe_av_reports_video_and_audio(tmp_path):
    path = make_h264_aac_fixture(tmp_path, seconds=1.0, fps=25)
    info = probe_av(path)
    assert info.video.codec == "h264"
    assert info.video.pixel_format == "yuv420p"
    assert info.video.fps == pytest.approx(25)
    assert info.audio.codec == "aac"
    assert info.audio.channels == 1

def test_probe_av_rejects_video_without_audio(tmp_path):
    with pytest.raises(ValueError, match="audio"):
        probe_av(make_video_only_fixture(tmp_path))
```

- [ ] **Step 2: Run the tests and confirm RED due to missing module.**

Run: `python -m pytest tests/test_media_probe.py -q`

- [ ] **Step 3: Implement one ffprobe JSON call and strict dataclasses.**

Define `VideoStreamInfo`, `AudioStreamInfo`, and `AVInfo`. Parse `v:0` and `a:0`, including codec, pixel format, dimensions, rational FPS, frames when available, stream durations, sample rate, channels, and format duration. Reject missing/unplayable streams.

- [ ] **Step 4: Run the tests and commit.**

```powershell
python -m pytest tests/test_media_probe.py -q
git add app/services/media_probe.py tests/test_media_probe.py
git commit -m "feat: probe audiovisual outputs"
```

### Task 3: Implement eligibility and atomic PHASE 8 persistence

**Files:**
- Create: `app/services/lipsync_generation.py`
- Create: `tests/test_phase8.py`

- [ ] **Step 1: Write the four-value gate matrix test.**

Use a provider whose `generate` raises if called. Seed all four combinations of the two booleans and assert the provider is invoked only for `True/True`; false combinations return `None` without Asset, Manifest, status, or link changes.

- [ ] **Step 2: Run and confirm RED.**

Run: `python -m pytest tests/test_phase8.py -q`

- [ ] **Step 3: Implement the gate and input snapshot only.**

`generate_shot_lipsync(database_url, project_id, shot_id, provider, output_dir)` must check ownership, gates, exactly one Dialogue, project-owned source VIDEO/AUDIO assets, source media validity, WAV duration agreement within 0.02 seconds, and source coverage before invoking the provider outside the transaction.

- [ ] **Step 4: Add a failing happy-path persistence test.**

The deterministic provider must create a real H.264/yuv420p + AAC 25 FPS MP4 with FFmpeg and return a JSON manifest. Assert source link immutability, `lipsync_asset_id`, `LIPSYNC_GENERATED`, `Asset(kind="LIPSYNC")`, and manifest inputs exactly `[video_asset_id, audio_asset_id]`.

- [ ] **Step 5: Implement output validation, stale-state recheck, and one final transaction.**

Require 640x368, 25 FPS, H.264/yuv420p, playable AAC audio, output coverage within one frame, A/V end difference at most 80 ms, valid provider manifest, and unchanged flags/source IDs before writing Asset + Manifest + Shot.

- [ ] **Step 6: Add failure tests.**

Cover wrong project, zero/multiple Dialogues, missing/malformed source files, wrong Asset kinds, stale duration, provider exception, invalid/video-only/short output, invalid manifest, and concurrent mutation. Every case must leave database success state unchanged.

- [ ] **Step 7: Run and commit.**

```powershell
python -m pytest tests/test_phase8.py -q
git add app/services/lipsync_generation.py tests/test_phase8.py
git commit -m "feat: persist validated lip sync outputs"
```

### Task 4: Add the application MuseTalk HTTP provider

**Files:**
- Create: `app/providers/musetalk_provider.py`
- Create: `tests/test_musetalk_provider.py`
- Modify: `app/core/config.py`
- Modify: `.env.example`

- [ ] **Step 1: Write failing MockTransport tests.**

Test `/health`, `/generate`, `/unload`, timeout/error propagation, missing source rejection, missing returned output rejection, and forwarding `target_duration`, `batch_size=4`, and `use_float16=true`.

- [ ] **Step 2: Run and confirm RED.**

Run: `python -m pytest tests/test_musetalk_provider.py -q`

- [ ] **Step 3: Implement the provider.**

`MuseTalkProvider(base_url="http://127.0.0.1:8030", timeout=1800)` uses `httpx.AsyncClient`; `generate(video_path, audio_path, output_dir, metadata)` posts absolute paths and validates the returned MP4/manifest exist before returning them.

- [ ] **Step 4: Add `musetalk_url` to settings/example env, run tests, and commit.**

```powershell
python -m pytest tests/test_musetalk_provider.py -q
git add app/providers/musetalk_provider.py tests/test_musetalk_provider.py app/core/config.py .env.example
git commit -m "feat: call local MuseTalk service"
```

### Task 5: Build the isolated MuseTalk service contract

**Files:**
- Create: `ai_services/musetalk/__init__.py`
- Create: `ai_services/musetalk/service.py`
- Create: `ai_services/musetalk/requirements.lock.txt`
- Create: `tests/test_phase8_service.py`

- [ ] **Step 1: Write failing pure-function and API validation tests.**

Test request path validation, unique job configuration generation, expected official output path, rejection when the CLI exits nonzero, rejection when it exits zero without output, and atomic publishing. Stub only the subprocess boundary; use real FFmpeg fixtures for normalization/probing.

- [ ] **Step 2: Run and confirm RED.**

Run: `python -m pytest tests/test_phase8_service.py -q`

- [ ] **Step 3: Implement the loopback service.**

Use FastAPI models plus a module-level `asyncio.Lock`. Normalize video to 25 FPS H.264/yuv420p and audio to 16 kHz mono PCM padded to target duration. Invoke:

```python
command = [
    str(settings.python_executable), "-m", "scripts.inference",
    "--inference_config", str(job_dir / "task.yaml"),
    "--result_dir", str(job_dir / "results"),
    "--unet_model_path", "models/musetalkV15/unet.pth",
    "--unet_config", "models/musetalkV15/musetalk.json",
    "--version", "v15", "--use_float16", "--batch_size", "4",
    "--extra_margin", "10", "--parsing_mode", "jaw",
    "--left_cheek_width", "90", "--right_cheek_width", "90",
    "--ffmpeg_path", str(settings.ffmpeg_bin),
]
```

Run with `cwd=E:/LocalDramaAI/MuseTalk`, an argument list, `shell=False`, captured logs, and a bounded timeout. Validate and canonicalize the output before atomic publication. `/health` checks repo/model/Python/CUDA paths; `/unload` reports no persistent model and refuses to claim unloaded while a child is active.

- [ ] **Step 4: Run tests and commit.**

```powershell
python -m pytest tests/test_phase8_service.py -q
git add ai_services/musetalk tests/test_phase8_service.py
git commit -m "feat: add isolated MuseTalk service"
```

### Task 6: Add reproducible Windows setup and lifecycle scripts

**Files:**
- Create: `scripts/setup_musetalk.ps1`
- Create: `scripts/download_musetalk_models.ps1`
- Create: `scripts/start_musetalk.ps1`
- Modify: `scripts/start_ai_services.ps1`
- Modify: `scripts/stop_all.ps1`
- Modify: `scripts/check_environment.ps1`
- Modify: `.gitignore`

- [ ] **Step 1: Add PowerShell parse verification before execution.**

Run the PowerShell parser over every new/modified `.ps1`; the setup scripts must fail on nonzero installer/download/hash checks and must never write into app/TTS/ComfyUI environments.

- [ ] **Step 2: Implement setup.**

Clone `https://github.com/TMElyralab/MuseTalk.git` at a resolved commit into `E:/LocalDramaAI/MuseTalk`; create `E:/LocalDramaAI/env-musetalk` with Python 3.10; install official Torch 2.0.1/cu118 packages, locked requirements, and official OpenMMLab versions. Validate imports and CUDA.

- [ ] **Step 3: Implement model download.**

Use official Hugging Face repositories/files only. Download MuseTalk V1.5, SD VAE, Whisper tiny, DWPose, LatentSync SyncNet, and face parsing weights; verify every expected file is nonempty and calculate SHA256 hashes.

- [ ] **Step 4: Implement lifecycle.**

Start Uvicorn on `127.0.0.1:8030` hidden, add it to AI-service startup, and stop both Uvicorn and any child `scripts.inference` process. Add `.worktrees/` to `.gitignore`.

- [ ] **Step 5: Parse, run setup/download, verify imports/CUDA/models, and commit scripts.**

### Task 7: Run a real official MuseTalk smoke

**Files:**
- Create: `scripts/smoke_phase8.py`
- Create: `scripts/verify_phase8.py`

- [ ] **Step 1: Seed a fresh database from verified PHASE 7 media.**

Create a project/scene/eligible Shot with source VIDEO and one Dialogue AUDIO Asset, preserving the 2.78-second Shot duration.

- [ ] **Step 2: Start only MuseTalk and monitor resources.**

Confirm ports 8020/8188 are free, start 8030, record VRAM/RAM/Windows commit/temperature once per second, and call `generate_shot_lipsync`.

- [ ] **Step 3: Assert evidence in the smoke script.**

Exit nonzero unless the output fully decodes, contains H.264/yuv420p + AAC, is 640x368 at 25 FPS, covers the Shot within one frame, has A/V end delta <=80 ms, and all Asset/Manifest/Shot links agree.

- [ ] **Step 4: Run the real smoke and inspect frames.**

Extract start/middle/end frames and a mouth-region contact sheet. Reject visible jaw/hand seam, mask edge, mouth tearing, severe flicker, identity change, or unchanged mouth motion.

- [ ] **Step 5: Stop service and verify VRAM/port release.**

`scripts.verify_phase8` must read PHASE 8 paths from `runtime/runtime-lock.yaml`, probe the locked A/V file, verify its SHA256 and database links, decode it with FFmpeg, and fail if ports 8020, 8030, or 8188 are listening.

### Task 8: Lock evidence, document, review, and publish

**Files:**
- Modify: `runtime/runtime-lock.yaml`
- Modify: `models/models.yaml`
- Modify: `README.md`
- Modify: `docs/ARCHITECTURE.md`
- Modify: `docs/BENCHMARKS.md`
- Modify: `docs/ENVIRONMENT.md`
- Modify: `docs/INSTALL-WINDOWS.md`
- Modify: `docs/MODELS.md`
- Modify: `docs/TROUBLESHOOTING.md`
- Modify: `docs/WORKFLOWS.md`

- [ ] **Step 1: Record exact evidence.**

Lock MuseTalk commit, Python/Torch/CUDA/package versions, all model revisions/hashes, launch args, real output/database/hash, elapsed time, peak resources, and visual limitations. Do not claim subtitles or final rendering.

- [ ] **Step 2: Run full verification.**

```powershell
python -m pytest -q
$pythonFiles = git ls-files '*.py'
python -m py_compile $pythonFiles
Get-ChildItem scripts -Filter *.ps1 | ForEach-Object { $errors=$null; [void][System.Management.Automation.Language.Parser]::ParseFile($_.FullName,[ref]$null,[ref]$errors); if ($errors.Count) { throw $errors } }
python -c "import yaml; yaml.safe_load(open('runtime/runtime-lock.yaml', encoding='utf-8')); yaml.safe_load(open('models/models.yaml', encoding='utf-8')); print('YAML_OK')"
$phase8Video = python -c "import yaml; print(yaml.safe_load(open('runtime/runtime-lock.yaml', encoding='utf-8'))['verification']['phase8_lipsync'])"
ffmpeg -v error -i $phase8Video -f null -
python -m scripts.verify_phase8
```

- [ ] **Step 3: Request an independent read-only review and fix every Critical/Important finding using RED-GREEN tests.**

- [ ] **Step 4: Re-run the entire verification command after review fixes.**

- [ ] **Step 5: Commit and push the feature branch.**

```powershell
git add -A
git commit -m "feat: complete phase 8 MuseTalk lip sync"
git push -u origin agent/phase8-musetalk
```
