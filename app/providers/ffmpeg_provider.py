import subprocess
from pathlib import Path

class FFmpegProvider:
    def __init__(self, executable: str = "ffmpeg"): self.executable = executable

    def image_to_mp4(self, input_path: Path, output_path: Path, duration: float, fps: int = 16):
        return self._convert(input_path, output_path, duration=duration, fps=fps, image_input=True)

    def to_mp4(self, input_path: Path, output_path: Path, fps: int = 16):
        return self._convert(input_path, output_path, fps=fps, image_input=False)

    def _convert(self, input_path: Path, output_path: Path, fps: int, image_input: bool, duration: float | None = None):
        output_path.parent.mkdir(parents=True, exist_ok=True); temp = output_path.with_name(output_path.stem + ".tmp" + output_path.suffix)
        args = [self.executable, "-y"]
        if image_input: args += ["-loop", "1"]
        args += ["-i", str(input_path)]
        if duration is not None: args += ["-t", str(duration)]
        args += ["-r", str(fps), "-c:v", "libx264", "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(temp)]
        result = subprocess.run(args, capture_output=True, text=True, timeout=300)
        if result.returncode != 0: raise RuntimeError(f"FFmpeg failed: {result.stderr[-1200:]}")
        temp.replace(output_path); return output_path
