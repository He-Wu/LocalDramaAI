"""Isolated FFmpeg boundary for canonical Phase 9 project renders."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import time
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path

from app.services.media_probe import AVInfo, probe_av
from app.services.audio_probe import probe_wav
from app.services.render_timeline import RenderTimeline, TimelineShot
from app.services.video_probe import probe_video


DEFAULT_FONT = Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts" / "msyh.ttc"
_MP4_FORMATS = {"mov", "mp4", "m4a", "3gp", "3g2", "mj2"}
_STDERR_LIMIT = 8192


@dataclass(frozen=True)
class FFmpegIdentity:
    executable: Path
    version: str
    configuration: str
    font_path: Path
    font_size: int
    font_sha256: str


@dataclass(frozen=True)
class FFmpegRenderResult:
    output_path: Path
    output_sha256: str
    media: AVInfo
    generation_time: float
    manifest_path: Path
    identity: FFmpegIdentity


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def resolve_native_executable(executable: str | os.PathLike[str]) -> Path:
    value = os.fspath(executable)
    located = shutil.which(value) if not Path(value).parent.name else value
    if not located:
        raise RuntimeError(f"native executable not found: {value}")
    try:
        path = Path(located).resolve(strict=True)
    except OSError as exc:
        raise RuntimeError(f"native executable cannot be resolved: {value}") from exc
    if path.suffix.lower() in {".cmd", ".bat"}:
        raise RuntimeError(f"native executable wrapper is not allowed: {path}")
    if not path.is_file():
        raise RuntimeError(f"native executable is not a file: {path}")
    return path


class FFmpegRenderProvider:
    def __init__(
        self,
        executable: str | os.PathLike[str] = "ffmpeg",
        probe_executable: str | os.PathLike[str] = "ffprobe",
        font_path: str | os.PathLike[str] = DEFAULT_FONT,
        *,
        timeout_seconds: float = 120.0,
        job_root: str | os.PathLike[str] | None = None,
    ) -> None:
        self.executable = resolve_native_executable(executable)
        self.probe_executable = resolve_native_executable(probe_executable)
        self.font_path = Path(font_path).resolve()
        self.timeout_seconds = float(timeout_seconds)
        if job_root is None:
            temp_anchor = Path(tempfile.gettempdir()).anchor
            job_root = Path(temp_anchor) / "LocalDramaAI-phase9-jobs"
        self.job_root = Path(job_root).resolve()
        if not str(self.job_root).isascii() or " " in str(self.job_root):
            raise ValueError("FFmpeg job root must use an ASCII path without spaces")
        if self.timeout_seconds <= 0:
            raise ValueError("FFmpeg timeout must be positive")

    def _run(
        self,
        arguments: list[str],
        *,
        cwd: Path,
        capture_stdout: bool = False,
    ) -> str:
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
        with tempfile.TemporaryFile() as stderr_file:
            try:
                process = subprocess.Popen(
                    arguments,
                    cwd=str(cwd),
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE if capture_stdout else subprocess.DEVNULL,
                    stderr=stderr_file,
                    shell=False,
                    creationflags=creationflags,
                )
            except OSError as exc:
                raise RuntimeError(f"cannot start native process: {arguments[0]}") from exc
            try:
                stdout, _ = process.communicate(timeout=self.timeout_seconds)
            except subprocess.TimeoutExpired as exc:
                cleanup_error: BaseException | None = None
                try:
                    process.terminate()
                    try:
                        process.communicate(timeout=5)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.communicate(timeout=5)
                except BaseException as cleanup_exc:
                    cleanup_error = cleanup_exc
                if cleanup_error is not None:
                    raise RuntimeError("native process timed out and cleanup failed") from cleanup_error
                raise RuntimeError("native process timed out") from exc
            stderr_file.seek(0, os.SEEK_END)
            stderr_size = stderr_file.tell()
            stderr_file.seek(max(0, stderr_size - _STDERR_LIMIT))
            stderr = stderr_file.read().decode("utf-8", errors="replace").strip()
            if process.returncode:
                detail = f": {stderr}" if stderr else ""
                raise RuntimeError(f"native process exited with code {process.returncode}{detail}")
            return (stdout or b"").decode("utf-8", errors="replace")

    def _identity(self, cwd: Path) -> FFmpegIdentity:
        try:
            if not self.font_path.is_file() or self.font_path.stat().st_size <= 0:
                raise OSError
            with self.font_path.open("rb") as source:
                source.read(1)
        except OSError as exc:
            raise RuntimeError(f"locked Microsoft YaHei font is unavailable: {self.font_path}") from exc
        version_text = self._run(
            [str(self.executable), "-hide_banner", "-version"], cwd=cwd, capture_stdout=True
        )
        filters_text = self._run(
            [str(self.executable), "-hide_banner", "-filters"], cwd=cwd, capture_stdout=True
        )
        if not any(
            len(parts := line.split()) >= 2 and parts[1] == "subtitles"
            for line in filters_text.splitlines()
        ):
            raise RuntimeError("FFmpeg runtime is missing the libass subtitles filter")
        lines = version_text.splitlines()
        if not lines or not lines[0].startswith("ffmpeg version "):
            raise RuntimeError("FFmpeg returned invalid version identity")
        configuration = next(
            (line.removeprefix("configuration: ") for line in lines if line.startswith("configuration: ")),
            "",
        )
        if "--enable-libass" not in configuration:
            raise RuntimeError("FFmpeg runtime is not built with libass")
        return FFmpegIdentity(
            executable=self.executable,
            version=lines[0].removeprefix("ffmpeg version ").strip(),
            configuration=configuration,
            font_path=self.font_path,
            font_size=self.font_path.stat().st_size,
            font_sha256=_sha256(self.font_path),
        )

    @staticmethod
    def _duration(shot: TimelineShot, timeline: RenderTimeline) -> str:
        return f"{shot.frame_count / timeline.profile.fps:.9f}"

    def _normalize_shot(
        self,
        timeline: RenderTimeline,
        shot: TimelineShot,
        index: int,
        job_dir: Path,
    ) -> Path:
        try:
            if (
                not shot.video_path.is_file()
                or shot.video_path.stat().st_size != shot.video_size
                or _sha256(shot.video_path) != shot.video_sha256
            ):
                raise ValueError
            source = probe_video(
                shot.video_path,
                executable=str(self.probe_executable),
            )
        except (OSError, ValueError) as exc:
            raise ValueError(f"Shot source is missing, corrupt, or changed: {shot.shot_id}") from exc
        coverage = source.duration
        if source.frames is not None:
            coverage = min(coverage, source.frames / source.fps)
        target = shot.frame_count / timeline.profile.fps
        if coverage + (1 / source.fps) < target:
            raise ValueError(f"Shot source is too short to cover timeline: {shot.shot_id}")
        clip = job_dir / f"clip-{index:06d}.mp4"
        arguments = [
            str(self.executable), "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
            "-i", str(shot.video_path),
        ]
        for dialogue in shot.dialogues:
            try:
                if (
                    not dialogue.audio_path.is_file()
                    or dialogue.audio_path.stat().st_size != dialogue.audio_size
                    or _sha256(dialogue.audio_path) != dialogue.audio_sha256
                ):
                    raise ValueError
                probe_wav(dialogue.audio_path)
            except (OSError, ValueError) as exc:
                raise ValueError(
                    f"dialogue audio is missing, corrupt, or changed: {dialogue.dialogue_id}"
                ) from exc
            arguments.extend(["-i", str(dialogue.audio_path)])

        duration = self._duration(shot, timeline)
        video_filter = (
            f"[0:v:0]scale={timeline.profile.width}:{timeline.profile.height}:"
            "force_original_aspect_ratio=decrease,"
            f"pad={timeline.profile.width}:{timeline.profile.height}:(ow-iw)/2:(oh-ih)/2:color=black,"
            f"setsar=1,fps={timeline.profile.fps},tpad=stop_mode=clone:stop_duration={duration},"
            f"trim=end_frame={shot.frame_count},setpts=PTS-STARTPTS[v]"
        )
        audio_chains: list[str] = []
        audio_labels: list[str] = []
        shot_start_ms = round(shot.start_frame * 1000 / timeline.profile.fps)
        for audio_index, dialogue in enumerate(shot.dialogues, start=1):
            local_start = dialogue.start_ms - shot_start_ms
            label = f"ad{audio_index}"
            audio_chains.append(
                f"[{audio_index}:a:0]aresample={timeline.profile.sample_rate},"
                "aformat=sample_fmts=fltp:channel_layouts=stereo,"
                f"adelay={local_start}|{local_start},apad,atrim=duration={duration},"
                f"asetpts=PTS-STARTPTS[{label}]"
            )
            audio_labels.append(f"[{label}]")
        if audio_labels:
            audio_filter = (
                ";".join(audio_chains)
                + ";"
                + "".join(audio_labels)
                + f"amix=inputs={len(audio_labels)}:duration=longest:normalize=0,"
                + f"atrim=duration={duration},asetpts=PTS-STARTPTS[a]"
            )
        else:
            audio_filter = (
                f"anullsrc=r={timeline.profile.sample_rate}:cl=stereo,"
                f"atrim=duration={duration},asetpts=PTS-STARTPTS[a]"
            )
        arguments.extend(
            [
                "-filter_complex", video_filter + ";" + audio_filter,
                "-map", "[v]", "-map", "[a]",
                "-c:v", "libx264", "-preset", "medium", "-crf", "18",
                "-pix_fmt", "yuv420p", "-r", str(timeline.profile.fps),
                "-fps_mode", "cfr", "-g", "50", "-keyint_min", "25",
                "-sc_threshold", "0", "-threads", "1",
                "-c:a", "aac", "-profile:a", "aac_low",
                "-ar", str(timeline.profile.sample_rate), "-ac", str(timeline.profile.channels),
                "-b:a", "192k", "-movflags", "+faststart", str(clip),
            ]
        )
        self._run(arguments, cwd=job_dir)
        if not clip.is_file() or clip.stat().st_size <= 0:
            raise RuntimeError(f"FFmpeg did not create canonical clip {index}")
        try:
            media = probe_av(clip, executable=str(self.probe_executable))
        except ValueError as exc:
            raise RuntimeError(f"canonical clip {index} is malformed: {exc}") from exc
        expected = timeline.profile
        if (
            media.video.codec != "h264"
            or media.video.pixel_format != "yuv420p"
            or (media.video.width, media.video.height) != (expected.width, expected.height)
            or abs(media.video.fps - expected.fps) > 0.01
            or media.video.frames != shot.frame_count
            or media.audio.codec != "aac"
            or media.audio.sample_rate != expected.sample_rate
            or media.audio.channels != expected.channels
        ):
            raise RuntimeError(f"canonical clip {index} does not match the locked A/V profile")
        return clip

    def _render_candidate(
        self,
        timeline: RenderTimeline,
        clips: list[Path],
        srt: bytes,
        job_dir: Path,
    ) -> Path:
        candidate = job_dir / "candidate.mp4"
        arguments = [
            str(self.executable), "-hide_banner", "-loglevel", "error", "-nostdin", "-y"
        ]
        for clip in clips:
            arguments.extend(["-i", clip.name])
        concat_inputs = "".join(f"[{index}:v:0][{index}:a:0]" for index in range(len(clips)))
        filters = [f"{concat_inputs}concat=n={len(clips)}:v=1:a=1[cv][ca]"]
        if srt:
            filters.append(
                "[cv]subtitles=subtitles.srt:fontsdir=fonts:"
                "force_style='FontName=Microsoft YaHei,FontSize=22,PrimaryColour=&H00FFFFFF,"
                "OutlineColour=&H00000000,Outline=2,Shadow=0,Alignment=2,MarginV=24'[v]"
            )
        else:
            filters.append("[cv]null[v]")
        duration = timeline.total_frames / timeline.profile.fps
        filters.append(f"[ca]apad,atrim=duration={duration:.9f},asetpts=PTS-STARTPTS[a]")
        arguments.extend(
            [
                "-filter_complex", ";".join(filters), "-map", "[v]", "-map", "[a]",
                "-c:v", "libx264", "-preset", "medium", "-crf", "18",
                "-pix_fmt", "yuv420p", "-r", str(timeline.profile.fps),
                "-fps_mode", "cfr", "-g", "50", "-keyint_min", "25",
                "-sc_threshold", "0", "-threads", "1",
                "-c:a", "aac", "-profile:a", "aac_low",
                "-ar", str(timeline.profile.sample_rate), "-ac", str(timeline.profile.channels),
                "-b:a", "192k", "-movflags", "+faststart", str(candidate),
            ]
        )
        self._run(arguments, cwd=job_dir)
        return candidate

    def _validate(self, path: Path, timeline: RenderTimeline) -> AVInfo:
        try:
            media = probe_av(path, executable=str(self.probe_executable))
        except ValueError as exc:
            raise RuntimeError(f"rendered output is malformed: {exc}") from exc
        formats = {item.strip().lower() for item in media.format_name.split(",")}
        expected = timeline.profile
        frame_count = media.video.frames
        if (
            not formats.intersection(_MP4_FORMATS)
            or media.video.codec != "h264"
            or media.video.pixel_format != "yuv420p"
            or (media.video.width, media.video.height) != (expected.width, expected.height)
            or abs(media.video.fps - expected.fps) > 0.01
            or frame_count != timeline.total_frames
            or media.audio.codec != "aac"
            or media.audio.sample_rate != expected.sample_rate
            or media.audio.channels != expected.channels
            or abs(media.video.duration - media.audio.duration) > 0.080000001
        ):
            raise RuntimeError("rendered output does not match the locked A/V profile")
        self._run(
            [
                str(self.executable), "-hide_banner", "-loglevel", "error", "-xerror", "-nostdin",
                "-i", str(path), "-map", "0:v:0", "-map", "0:a:0", "-f", "null", os.devnull,
            ],
            cwd=path.parent,
        )
        return media

    @staticmethod
    def _fsync_file(path: Path) -> None:
        with path.open("r+b") as handle:
            os.fsync(handle.fileno())

    def render(
        self,
        timeline: RenderTimeline,
        srt: bytes,
        output_path: str | os.PathLike[str],
        manifest_path: str | os.PathLike[str],
    ) -> FFmpegRenderResult:
        started = time.monotonic()
        output = Path(output_path).resolve()
        manifest = Path(manifest_path).resolve()
        if output == manifest:
            raise ValueError("output and manifest paths must differ")
        if output.exists() or manifest.exists():
            raise FileExistsError("immutable render output or manifest already exists")
        if not timeline.shots or timeline.total_frames <= 0:
            raise ValueError("render timeline must contain positive frames")
        if not isinstance(srt, bytes):
            raise TypeError("SRT payload must be bytes")

        root = self.job_root
        root.mkdir(parents=True, exist_ok=True)
        job_dir = Path(
            tempfile.mkdtemp(prefix=f".phase9-render-{uuid.uuid4().hex}-", dir=root)
        ).resolve()
        staged_output: Path | None = None
        staged_manifest: Path | None = None
        try:
            identity = self._identity(job_dir)
            fonts_dir = job_dir / "fonts"
            fonts_dir.mkdir()
            staged_font = fonts_dir / "locked-font.ttc"
            shutil.copyfile(identity.font_path, staged_font)
            if (
                staged_font.stat().st_size != identity.font_size
                or _sha256(staged_font) != identity.font_sha256
            ):
                raise RuntimeError("staged font does not match attested identity")
            (job_dir / "subtitles.srt").write_bytes(srt)
            clips = [
                self._normalize_shot(timeline, shot, index, job_dir)
                for index, shot in enumerate(timeline.shots)
            ]
            candidate = self._render_candidate(timeline, clips, srt, job_dir)
            if not candidate.is_file() or candidate.stat().st_size <= 0:
                raise RuntimeError("FFmpeg did not create the rendered output")
            media = self._validate(candidate, timeline)
            output_sha256 = _sha256(candidate)
            generation_time = time.monotonic() - started
            payload = {
                "provider": "ffmpeg",
                "workflow": "final_render_v1",
                "generation_time": generation_time,
                "project_id": timeline.project_id,
                "workflow_hash": timeline.workflow_hash,
                "output_path": str(output),
                "output_sha256": output_sha256,
                "srt_sha256": hashlib.sha256(srt).hexdigest(),
                "cue_count": sum(len(shot.dialogues) for shot in timeline.shots),
                "profile": asdict(timeline.profile),
                "total_frames": timeline.total_frames,
                "ffmpeg": {
                    "executable": str(identity.executable),
                    "version": identity.version,
                    "configuration": identity.configuration,
                },
                "font": {
                    "path": str(identity.font_path),
                    "size": identity.font_size,
                    "sha256": identity.font_sha256,
                },
                "timeline": [
                    {
                        "shot_id": shot.shot_id,
                        "start_frame": shot.start_frame,
                        "frame_count": shot.frame_count,
                        "video": {
                            "asset_id": shot.video_asset_id,
                            "path": str(shot.video_path),
                            "sha256": shot.video_sha256,
                        },
                        "audio": [
                            {
                                "asset_id": dialogue.audio_asset_id,
                                "path": str(dialogue.audio_path),
                                "sha256": dialogue.audio_sha256,
                                "start_ms": dialogue.start_ms,
                                "end_ms": dialogue.end_ms,
                            }
                            for dialogue in shot.dialogues
                        ],
                    }
                    for shot in timeline.shots
                ],
            }

            output.parent.mkdir(parents=True, exist_ok=True)
            manifest.parent.mkdir(parents=True, exist_ok=True)
            staged_output = output.with_name(f".{output.name}.{uuid.uuid4().hex}.tmp")
            staged_manifest = manifest.with_name(f".{manifest.name}.{uuid.uuid4().hex}.tmp")
            shutil.copyfile(candidate, staged_output)
            staged_manifest.write_text(
                json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
                encoding="utf-8",
            )
            self._fsync_file(staged_output)
            self._fsync_file(staged_manifest)
            try:
                durable_payload = json.loads(staged_manifest.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError) as exc:
                raise RuntimeError("durable provider manifest mismatch") from exc
            if durable_payload != payload:
                raise RuntimeError("durable provider manifest mismatch")
            if _sha256(staged_output) != output_sha256:
                raise RuntimeError("durable rendered output mismatch")
        except BaseException as primary_error:
            cleanup_error: OSError | None = None
            try:
                shutil.rmtree(job_dir)
            except OSError as exc:
                cleanup_error = exc
            for staged in (staged_output, staged_manifest):
                if staged is not None:
                    try:
                        staged.unlink(missing_ok=True)
                    except OSError as exc:
                        cleanup_error = cleanup_error or exc
            if cleanup_error is not None:
                failure = RuntimeError(
                    f"FFmpeg render failed ({primary_error}); job cleanup failed ({cleanup_error})"
                )
                failure.add_note(f"cleanup failure: {cleanup_error!r}")
                raise failure from primary_error
            raise
        try:
            shutil.rmtree(job_dir)
        except OSError as exc:
            for staged in (staged_output, staged_manifest):
                if staged is not None:
                    staged.unlink(missing_ok=True)
            raise RuntimeError("FFmpeg render job cleanup failed") from exc

        assert staged_output is not None and staged_manifest is not None
        published_output = False
        published_manifest = False
        try:
            os.link(staged_output, output)
            published_output = True
            staged_output.unlink()
            try:
                os.link(staged_manifest, manifest)
                published_manifest = True
                staged_manifest.unlink()
            except BaseException:
                output.unlink(missing_ok=True)
                published_output = False
                raise
        except BaseException:
            staged_output.unlink(missing_ok=True)
            staged_manifest.unlink(missing_ok=True)
            if published_output:
                output.unlink(missing_ok=True)
            if published_manifest:
                manifest.unlink(missing_ok=True)
            raise
        return FFmpegRenderResult(
            output_path=output,
            output_sha256=output_sha256,
            media=media,
            generation_time=generation_time,
            manifest_path=manifest,
            identity=identity,
        )
