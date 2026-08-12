"""Run the real manually-seeded Phase 9 subtitle/final-render smoke."""

from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import os
import re
import sqlite3
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

import psutil

from app.db.session import create_schema, get_engine, session_scope
from app.models import Asset, Dialogue, Project, Scene, Shot
from app.providers.ffmpeg_render_provider import FFmpegRenderProvider
from app.services.audio_probe import probe_wav
from app.services.render_generation import render_project
from app.services.video_probe import probe_video
from app.services.media_probe import probe_av
from scripts.verify_phase9 import verify_phase9


ROOT = Path(__file__).resolve().parents[1]
PHASE8_LIPSYNC = Path(
    "E:/kang/github/Movie/artifacts/phase8/20260812-205621-fad213b5/output/"
    "musetalk-e0eac769dadc4242ae8b6ac2f9ea55ab.mp4"
)
PHASE7_AUDIO = Path(
    "E:/kang/github/Movie/artifacts/phase7/1786342910/audio/"
    "79002d71-d985-4658-aa8e-f30731dc0291.wav"
)
PHASE5_VIDEO = Path(
    "E:/kang/github/Movie/artifacts/phase5/1786277293/phase5_00002_.mp4"
)
PHASE8_LIPSYNC_SHA256 = "58029ed0c9f539daed13faf643fba3c03f0c93e23d2814bfd36a44c144d09f98"
PHASE7_AUDIO_SHA256 = "c77af7486266d780b2d9a8bc30e7c064cb244502d14d8f132485c596a5c72d49"
PHASE5_VIDEO_SHA256 = "8382967b7b092afa3a1948d1374b0e94496db7bf90c83c0233af7c57ee10b87e"
DIALOGUE_TEXT = "别怕，我已经找到回家的路了。"
_RUN_ID = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9._-]{0,126}[A-Za-z0-9])?", re.ASCII)
_RESERVED = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{number}" for number in range(1, 10)),
    *(f"LPT{number}" for number in range(1, 10)),
}


@dataclass(frozen=True)
class SeededPhase9:
    database_path: Path
    project_id: str
    scene_id: str
    shot_ids: tuple[str, str]
    asset_ids: dict[str, str]
    source_hashes: dict[str, str]


class _MemoryStatus(ctypes.Structure):
    _fields_ = [
        ("dwLength", ctypes.c_ulong),
        ("dwMemoryLoad", ctypes.c_ulong),
        ("ullTotalPhys", ctypes.c_ulonglong),
        ("ullAvailPhys", ctypes.c_ulonglong),
        ("ullTotalPageFile", ctypes.c_ulonglong),
        ("ullAvailPageFile", ctypes.c_ulonglong),
        ("ullTotalVirtual", ctypes.c_ulonglong),
        ("ullAvailVirtual", ctypes.c_ulonglong),
        ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
    ]


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def validate_run_id(value: str) -> str:
    if not isinstance(value, str) or _RUN_ID.fullmatch(value) is None:
        raise ValueError("PHASE 9 run ID must be safe 1-128 character ASCII text")
    if value in {".", ".."} or value.split(".", 1)[0].upper() in _RESERVED:
        raise ValueError("PHASE 9 run ID is a reserved filesystem name")
    return value


def _contained(path: Path, root: Path, label: str) -> Path:
    path = Path(path).resolve()
    root = Path(root).resolve()
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise RuntimeError(f"PHASE 9 {label} path escapes evidence root") from exc
    return path


def build_evidence_paths(evidence_root: Path, run_id: str) -> dict[str, Path]:
    root = Path(evidence_root).resolve()
    run_id = validate_run_id(run_id)
    artifacts = (root / "artifacts").resolve()
    runtime = (root / ".runtime").resolve()
    run_dir = _contained(artifacts / "phase9" / run_id, artifacts, "run")
    return {
        "run_dir": run_dir,
        "storage": _contained(run_dir / "storage", artifacts, "storage"),
        "review_dir": _contained(run_dir / "review", artifacts, "review"),
        "resources": _contained(run_dir / "resources.json", artifacts, "resources"),
        "evidence": _contained(run_dir / "evidence.json", artifacts, "evidence"),
        "database": _contained(
            runtime / "phase9" / f"phase9-smoke-{run_id}.db", runtime, "database"
        ),
    }


def _locked_source(path: Path, locked_path: Path, expected_sha256: str, label: str) -> Path:
    path = Path(path).resolve()
    if path != locked_path.resolve():
        raise RuntimeError(f"{label} must use the locked source path")
    if not path.is_file() or path.stat().st_size <= 0:
        raise FileNotFoundError(f"locked {label} is missing or empty: {path}")
    measured = sha256_file(path)
    if measured != expected_sha256:
        raise RuntimeError(f"locked {label} SHA256 mismatch")
    return path


def seed_phase9_database(
    database_path: Path,
    *,
    phase8_lipsync: Path = PHASE8_LIPSYNC,
    phase7_audio: Path = PHASE7_AUDIO,
    phase5_video: Path = PHASE5_VIDEO,
) -> SeededPhase9:
    database_path = Path(database_path).resolve()
    if database_path.exists():
        raise FileExistsError(f"PHASE 9 smoke database must be fresh: {database_path}")
    lipsync = _locked_source(
        phase8_lipsync, PHASE8_LIPSYNC, PHASE8_LIPSYNC_SHA256, "Phase 8 LIPSYNC"
    )
    audio = _locked_source(
        phase7_audio, PHASE7_AUDIO, PHASE7_AUDIO_SHA256, "Phase 7 AUDIO"
    )
    video = _locked_source(
        phase5_video, PHASE5_VIDEO, PHASE5_VIDEO_SHA256, "Phase 5 VIDEO"
    )
    wav = probe_wav(audio)
    if wav.sample_rate != 24_000 or wav.channels != 1 or abs(wav.duration - 2.48) > 0.001:
        raise RuntimeError("locked Phase 7 AUDIO profile mismatch")
    lipsync_info = probe_video(lipsync)
    video_info = probe_video(video)
    if (
        lipsync_info.codec != "h264"
        or (lipsync_info.width, lipsync_info.height) != (640, 368)
        or abs(lipsync_info.fps - 25) > 0.01
        or lipsync_info.frames != 70
    ):
        raise RuntimeError("locked Phase 8 LIPSYNC profile mismatch")
    if (
        video_info.codec != "h264"
        or (video_info.width, video_info.height) != (640, 368)
        or abs(video_info.fps - 16) > 0.01
        or video_info.frames != 49
    ):
        raise RuntimeError("locked Phase 5 VIDEO profile mismatch")

    database_path.parent.mkdir(parents=True, exist_ok=True)
    create_schema(str(database_path))
    with session_scope(str(database_path)) as session:
        project = Project(
            name="PHASE 9 real subtitle/final render smoke",
            story="雨夜归途",
            description="Manually seeded Phase 9; not Phase 10 orchestration",
            language="zh-CN",
            status="LIPSYNC_GENERATED",
        )
        session.add(project)
        session.flush()
        scene = Scene(
            project_id=project.id,
            order=1,
            title="雨夜归途",
            description="真实两镜头渲染验收",
            estimated_duration=5.8,
        )
        session.add(scene)
        session.flush()
        lipsync_asset = Asset(
            project_id=project.id,
            kind="LIPSYNC",
            path=str(lipsync),
            mime_type="video/mp4",
            metadata_json={"sha256": PHASE8_LIPSYNC_SHA256, "locked": True},
        )
        audio_asset = Asset(
            project_id=project.id,
            kind="AUDIO",
            path=str(audio),
            mime_type="audio/wav",
            metadata_json={"sha256": PHASE7_AUDIO_SHA256, "locked": True},
        )
        video_asset = Asset(
            project_id=project.id,
            kind="VIDEO",
            path=str(video),
            mime_type="video/mp4",
            metadata_json={"sha256": PHASE5_VIDEO_SHA256, "locked": True},
        )
        session.add_all([lipsync_asset, audio_asset, video_asset])
        session.flush()
        talking = Shot(
            scene_id=scene.id,
            order=1,
            title="坚定回应",
            description="Phase 8 lip-sync accepted output",
            duration=2.8,
            lipsync_asset_id=lipsync_asset.id,
            requires_lip_sync=True,
            speaker_visible=True,
            status="LIPSYNC_GENERATED",
        )
        silent = Shot(
            scene_id=scene.id,
            order=2,
            title="无声转场",
            description="Phase 5 accepted video-only output",
            duration=3.0,
            video_asset_id=video_asset.id,
            requires_lip_sync=False,
            speaker_visible=False,
            status="VIDEO_GENERATED",
        )
        session.add_all([talking, silent])
        session.flush()
        session.add(
            Dialogue(
                shot_id=talking.id,
                order=1,
                text=DIALOGUE_TEXT,
                emotion="坚定而温柔",
                audio_asset_id=audio_asset.id,
                duration=2.48,
                start_time=0.0,
                end_time=2.48,
            )
        )
        session.flush()
        return SeededPhase9(
            database_path=database_path,
            project_id=project.id,
            scene_id=scene.id,
            shot_ids=(talking.id, silent.id),
            asset_ids={
                "phase8_lipsync": lipsync_asset.id,
                "phase7_audio": audio_asset.id,
                "phase5_video": video_asset.id,
            },
            source_hashes={
                "phase8_lipsync": PHASE8_LIPSYNC_SHA256,
                "phase7_audio": PHASE7_AUDIO_SHA256,
                "phase5_video": PHASE5_VIDEO_SHA256,
            },
        )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evidence-root", required=True)
    parser.add_argument("--run-id", default=None)
    parser.add_argument("--visual-review", choices=("approved", "pending"), default="pending")
    return parser


def _git_identity() -> dict:
    result = subprocess.run(
        ["git", "-C", str(ROOT), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        timeout=30,
        shell=False,
    )
    if result.returncode != 0:
        raise RuntimeError("cannot attest LocalDramaAI Git commit")
    return {"repository": str(ROOT), "commit": result.stdout.strip()}


def _resource_snapshot() -> dict:
    memory = psutil.virtual_memory()
    process = psutil.Process()
    commit_mib = None
    if os.name == "nt":
        status = _MemoryStatus()
        status.dwLength = ctypes.sizeof(_MemoryStatus)
        if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
            commit_mib = (status.ullTotalPageFile - status.ullAvailPageFile) / (1024 * 1024)
    return {
        "time": time.time(),
        "cpu_percent": process.cpu_percent(interval=None),
        "rss_mib": process.memory_info().rss / (1024 * 1024),
        "system_ram_mib": (memory.total - memory.available) / (1024 * 1024),
        "windows_commit_mib": commit_mib,
    }


def _monitor_resources(stop: threading.Event, samples: list[dict]) -> None:
    while not stop.wait(0.1):
        samples.append(_resource_snapshot())


def _extract_review(output: Path, review_dir: Path, ffmpeg: Path) -> dict[str, dict]:
    review_dir.mkdir(parents=True, exist_ok=True)
    timestamps = {
        "pre_cue": 0.0,
        "in_cue": 1.2,
        "post_cue": 2.6,
        "pre_boundary": 2.76,
        "post_boundary": 2.84,
    }
    result: dict[str, dict] = {}
    for label, timestamp in timestamps.items():
        path = review_dir / f"{label}.png"
        subprocess.run(
            [
                str(ffmpeg), "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
                "-ss", f"{timestamp:.3f}", "-i", str(output), "-frames:v", "1", str(path),
            ],
            check=True,
            capture_output=True,
            timeout=60,
            shell=False,
        )
        result[label] = {"path": str(path.resolve()), "sha256": sha256_file(path)}
    contact = review_dir / "contact-sheet.png"
    subprocess.run(
        [
            str(ffmpeg), "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
            "-i", str(output), "-vf", "fps=2,scale=320:184,tile=4x3", "-frames:v", "1",
            str(contact),
        ],
        check=True,
        capture_output=True,
        timeout=60,
        shell=False,
    )
    result["contact_sheet"] = {
        "path": str(contact.resolve()),
        "sha256": sha256_file(contact),
    }
    return result


def run_smoke(args: argparse.Namespace) -> dict:
    run_id = args.run_id or time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:8]
    paths = build_evidence_paths(Path(args.evidence_root), run_id)
    if paths["run_dir"].exists() or paths["database"].exists():
        raise FileExistsError("PHASE 9 evidence run must be fresh")
    paths["run_dir"].mkdir(parents=True)
    seeded = seed_phase9_database(paths["database"])
    provider = FFmpegRenderProvider()
    samples = [_resource_snapshot()]
    monitor_stop = threading.Event()
    monitor = threading.Thread(
        target=_monitor_resources,
        args=(monitor_stop, samples),
        name=f"phase9-resource-monitor-{run_id}",
        daemon=False,
    )
    monitor.start()
    started = time.monotonic()
    try:
        result = render_project(
            str(seeded.database_path), seeded.project_id, provider, paths["storage"]
        )
    finally:
        elapsed = time.monotonic() - started
        monitor_stop.set()
        monitor.join(timeout=5)
        if monitor.is_alive():
            raise RuntimeError("PHASE 9 resource monitor did not stop")
        samples.append(_resource_snapshot())
    if result.alias_status != "READY":
        raise RuntimeError(f"PHASE 9 smoke requires READY alias: {result.alias_error}")
    media = probe_av(result.published_path, executable=str(provider.probe_executable))
    review = _extract_review(result.published_path, paths["review_dir"], provider.executable)
    get_engine(str(seeded.database_path)).dispose()
    with sqlite3.connect(str(seeded.database_path)) as connection:
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchall()
    resources = {
        "samples": samples,
        "elapsed_seconds": elapsed,
        "peaks": {
            key: max(sample[key] for sample in samples if sample[key] is not None)
            for key in ("cpu_percent", "rss_mib", "system_ram_mib", "windows_commit_mib")
        },
    }
    paths["resources"].write_text(
        json.dumps(resources, ensure_ascii=False, sort_keys=True, indent=2), encoding="utf-8"
    )
    evidence = {
        "phase": 9,
        "scope": "manually seeded Phase 9 rendering; not Phase 10 automation",
        "run_id": run_id,
        "project_id": seeded.project_id,
        "shot_ids": list(seeded.shot_ids),
        "alias_status": result.alias_status,
        "visual_review": args.visual_review,
        "database": {"path": str(seeded.database_path), "sha256": sha256_file(seeded.database_path)},
        "sources": {
            "phase8_lipsync": {"path": str(PHASE8_LIPSYNC.resolve()), "sha256": PHASE8_LIPSYNC_SHA256},
            "phase7_audio": {"path": str(PHASE7_AUDIO.resolve()), "sha256": PHASE7_AUDIO_SHA256},
            "phase5_video": {"path": str(PHASE5_VIDEO.resolve()), "sha256": PHASE5_VIDEO_SHA256},
        },
        "subtitle": {
            "asset_id": result.subtitle_asset.id,
            "path": result.subtitle_asset.path,
            "sha256": sha256_file(Path(result.subtitle_asset.path)),
            "text": DIALOGUE_TEXT,
            "cue": "00:00:00,000 --> 00:00:02,480",
        },
        "output": {
            "asset_id": result.final_asset.id,
            "immutable_path": result.final_asset.path,
            "alias_path": str(result.published_path),
            "sha256": result.provider_result.output_sha256,
            "frames": media.video.frames,
            "video_duration": media.video.duration,
            "audio_duration": media.audio.duration,
        },
        "provider_manifest": {
            "path": str(result.provider_result.manifest_path),
            "sha256": sha256_file(result.provider_result.manifest_path),
        },
        "profile": {
            "video_codec": media.video.codec,
            "pixel_format": media.video.pixel_format,
            "width": media.video.width,
            "height": media.video.height,
            "fps": media.video.fps,
            "frames": media.video.frames,
            "audio_codec": media.audio.codec,
            "sample_rate": media.audio.sample_rate,
            "channels": media.audio.channels,
        },
        "ffmpeg": {
            "executable": str(result.provider_result.identity.executable),
            "version": result.provider_result.identity.version,
            "configuration": result.provider_result.identity.configuration,
        },
        "font": {
            "path": str(result.provider_result.identity.font_path),
            "size": result.provider_result.identity.font_size,
            "sha256": result.provider_result.identity.font_sha256,
        },
        "git": _git_identity(),
        "review": review,
        "review_timing_note": "the subtitle cue starts at frame zero, so no true pre-cue frame exists",
        "resources": {"path": str(paths["resources"]), "sha256": sha256_file(paths["resources"]), **resources},
    }
    paths["evidence"].write_text(
        json.dumps(evidence, ensure_ascii=False, sort_keys=True, indent=2), encoding="utf-8"
    )
    verify_phase9(paths["evidence"], evidence_root=Path(args.evidence_root))
    return {**evidence, "evidence_path": str(paths["evidence"])}


def main() -> None:
    args = _parser().parse_args()
    result = run_smoke(args)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
