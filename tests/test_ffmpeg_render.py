from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass, replace
from pathlib import Path

import pytest

from app.providers.ffmpeg_render_provider import FFmpegRenderProvider
from app.providers.ffmpeg_render_provider import resolve_native_executable
from app.services.media_probe import probe_av
from app.services.render_timeline import (
    RenderProfile,
    RenderTimeline,
    TimelineDialogue,
    TimelineSceneSnapshot,
    TimelineShot,
)


def _run_ffmpeg(*arguments: object) -> None:
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *map(str, arguments)],
        check=True,
        capture_output=True,
        timeout=30,
        shell=False,
    )


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _frame_hash(path: Path, timestamp: float) -> str:
    result = subprocess.run(
        [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-ss", str(timestamp),
            "-i", str(path), "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "rgb24", "-",
        ],
        check=True,
        capture_output=True,
        timeout=30,
        shell=False,
    )
    return hashlib.sha256(result.stdout).hexdigest()


@dataclass(frozen=True)
class RenderFixture:
    timeline: RenderTimeline
    srt: bytes
    output: Path
    manifest: Path


@pytest.fixture
def render_fixture(tmp_path: Path) -> RenderFixture:
    source_dir = tmp_path / "inputs with spaces 中国's"
    source_dir.mkdir()
    video_16 = source_dir / "第一 shot.mp4"
    video_25 = source_dir / "second's 镜头.mp4"
    dialogue_wav = source_dir / "对白 voice.wav"
    _run_ffmpeg(
        "-f", "lavfi", "-i", "testsrc2=size=320x240:rate=16:duration=0.5",
        "-an", "-c:v", "libx264", "-pix_fmt", "yuv420p", video_16,
    )
    _run_ffmpeg(
        "-f", "lavfi", "-i", "color=c=blue:size=800x450:rate=25:duration=0.4",
        "-f", "lavfi", "-i", "sine=frequency=900:sample_rate=44100:duration=0.4",
        "-map", "0:v:0", "-map", "1:a:0", "-c:v", "libx264",
        "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", video_25,
    )
    _run_ffmpeg(
        "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=16000:duration=0.2",
        "-c:a", "pcm_s16le", dialogue_wav,
    )

    dialogue = TimelineDialogue(
        dialogue_id="dialogue-1", order=0, text="你好，世界", persisted_duration=0.2,
        persisted_start_time=0.1, persisted_end_time=0.3,
        audio_asset_id="audio-1", audio_asset_project_id="project-1",
        audio_asset_kind="AUDIO", audio_raw_path=str(dialogue_wav),
        audio_path=dialogue_wav.resolve(), audio_size=dialogue_wav.stat().st_size,
        audio_sha256=_sha256(dialogue_wav), start_ms=100, end_ms=300,
    )

    def shot(
        shot_id: str,
        source: Path,
        start_frame: int,
        frames: int,
        dialogues: tuple[TimelineDialogue, ...] = (),
    ) -> TimelineShot:
        return TimelineShot(
            shot_id=shot_id, scene_id="scene-1", character_id=None,
            order=start_frame, persisted_duration=frames / 25, status="READY",
            requires_lip_sync=False, speaker_visible=False,
            storyboard_asset_id=None, source_video_asset_id=f"asset-{shot_id}",
            source_lipsync_asset_id=None, video_asset_id=f"asset-{shot_id}",
            video_asset_project_id="project-1", video_asset_kind="VIDEO",
            video_raw_path=str(source), video_path=source.resolve(),
            video_size=source.stat().st_size, video_sha256=_sha256(source),
            start_frame=start_frame, frame_count=frames, dialogues=dialogues,
        )

    shots = (
        shot("shot-1", video_16, 0, 10, (dialogue,)),
        shot("shot-2", video_25, 10, 10),
    )
    timeline = RenderTimeline(
        project_id="project-1", subtitle_asset_id=None, final_video_asset_id=None,
        profile=RenderProfile(), scenes=(TimelineSceneSnapshot("scene-1", 0, shots),),
        shots=shots, total_frames=20, canonical_json='{"project_id":"project-1"}',
        workflow_hash="a" * 64,
    )
    return RenderFixture(
        timeline=timeline,
        srt=b"1\r\n00:00:00,100 --> 00:00:00,300\r\n\xe4\xbd\xa0\xe5\xa5\xbd\xef\xbc\x8c\xe4\xb8\x96\xe7\x95\x8c\r\n",
        output=tmp_path / "published output" / "final immutable.mp4",
        manifest=tmp_path / "published output" / "manifest.json",
    )


def test_renderer_normalizes_concatenates_audio_and_burns_subtitles(
    render_fixture: RenderFixture,
) -> None:
    result = FFmpegRenderProvider().render(
        render_fixture.timeline,
        render_fixture.srt,
        render_fixture.output,
        render_fixture.manifest,
    )

    info = probe_av(result.output_path)
    assert (info.video.width, info.video.height) == (640, 368)
    assert info.video.fps == pytest.approx(25)
    assert info.video.frames == render_fixture.timeline.total_frames
    assert info.audio.codec == "aac"
    assert info.audio.sample_rate == 48_000
    assert info.audio.channels == 2
    assert result.output_sha256 == _sha256(result.output_path)
    manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))
    assert manifest["output_sha256"] == result.output_sha256
    assert manifest["srt_sha256"] == hashlib.sha256(render_fixture.srt).hexdigest()
    assert manifest["timeline"] == [
        {
            "shot_id": "shot-1",
            "start_frame": 0,
            "frame_count": 10,
            "video": {
                "asset_id": "asset-shot-1",
                "path": str(render_fixture.timeline.shots[0].video_path),
                "sha256": render_fixture.timeline.shots[0].video_sha256,
            },
            "audio": [
                {
                    "asset_id": "audio-1",
                    "path": str(render_fixture.timeline.shots[0].dialogues[0].audio_path),
                    "sha256": render_fixture.timeline.shots[0].dialogues[0].audio_sha256,
                    "start_ms": 100,
                    "end_ms": 300,
                }
            ],
        },
        {
            "shot_id": "shot-2",
            "start_frame": 10,
            "frame_count": 10,
            "video": {
                "asset_id": "asset-shot-2",
                "path": str(render_fixture.timeline.shots[1].video_path),
                "sha256": render_fixture.timeline.shots[1].video_sha256,
            },
            "audio": [],
        },
    ]


def test_renderer_rejects_source_that_no_longer_covers_snapshot_duration(
    render_fixture: RenderFixture,
    tmp_path: Path,
) -> None:
    short = tmp_path / "short.mp4"
    _run_ffmpeg(
        "-f", "lavfi", "-i", "color=c=red:size=640x368:rate=25",
        "-frames:v", "1", "-an", "-c:v", "libx264", "-pix_fmt", "yuv420p", short,
    )
    first = replace(
        render_fixture.timeline.shots[0],
        video_path=short.resolve(),
        video_raw_path=str(short),
        video_size=short.stat().st_size,
        video_sha256=_sha256(short),
    )
    shots = (first, render_fixture.timeline.shots[1])
    timeline = replace(
        render_fixture.timeline,
        shots=shots,
        scenes=(TimelineSceneSnapshot("scene-1", 0, shots),),
    )

    with pytest.raises((ValueError, RuntimeError), match="short|cover"):
        FFmpegRenderProvider().render(
            timeline,
            render_fixture.srt,
            render_fixture.output,
            render_fixture.manifest,
        )

    assert not render_fixture.output.exists()
    assert not render_fixture.manifest.exists()
    assert not list(render_fixture.output.parent.glob("*.tmp"))
    assert not list(render_fixture.output.parent.glob(".*.tmp"))


def test_resolve_native_executable_rejects_shell_wrapper(tmp_path: Path) -> None:
    wrapper = tmp_path / "ffmpeg.cmd"
    wrapper.write_text("@exit /b 0\n", encoding="ascii")

    with pytest.raises(RuntimeError, match="wrapper"):
        resolve_native_executable(wrapper)


def test_native_runner_reports_nonzero_exit_with_bounded_stderr(tmp_path: Path) -> None:
    provider = FFmpegRenderProvider()

    with pytest.raises(RuntimeError, match="code 7.*sentinel") as raised:
        provider._run(
            [
                sys.executable,
                "-c",
                "import sys; sys.stderr.write('x'*20000 + 'sentinel'); raise SystemExit(7)",
            ],
            cwd=tmp_path,
        )

    assert len(str(raised.value)) < 9000


@pytest.mark.skipif(os.name != "nt", reason="verifies Windows process cleanup")
def test_native_runner_timeout_terminates_and_reaps_exact_process(tmp_path: Path) -> None:
    provider = FFmpegRenderProvider(timeout_seconds=0.2)
    pid_path = tmp_path / "child.pid"

    with pytest.raises(RuntimeError, match="timed out"):
        provider._run(
            [
                sys.executable,
                "-c",
                (
                    "import os,pathlib,time; "
                    "pathlib.Path(r'child.pid').write_text(str(os.getpid())); time.sleep(60)"
                ),
            ],
            cwd=tmp_path,
        )

    pid = int(pid_path.read_text(encoding="utf-8"))
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        result = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
            capture_output=True,
            text=True,
            shell=False,
        )
        if str(pid) not in result.stdout:
            break
        time.sleep(0.05)
    assert str(pid) not in result.stdout


def test_renderer_rejects_missing_locked_font_without_publication(
    render_fixture: RenderFixture,
    tmp_path: Path,
) -> None:
    provider = FFmpegRenderProvider(font_path=tmp_path / "missing-font.ttc")

    with pytest.raises(RuntimeError, match="font"):
        provider.render(
            render_fixture.timeline,
            render_fixture.srt,
            render_fixture.output,
            render_fixture.manifest,
        )

    assert not render_fixture.output.exists()
    assert not render_fixture.manifest.exists()


def test_renderer_rejects_dialogue_wav_changed_after_snapshot(
    render_fixture: RenderFixture,
) -> None:
    audio_path = render_fixture.timeline.shots[0].dialogues[0].audio_path
    audio_path.write_bytes(audio_path.read_bytes() + b"changed")

    with pytest.raises(ValueError, match="audio.*changed"):
        FFmpegRenderProvider().render(
            render_fixture.timeline,
            render_fixture.srt,
            render_fixture.output,
            render_fixture.manifest,
        )

    assert not render_fixture.output.exists()
    assert not render_fixture.manifest.exists()


def test_renderer_rejects_video_changed_after_snapshot(
    render_fixture: RenderFixture,
) -> None:
    source = render_fixture.timeline.shots[0].video_path
    source.write_bytes(source.read_bytes() + b"changed")

    with pytest.raises(ValueError, match="source.*changed"):
        FFmpegRenderProvider().render(
            render_fixture.timeline,
            render_fixture.srt,
            render_fixture.output,
            render_fixture.manifest,
        )


def test_zero_cue_render_skips_burn_in_and_keeps_silent_aac(
    render_fixture: RenderFixture,
    tmp_path: Path,
) -> None:
    shots = tuple(replace(shot, dialogues=()) for shot in render_fixture.timeline.shots)
    timeline = replace(
        render_fixture.timeline,
        shots=shots,
        scenes=(TimelineSceneSnapshot("scene-1", 0, shots),),
    )
    output = tmp_path / "zero" / "final.mp4"
    manifest = tmp_path / "zero" / "manifest.json"

    result = FFmpegRenderProvider().render(timeline, b"", output, manifest)

    info = probe_av(result.output_path)
    assert info.audio.codec == "aac"
    assert info.audio.channels == 2
    assert info.video.frames == timeline.total_frames
    assert json.loads(manifest.read_text(encoding="utf-8"))["cue_count"] == 0


def test_nonempty_srt_changes_pixels_only_when_burned(
    render_fixture: RenderFixture,
    tmp_path: Path,
) -> None:
    plain_output = tmp_path / "plain" / "final.mp4"
    plain_manifest = tmp_path / "plain" / "manifest.json"
    plain = FFmpegRenderProvider().render(
        render_fixture.timeline,
        b"",
        plain_output,
        plain_manifest,
    )
    burned = FFmpegRenderProvider().render(
        render_fixture.timeline,
        render_fixture.srt,
        render_fixture.output,
        render_fixture.manifest,
    )

    assert _frame_hash(plain.output_path, 0.2) != _frame_hash(burned.output_path, 0.2)


@pytest.mark.parametrize("existing", ["output", "manifest"])
def test_renderer_preserves_preexisting_immutable_artifact(
    render_fixture: RenderFixture,
    existing: str,
) -> None:
    path = render_fixture.output if existing == "output" else render_fixture.manifest
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"historical")

    with pytest.raises(FileExistsError):
        FFmpegRenderProvider().render(
            render_fixture.timeline,
            render_fixture.srt,
            render_fixture.output,
            render_fixture.manifest,
        )

    assert path.read_bytes() == b"historical"


def test_renderer_cleans_private_job_after_success(
    render_fixture: RenderFixture,
) -> None:
    observed_jobs: list[Path] = []

    class RecordingProvider(FFmpegRenderProvider):
        def _run(self, arguments, *, cwd, capture_stdout=False):
            if ".phase9-render-" in Path(cwd).name:
                observed_jobs.append(Path(cwd))
            return super()._run(arguments, cwd=cwd, capture_stdout=capture_stdout)

    RecordingProvider().render(
        render_fixture.timeline,
        render_fixture.srt,
        render_fixture.output,
        render_fixture.manifest,
    )

    assert observed_jobs
    assert all(not job.exists() for job in observed_jobs)


def test_renderer_does_not_publish_when_durable_manifest_fsync_fails(
    render_fixture: RenderFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = FFmpegRenderProvider._fsync_file

    def fail_manifest(path: Path) -> None:
        if path.name.endswith(".tmp") and ".json." in path.name:
            raise OSError("manifest disk failure")
        original(path)

    monkeypatch.setattr(FFmpegRenderProvider, "_fsync_file", staticmethod(fail_manifest))

    with pytest.raises(OSError, match="manifest disk failure"):
        FFmpegRenderProvider().render(
            render_fixture.timeline,
            render_fixture.srt,
            render_fixture.output,
            render_fixture.manifest,
        )

    assert not render_fixture.output.exists()
    assert not render_fixture.manifest.exists()
    assert not list(render_fixture.output.parent.glob("*.tmp"))


def test_renderer_probes_every_canonical_clip_and_final_output(
    render_fixture: RenderFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.providers.ffmpeg_render_provider as render_module

    original = render_module.probe_av
    probed: list[Path] = []

    def recording_probe(path: Path, executable: str):
        probed.append(Path(path))
        return original(path, executable=executable)

    monkeypatch.setattr(render_module, "probe_av", recording_probe)

    FFmpegRenderProvider().render(
        render_fixture.timeline,
        render_fixture.srt,
        render_fixture.output,
        render_fixture.manifest,
    )

    assert len([path for path in probed if path.name.startswith("clip-")]) == 2
    assert len([path for path in probed if path.name == "candidate.mp4"]) == 1


def test_default_private_job_path_and_filter_operands_are_ascii_only(
    render_fixture: RenderFixture,
) -> None:
    observed: list[tuple[list[str], Path]] = []

    class RecordingProvider(FFmpegRenderProvider):
        def _run(self, arguments, *, cwd, capture_stdout=False):
            observed.append((list(arguments), Path(cwd)))
            return super()._run(arguments, cwd=cwd, capture_stdout=capture_stdout)

    RecordingProvider().render(
        render_fixture.timeline,
        render_fixture.srt,
        render_fixture.output,
        render_fixture.manifest,
    )

    job_paths = {cwd for _arguments, cwd in observed if ".phase9-render-" in cwd.name}
    assert job_paths
    assert all(str(path).isascii() and " " not in str(path) for path in job_paths)
    filter_values = [
        arguments[arguments.index("-filter_complex") + 1]
        for arguments, _cwd in observed
        if "-filter_complex" in arguments and "subtitles=" in arguments[arguments.index("-filter_complex") + 1]
    ]
    assert len(filter_values) == 1
    assert "subtitles=subtitles.srt:fontsdir=fonts" in filter_values[0]
    assert str(render_fixture.output.parent) not in filter_values[0]


def test_renderer_rejects_durable_manifest_readback_mismatch(
    render_fixture: RenderFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = FFmpegRenderProvider._fsync_file

    def corrupt_after_fsync(path: Path) -> None:
        original(path)
        if path.name.endswith(".tmp") and ".json." in path.name:
            path.write_text("{}", encoding="utf-8")

    monkeypatch.setattr(
        FFmpegRenderProvider,
        "_fsync_file",
        staticmethod(corrupt_after_fsync),
    )

    with pytest.raises(RuntimeError, match="manifest.*mismatch"):
        FFmpegRenderProvider().render(
            render_fixture.timeline,
            render_fixture.srt,
            render_fixture.output,
            render_fixture.manifest,
        )

    assert not render_fixture.output.exists()
    assert not render_fixture.manifest.exists()


def test_renderer_never_overwrites_output_created_during_render(
    render_fixture: RenderFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = FFmpegRenderProvider._fsync_file
    injected = False

    def create_racing_output(path: Path) -> None:
        nonlocal injected
        original(path)
        if not injected and path.suffix == ".tmp" and ".mp4." in path.name:
            render_fixture.output.write_bytes(b"concurrent historical output")
            injected = True

    monkeypatch.setattr(
        FFmpegRenderProvider,
        "_fsync_file",
        staticmethod(create_racing_output),
    )

    with pytest.raises(FileExistsError):
        FFmpegRenderProvider().render(
            render_fixture.timeline,
            render_fixture.srt,
            render_fixture.output,
            render_fixture.manifest,
        )

    assert render_fixture.output.read_bytes() == b"concurrent historical output"
    assert not render_fixture.manifest.exists()


def test_runtime_identity_rejects_ffmpeg_without_libass(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = FFmpegRenderProvider()
    responses = iter(
        [
            "ffmpeg version test\nconfiguration: --enable-libass\n",
            "Filters:\n .. scale V->V scale video\n",
        ]
    )
    monkeypatch.setattr(provider, "_run", lambda *args, **kwargs: next(responses))

    with pytest.raises(RuntimeError, match="libass subtitles"):
        provider._identity(tmp_path)


def test_timeout_propagates_exact_process_cleanup_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.providers.ffmpeg_render_provider as render_module

    class UncleanableProcess:
        returncode = None

        def communicate(self, timeout):
            raise subprocess.TimeoutExpired("fake", timeout)

        def terminate(self):
            raise OSError("cannot terminate exact child")

    monkeypatch.setattr(render_module.subprocess, "Popen", lambda *args, **kwargs: UncleanableProcess())

    with pytest.raises(RuntimeError, match="cleanup failed") as raised:
        FFmpegRenderProvider(timeout_seconds=0.01)._run(
            [sys.executable, "-c", "pass"],
            cwd=tmp_path,
        )

    assert isinstance(raised.value.__cause__, OSError)


def test_renderer_rejects_malformed_candidate_without_publication(
    render_fixture: RenderFixture,
) -> None:
    class MalformedProvider(FFmpegRenderProvider):
        def _render_candidate(self, timeline, clips, srt, job_dir):
            candidate = job_dir / "candidate.mp4"
            candidate.write_bytes(b"not media")
            return candidate

    with pytest.raises(RuntimeError, match="malformed"):
        MalformedProvider().render(
            render_fixture.timeline,
            render_fixture.srt,
            render_fixture.output,
            render_fixture.manifest,
        )

    assert not render_fixture.output.exists()
    assert not render_fixture.manifest.exists()


def test_renderer_rejects_candidate_with_wrong_frame_count(
    render_fixture: RenderFixture,
) -> None:
    class WrongFramesProvider(FFmpegRenderProvider):
        def _render_candidate(self, timeline, clips, srt, job_dir):
            candidate = job_dir / "candidate.mp4"
            candidate.write_bytes(clips[0].read_bytes())
            return candidate

    with pytest.raises(RuntimeError, match="locked A/V profile"):
        WrongFramesProvider().render(
            render_fixture.timeline,
            render_fixture.srt,
            render_fixture.output,
            render_fixture.manifest,
        )


def test_renderer_propagates_private_job_cleanup_failure(
    render_fixture: RenderFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.providers.ffmpeg_render_provider as render_module

    original = render_module.shutil.rmtree

    def fail_job_cleanup(path: Path) -> None:
        if ".phase9-render-" in Path(path).name:
            original(path)
            raise OSError("cleanup sentinel")
        original(path)

    monkeypatch.setattr(render_module.shutil, "rmtree", fail_job_cleanup)

    with pytest.raises(RuntimeError, match="cleanup failed"):
        FFmpegRenderProvider().render(
            render_fixture.timeline,
            render_fixture.srt,
            render_fixture.output,
            render_fixture.manifest,
        )

    assert not render_fixture.output.exists()
    assert not render_fixture.manifest.exists()
    assert not list(render_fixture.output.parent.glob("*.tmp"))


def test_renderer_cleans_durable_temps_even_when_job_cleanup_also_fails(
    render_fixture: RenderFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.providers.ffmpeg_render_provider as render_module

    original_rmtree = render_module.shutil.rmtree
    original_fsync = FFmpegRenderProvider._fsync_file

    def fail_manifest(path: Path) -> None:
        original_fsync(path)
        if ".json." in path.name:
            raise OSError("manifest failure")

    def remove_then_fail(path: Path) -> None:
        original_rmtree(path)
        raise OSError("cleanup failure")

    monkeypatch.setattr(FFmpegRenderProvider, "_fsync_file", staticmethod(fail_manifest))
    monkeypatch.setattr(render_module.shutil, "rmtree", remove_then_fail)

    with pytest.raises(RuntimeError, match="manifest failure.*cleanup failed") as raised:
        FFmpegRenderProvider().render(
            render_fixture.timeline,
            render_fixture.srt,
            render_fixture.output,
            render_fixture.manifest,
        )

    assert not list(render_fixture.output.parent.glob("*.tmp"))
    assert isinstance(raised.value.__cause__, OSError)
    assert "manifest failure" in str(raised.value.__cause__)


def test_renderer_preserves_manifest_created_during_atomic_publication(
    render_fixture: RenderFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_link = os.link
    calls = 0

    def race_manifest(source: Path, destination: Path) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            render_fixture.manifest.write_bytes(b"concurrent historical manifest")
        original_link(source, destination)

    monkeypatch.setattr(os, "link", race_manifest)

    with pytest.raises(FileExistsError):
        FFmpegRenderProvider().render(
            render_fixture.timeline,
            render_fixture.srt,
            render_fixture.output,
            render_fixture.manifest,
        )

    assert not render_fixture.output.exists()
    assert render_fixture.manifest.read_bytes() == b"concurrent historical manifest"
    assert not list(render_fixture.output.parent.glob("*.tmp"))
