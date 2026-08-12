# LocalDramaAI Phase 9 Subtitle and Final Render Design

## Scope

Phase 9 turns one already-generated Project timeline into a real, subtitled
`final.mp4`. It generates SRT from persisted Dialogue text and measured Dialogue
WAV durations, selects each Shot's existing video derivative, normalizes the
heterogeneous media, concatenates the Shots, rebuilds the project audio from the
Dialogue WAV Assets, burns Chinese subtitles, validates the result, and registers
the subtitle and final video as Assets.

Phase 9 does not enqueue upstream generation, implement PipelineOrchestrator,
add approval gates, add background music, implement retries or GenerationAttempt,
or build UI. Those remain Phase 10-13 work.

## Chosen rendering approach

Use staged canonical clips followed by concat and subtitle burn-in. Each Shot is
first normalized in a private render job directory. The canonical clips are then
concatenated and the project SRT is burned into the final output.

This is preferred over one large `filter_complex` because the accepted sources
mix 16 and 25 FPS, video-only and audiovisual MP4s. Staging costs temporary disk
and one extra encode, but makes Windows path handling, per-Shot diagnostics,
timeouts, cleanup, and exact duration checks substantially safer. A soft subtitle
track is rejected because a player may hide it and would provide weaker evidence
that Phase 9 produced a visibly subtitled result.

## Timeline and source selection

Build an immutable Project snapshot ordered by `Scene.order`, then `Shot.order`.
Reject duplicate Scene orders within a Project and duplicate Shot orders within a
Scene; never use creation time as a hidden tiebreaker.

For every Shot:

- If `requires_lip_sync` and `speaker_visible` are both true, require its
  project-owned `LIPSYNC` Asset and use `lipsync_asset_id`. A missing lip-sync
  result is an error rather than a silent VIDEO fallback.
- Otherwise require its project-owned `VIDEO` Asset and use `video_asset_id`.
- Preserve every upstream Shot link and status.
- Snapshot `character_id`, `storyboard_asset_id`, `video_asset_id`, and
  `lipsync_asset_id`; any concurrent change is stale even when the field is not
  the selected render source.
- Require a readable, nonempty real media file and snapshot its resolved path,
  byte size, SHA256, Asset ID, and relevant database fields.

The editorial Shot duration is converted to an integer 25 FPS frame count with
`ceil(Decimal(duration) * 25)`. Shot start times are accumulated as integer
frames, not binary floats. A source must cover its editorial duration within one
source frame. Padding is allowed only for the final quantization frame.

## Dialogue audio and subtitle timing

Dialogue WAV Assets are the audio authority, even when a selected LIPSYNC MP4
contains audio. Rebuilding from the immutable WAV avoids double mixing and binds
the rendered audio and SRT to the same measured bytes.

Dialogues are ordered by `Dialogue.order`; duplicate orders are rejected. Every
Dialogue must have nonempty text, a project-owned `AUDIO` Asset, a valid PCM WAV,
and a persisted positive duration that agrees with the measured WAV within 20 ms.
For the current lip-sync contract, an eligible lip-sync Shot must contain exactly
one Dialogue starting at zero.

If both `start_time` and `end_time` are present, they are Shot-local, finite,
nonnegative, ordered, nonoverlapping, within the Shot, and their span must agree
with the WAV duration within 20 ms. If both are absent, Dialogues are packed
sequentially from zero. A partial pair is invalid. Absolute cue positions are the
integer-frame Shot start plus the local Dialogue position.

SRT is deterministic UTF-8 with CRLF lines, sequential cue numbers, and
`HH:MM:SS,mmm` timestamps rounded half-up from decimal seconds. Normalize input
newlines, remove disallowed control characters, and escape styling metacharacters
so Dialogue text cannot inject ASS/HTML styling. Projects without Dialogue may
produce an empty SRT and an all-silent AAC track; projects with zero Shots fail.

## FFmpeg rendering boundary

Add a dedicated Phase 9 renderer rather than expanding the existing single-file
conversion helper into an unsafe multi-purpose function.

The fixed render profile is:

- MP4 container with H.264/libx264, yuv420p, 640x368, 25 FPS, SAR 1.
- AAC-LC, 48 kHz stereo, timeline duration equal to the video within 80 ms.
- libx264 preset `medium`, CRF 18, GOP 50, `keyint_min=25`,
  `sc_threshold=0`, one encoding thread, CFR 25, and `+faststart`.
- AAC-LC at 48 kHz stereo and 192 kbit/s.
- Microsoft YaHei from the local Windows font directory, with exact path, byte
  size, and SHA256 recorded in the Phase 9 evidence. The renderer fails if the
  required libass/subtitles filter or locked font is unavailable.

Each Shot normalization command receives paths only as argv entries, with
`shell=False`, `-nostdin`, `stdin=DEVNULL`, bounded timeout, hidden-window flags,
and a private UUID directory. Bare command names are resolved with `shutil.which`
before their real executable paths are attested. Video is scaled with preserved aspect ratio, padded,
setsar/fps normalized, trimmed to the exact frame count, and padded only for the
quantization tail. Audio is rebuilt from WAV inputs using resampling, explicit
delays, and finite silence, then trimmed/padded to the exact total frame duration.
Before filter construction, copy the attested font bytes and SRT into the private
job directory as controlled ASCII names `fonts/locked-font.ttc` and
`subtitles.srt`. Run FFmpeg with that directory as `cwd` and use only those ASCII
relative names in `subtitles=...:fontsdir=fonts`; no caller path appears inside a
filter expression. Subtitle burn-in uses Microsoft YaHei at 22 px, white with a black 2 px outline,
no shadow, bottom-centre alignment, and a 24 px bottom margin. A zero-cue project
skips the subtitles filter while retaining the same silent AAC output profile.

FFmpeg commands write unique temporary files. A nonzero exit, timeout, missing or
empty output, failed probe, full-decode error, or cleanup failure is an error.
Timeout handling terminates and reaps the exact FFmpeg process. No partial output
is published or registered.

## Persistence and publication

Add two nullable Project foreign keys with `ON DELETE SET NULL`:

- `subtitle_asset_id`: current project-level `SUBTITLE` Asset.
- `final_video_asset_id`: current project-level `FINAL_VIDEO` Asset.

An Alembic revision upgrades existing Phase 8 databases idempotently and preserves
all existing rows. No broad Asset or GenerationManifest schema expansion enters
Phase 9.

Never overwrite historical Asset bytes. Store immutable versioned artifacts at:

- `storage/projects/{project_id}/subtitles/{generation_id}.srt`
- `storage/projects/{project_id}/render/{generation_id}.mp4`

The final video Asset points to the immutable render file. Its provider manifest
is atomically stored at `storage/projects/{project_id}/manifests/{generation_id}.json`.
The required canonical alias is atomically published through a same-directory temporary file to:

`storage/projects/{project_id}/output/final.mp4`

The canonical alias is a reconstructible cache; the Project pointer and immutable
Asset are authoritative. On every successful return the alias and immutable Asset
must have identical SHA256 hashes. Asset
metadata records the render profile, ordered role-tagged timeline, exact source
paths/hashes, cue count, durations, FFmpeg/font identity, immutable path,
published path, and output hash.

Create one GenerationManifest per output. Subtitle uses provider `local`, workflow
`subtitle_srt_v1`; final video uses provider `ffmpeg`, workflow
`final_render_v1`. For both, `asset_id == output_asset == Asset.id`.
`input_assets` preserves exact consumption order, and a SHA256 of the canonical
timeline/render plan is stored as `workflow_hash`.

## Concurrency, stale state, and failure behavior

Rendering never holds a long database write transaction. It snapshots and hashes
inputs, writes the immutable SRT, durable provider manifest, and candidate render,
probes and fully decodes the candidate, then acquires a per-project filesystem
publication lock. It first reconciles a missing/stale canonical alias from the
current authoritative Project pointer. Inside the lock it opens a short SQLite
`BEGIN IMMEDIATE` transaction (row locks on other databases), rereads the complete
explicit snapshot, and rejects changed Project pointers, order, text, persisted
timing, flags/status, upstream links, Asset owner/kind/path/size, or input hashes.

After the stale-state check, the transaction adds the two Assets, two Manifests,
and Project pointers and commits. It then atomically rebuilds the canonical alias
from the new immutable Asset and verifies the hash before returning. Successful
publication returns `alias_status=READY`. If publication fails after commit, the
operation returns a committed result with `alias_status=DEGRADED` and the cleanup
error; it does not raise an ordinary precommit failure or claim the pointer stayed
unchanged. Phase 9 acceptance and Job completion require READY. A crash may
leave orphan immutable files or a missing/stale alias cache, but never a Project
pointer to missing immutable bytes. The next render reconciles the alias from the
authoritative Project pointer. `verify_phase9` is strictly read-only and rejects
a missing/stale alias. Alias publication failure is reported and is not described
as an accepted Phase 9 operation.

Two concurrent renders for the same Project are serialized at publication. A
stale loser cannot overwrite the winner. Precommit failures leave upstream Assets,
Shot links/statuses, Project pointers, and any previously accepted final output
unchanged. Postcommit alias degradation preserves the new authoritative result
and is explicitly distinguishable and repairable.

Manifest inputs are exact. The SUBTITLE manifest contains chronological AUDIO
Asset IDs, one per cue (duplicates preserved), or `[]` for zero cues. The
FINAL_VIDEO manifest flattens each chronological Shot as its selected VIDEO or
LIPSYNC Asset ID followed by that Shot's chronological Dialogue AUDIO Asset IDs;
after all Shots it appends the new SUBTITLE Asset ID only when at least one cue was
burned. Zero-cue renders do not list the unconsumed subtitle Asset.

## Real smoke and acceptance

The real smoke creates a fresh Phase 9 database and manually seeds a two-Shot
timeline from locked real AI outputs:

- the accepted Phase 8 LIPSYNC MP4 plus its exact Phase 7 Dialogue WAV;
- the accepted Phase 5 video-only MP4 as a silent non-dialogue Shot.

This honestly proves the Phase 9 renderer and does not claim the Phase 10
story-to-final orchestrator is complete.

Acceptance requires:

- Source hashes equal the locked real artifacts; accepted video evidence is not
  generated with lavfi or mocked.
- The SRT contains the exact Chinese Dialogue text and expected frame-derived
  timestamps.
- `storage/projects/{project_id}/output/final.mp4` exists and no temporary output
  remains.
- Full FFmpeg decode passes. Output is H.264/yuv420p 640x368 at 25 FPS with AAC
  audio, exact calculated frame count, and A/V end delta at most 80 ms.
- Extracted frames show the expected transition between both real Shots. A frame
  inside the cue visibly contains legible Chinese glyphs within safe margins and
  a post-cue frame has no subtitle.
- The immutable files, canonical alias, Assets, Manifests, Project pointers,
  ordered inputs, hashes, and database snapshot all agree.
- `scripts.verify_phase9` independently rechecks paths, hashes, SRT timing,
  database links, probes, frame count, full decode, and absence of owned FFmpeg
  processes or temporary files. Evidence must record `alias_status=READY`; a
  DEGRADED result or alias mismatch fails read-only verification.
- Runtime lock and documentation record FFmpeg build, font identity, timing,
  resource use, output/database/evidence hashes, visual review, and the explicit
  Phase 10 limitation.

## Testing strategy

Use test-driven development. Unit and integration coverage includes migration and
fresh-schema behavior; deterministic SRT bytes and Unicode/control escaping;
integer-frame timing and one-hour timestamps; duplicate/invalid order and timing;
asset priority/ownership/kind/path checks; video-only and audiovisual source
normalization; Windows paths with spaces, Unicode, and apostrophes; real FFmpeg
timeout/nonzero/corrupt input behavior; atomic publication; stale snapshots;
concurrent render attempts; rollback injection; final probe/full decode; and the
real two-Shot smoke evidence.
