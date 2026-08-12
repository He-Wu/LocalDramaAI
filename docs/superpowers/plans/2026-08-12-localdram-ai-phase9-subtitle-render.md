# LocalDramaAI Phase 9 Subtitle and Final Render Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Generate deterministic SRT subtitles and a real multi-Shot H.264/AAC `final.mp4` from existing project Assets, persist immutable outputs and manifests, and atomically publish the canonical project output.

**Architecture:** A pure timeline layer validates order, source ownership, frame timing, Dialogue WAVs, and cues. A dedicated FFmpeg renderer normalizes each Shot, rebuilds Dialogue audio, concatenates clips, burns a locked Chinese font, and validates an immutable candidate. A short stale-state transaction plus per-project publication lock registers outputs and Project pointers without entering Phase 10.

**Tech Stack:** Python 3.13, SQLAlchemy/Alembic/SQLite, FFmpeg/ffprobe 8.1, libx264/AAC/libass, Microsoft YaHei, pytest, PowerShell on Windows.

---

## File map

- `app/models/project.py`: project subtitle/final-video pointers.
- `migrations/versions/0002_phase9_project_outputs.py`: idempotent upgrade.
- `app/services/render_timeline.py`: snapshot, ordering, source selection, WAV validation, timing, plan hash.
- `app/services/subtitle_generation.py`: cues, SRT bytes, atomic SRT write.
- `app/providers/ffmpeg_render_provider.py`: isolated normalization, audio, concat, burn-in, probe/decode, cleanup.
- `app/services/render_generation.py`: stale recheck, lock, transaction, publication.
- `scripts/smoke_phase9.py` and `scripts/verify_phase9.py`: real evidence and independent verification.
- `tests/test_phase9_*.py` plus focused timeline/subtitle/FFmpeg tests.

### Task 1: Add project output pointers and migration

**Files:**
- Modify: `app/models/project.py`
- Create: `migrations/versions/0002_phase9_project_outputs.py`
- Create: `tests/test_phase9_migration.py`

- [ ] **Step 1: Write failing fresh-schema and Phase 8 upgrade tests.**

```python
def test_new_project_has_no_phase9_output_links(tmp_path):
    database = str(tmp_path / "fresh.db")
    initialize_database(database)
    with session_scope(database) as session:
        project = Project(name="Phase 9")
        session.add(project)
        session.flush()
        assert project.subtitle_asset_id is None
        assert project.final_video_asset_id is None

def test_phase8_database_upgrades_project_links_without_data_loss(tmp_path):
    database = seed_literal_phase8_database(tmp_path / "phase8.db")
    upgrade_schema(str(database))
    assert {"subtitle_asset_id", "final_video_asset_id"} <= inspect_columns(database, "projects")
    assert read_project(database)["name"] == "preserved"
```

- [ ] **Step 2: Run `python -m pytest tests/test_phase9_migration.py -q` and confirm RED for missing attributes/revision.**

- [ ] **Step 3: Before implementation, add RED tests for repeated initialization, downgrade, old-row preservation, and `ON DELETE SET NULL`; then add nullable named foreign keys and the migration.**

```python
subtitle_asset_id: Mapped[str | None] = mapped_column(
    ForeignKey("assets.id", name="fk_projects_subtitle_asset_id_assets", ondelete="SET NULL"),
    nullable=True,
)
final_video_asset_id: Mapped[str | None] = mapped_column(
    ForeignKey("assets.id", name="fk_projects_final_video_asset_id_assets", ondelete="SET NULL"),
    nullable=True,
)
```

Revision `0002_phase9_project_outputs` uses `down_revision = "0001_phase8_shot_lipsync"`, inspects existing columns, and uses SQLite batch recreation only for missing fields. Downgrade drops named FKs and columns. Repeated initialization stays safe when `create_all` created columns before Alembic.

- [ ] **Step 4: Run `python -m pytest tests/test_phase9_migration.py tests/test_phase8_migration.py -q` and observe GREEN.**

- [ ] **Step 5: Commit.**

```powershell
git add app/models/project.py migrations/versions/0002_phase9_project_outputs.py tests/test_phase9_migration.py
git commit -m "feat: migrate Phase 9 project outputs"
```

### Task 2: Build the immutable render timeline

**Files:**
- Create: `app/services/render_timeline.py`
- Create: `tests/test_render_timeline.py`

- [ ] **Step 1: Write RED tests for Scene/Shot/Dialogue order, duplicate rejection, and source priority.**

```python
def test_timeline_orders_and_selects_required_assets(seed_project):
    timeline = build_render_timeline(seed_project.database, seed_project.project_id)
    assert [shot.shot_id for shot in timeline.shots] == seed_project.expected_order
    assert [shot.video_asset_id for shot in timeline.shots] == [
        seed_project.lipsync_asset_id,
        seed_project.video_asset_id,
    ]

@pytest.mark.parametrize("duplicate", ["scene", "shot", "dialogue"])
def test_timeline_rejects_duplicate_order(seed_project, duplicate):
    seed_project.add_duplicate(duplicate)
    with pytest.raises(ValueError, match="duplicate.*order"):
        build_render_timeline(seed_project.database, seed_project.project_id)
```

- [ ] **Step 2: Run `python -m pytest tests/test_render_timeline.py -q` and confirm missing-module RED.**

- [ ] **Step 3: Implement only the immutable types, deterministic ordering, source selection, and integer-frame timing required by the first RED tests.**

```python
@dataclass(frozen=True)
class RenderProfile:
    width: int = 640
    height: int = 368
    fps: int = 25
    sample_rate: int = 48_000
    channels: int = 2

@dataclass(frozen=True)
class TimelineDialogue:
    dialogue_id: str
    order: int
    text: str
    persisted_duration: float
    persisted_start_time: float | None
    persisted_end_time: float | None
    audio_asset_id: str
    audio_asset_project_id: str
    audio_asset_kind: str
    audio_raw_path: str
    audio_path: Path
    audio_size: int
    audio_sha256: str
    start_ms: int
    end_ms: int

@dataclass(frozen=True)
class TimelineShot:
    shot_id: str
    scene_id: str
    character_id: str | None
    order: int
    persisted_duration: float
    status: str
    requires_lip_sync: bool
    speaker_visible: bool
    storyboard_asset_id: str | None
    source_video_asset_id: str | None
    source_lipsync_asset_id: str | None
    video_asset_id: str
    video_asset_project_id: str
    video_asset_kind: str
    video_raw_path: str
    video_path: Path
    video_size: int
    video_sha256: str
    start_frame: int
    frame_count: int
    dialogues: tuple[TimelineDialogue, ...]

@dataclass(frozen=True)
class TimelineSceneSnapshot:
    scene_id: str
    order: int
    shots: tuple[TimelineShot, ...]

@dataclass(frozen=True)
class RenderTimeline:
    project_id: str
    subtitle_asset_id: str | None
    final_video_asset_id: str | None
    profile: RenderProfile
    scenes: tuple[TimelineSceneSnapshot, ...]
    shots: tuple[TimelineShot, ...]
    total_frames: int
    canonical_json: str
    workflow_hash: str
```

Scene, Shot, Dialogue, and Asset snapshot records explicitly retain persisted
orders, durations/times/text, flags/status, upstream/current links, owner/kind,
raw/resolved path, byte size, and SHA256. Use `Decimal(str(duration))`,
`ROUND_CEILING` for frames, and `ROUND_HALF_UP` for milliseconds. Explicitly
join/order Scene, Shot, Dialogue. Require project ownership/kind, readable
nonempty paths, exact SHA256, valid probes, and no write transaction while probing.

- [ ] **Step 4: Add and run RED tests for wrong ownership/kind, missing/empty/corrupt/short media, invalid durations, partial times, overlap/overflow, and eligible Shot rules before adding those validations.**

Eligible visible-speaker Shots require LIPSYNC and exactly one Dialogue starting at zero; other Shots require VIDEO. Persisted WAV duration must agree within 20 ms.

- [ ] **Step 5: Implement the Step 4 validations, then add a separate stale-snapshot RED test before implementing `assert_render_timeline_unchanged`.**

It rereads Project pointers plus every used Scene/Shot/Dialogue/Asset order, text, duration, flag, link, and path under `BEGIN IMMEDIATE`/row locks and raises `RuntimeError("project timeline changed during Phase 9 render")` on change.

- [ ] **Step 6: Run `python -m pytest tests/test_render_timeline.py tests/test_phase8.py -q` and commit.**

```powershell
git add app/services/render_timeline.py tests/test_render_timeline.py
git commit -m "feat: snapshot Phase 9 render timelines"
```

### Task 3: Serialize deterministic safe SRT

**Files:**
- Create: `app/services/subtitle_generation.py`
- Create: `tests/test_subtitle_generation.py`

- [ ] **Step 1: Write an exact Chinese UTF-8/CRLF golden-byte RED test.**

```python
def test_serialize_srt_uses_exact_utf8_crlf_and_offsets():
    cues = (
        SubtitleCue(1, 0, 2480, "你好，欢迎来到本地短剧。"),
        SubtitleCue(2, 2800, 3800, "第二个镜头。"),
    )
    assert serialize_srt(cues) == (
        "1\r\n00:00:00,000 --> 00:00:02,480\r\n"
        "你好，欢迎来到本地短剧。\r\n\r\n"
        "2\r\n00:00:02,800 --> 00:00:03,800\r\n"
        "第二个镜头。\r\n"
    ).encode("utf-8")
```

- [ ] **Step 2: Run `python -m pytest tests/test_subtitle_generation.py -q` and confirm RED.**

- [ ] **Step 3: Implement only the exact golden path. Then add RED tests for one-hour time, multiline/control/style input, empty Dialogue timeline, non-overlap, atomic replacement, and cleanup failure before implementing each edge behavior.**

```python
@dataclass(frozen=True)
class SubtitleCue:
    index: int
    start_ms: int
    end_ms: int
    text: str

payload = serialize_srt(cues_from_timeline(timeline))
write_subtitle_atomic(output_path, payload)
```

Normalize CR/LF, remove disallowed C0 controls, escape styling metacharacters, reject control-only text, preserve Chinese/emoji, support one-hour timestamps, and use a unique same-directory temporary plus `os.replace`.

- [ ] **Step 4: Implement the edge behaviors required by the Step 3 RED tests and run focused GREEN.**

- [ ] **Step 5: Commit `app/services/subtitle_generation.py` and `tests/test_subtitle_generation.py` as `feat: generate deterministic Phase 9 subtitles`.**

### Task 4: Implement the isolated FFmpeg renderer

**Files:**
- Create: `app/providers/ffmpeg_render_provider.py`
- Create: `tests/test_ffmpeg_render.py`

- [ ] **Step 1: Write real-fixture RED tests with a 16 FPS video-only Shot and 25 FPS A/V Shot in paths containing spaces, Chinese, and apostrophes.**

```python
def test_renderer_normalizes_concatenates_audio_and_burns_subtitles(fixtures):
    result = FFmpegRenderProvider().render(
        fixtures.timeline,
        fixtures.srt,
        fixtures.output,
        fixtures.manifest,
    )
    info = probe_av(result.output_path)
    assert (info.video.width, info.video.height) == (640, 368)
    assert info.video.fps == pytest.approx(25)
    assert info.video.frames == fixtures.timeline.total_frames
    assert info.audio.codec == "aac"
```

- [ ] **Step 2: Run `python -m pytest tests/test_ffmpeg_render.py -q` and confirm missing-provider RED.**

- [ ] **Step 3: Implement runtime identity and bounded native process execution.**

```python
@dataclass(frozen=True)
class FFmpegIdentity:
    executable: Path
    version: str
    configuration: str
    font_path: Path
    font_sha256: str

@dataclass(frozen=True)
class FFmpegRenderResult:
    output_path: Path
    output_sha256: str
    media: AVInfo
    generation_time: float
    manifest_path: Path

class FFmpegRenderProvider:
    def __init__(self, executable="ffmpeg", probe_executable="ffprobe", font_path=DEFAULT_FONT):
        self.executable = resolve_native_executable(executable)
        self.probe_executable = resolve_native_executable(probe_executable)
        self.font_path = Path(font_path).resolve()
```

`resolve_native_executable` uses `shutil.which` for bare names, then resolves and
rejects `.cmd`/`.bat` wrappers. Require libass and readable Microsoft YaHei; use
argv, `shell=False`, `stdin=DEVNULL`, `-nostdin`, hidden window, bounded stderr,
`Popen.communicate(timeout)`, exact-process terminate/kill/wait, and propagated cleanup errors.

- [ ] **Step 4: Add RED tests for nonzero exit, timeout/process cleanup, missing libass/font, and corrupt/short inputs; then implement runtime/process handling and normalize each Shot in a private UUID job directory.**

Use scale/pad/setsar/fps/trim/tpad to exact frame count. Rebuild per-Shot audio from Dialogue WAVs with explicit delay/resampling and finite silence; never copy embedded video audio. Probe every canonical clip.

- [ ] **Step 5: Add RED tests for concat/profile/frame/A-V/burn-in and zero cues. Copy the attested font and SRT to private ASCII names `fonts/locked-font.ttc` and `subtitles.srt`, run FFmpeg with the job directory as cwd, and use only `subtitles.srt`/`fonts` in filter expressions. Then implement controlled concat and a zero-cue filter-skip. Encode libx264 `medium`, CRF 18, GOP 50, `keyint_min=25`, `sc_threshold=0`, `threads=1`, CFR 25; AAC-LC 48 kHz stereo 192 kbit/s; Microsoft YaHei 22 px, white, black 2 px outline, no shadow, bottom-centre, margin 24. Probe, fully decode, fsync, atomically move the immutable output, and atomically store the manifest at the caller-selected durable path outside the cleaned job directory.**

- [ ] **Step 6: Add RED tests for remaining malformed output, wrong frames, preexisting output preservation, temp cleanup, and durable manifest mismatch; then implement only those checks and observe GREEN.**

- [ ] **Step 7: Run `python -m pytest tests/test_ffmpeg_render.py tests/test_media_probe.py -q` and commit as `feat: render canonical Phase 9 timelines`.**

### Task 5: Persist and publish a project render

**Files:**
- Create: `app/services/render_generation.py`
- Create: `tests/test_phase9.py`

- [ ] **Step 1: Write the happy-path persistence RED test.**

```python
def test_render_project_persists_immutable_outputs_and_alias(seed_project, provider):
    result = render_project(seed_project.database, seed_project.project_id, provider, seed_project.storage)
    assert result.subtitle_asset.kind == "SUBTITLE"
    assert result.final_asset.kind == "FINAL_VIDEO"
    assert result.published_path == (
        seed_project.storage / "projects" / seed_project.project_id / "output" / "final.mp4"
    )
    assert sha256_file(Path(result.final_asset.path)) == sha256_file(result.published_path)
    assert result.alias_status == "READY"
```

- [ ] **Step 2: Run `python -m pytest tests/test_phase9.py -q` and confirm missing-service RED.**

- [ ] **Step 3: Implement only the happy path required by Step 1. Project pointer plus immutable Asset are authoritative; `output/final.mp4` is a reconstructible cache.**

1. Snapshot the timeline.
2. Write immutable `subtitles/{generation_id}.srt`.
3. Render immutable `render/{generation_id}.mp4` and durable `manifests/{generation_id}.json` outside a write transaction.
4. Verify SRT/output/provider manifest.
5. Acquire per-project OS file lock.
6. While holding that lock, reconcile a missing/stale alias from the current Project pointer after validating its immutable Asset.
7. Begin `BEGIN IMMEDIATE`/row locks and recheck timeline plus current pointers.
8. Insert SUBTITLE and FINAL_VIDEO Assets/manifests, update Project pointers, and commit.
9. Still holding the same lock, atomically rebuild `output/final.mp4`, verify its hash, and determine READY or DEGRADED before returning.

- [ ] **Step 4: Add RED assertions for exact metadata and manifest input ordering before implementing those records.**

Subtitle uses `application/x-subrip`, provider `local`, workflow
`subtitle_srt_v1`, and chronological cue AUDIO IDs with duplicates preserved
(`[]` for zero cues). Final uses `video/mp4`, provider `ffmpeg`, workflow
`final_render_v1`; it flattens each Shot as selected video ID then its Dialogue
AUDIO IDs, and appends the new SUBTITLE Asset ID only when cues were burned. Both
require `asset_id == output_asset`. After the RED assertions, implement and observe GREEN.

- [ ] **Step 5: Add RED tests before each implementation increment for stale order/text/duration/flags/links/paths/pointers, provider/manifest failure, alias failure/reconciliation, and two concurrent renders. Then implement and observe GREEN. Precommit failures preserve pointers. Postcommit alias failure returns the committed result with `alias_status="DEGRADED"` plus its error rather than raising ordinary failure; acceptance requires READY. Exactly one coherent selected Asset wins and its alias is repairable.**

- [ ] **Step 6: Run `python -m pytest tests/test_phase9.py tests/test_render_timeline.py tests/test_subtitle_generation.py tests/test_ffmpeg_render.py -q` and commit as `feat: persist Phase 9 final renders`.**

### Task 6: Add real Phase 9 smoke and verifier

**Files:**
- Create: `scripts/smoke_phase9.py`
- Create: `scripts/verify_phase9.py`
- Create: `tests/test_phase9_smoke.py`

- [ ] **Step 1: Write RED tests for safe run IDs/path containment and evidence binding.**

Reject traversal/absolute destinations, unlocked sources, missing evidence, database/output/alias mismatch, invalid SRT, wrong frames, failed decode, temps, and live owned FFmpeg.

- [ ] **Step 2: Run `python -m pytest tests/test_phase9_smoke.py -q` and confirm RED.**

- [ ] **Step 3: Implement `smoke_phase9` with a fresh two-Shot database using locked real inputs only.**

Use Phase 8 LIPSYNC SHA `58029ed0c9f539daed13faf643fba3c03f0c93e23d2814bfd36a44c144d09f98` plus the locked Phase 7 Dialogue WAV, and Phase 5 VIDEO SHA `8382967b7b092afa3a1948d1374b0e94496db7bf90c83c0233af7c57ee10b87e` as a silent Shot. Run `render_project`, require and record `alias_status="READY"`, monitor CPU/RAM/Windows commit/time, extract pre/in/post-cue and boundary frames/contact sheet, and write evidence JSON. State that this is manually seeded Phase 9 rendering, not Phase 10 automation.

- [ ] **Step 4: Implement `verify_phase9`.**

Read locked paths; constrain approved roots; require evidence `alias_status="READY"`; verify input/output/SRT/database/provider/evidence/resource/font hashes; parse SRT; validate Assets/manifests/Project pointers; require alias hash equals immutable Asset; count frames with ffprobe; fully decode; reject temps and owned FFmpeg survivors. The verifier is read-only and rejects DEGRADED/missing/stale aliases; only a later render reconciles them.

- [ ] **Step 5: Run `python -m pytest tests/test_phase9_smoke.py -q` and commit as `test: add Phase 9 real render smoke`.**

### Task 7: Run evidence, document, review, and publish

**Files:**
- Modify: `runtime/runtime-lock.yaml`
- Modify: `README.md`
- Modify: `docs/ARCHITECTURE.md`
- Modify: `docs/BENCHMARKS.md`
- Modify: `docs/ENVIRONMENT.md`
- Modify: `docs/PHASE_CHECKLIST.md`
- Modify: `docs/PHASE_STATUS.md`
- Modify: `docs/TROUBLESHOOTING.md`
- Modify: `docs/WORKFLOWS.md`

- [ ] **Step 1: Run `python -m scripts.smoke_phase9 --evidence-root E:\kang\github\Movie --visual-review approved`. It must fail unless real locked inputs, final/SRT/immutable outputs, DB/manifests/hashes/profile/decode/cleanup/review all pass.**

- [ ] **Step 2: Visually approve only if the transition is correct, Chinese glyphs are legible in safe margins during the cue, absent outside it, framing is undistorted, and there is no black/duplicate boundary frame.**

- [ ] **Step 3: Lock paths/hashes, FFmpeg build/config, font bytes/hash, profile, timing/resources, frames/A-V delta, database IDs, contact sheet, and visual result. Explicitly document missing Phase 10 orchestration, BGM, quality gates, reliability expansion, and UI.**

- [ ] **Step 4: Run complete verification.**

```powershell
python -m pytest -q
python -m compileall -q app scripts tests
Get-ChildItem -Recurse -Filter *.ps1 | ForEach-Object {
    $tokens=$null; $errors=$null
    [System.Management.Automation.Language.Parser]::ParseFile($_.FullName,[ref]$tokens,[ref]$errors) | Out-Null
    if ($errors.Count) { throw ($errors | Out-String) }
}
python -c "import yaml; yaml.safe_load(open('runtime/runtime-lock.yaml', encoding='utf-8')); print('YAML_OK')"
python -m scripts.verify_phase9 --evidence-root E:\kang\github\Movie
git diff --check
```

- [ ] **Step 5: Request independent read-only review of path/command injection, timing, media, cleanup, stale-state/concurrency, file/database atomicity, evidence binding, and Phase 10 scope. Fix every Critical/Important issue with witnessed RED-GREEN tests.**

- [ ] **Step 6: Re-run the exact complete verification after review fixes.**

- [ ] **Step 7: Commit and publish.**

```powershell
git add runtime/runtime-lock.yaml README.md docs
git commit -m "docs: lock Phase 9 final render evidence"
git push -u origin codex/phase9-subtitle-render
gh pr create --draft --base main --head codex/phase9-subtitle-render --title "feat: complete Phase 9 subtitle rendering"
```
