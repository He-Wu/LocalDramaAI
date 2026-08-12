import json
import os
import subprocess
import tempfile
from pathlib import Path


class FFmpegProvider:
    def __init__(self, executable: str = "ffmpeg", probe_executable: str = "ffprobe"):
        self.executable = executable
        self.probe_executable = probe_executable

    def _run(self, args: list[str], timeout: int = 600) -> subprocess.CompletedProcess[str]:
        try:
            result = subprocess.run(
                args,
                capture_output=True,
                text=True,
                timeout=timeout,
                encoding="utf-8",
                errors="replace",
            )
        except subprocess.TimeoutExpired as error:
            stderr = error.stderr or ""
            if isinstance(stderr, bytes):
                stderr = stderr.decode("utf-8", errors="replace")
            raise RuntimeError(
                f"FFmpeg timed out after {timeout} seconds: {stderr[-1200:]}"
            ) from error
        if result.returncode != 0:
            stderr = result.stderr or ""
            raise RuntimeError(f"FFmpeg failed: {stderr[-1200:]}")
        return result

    def image_to_mp4(
        self,
        input_path: Path,
        output_path: Path,
        duration: float,
        fps: int = 16,
    ) -> Path:
        return self._convert(
            input_path,
            output_path,
            duration=duration,
            fps=fps,
            image_input=True,
        )

    def to_mp4(self, input_path: Path, output_path: Path, fps: int = 16) -> Path:
        return self._convert(input_path, output_path, fps=fps, image_input=False)

    def _convert(
        self,
        input_path: Path,
        output_path: Path,
        fps: int,
        image_input: bool,
        duration: float | None = None,
    ) -> Path:
        output_path = Path(output_path)
        temp = self._temporary_path(output_path)
        try:
            args = [self.executable, "-nostdin", "-y"]
            if image_input:
                args += ["-loop", "1"]
            args += ["-i", str(input_path)]
            if duration is not None:
                args += ["-t", str(duration)]
            args += [
                "-r",
                str(fps),
                "-c:v",
                "libx264",
                "-pix_fmt",
                "yuv420p",
                "-movflags",
                "+faststart",
                str(temp),
            ]
            self._run(args, timeout=300)
            self._replace_nonempty(temp, output_path)
            return output_path
        finally:
            temp.unlink(missing_ok=True)

    def mux_audio(self, video_path: Path, audio_path: Path, output_path: Path) -> Path:
        video_path = self._validated_input(video_path, "video input")
        audio_path = self._validated_input(audio_path, "audio input")
        output_path = Path(output_path)
        temp = self._temporary_path(output_path)
        try:
            self._run(
                [
                    self.executable,
                    "-nostdin",
                    "-y",
                    "-i",
                    str(video_path),
                    "-i",
                    str(audio_path),
                    "-map",
                    "0:v:0",
                    "-map",
                    "1:a:0",
                    "-c:v",
                    "copy",
                    "-c:a",
                    "aac",
                    "-af",
                    "apad",
                    "-ar",
                    "48000",
                    "-ac",
                    "2",
                    "-b:a",
                    "192k",
                    "-shortest",
                    "-movflags",
                    "+faststart",
                    str(temp),
                ]
            )
            self._replace_nonempty(temp, output_path)
            return output_path
        finally:
            temp.unlink(missing_ok=True)

    def concat(self, inputs: list[Path], output_path: Path) -> Path:
        if not inputs:
            raise ValueError("concat requires at least one input")
        normalized_inputs = []
        for input_path in inputs:
            input_path = Path(input_path)
            if "\n" in str(input_path) or "\r" in str(input_path):
                raise ValueError("concat input path cannot contain a newline")
            normalized_inputs.append(self._validated_input(input_path, "concat input"))

        expected_signature = self._media_signature(normalized_inputs[0])
        for input_path in normalized_inputs[1:]:
            signature = self._media_signature(input_path)
            for field, expected in expected_signature.items():
                if signature[field] != expected:
                    raise ValueError(
                        "concat input profile mismatch for "
                        f"{field}: expected {expected!r}, got {signature[field]!r} "
                        f"in {input_path}"
                    )

        output_path = Path(output_path)
        temp = self._temporary_path(output_path)
        manifest = None
        try:
            manifest = self._temporary_path(output_path, suffix=".concat.txt")
            entries = [f"file {self._quote_manifest_path(path)}" for path in normalized_inputs]
            manifest.write_text("\n".join(entries) + "\n", encoding="utf-8")
            self._run(
                [
                    self.executable,
                    "-nostdin",
                    "-y",
                    "-f",
                    "concat",
                    "-safe",
                    "0",
                    "-i",
                    str(manifest),
                    "-c",
                    "copy",
                    "-movflags",
                    "+faststart",
                    str(temp),
                ]
            )
            self._replace_nonempty(temp, output_path)
            return output_path
        finally:
            if manifest is not None:
                manifest.unlink(missing_ok=True)
            temp.unlink(missing_ok=True)

    @staticmethod
    def _validated_input(path: Path, label: str) -> Path:
        path = Path(path)
        if not path.is_file() or path.stat().st_size <= 0:
            raise ValueError(f"{label} must exist and be nonempty: {path}")
        return path

    def _media_signature(self, path: Path) -> dict[str, object]:
        result = self._run(
            [
                self.probe_executable,
                "-v",
                "error",
                "-show_streams",
                "-of",
                "json",
                str(path),
            ]
        )
        probe = json.loads(result.stdout)
        video = next(
            (stream for stream in probe.get("streams", []) if stream.get("codec_type") == "video"),
            None,
        )
        audio = next(
            (stream for stream in probe.get("streams", []) if stream.get("codec_type") == "audio"),
            None,
        )
        if video is None:
            raise ValueError(f"concat input requires a video stream: {path}")
        if audio is None:
            raise ValueError(f"concat input requires an audio stream: {path}")
        return {
            "video.codec": video.get("codec_name"),
            "video.width": video.get("width"),
            "video.height": video.get("height"),
            "video.pixel_format": video.get("pix_fmt"),
            "video.frame_rate": video.get("r_frame_rate"),
            "video.time_base": video.get("time_base"),
            "audio.codec": audio.get("codec_name"),
            "audio.sample_rate": audio.get("sample_rate"),
            "audio.channels": audio.get("channels"),
            "audio.channel_layout": audio.get("channel_layout"),
            "audio.time_base": audio.get("time_base"),
        }

    @staticmethod
    def _quote_manifest_path(path: Path) -> str:
        escaped = []
        for character in path.resolve().as_posix():
            if character == "'":
                escaped.append("'\\''")
            elif character == "\\":
                escaped.append("'\\\\'")
            else:
                escaped.append(character)
        return "'" + "".join(escaped) + "'"

    @staticmethod
    def _temporary_path(output_path: Path, suffix: str | None = None) -> Path:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        file_descriptor, name = tempfile.mkstemp(
            prefix=f".{output_path.stem}.",
            suffix=suffix or f".tmp{output_path.suffix}",
            dir=output_path.parent,
        )
        os.close(file_descriptor)
        return Path(name)

    @staticmethod
    def _replace_nonempty(temp: Path, output_path: Path) -> None:
        if not temp.is_file() or temp.stat().st_size <= 0:
            raise RuntimeError("FFmpeg did not produce a nonempty output file")
        temp.replace(output_path)
