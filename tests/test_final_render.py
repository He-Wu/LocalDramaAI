import json
import shutil
import sqlite3
import subprocess
import sys
import wave
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image
from sqlalchemy.exc import IntegrityError

from app.db.session import create_schema, session_scope
from app.models import Asset, Dialogue, GenerationManifest, Project, Scene, Shot
from app.providers.ffmpeg_provider import FFmpegProvider
from app.services.audio_probe import probe_wav


def _wav(path: Path, seconds: float = 1.0) -> Path:
    with wave.open(str(path), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(24000)
        output.writeframes(b"\x00\x00" * int(seconds * 24000))
    return path


def _video(
    provider: FFmpegProvider,
    path: Path,
    color: tuple[int, int, int],
    *,
    seconds: float = 1.0,
    size: tuple[int, int] = (640, 368),
) -> Path:
    frame = path.with_suffix(".png")
    Image.new("RGB", size, color).save(frame)
    return provider.image_to_mp4(frame, path, duration=seconds, fps=16)


def _probe(path: Path) -> dict:
    result = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_streams",
            "-show_format",
            "-of",
            "json",
            str(path),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(result.stdout)


def _sample_frame(path: Path, timestamp: float, output_path: Path) -> tuple[int, int, int]:
    subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-y",
            "-ss",
            str(timestamp),
            "-i",
            str(path),
            "-frames:v",
            "1",
            str(output_path),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    with Image.open(output_path) as frame:
        return frame.convert("RGB").getpixel((320, 184))


def test_mux_and_concat_real_shots_preserve_streams_profile_and_order(tmp_path):
    provider = FFmpegProvider()
    media_dir = tmp_path / "director's cut"
    media_dir.mkdir()
    first_video = _video(provider, media_dir / "first shot.mp4", (220, 20, 20))
    second_video = _video(provider, media_dir / "second shot.mp4", (20, 20, 220))
    first_audio = _wav(media_dir / "first audio.wav")
    second_audio = _wav(media_dir / "second audio.wav")

    first_muxed = provider.mux_audio(first_video, first_audio, media_dir / "first muxed.mp4")
    second_muxed = provider.mux_audio(second_video, second_audio, media_dir / "second muxed.mp4")
    final = provider.concat([first_muxed, second_muxed], media_dir / "final movie.mp4")

    probe = _probe(final)
    video_stream = next(stream for stream in probe["streams"] if stream["codec_type"] == "video")
    audio_stream = next(stream for stream in probe["streams"] if stream["codec_type"] == "audio")
    assert video_stream["codec_name"] == "h264"
    assert (video_stream["width"], video_stream["height"]) == (640, 368)
    assert audio_stream["codec_name"] == "aac"
    assert float(probe["format"]["duration"]) >= 1.9

    first_pixel = _sample_frame(final, 0.25, tmp_path / "first-sample.png")
    second_pixel = _sample_frame(final, 1.25, tmp_path / "second-sample.png")
    assert first_pixel[0] > first_pixel[2] + 100
    assert second_pixel[2] > second_pixel[0] + 100


def test_mux_pads_and_normalizes_short_audio_to_preserve_video_tail(tmp_path):
    provider = FFmpegProvider()
    video = _video(
        provider,
        tmp_path / "long visual.mp4",
        (20, 120, 20),
        seconds=1.3,
    )
    audio = _wav(tmp_path / "short dialogue.wav", seconds=1.0)

    output = provider.mux_audio(video, audio, tmp_path / "muxed.mp4")

    video_duration = float(_probe(video)["format"]["duration"])
    probe = _probe(output)
    video_stream = next(stream for stream in probe["streams"] if stream["codec_type"] == "video")
    audio_stream = next(stream for stream in probe["streams"] if stream["codec_type"] == "audio")
    assert float(probe["format"]["duration"]) == pytest.approx(video_duration, abs=0.08)
    assert float(video_stream["duration"]) == pytest.approx(video_duration, abs=0.08)
    assert audio_stream["codec_name"] == "aac"
    assert audio_stream["sample_rate"] == "48000"
    assert audio_stream["channels"] == 2
    assert audio_stream["channel_layout"] == "stereo"


def test_run_captures_output_uses_timeout_and_reports_stderr_tail(monkeypatch):
    captured = {}

    def fake_run(args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs
        return SimpleNamespace(returncode=9, stderr="x" * 1500 + "useful tail")

    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(RuntimeError, match="useful tail"):
        FFmpegProvider()._run(["ffmpeg", "-version"], timeout=17)
    assert captured == {
        "args": ["ffmpeg", "-version"],
        "kwargs": {
            "capture_output": True,
            "text": True,
            "timeout": 17,
            "encoding": "utf-8",
            "errors": "replace",
        },
    }


def test_run_handles_invalid_utf8_error_from_chinese_path(tmp_path):
    script_dir = tmp_path / "中文目录"
    script_dir.mkdir()
    script = script_dir / "错误.py"
    script.write_text(
        "import sys\nsys.stderr.buffer.write(b'\\xff\\xff useful error')\nsys.exit(3)\n",
        encoding="utf-8",
    )

    with pytest.raises(RuntimeError, match="useful error"):
        FFmpegProvider()._run([sys.executable, str(script)])


def test_mux_timeout_is_runtime_error_and_cleans_temporary_output(tmp_path, monkeypatch):
    provider = FFmpegProvider()
    video = tmp_path / "video.mp4"
    audio = tmp_path / "audio.wav"
    video.write_bytes(b"video")
    audio.write_bytes(b"audio")
    output = tmp_path / "existing.mp4"
    output.write_bytes(b"keep me")
    files_before = set(tmp_path.iterdir())
    captured_args = None

    def time_out(args, **kwargs):
        nonlocal captured_args
        captured_args = args
        raise subprocess.TimeoutExpired(args, kwargs["timeout"], stderr="stalled")

    monkeypatch.setattr(subprocess, "run", time_out)
    with pytest.raises(RuntimeError, match="timed out.*stalled"):
        provider.mux_audio(video, audio, output)

    assert captured_args is not None and "-nostdin" in captured_args
    assert captured_args[captured_args.index("-b:a") + 1] == "192k"
    assert output.read_bytes() == b"keep me"
    assert set(tmp_path.iterdir()) == files_before


@pytest.mark.parametrize("missing", ["video", "audio"])
def test_mux_rejects_missing_or_empty_input_before_launch(tmp_path, monkeypatch, missing):
    provider = FFmpegProvider()
    video = tmp_path / "video.mp4"
    audio = tmp_path / "audio.wav"
    video.write_bytes(b"video")
    audio.write_bytes(b"audio")
    target = video if missing == "video" else audio
    if missing == "video":
        target.unlink()
    else:
        target.write_bytes(b"")
    monkeypatch.setattr(
        provider,
        "_run",
        lambda *args, **kwargs: pytest.fail("FFmpeg launched"),
        raising=False,
    )

    with pytest.raises(ValueError, match=missing):
        provider.mux_audio(video, audio, tmp_path / "output.mp4")


def test_concat_rejects_empty_list_and_invalid_input_before_launch(tmp_path, monkeypatch):
    provider = FFmpegProvider()
    monkeypatch.setattr(
        provider,
        "_run",
        lambda *args, **kwargs: pytest.fail("FFmpeg launched"),
        raising=False,
    )
    with pytest.raises(ValueError, match="at least one"):
        provider.concat([], tmp_path / "output.mp4")

    empty = tmp_path / "empty.mp4"
    empty.write_bytes(b"")
    with pytest.raises(ValueError, match="concat input"):
        provider.concat([empty], tmp_path / "output.mp4")


def test_mux_failure_preserves_destination_and_cleans_temporary_output(tmp_path):
    provider = FFmpegProvider()
    video = _video(provider, tmp_path / "valid.mp4", (20, 120, 20))
    invalid_audio = tmp_path / "invalid.wav"
    invalid_audio.write_bytes(b"not audio")
    output = tmp_path / "existing.mp4"
    output.write_bytes(b"keep me")
    files_before = set(tmp_path.iterdir())

    with pytest.raises(RuntimeError, match="FFmpeg failed"):
        provider.mux_audio(video, invalid_audio, output)

    assert output.read_bytes() == b"keep me"
    assert set(tmp_path.iterdir()) == files_before


def test_concat_failure_preserves_destination_and_cleans_manifest_and_output(tmp_path):
    ffmpeg = FFmpegProvider()
    valid = ffmpeg.mux_audio(
        _video(ffmpeg, tmp_path / "valid-video.mp4", (20, 120, 20)),
        _wav(tmp_path / "valid-audio.wav"),
        tmp_path / "valid.mp4",
    )
    provider = FFmpegProvider(executable=sys.executable)
    output = tmp_path / "existing.mp4"
    output.write_bytes(b"keep me")
    files_before = set(tmp_path.iterdir())

    with pytest.raises(RuntimeError, match="FFmpeg failed"):
        provider.concat([valid], output)

    assert output.read_bytes() == b"keep me"
    assert set(tmp_path.iterdir()) == files_before


def test_concat_rejects_incompatible_video_profile_before_output_allocation(tmp_path):
    provider = FFmpegProvider()
    audio = _wav(tmp_path / "audio.wav")
    first = provider.mux_audio(
        _video(provider, tmp_path / "wide.mp4", (20, 120, 20)),
        audio,
        tmp_path / "wide-muxed.mp4",
    )
    second = provider.mux_audio(
        _video(
            provider,
            tmp_path / "small.mp4",
            (120, 20, 20),
            size=(320, 184),
        ),
        audio,
        tmp_path / "small-muxed.mp4",
    )
    output = tmp_path / "existing.mp4"
    output.write_bytes(b"keep me")
    files_before = set(tmp_path.iterdir())

    with pytest.raises(ValueError, match="profile.*width"):
        provider.concat([first, second], output)

    assert output.read_bytes() == b"keep me"
    assert set(tmp_path.iterdir()) == files_before


def test_concat_requires_video_and_audio_streams(tmp_path):
    provider = FFmpegProvider()
    video_only = _video(provider, tmp_path / "video-only.mp4", (20, 120, 20))
    output = tmp_path / "output.mp4"

    with pytest.raises(ValueError, match="audio stream"):
        provider.concat([video_only], output)

    assert not output.exists()


def test_concat_requires_video_stream(tmp_path):
    provider = FFmpegProvider()
    audio_only = _wav(tmp_path / "audio-only.wav")

    with pytest.raises(ValueError, match="video stream"):
        provider.concat([audio_only], tmp_path / "output.mp4")


@pytest.mark.parametrize(
    "field",
    [
        "video.codec",
        "video.width",
        "video.height",
        "video.pixel_format",
        "video.frame_rate",
        "video.time_base",
        "audio.codec",
        "audio.sample_rate",
        "audio.channels",
        "audio.channel_layout",
        "audio.time_base",
    ],
)
def test_concat_checks_every_signature_field_before_temp_allocation(
    tmp_path,
    monkeypatch,
    field,
):
    provider = FFmpegProvider()
    first = tmp_path / "first.mp4"
    second = tmp_path / "second.mp4"
    first.write_bytes(b"first")
    second.write_bytes(b"second")
    baseline = {
        "video.codec": "h264",
        "video.width": 640,
        "video.height": 368,
        "video.pixel_format": "yuv420p",
        "video.frame_rate": "16/1",
        "video.time_base": "1/16384",
        "audio.codec": "aac",
        "audio.sample_rate": "48000",
        "audio.channels": 2,
        "audio.channel_layout": "stereo",
        "audio.time_base": "1/48000",
    }
    mismatched = baseline | {field: "different"}
    signatures = iter([baseline, mismatched])
    monkeypatch.setattr(provider, "_media_signature", lambda path: next(signatures))
    monkeypatch.setattr(
        provider,
        "_temporary_path",
        lambda *args, **kwargs: pytest.fail("temporary output allocated before validation"),
    )

    with pytest.raises(ValueError, match="profile mismatch"):
        provider.concat([first, second], tmp_path / "output.mp4")


def test_concat_cleans_output_temp_if_manifest_allocation_fails(tmp_path, monkeypatch):
    provider = FFmpegProvider()
    valid = tmp_path / "valid.mp4"
    valid.write_bytes(b"video")
    leaked_temp = tmp_path / ".output.tmp.mp4"

    def allocate(output_path, suffix=None):
        if suffix is None:
            leaked_temp.write_bytes(b"")
            return leaked_temp
        raise OSError("manifest allocation failed")

    monkeypatch.setattr(provider, "_media_signature", lambda path: {}, raising=False)
    monkeypatch.setattr(provider, "_temporary_path", allocate)
    with pytest.raises(OSError, match="manifest allocation failed"):
        provider.concat([valid], tmp_path / "output.mp4")

    assert not leaked_temp.exists()


def test_concat_manifest_escapes_literal_backslashes():
    class PosixPathWithBackslash:
        def resolve(self):
            return self

        def as_posix(self):
            return "root/back\\slash/clip.mp4"

    assert FFmpegProvider._quote_manifest_path(PosixPathWithBackslash()) == (
        "'root/back'\\\\'slash/clip.mp4'"
    )


def test_concat_rejects_newline_path_before_launch(tmp_path, monkeypatch):
    provider = FFmpegProvider()
    unsafe = tmp_path / "unsafe\nname.mp4"
    monkeypatch.setattr(
        provider,
        "_run",
        lambda *args, **kwargs: pytest.fail("FFmpeg launched"),
        raising=False,
    )

    with pytest.raises(ValueError, match="newline"):
        provider.concat([unsafe], tmp_path / "output.mp4")


def test_concat_audio_preserves_dialogue_order_and_normalizes_pcm(tmp_path):
    provider = FFmpegProvider()
    first = _wav(tmp_path / "first.wav", seconds=0.2)
    second = _wav(tmp_path / "second.wav", seconds=0.3)

    output = provider.concat_audio([first, second], tmp_path / "dialogues.wav")

    info = probe_wav(output)
    assert info.duration == pytest.approx(0.5, abs=0.002)
    assert info.sample_rate == 48000
    assert info.channels == 2
    assert info.sample_width == 2


def test_create_silence_writes_requested_normalized_duration(tmp_path):
    output = FFmpegProvider().create_silence(tmp_path / "silence.wav", duration=0.45)

    info = probe_wav(output)
    assert info.duration == pytest.approx(0.45, abs=0.002)
    assert info.sample_rate == 48000
    assert info.channels == 2


def _seed_render_project(tmp_path: Path, *, name: str = "成片测试") -> dict:
    provider = FFmpegProvider()
    source_dir = tmp_path / f"{name}-sources"
    source_dir.mkdir()
    red_video = _video(
        provider, source_dir / "red.mp4", (220, 20, 20), seconds=1.3
    )
    green_video = _video(
        provider, source_dir / "green.mp4", (20, 220, 20), seconds=0.8
    )
    blue_video = _video(
        provider, source_dir / "blue.mp4", (20, 20, 220), seconds=0.75
    )
    first_audio = _wav(source_dir / "first.wav", seconds=0.4)
    second_audio = _wav(source_dir / "second.wav", seconds=0.5)
    third_audio = _wav(source_dir / "third.wav", seconds=0.5)
    database = str(tmp_path / f"{name}.db")
    create_schema(database)
    with session_scope(database) as session:
        project = Project(
            name=name,
            story="回家",
            description="三镜头短剧",
            language="zh-CN",
            style="电影感",
        )
        session.add(project)
        session.flush()
        later_scene = Scene(
            project_id=project.id,
            order=2,
            title="街口",
            description="抵达街口",
        )
        first_scene = Scene(
            project_id=project.id,
            order=1,
            title="巷子",
            description="穿过巷子",
        )
        session.add_all([later_scene, first_scene])
        session.flush()
        video_assets = [
            Asset(
                project_id=project.id,
                kind="VIDEO",
                path=str(path),
                mime_type="video/mp4",
            )
            for path in (red_video, green_video, blue_video)
        ]
        audio_assets = [
            Asset(
                project_id=project.id,
                kind="AUDIO",
                path=str(path),
                mime_type="audio/wav",
                metadata_json={"duration": duration},
            )
            for path, duration in (
                (first_audio, 0.4),
                (second_audio, 0.5),
                (third_audio, 0.5),
            )
        ]
        session.add_all([*video_assets, *audio_assets])
        session.flush()
        blue_shot = Shot(
            scene_id=later_scene.id,
            order=2,
            title="蓝色远景",
            description="看见家门",
            duration=0.75,
            video_asset_id=video_assets[2].id,
        )
        silent_shot = Shot(
            scene_id=later_scene.id,
            order=1,
            title="绿色空镜",
            description="停顿",
            duration=0.8,
            video_asset_id=video_assets[1].id,
        )
        red_shot = Shot(
            scene_id=first_scene.id,
            order=1,
            title="红色近景",
            description="连续说两句",
            duration=1.3,
            video_asset_id=video_assets[0].id,
        )
        session.add_all([blue_shot, silent_shot, red_shot])
        session.flush()
        session.add_all(
            [
                Dialogue(
                    shot_id=red_shot.id,
                    order=2,
                    text="第二句，继续走。",
                    duration=0.5,
                    audio_asset_id=audio_assets[1].id,
                ),
                Dialogue(
                    shot_id=red_shot.id,
                    order=1,
                    text="第一句。",
                    duration=0.4,
                    audio_asset_id=audio_assets[0].id,
                ),
                Dialogue(
                    shot_id=blue_shot.id,
                    order=1,
                    text="到了。",
                    duration=0.5,
                    audio_asset_id=audio_assets[2].id,
                ),
            ]
        )
        session.add(
            GenerationManifest(
                asset_id=video_assets[0].id,
                provider="comfyui",
                provider_version="0.31.0",
                model_name="wan2.2",
                prompt="红色近景",
                negative_prompt="模糊",
                seed=42,
                workflow_name="wan-i2v",
                workflow_hash="workflow-sha",
                binding_version="1",
                generation_time=1.25,
                input_assets=[audio_assets[0].id, audio_assets[1].id],
                output_asset=video_assets[0].id,
            )
        )
        session.flush()
        return {
            "database": database,
            "project_id": project.id,
            "shot_ids": [red_shot.id, silent_shot.id, blue_shot.id],
            "video_asset_ids": [asset.id for asset in video_assets],
            "audio_asset_ids": [asset.id for asset in audio_assets],
            "source_videos": [red_video, green_video, blue_video],
        }


def _render_service(seed: dict, tmp_path: Path, provider=None):
    from app.services.final_render import FinalRenderService

    return FinalRenderService(
        seed["database"],
        provider or FFmpegProvider(),
        tmp_path / "exports",
    )


def test_service_muxes_ordered_shots_multiple_dialogues_and_silence(tmp_path):
    seed = _seed_render_project(tmp_path)
    service = _render_service(seed, tmp_path)

    result = service.mux_shots(seed["project_id"])

    assert result["shot_ids"] == seed["shot_ids"]
    assert len(result["muxed_paths"]) == 3
    for muxed_path, source_path in zip(result["muxed_paths"], seed["source_videos"]):
        muxed = _probe(Path(muxed_path))
        source = _probe(source_path)
        assert {stream["codec_type"] for stream in muxed["streams"]} >= {"video", "audio"}
        assert float(muxed["format"]["duration"]) == pytest.approx(
            float(source["format"]["duration"]), abs=0.08
        )
    first_audio = next(
        stream
        for stream in _probe(Path(result["muxed_paths"][0]))["streams"]
        if stream["codec_type"] == "audio"
    )
    assert float(first_audio["duration"]) >= 1.25
    silent_audio = next(
        stream
        for stream in _probe(Path(result["muxed_paths"][1]))["streams"]
        if stream["codec_type"] == "audio"
    )
    assert silent_audio["codec_name"] == "aac"
    assert silent_audio["sample_rate"] == "48000"
    assert silent_audio["channels"] == 2


def test_concat_project_registers_one_probed_h264_aac_final(tmp_path):
    seed = _seed_render_project(tmp_path)
    service = _render_service(seed, tmp_path)
    muxed = service.mux_shots(seed["project_id"])["muxed_paths"]

    result = service.concat_project(seed["project_id"], muxed)

    probe = _probe(Path(result["path"]))
    video = next(stream for stream in probe["streams"] if stream["codec_type"] == "video")
    audio = next(stream for stream in probe["streams"] if stream["codec_type"] == "audio")
    assert video["codec_name"] == "h264"
    assert audio["codec_name"] == "aac"
    assert float(probe["format"]["duration"]) == pytest.approx(2.85, abs=0.15)
    assert result["metadata"]["sha256"] == _sha256(Path(result["path"]))
    with session_scope(seed["database"]) as session:
        assets = session.query(Asset).filter_by(
            project_id=seed["project_id"], kind="FINAL_VIDEO"
        ).all()
        assert len(assets) == 1
        assert assets[0].id == result["asset_id"]
        assert assets[0].mime_type == "video/mp4"


def _sha256(path: Path) -> str:
    import hashlib

    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _asset_snapshot(database: str, asset_id: str) -> tuple[str, str, dict]:
    with session_scope(database) as session:
        asset = session.get(Asset, asset_id)
        return asset.path, asset.mime_type, dict(asset.metadata_json)


def _reject_asset_update(database: str, kind: str) -> None:
    trigger_name = f"reject_{kind.lower()}_update"
    with sqlite3.connect(database) as connection:
        connection.execute(
            f"""
            CREATE TRIGGER {trigger_name}
            BEFORE UPDATE ON assets
            WHEN OLD.kind = '{kind}'
            BEGIN
                SELECT RAISE(ABORT, '{kind} registration rejected');
            END
            """
        )


def _publish_leaks(project_dir: Path) -> list[Path]:
    if not project_dir.exists():
        return []
    return [
        path
        for path in project_dir.rglob("*")
        if path.is_file()
        and path.name.startswith(".")
        and (".tmp" in path.name or ".bak" in path.name)
    ]


def test_export_subtitles_uses_persisted_dialogue_durations_and_media_tails(tmp_path):
    seed = _seed_render_project(tmp_path)
    service = _render_service(seed, tmp_path)

    result = service.export_subtitles(seed["project_id"])

    path = Path(result["path"])
    assert path.read_bytes().startswith(b"\xef\xbb\xbf")
    assert path.read_text(encoding="utf-8-sig") == (
        "1\n00:00:00,000 --> 00:00:00,400\n第一句。\n\n"
        "2\n00:00:00,400 --> 00:00:00,900\n第二句，继续走。\n\n"
        "3\n00:00:02,100 --> 00:00:02,600\n到了。\n"
    )
    with session_scope(seed["database"]) as session:
        asset = session.get(Asset, result["asset_id"])
        assert asset.kind == "SUBTITLE"
        assert asset.mime_type == "application/x-subrip"


def test_subtitle_timeline_follows_persisted_shot_boundaries(tmp_path):
    seed = _seed_render_project(tmp_path)
    service = _render_service(seed, tmp_path)

    result = service.export_subtitles(seed["project_id"])

    text = Path(result["path"]).read_text(encoding="utf-8-sig")
    assert "00:00:02,100 --> 00:00:02,600\n到了。" in text


def test_export_manifest_is_deterministic_and_contains_hash_evidence(tmp_path):
    seed = _seed_render_project(tmp_path)
    service = _render_service(seed, tmp_path)
    muxed = service.mux_shots(seed["project_id"])["muxed_paths"]
    final = service.concat_project(seed["project_id"], muxed)
    service.export_subtitles(seed["project_id"])
    secret = tmp_path / "unrelated-secret.txt"
    secret.write_text("DO NOT READ THIS VALUE", encoding="utf-8")
    with session_scope(seed["database"]) as session:
        session.get(Asset, seed["video_asset_ids"][0]).metadata_json = {
            "manifest_path": str(secret)
        }

    first = service.export_manifest(seed["project_id"], final["asset_id"])
    first_bytes = Path(first["path"]).read_bytes()
    second = service.export_manifest(seed["project_id"], final["asset_id"])

    assert Path(second["path"]).read_bytes() == first_bytes
    assert first["sha256"] == second["sha256"] == _sha256(Path(first["path"]))
    payload = json.loads(first_bytes)
    assert payload["project"]["id"] == seed["project_id"]
    assert [scene["order"] for scene in payload["scenes"]] == [1, 2]
    assert [shot["id"] for shot in payload["shots"]] == seed["shot_ids"]
    assert [dialogue["text"] for dialogue in payload["dialogues"]] == [
        "第一句。",
        "第二句，继续走。",
        "到了。",
    ]
    assert all(len(asset["sha256"]) == 64 for asset in payload["assets"])
    assert payload["generation_manifests"] == [
        {
            "asset_id": seed["video_asset_ids"][0],
            "binding_version": "1",
            "generation_time": 1.25,
            "input_assets": seed["audio_asset_ids"][:2],
            "model_name": "wan2.2",
            "output_asset": seed["video_asset_ids"][0],
            "provider": "comfyui",
            "provider_version": "0.31.0",
            "seed": 42,
            "workflow_hash": "workflow-sha",
            "workflow_name": "wan-i2v",
        }
    ]
    assert b"DO NOT READ THIS VALUE" not in first_bytes
    with session_scope(seed["database"]) as session:
        assets = session.query(Asset).filter_by(
            project_id=seed["project_id"], kind="MANIFEST"
        ).all()
        assert len(assets) == 1
        assert assets[0].id == first["asset_id"] == second["asset_id"]


def test_render_is_idempotent_and_regenerates_corrupted_outputs(tmp_path):
    seed = _seed_render_project(tmp_path)
    service = _render_service(seed, tmp_path)

    first = service.render(seed["project_id"])
    second = service.render(seed["project_id"])
    assert {key: first[key] for key in ("video_asset_id", "subtitle_asset_id", "manifest_asset_id")} == {
        key: second[key] for key in ("video_asset_id", "subtitle_asset_id", "manifest_asset_id")
    }
    assert all(Path(first[key]).is_file() for key in ("video_path", "subtitle_path", "manifest_path"))
    Path(first["video_path"]).write_bytes(b"corrupt video")
    Path(first["subtitle_path"]).write_bytes(b"corrupt subtitle")
    Path(first["manifest_path"]).write_bytes(b"corrupt manifest")

    repaired = service.render(seed["project_id"])

    assert repaired["video_asset_id"] == first["video_asset_id"]
    assert repaired["subtitle_asset_id"] == first["subtitle_asset_id"]
    assert repaired["manifest_asset_id"] == first["manifest_asset_id"]
    assert _probe(Path(repaired["video_path"]))["streams"]
    assert Path(repaired["subtitle_path"]).read_bytes().startswith(b"\xef\xbb\xbf")
    assert json.loads(Path(repaired["manifest_path"]).read_text(encoding="utf-8"))
    with session_scope(seed["database"]) as session:
        for kind in ("FINAL_VIDEO", "SUBTITLE", "MANIFEST"):
            assert session.query(Asset).filter_by(
                project_id=seed["project_id"], kind=kind
            ).count() == 1


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("video_kind", "AUDIO", "VIDEO"),
        ("video_file", b"not video", "video"),
        ("audio_kind", "VIDEO", "AUDIO"),
        ("audio_file", b"not wav", "WAV"),
        ("audio_duration", 9.0, "duration"),
    ],
)
def test_mux_rejects_invalid_persisted_media_without_final_registration(
    tmp_path, field, value, message
):
    seed = _seed_render_project(tmp_path)
    with session_scope(seed["database"]) as session:
        if field == "video_kind":
            session.get(Asset, seed["video_asset_ids"][0]).kind = value
        elif field == "video_file":
            Path(session.get(Asset, seed["video_asset_ids"][0]).path).write_bytes(value)
        elif field == "audio_kind":
            session.get(Asset, seed["audio_asset_ids"][0]).kind = value
        elif field == "audio_file":
            Path(session.get(Asset, seed["audio_asset_ids"][0]).path).write_bytes(value)
        else:
            first_dialogue = session.query(Dialogue).filter_by(
                audio_asset_id=seed["audio_asset_ids"][0]
            ).one()
            first_dialogue.duration = value

    with pytest.raises((ValueError, RuntimeError), match=message):
        _render_service(seed, tmp_path).mux_shots(seed["project_id"])
    with session_scope(seed["database"]) as session:
        assert session.query(Asset).filter_by(kind="FINAL_VIDEO").count() == 0


def test_ffmpeg_concat_failure_preserves_prior_final_asset_and_file(tmp_path):
    seed = _seed_render_project(tmp_path)
    service = _render_service(seed, tmp_path)
    muxed = service.mux_shots(seed["project_id"])["muxed_paths"]
    final = service.concat_project(seed["project_id"], muxed)
    final_bytes = Path(final["path"]).read_bytes()
    _video(
        FFmpegProvider(),
        seed["source_videos"][0],
        (120, 20, 120),
        seconds=1.3,
    )
    muxed = service.mux_shots(seed["project_id"])["muxed_paths"]

    class FailingConcatProvider(FFmpegProvider):
        def concat(self, inputs, output_path):
            raise RuntimeError("FFmpeg failed intentionally")

    failing_service = _render_service(seed, tmp_path, FailingConcatProvider())
    with pytest.raises(RuntimeError, match="intentionally"):
        failing_service.concat_project(seed["project_id"], muxed)

    assert Path(final["path"]).read_bytes() == final_bytes
    with session_scope(seed["database"]) as session:
        asset = session.query(Asset).filter_by(kind="FINAL_VIDEO").one()
        assert asset.id == final["asset_id"]
        assert asset.metadata_json["sha256"] == _sha256(Path(final["path"]))


def test_mux_validation_failure_preserves_prior_valid_mux(tmp_path):
    seed = _seed_render_project(tmp_path)
    service = _render_service(seed, tmp_path)
    muxed = service.mux_shots(seed["project_id"])["muxed_paths"]
    prior_path = Path(muxed[0])
    prior_bytes = prior_path.read_bytes()
    vp9_path = tmp_path / "vp9.mp4"
    subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-y",
            "-i",
            str(seed["source_videos"][0]),
            "-c:v",
            "libvpx-vp9",
            "-an",
            str(vp9_path),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    vp9_path.replace(seed["source_videos"][0])

    with pytest.raises(ValueError, match="H.264"):
        service.mux_shots(seed["project_id"])

    assert prior_path.read_bytes() == prior_bytes


def test_final_validation_failure_preserves_prior_valid_final(tmp_path):
    seed = _seed_render_project(tmp_path)
    service = _render_service(seed, tmp_path)
    muxed = service.mux_shots(seed["project_id"])["muxed_paths"]
    final = service.concat_project(seed["project_id"], muxed)
    final_path = Path(final["path"])
    prior_bytes = final_path.read_bytes()
    _video(
        FFmpegProvider(),
        seed["source_videos"][0],
        (120, 40, 100),
        seconds=1.3,
    )
    muxed = service.mux_shots(seed["project_id"])["muxed_paths"]

    class InvalidConcatProvider(FFmpegProvider):
        def concat(self, inputs, output_path):
            shutil.copy2(seed["source_videos"][0], output_path)
            return Path(output_path)

    with pytest.raises(ValueError, match="audio stream"):
        _render_service(seed, tmp_path, InvalidConcatProvider()).concat_project(
            seed["project_id"], muxed
        )

    assert final_path.read_bytes() == prior_bytes


def test_final_duration_validation_rejects_truncated_valid_media(tmp_path):
    seed = _seed_render_project(tmp_path)
    service = _render_service(seed, tmp_path)
    muxed = service.mux_shots(seed["project_id"])["muxed_paths"]

    class TruncatedConcatProvider(FFmpegProvider):
        def concat(self, inputs, output_path):
            shutil.copy2(inputs[0], output_path)
            return Path(output_path)

    with pytest.raises(ValueError, match="duration"):
        _render_service(seed, tmp_path, TruncatedConcatProvider()).concat_project(
            seed["project_id"], muxed
        )

    with session_scope(seed["database"]) as session:
        assert session.query(Asset).filter_by(kind="FINAL_VIDEO").count() == 0


def test_final_duration_tolerance_cannot_hide_dropped_short_shot(tmp_path):
    seed = _seed_render_project(tmp_path)
    short_video = _video(
        FFmpegProvider(),
        tmp_path / "short-shot.mp4",
        (30, 30, 220),
        seconds=0.0625,
    )
    short_duration = float(_probe(short_video)["format"]["duration"])
    with session_scope(seed["database"]) as session:
        short_asset = session.get(Asset, seed["video_asset_ids"][2])
        short_asset.path = str(short_video)
        session.get(Shot, seed["shot_ids"][2]).duration = short_duration
        short_dialogue = session.query(Dialogue).filter_by(
            shot_id=seed["shot_ids"][2]
        ).one()
        session.delete(short_dialogue)
    service = _render_service(seed, tmp_path)
    muxed = service.mux_shots(seed["project_id"])["muxed_paths"]

    class DropsLastShotProvider(FFmpegProvider):
        def concat(self, inputs, output_path):
            return super().concat(inputs[:-1], output_path)

    with pytest.raises(ValueError, match="duration"):
        _render_service(seed, tmp_path, DropsLastShotProvider()).concat_project(
            seed["project_id"], muxed
        )

    with session_scope(seed["database"]) as session:
        assert session.query(Asset).filter_by(kind="FINAL_VIDEO").count() == 0


def test_first_concat_failure_does_not_register_final_asset(tmp_path):
    seed = _seed_render_project(tmp_path)
    muxed = _render_service(seed, tmp_path).mux_shots(seed["project_id"])[
        "muxed_paths"
    ]

    class FailingConcatProvider(FFmpegProvider):
        def concat(self, inputs, output_path):
            raise RuntimeError("FFmpeg failed before output")

    with pytest.raises(RuntimeError, match="before output"):
        _render_service(seed, tmp_path, FailingConcatProvider()).concat_project(
            seed["project_id"], muxed
        )

    with session_scope(seed["database"]) as session:
        assert session.query(Asset).filter_by(kind="FINAL_VIDEO").count() == 0


def test_database_registration_failure_rolls_back_asset_row(tmp_path):
    seed = _seed_render_project(tmp_path)
    service = _render_service(seed, tmp_path)
    muxed = service.mux_shots(seed["project_id"])["muxed_paths"]
    with sqlite3.connect(seed["database"]) as connection:
        connection.execute(
            """
            CREATE TRIGGER reject_final_registration
            BEFORE INSERT ON assets
            WHEN NEW.kind = 'FINAL_VIDEO'
            BEGIN
                SELECT RAISE(ABORT, 'registration rejected');
            END
            """
        )

    with pytest.raises(IntegrityError, match="registration rejected"):
        service.concat_project(seed["project_id"], muxed)

    with session_scope(seed["database"]) as session:
        assert session.query(Asset).filter_by(kind="FINAL_VIDEO").count() == 0
    final_path = tmp_path / "exports" / seed["project_id"] / "final.mp4"
    assert not final_path.exists()
    assert _publish_leaks(final_path.parent) == []


def test_final_update_failure_restores_prior_file_and_asset_metadata(tmp_path):
    seed = _seed_render_project(tmp_path)
    service = _render_service(seed, tmp_path)
    muxed = service.mux_shots(seed["project_id"])["muxed_paths"]
    final = service.concat_project(seed["project_id"], muxed)
    final_path = Path(final["path"])
    prior_bytes = final_path.read_bytes()
    prior_asset = _asset_snapshot(seed["database"], final["asset_id"])
    _video(
        FFmpegProvider(),
        seed["source_videos"][0],
        (130, 30, 120),
        seconds=1.3,
    )
    changed_muxed = service.mux_shots(seed["project_id"])["muxed_paths"]
    _reject_asset_update(seed["database"], "FINAL_VIDEO")

    with pytest.raises(IntegrityError, match="FINAL_VIDEO registration rejected"):
        service.concat_project(seed["project_id"], changed_muxed)

    assert final_path.read_bytes() == prior_bytes
    assert _sha256(final_path) == prior_asset[2]["sha256"]
    assert _asset_snapshot(seed["database"], final["asset_id"]) == prior_asset
    assert _publish_leaks(final_path.parent) == []


def test_subtitle_update_failure_restores_prior_file_and_asset_metadata(tmp_path):
    seed = _seed_render_project(tmp_path)
    service = _render_service(seed, tmp_path)
    subtitle = service.export_subtitles(seed["project_id"])
    subtitle_path = Path(subtitle["path"])
    prior_bytes = subtitle_path.read_bytes()
    prior_asset = _asset_snapshot(seed["database"], subtitle["asset_id"])
    with session_scope(seed["database"]) as session:
        session.query(Dialogue).filter_by(
            audio_asset_id=seed["audio_asset_ids"][0]
        ).one().text = "被拒绝的新字幕"
    _reject_asset_update(seed["database"], "SUBTITLE")

    with pytest.raises(IntegrityError, match="SUBTITLE registration rejected"):
        service.export_subtitles(seed["project_id"])

    assert subtitle_path.read_bytes() == prior_bytes
    assert _sha256(subtitle_path) == prior_asset[2]["sha256"]
    assert _asset_snapshot(seed["database"], subtitle["asset_id"]) == prior_asset
    assert _publish_leaks(subtitle_path.parent) == []


def test_manifest_update_failure_restores_prior_file_and_asset_metadata(tmp_path):
    seed = _seed_render_project(tmp_path)
    service = _render_service(seed, tmp_path)
    muxed = service.mux_shots(seed["project_id"])["muxed_paths"]
    final = service.concat_project(seed["project_id"], muxed)
    manifest = service.export_manifest(seed["project_id"], final["asset_id"])
    manifest_path = Path(manifest["path"])
    prior_bytes = manifest_path.read_bytes()
    prior_asset = _asset_snapshot(seed["database"], manifest["asset_id"])
    with session_scope(seed["database"]) as session:
        session.get(Project, seed["project_id"]).description = "被拒绝的新项目描述"
    _reject_asset_update(seed["database"], "MANIFEST")

    with pytest.raises(IntegrityError, match="MANIFEST registration rejected"):
        service.export_manifest(seed["project_id"], final["asset_id"])

    assert manifest_path.read_bytes() == prior_bytes
    assert _sha256(manifest_path) == prior_asset[2]["sha256"]
    assert _asset_snapshot(seed["database"], manifest["asset_id"]) == prior_asset
    assert _publish_leaks(manifest_path.parent) == []


def test_registration_rollback_atomically_replaces_destination_without_unlink(
    tmp_path, monkeypatch
):
    seed = _seed_render_project(tmp_path)
    service = _render_service(seed, tmp_path)
    subtitle = service.export_subtitles(seed["project_id"])
    subtitle_path = Path(subtitle["path"])
    prior_bytes = subtitle_path.read_bytes()
    with session_scope(seed["database"]) as session:
        session.query(Dialogue).filter_by(
            audio_asset_id=seed["audio_asset_ids"][0]
        ).one().text = "触发原子恢复"
    _reject_asset_update(seed["database"], "SUBTITLE")
    original_unlink = Path.unlink

    def reject_destination_unlink(path, *args, **kwargs):
        if path == subtitle_path:
            raise AssertionError("rollback must replace destination without an unlink gap")
        return original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", reject_destination_unlink)

    with pytest.raises(IntegrityError, match="SUBTITLE registration rejected"):
        service.export_subtitles(seed["project_id"])

    assert subtitle_path.read_bytes() == prior_bytes
    assert _publish_leaks(subtitle_path.parent) == []


def test_post_commit_backup_cleanup_retries_without_failing_export(
    tmp_path, monkeypatch
):
    seed = _seed_render_project(tmp_path)
    service = _render_service(seed, tmp_path)
    subtitle = service.export_subtitles(seed["project_id"])
    with session_scope(seed["database"]) as session:
        session.query(Dialogue).filter_by(
            audio_asset_id=seed["audio_asset_ids"][0]
        ).one().text = "提交后的新字幕"
    original_unlink = Path.unlink
    failed_once = False

    def fail_first_backup_cleanup(path, *args, **kwargs):
        nonlocal failed_once
        if ".bak" in path.name and not failed_once:
            failed_once = True
            raise OSError("transient backup cleanup failure")
        return original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", fail_first_backup_cleanup)

    updated = service.export_subtitles(seed["project_id"])

    assert updated["asset_id"] == subtitle["asset_id"]
    assert "提交后的新字幕" in Path(updated["path"]).read_text(encoding="utf-8-sig")
    assert failed_once
    assert _publish_leaks(Path(updated["path"]).parent) == []


def test_concat_rejects_stale_mux_evidence(tmp_path):
    seed = _seed_render_project(tmp_path)
    service = _render_service(seed, tmp_path)
    muxed = service.mux_shots(seed["project_id"])["muxed_paths"]
    state_path = Path(muxed[0]).with_suffix(".mux.json")
    state_path.write_text('{"input_hash": "stale", "output_sha256": "stale"}', encoding="utf-8")

    with pytest.raises(ValueError, match="evidence"):
        service.concat_project(seed["project_id"], muxed)

    with session_scope(seed["database"]) as session:
        assert session.query(Asset).filter_by(kind="FINAL_VIDEO").count() == 0


def test_mux_regenerates_non_object_state_file(tmp_path):
    seed = _seed_render_project(tmp_path)
    service = _render_service(seed, tmp_path)
    first = service.mux_shots(seed["project_id"])
    state_path = Path(first["muxed_paths"][0]).with_suffix(".mux.json")
    state_path.write_text("[]", encoding="utf-8")

    second = service.mux_shots(seed["project_id"])

    assert second["muxed_paths"] == first["muxed_paths"]
    assert isinstance(json.loads(state_path.read_text(encoding="utf-8")), dict)


def test_concat_rejects_non_object_mux_evidence_as_invalid(tmp_path):
    seed = _seed_render_project(tmp_path)
    service = _render_service(seed, tmp_path)
    muxed = service.mux_shots(seed["project_id"])["muxed_paths"]
    Path(muxed[0]).with_suffix(".mux.json").write_text("null", encoding="utf-8")

    with pytest.raises(ValueError, match="evidence"):
        service.concat_project(seed["project_id"], muxed)


def test_manifest_rejects_project_asset_outside_deterministic_final_path(tmp_path):
    seed = _seed_render_project(tmp_path)
    service = _render_service(seed, tmp_path)
    muxed = service.mux_shots(seed["project_id"])["muxed_paths"]
    with session_scope(seed["database"]) as session:
        fake_final = Asset(
            project_id=seed["project_id"],
            kind="FINAL_VIDEO",
            path=muxed[0],
            mime_type="video/mp4",
        )
        session.add(fake_final)
        session.flush()
        fake_final_id = fake_final.id

    with pytest.raises(ValueError, match="project output"):
        service.export_manifest(seed["project_id"], fake_final_id)

    with session_scope(seed["database"]) as session:
        assert session.query(Asset).filter_by(kind="MANIFEST").count() == 0


def test_manifest_rejects_replaced_final_video_with_stale_registration(tmp_path):
    seed = _seed_render_project(tmp_path)
    service = _render_service(seed, tmp_path)
    muxed = service.mux_shots(seed["project_id"])["muxed_paths"]
    final = service.concat_project(seed["project_id"], muxed)
    shutil.copyfile(muxed[0], final["path"])

    with pytest.raises(ValueError, match="registered.*evidence"):
        service.export_manifest(seed["project_id"], final["asset_id"])

    with session_scope(seed["database"]) as session:
        assert session.query(Asset).filter_by(kind="MANIFEST").count() == 0


def test_manifest_rejects_foreign_generation_input_assets(tmp_path):
    seed = _seed_render_project(tmp_path)
    service = _render_service(seed, tmp_path)
    muxed = service.mux_shots(seed["project_id"])["muxed_paths"]
    final = service.concat_project(seed["project_id"], muxed)
    with session_scope(seed["database"]) as session:
        other_project = Project(name="外部生成输入")
        session.add(other_project)
        session.flush()
        foreign_input = Asset(
            project_id=other_project.id,
            kind="AUDIO",
            path=str(tmp_path / "unused.wav"),
            mime_type="audio/wav",
        )
        session.add(foreign_input)
        session.flush()
        session.query(GenerationManifest).filter_by(
            asset_id=seed["video_asset_ids"][0]
        ).one().input_assets = [foreign_input.id]

    with pytest.raises(ValueError, match="input asset.*belong"):
        service.export_manifest(seed["project_id"], final["asset_id"])

    with session_scope(seed["database"]) as session:
        assert session.query(Asset).filter_by(kind="MANIFEST").count() == 0


def test_manifest_allows_non_asset_generation_provenance_tokens(tmp_path):
    seed = _seed_render_project(tmp_path)
    service = _render_service(seed, tmp_path)
    muxed = service.mux_shots(seed["project_id"])["muxed_paths"]
    final = service.concat_project(seed["project_id"], muxed)
    with session_scope(seed["database"]) as session:
        session.query(GenerationManifest).filter_by(
            asset_id=seed["video_asset_ids"][0]
        ).one().input_assets = ["phase4_reference_image.png"]

    manifest = service.export_manifest(seed["project_id"], final["asset_id"])

    payload = json.loads(Path(manifest["path"]).read_text(encoding="utf-8"))
    assert payload["generation_manifests"][0]["input_assets"] == [
        "phase4_reference_image.png"
    ]


def test_manifest_rejects_cross_project_storyboard_and_preserves_prior(tmp_path):
    seed = _seed_render_project(tmp_path)
    service = _render_service(seed, tmp_path)
    muxed = service.mux_shots(seed["project_id"])["muxed_paths"]
    final = service.concat_project(seed["project_id"], muxed)
    manifest = service.export_manifest(seed["project_id"], final["asset_id"])
    manifest_path = Path(manifest["path"])
    prior_bytes = manifest_path.read_bytes()
    prior_asset = _asset_snapshot(seed["database"], manifest["asset_id"])
    storyboard_path = tmp_path / "foreign-storyboard.png"
    Image.new("RGB", (32, 32), (20, 40, 60)).save(storyboard_path)
    with session_scope(seed["database"]) as session:
        other_project = Project(name="外部 storyboard 项目")
        session.add(other_project)
        session.flush()
        foreign_storyboard = Asset(
            project_id=other_project.id,
            kind="IMAGE",
            path=str(storyboard_path),
            mime_type="image/png",
        )
        session.add(foreign_storyboard)
        session.flush()
        session.get(Shot, seed["shot_ids"][0]).storyboard_asset_id = (
            foreign_storyboard.id
        )

    with pytest.raises(ValueError, match="storyboard.*belong"):
        service.export_manifest(seed["project_id"], final["asset_id"])

    assert manifest_path.read_bytes() == prior_bytes
    assert _asset_snapshot(seed["database"], manifest["asset_id"]) == prior_asset
    assert _publish_leaks(manifest_path.parent) == []


def test_project_ownership_is_enforced_for_inputs_and_final_asset(tmp_path):
    first = _seed_render_project(tmp_path, name="项目一")
    second_dir = tmp_path / "other"
    second_dir.mkdir()
    second = _seed_render_project(second_dir, name="项目二")
    service = _render_service(first, tmp_path)
    with session_scope(first["database"]) as session:
        foreign_video_path = second["source_videos"][0]
        foreign = Asset(
            project_id=first["project_id"],
            kind="VIDEO",
            path=str(foreign_video_path),
            mime_type="video/mp4",
        )
        session.add(foreign)
        session.flush()
        # A cross-project row cannot exist across separate DBs, so create a second
        # project in the same DB and deliberately link its asset from the first.
        other_project = Project(name="同库外部项目")
        session.add(other_project)
        session.flush()
        foreign.project_id = other_project.id
        first_shot = session.get(Shot, first["shot_ids"][0])
        original_video_id = first_shot.video_asset_id
        first_shot.video_asset_id = foreign.id
        other_final = Asset(
            project_id=other_project.id,
            kind="FINAL_VIDEO",
            path=str(foreign_video_path),
            mime_type="video/mp4",
        )
        session.add(other_final)
        session.flush()
        other_final_id = other_final.id

    with pytest.raises(ValueError, match="belong"):
        service.mux_shots(first["project_id"])
    with session_scope(first["database"]) as session:
        session.get(Shot, first["shot_ids"][0]).video_asset_id = original_video_id

    muxed = service.mux_shots(first["project_id"])["muxed_paths"]
    with pytest.raises(ValueError, match="project output"):
        service.concat_project(first["project_id"], [second["source_videos"][0]])
    with pytest.raises(ValueError, match="belong"):
        service.export_manifest(first["project_id"], other_final_id)
    assert muxed
