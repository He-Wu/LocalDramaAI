"""Run one real, isolated PHASE 8 MuseTalk smoke and emit review evidence.

The script intentionally starts no PHASE 6/7 services.  It refuses occupied
AI ports, starts one loopback MuseTalk service, persists through the normal
application service, validates the result independently, and always tears the
service process tree down.
"""

from __future__ import annotations

import argparse
import asyncio
import ctypes
import hashlib
import json
import math
import os
import re
import socket
import subprocess
import time
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence
from urllib.parse import urlsplit

import psutil

from app.db.session import create_schema, session_scope
from app.models import Asset, Dialogue, GenerationManifest, Project, Scene, Shot
from app.providers.musetalk_provider import MuseTalkProvider
from app.services.audio_probe import probe_wav
from app.services.lipsync_generation import generate_shot_lipsync
from app.services.media_probe import AVInfo, probe_av
from app.services.video_probe import probe_video


ROOT = Path(__file__).resolve().parents[1]
PHASE7_VIDEO = Path(
    "E:/kang/github/Movie/artifacts/phase7/1786342910/video/"
    "0753ed98-8ccc-4c4c-99dc-f9fc96b822ac_00001_.mp4"
)
PHASE7_AUDIO = Path(
    "E:/kang/github/Movie/artifacts/phase7/1786342910/audio/"
    "79002d71-d985-4658-aa8e-f30731dc0291.wav"
)
PHASE7_VIDEO_SHA256 = "009cf772e2ba8f36604cc7f72c6799c15f093699aecb51c8cbea89f1db9a95f6"
PHASE7_AUDIO_SHA256 = "c77af7486266d780b2d9a8bc30e7c064cb244502d14d8f132485c596a5c72d49"
SHOT_DURATION = 2.78
DIALOGUE_DURATION = 2.48
OUTPUT_FPS = 25.0
AI_PORTS = (8020, 8030, 8188)
DEFAULT_MUSETALK_URL = "http://127.0.0.1:8030"
DEFAULT_MUSETALK_PYTHON = Path("E:/LocalDramaAI/env-musetalk/Scripts/python.exe")
DEFAULT_MUSETALK_REPO = Path("E:/LocalDramaAI/MuseTalk")
DEFAULT_FFMPEG_BIN = Path("E:/LocalDramaAI/ffmpeg/bin")
EXPECTED_MUSETALK_COMMIT = "0a89dec45a0192b824e3cf4daf96c239440c5ed8"
DEFAULT_MODEL_LOCK = ROOT / "scripts" / "musetalk-models.lock.json"
_MP4_MOV_FORMATS = {"mov", "mp4", "m4a", "3gp", "3g2", "mj2"}
_RUN_ID = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9._-]{0,126}[A-Za-z0-9])?", re.ASCII)
_SHA256 = re.compile(r"[0-9a-f]{64}", re.ASCII)
_GIT_COMMIT = re.compile(r"[0-9a-f]{40}", re.ASCII)
_WINDOWS_RESERVED_NAMES = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{number}" for number in range(1, 10)),
    *(f"LPT{number}" for number in range(1, 10)),
}
_VISUAL_REVIEW_CHECKLIST = (
    "no visible jaw or hand seam",
    "no mask edge",
    "no mouth tearing",
    "no severe flicker",
    "no identity change",
    "mouth motion is visibly changed and plausible",
)


@dataclass(frozen=True)
class SeededPhase8:
    database_path: Path
    project_id: str
    shot_id: str
    video_asset_id: str
    audio_asset_id: str
    dialogue_id: str


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
    try:
        with Path(path).open("rb") as source:
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        raise RuntimeError(f"cannot hash evidence file: {path}") from exc
    return digest.hexdigest()


def _run_git(repository: Path, *arguments: str) -> str:
    try:
        result = subprocess.run(
            ["git", "-c", f"safe.directory={repository}", "-C", str(repository), *arguments],
            capture_output=True,
            text=True,
            timeout=30,
            shell=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError(f"cannot inspect MuseTalk Git checkout: {repository}") from exc
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()[-1000:]
        raise RuntimeError(f"cannot inspect MuseTalk Git checkout: {detail}")
    return result.stdout.strip()


def _load_json_array(path: Path, label: str) -> list[dict[str, Any]]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"{label} is not valid JSON: {path}") from exc
    if not isinstance(payload, list) or not all(isinstance(record, dict) for record in payload):
        raise RuntimeError(f"{label} must be a JSON array of objects: {path}")
    return payload


def _model_tuple(record: dict[str, Any], *, include_source: bool) -> dict[str, Any]:
    required = {"repository", "revision", "path", "bytes", "sha256"}
    if include_source:
        required.add("source")
    if not required.issubset(record):
        raise RuntimeError("MuseTalk model lock tuple is missing required fields")
    result = {key: record[key] for key in ("path", "repository", "revision", "bytes", "sha256")}
    if (
        not isinstance(result["path"], str)
        or not result["path"].startswith("models/")
        or "\\" in result["path"]
        or not isinstance(result["repository"], str)
        or "/" not in result["repository"]
        or not isinstance(result["revision"], str)
        or _GIT_COMMIT.fullmatch(result["revision"].lower()) is None
        or isinstance(result["bytes"], bool)
        or not isinstance(result["bytes"], int)
        or result["bytes"] <= 0
        or not isinstance(result["sha256"], str)
        or _SHA256.fullmatch(result["sha256"].lower()) is None
    ):
        raise RuntimeError(f"MuseTalk model lock tuple is invalid: {result.get('path')}")
    result["revision"] = result["revision"].lower()
    result["sha256"] = result["sha256"].lower()
    return result


def attest_musetalk_runtime(
    repository: Path,
    expected_commit: str,
    model_lock_path: Path = DEFAULT_MODEL_LOCK,
) -> dict[str, Any]:
    """Attest a clean exact Git checkout and every authoritative model byte."""
    repository = Path(repository).resolve()
    model_lock_path = Path(model_lock_path).resolve()
    if not repository.is_dir() or not (repository / ".git").exists():
        raise RuntimeError(f"MuseTalk Git checkout is missing: {repository}")
    if not isinstance(expected_commit, str) or _GIT_COMMIT.fullmatch(expected_commit.lower()) is None:
        raise RuntimeError("configured MuseTalk Git commit is invalid")
    actual_commit = _run_git(repository, "rev-parse", "HEAD").lower()
    if actual_commit != expected_commit.lower():
        raise RuntimeError(
            f"MuseTalk Git HEAD mismatch: expected {expected_commit.lower()}, got {actual_commit}"
        )
    dirty = _run_git(repository, "status", "--porcelain=v1", "--untracked-files=normal")
    if dirty:
        raise RuntimeError("MuseTalk Git checkout must be clean before PHASE 8 smoke")

    locked_records = _load_json_array(model_lock_path, "authoritative MuseTalk model lock")
    if len(locked_records) != 11:
        raise RuntimeError(
            f"authoritative MuseTalk model lock must contain exactly 11 records; got {len(locked_records)}"
        )
    locked_models = [_model_tuple(record, include_source=True) for record in locked_records]
    locked_by_path = {record["path"]: record for record in locked_models}
    if len(locked_by_path) != 11:
        raise RuntimeError("authoritative MuseTalk model lock contains duplicate paths")

    manifest_path = repository / "models" / "model-hashes.json"
    manifest_records = _load_json_array(manifest_path, "MuseTalk model hash manifest")
    if len(manifest_records) != 11:
        raise RuntimeError(
            f"MuseTalk model hash manifest must contain exactly 11 records; got {len(manifest_records)}"
        )
    manifest_models = [_model_tuple(record, include_source=False) for record in manifest_records]
    manifest_by_path = {record["path"]: record for record in manifest_models}
    if len(manifest_by_path) != 11 or set(manifest_by_path) != set(locked_by_path):
        raise RuntimeError("MuseTalk model manifest paths differ from the authoritative lock")

    models_root = (repository / "models").resolve()
    for relative_path, expected in locked_by_path.items():
        if manifest_by_path[relative_path] != expected:
            raise RuntimeError(f"MuseTalk model manifest tuple differs from lock: {relative_path}")
        model_path = (repository / relative_path).resolve()
        try:
            model_path.relative_to(models_root)
        except ValueError as exc:
            raise RuntimeError(f"MuseTalk model path escapes models root: {relative_path}") from exc
        if not model_path.is_file():
            raise RuntimeError(f"MuseTalk model file is missing: {model_path}")
        measured_bytes = model_path.stat().st_size
        if measured_bytes != expected["bytes"]:
            raise RuntimeError(
                f"MuseTalk model byte count mismatch for {relative_path}: "
                f"expected {expected['bytes']}, got {measured_bytes}"
            )
        measured_hash = sha256_file(model_path)
        if measured_hash != expected["sha256"]:
            raise RuntimeError(
                f"MuseTalk model SHA256 mismatch for {relative_path}: "
                f"expected {expected['sha256']}, got {measured_hash}"
            )

    canonical = json.dumps(locked_models, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    return {
        "repo_path": str(repository),
        "repo_commit": actual_commit,
        "repo_clean": True,
        "model_count": 11,
        "model_identity_sha256": hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
        "model_lock": {"path": str(model_lock_path), "sha256": sha256_file(model_lock_path)},
        "model_manifest": {"path": str(manifest_path.resolve()), "sha256": sha256_file(manifest_path)},
        "models": locked_models,
    }


def validate_run_id(value: str) -> str:
    if not isinstance(value, str) or _RUN_ID.fullmatch(value) is None:
        raise ValueError("PHASE 8 run ID must be 1-128 separator-free ASCII letters, digits, dot, underscore, or hyphen")
    if value in {".", ".."} or value.split(".", 1)[0].upper() in _WINDOWS_RESERVED_NAMES:
        raise ValueError("PHASE 8 run ID is a reserved filesystem name")
    return value


def _contained_path(path: Path, root: Path, label: str) -> Path:
    resolved = Path(path).resolve()
    root = Path(root).resolve()
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise RuntimeError(f"PHASE 8 {label} path escapes its evidence root: {resolved}") from exc
    return resolved


def build_evidence_paths(evidence_root: Path, run_id: str) -> dict[str, Path]:
    evidence_root = Path(evidence_root).resolve()
    run_id = validate_run_id(run_id)
    artifacts_root = (evidence_root / "artifacts").resolve()
    runtime_root = (evidence_root / ".runtime").resolve()
    logs_root = (evidence_root / "logs").resolve()
    run_dir = _contained_path(artifacts_root / "phase8" / run_id, artifacts_root, "artifact")
    return {
        "run_dir": run_dir,
        "output_dir": _contained_path(run_dir / "output", artifacts_root, "output"),
        "review_dir": _contained_path(run_dir / "review", artifacts_root, "review"),
        "resources": _contained_path(run_dir / "resources.json", artifacts_root, "resources"),
        "evidence": _contained_path(run_dir / "evidence.json", artifacts_root, "evidence"),
        "database": _contained_path(
            runtime_root / "phase8" / f"phase8-smoke-{run_id}.db", runtime_root, "database"
        ),
        "log": _contained_path(logs_root / f"musetalk-phase8-{run_id}.log", logs_root, "log"),
    }


def _require_sha256(path: Path, expected: str, label: str) -> str:
    if not isinstance(expected, str) or len(expected) != 64:
        raise ValueError(f"{label} expected SHA256 is invalid")
    measured = sha256_file(path)
    if measured != expected.lower():
        raise RuntimeError(
            f"{label} SHA256 mismatch: expected {expected.lower()}, got {measured}"
        )
    return measured


def _validate_phase7_sources(video_path: Path, audio_path: Path) -> None:
    video = probe_video(video_path)
    if (
        video.codec != "h264"
        or (video.width, video.height) != (640, 368)
        or abs(video.fps - 16.0) > 0.01
        or video.frames != 49
        or abs(video.duration - 3.0625) > (1 / 16)
    ):
        raise RuntimeError("PHASE 7 video does not match the verified 640x368/16 FPS profile")
    audio = probe_wav(audio_path)
    if (
        audio.sample_rate != 24000
        or audio.channels != 1
        or audio.sample_width != 2
        or abs(audio.duration - DIALOGUE_DURATION) > 0.02
    ):
        raise RuntimeError("PHASE 7 audio does not match the verified PCM mono 24 kHz profile")
    if video.duration + (1 / 16) < SHOT_DURATION:
        raise RuntimeError("PHASE 7 video does not cover the PHASE 8 Shot duration")


def seed_phase8_database(
    database_path: Path,
    video_path: Path = PHASE7_VIDEO,
    audio_path: Path = PHASE7_AUDIO,
    *,
    expected_video_sha256: str = PHASE7_VIDEO_SHA256,
    expected_audio_sha256: str = PHASE7_AUDIO_SHA256,
) -> SeededPhase8:
    """Create one fresh eligible Shot bound to the immutable PHASE 7 files."""
    database_path = Path(database_path).resolve()
    video_path = Path(video_path).resolve()
    audio_path = Path(audio_path).resolve()
    if database_path.exists():
        raise FileExistsError(f"PHASE 8 smoke database must be fresh: {database_path}")
    for label, path in (("PHASE 7 video", video_path), ("PHASE 7 audio", audio_path)):
        if not path.is_file() or path.stat().st_size <= 0:
            raise FileNotFoundError(f"{label} is missing or empty: {path}")
    video_sha256 = _require_sha256(video_path, expected_video_sha256, "PHASE 7 video")
    audio_sha256 = _require_sha256(audio_path, expected_audio_sha256, "PHASE 7 audio")
    _validate_phase7_sources(video_path, audio_path)

    database_path.parent.mkdir(parents=True, exist_ok=True)
    create_schema(str(database_path))
    with session_scope(str(database_path)) as session:
        project = Project(
            name="PHASE 8 MuseTalk smoke",
            story="雨夜归途",
            description="Verified PHASE 7 media to official MuseTalk 1.5",
            language="zh-CN",
            status="VIDEO_GENERATED",
        )
        session.add(project)
        session.flush()
        scene = Scene(
            project_id=project.id,
            order=1,
            title="雨夜归途",
            description="林遥在雨中找到方向",
            estimated_duration=SHOT_DURATION,
        )
        session.add(scene)
        session.flush()
        source_video = Asset(
            project_id=project.id,
            kind="VIDEO",
            path=str(video_path),
            mime_type="video/mp4",
            metadata_json={
                "phase": 7,
                "sha256": video_sha256,
                "immutable_source": True,
            },
        )
        source_audio = Asset(
            project_id=project.id,
            kind="AUDIO",
            path=str(audio_path),
            mime_type="audio/wav",
            metadata_json={
                "phase": 7,
                "sha256": audio_sha256,
                "immutable_source": True,
            },
        )
        session.add_all([source_video, source_audio])
        session.flush()
        shot = Shot(
            scene_id=scene.id,
            order=1,
            title="坚定回应",
            description="林遥面对镜头说话，保留身份、背景、眼睛和手部位置",
            shot_type="DIALOGUE_CLOSEUP",
            duration=SHOT_DURATION,
            video_asset_id=source_video.id,
            requires_lip_sync=True,
            speaker_visible=True,
            status="VIDEO_GENERATED",
        )
        session.add(shot)
        session.flush()
        dialogue = Dialogue(
            shot_id=shot.id,
            order=1,
            text="别怕，我已经找到回家的路了。",
            emotion="坚定而温柔",
            audio_asset_id=source_audio.id,
            duration=DIALOGUE_DURATION,
        )
        session.add(dialogue)
        session.flush()
        return SeededPhase8(
            database_path=database_path,
            project_id=project.id,
            shot_id=shot.id,
            video_asset_id=source_video.id,
            audio_asset_id=source_audio.id,
            dialogue_id=dialogue.id,
        )


def validate_musetalk_url(value: str) -> str:
    """Accept no endpoint except the fixed unauthenticated IPv4 loopback port."""
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except (TypeError, ValueError) as exc:
        raise ValueError("MuseTalk URL must be exactly http://127.0.0.1:8030") from exc
    if (
        parsed.scheme != "http"
        or parsed.hostname != "127.0.0.1"
        or port != 8030
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in ("", "/")
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("MuseTalk URL must be exactly http://127.0.0.1:8030")
    return DEFAULT_MUSETALK_URL


def is_port_listening(port: int, host: str = "127.0.0.1") -> bool:
    try:
        with socket.create_connection((host, int(port)), timeout=0.25):
            return True
    except (ConnectionRefusedError, OSError, TimeoutError):
        return False


def assert_ports_free(
    ports: Iterable[int] = AI_PORTS,
    *,
    is_listening: Callable[[int], bool] = is_port_listening,
) -> None:
    occupied = [int(port) for port in ports if is_listening(int(port))]
    if occupied:
        raise RuntimeError(f"AI service ports must be free: {', '.join(map(str, occupied))}")


async def wait_ports_free(
    ports: Iterable[int] = AI_PORTS,
    *,
    timeout: float = 30.0,
    is_listening: Callable[[int], bool] = is_port_listening,
) -> None:
    deadline = time.monotonic() + timeout
    while True:
        occupied = [int(port) for port in ports if is_listening(int(port))]
        if not occupied:
            return
        if time.monotonic() >= deadline:
            raise RuntimeError(
                f"AI service ports remained occupied after cleanup: {', '.join(map(str, occupied))}"
            )
        await asyncio.sleep(0.25)


def validate_media_profile(info: AVInfo, *, target_duration: float = SHOT_DURATION) -> None:
    formats = {part.strip().lower() for part in info.format_name.split(",") if part.strip()}
    if not formats.intersection(_MP4_MOV_FORMATS):
        raise RuntimeError(f"PHASE 8 output must use an MP4/MOV-family container, got {info.format_name}")
    if info.video.codec != "h264":
        raise RuntimeError(f"PHASE 8 output must use H.264, got {info.video.codec}")
    if info.video.pixel_format != "yuv420p":
        raise RuntimeError(f"PHASE 8 output must use yuv420p, got {info.video.pixel_format}")
    if (info.video.width, info.video.height) != (640, 368):
        raise RuntimeError(
            f"PHASE 8 output must be 640x368, got {info.video.width}x{info.video.height}"
        )
    if abs(info.video.fps - OUTPUT_FPS) > 0.01:
        raise RuntimeError(f"PHASE 8 output must be 25 FPS, got {info.video.fps:g}")
    if info.audio.codec != "aac":
        raise RuntimeError(f"PHASE 8 output audio must be AAC, got {info.audio.codec}")
    if info.audio.sample_rate <= 0 or info.audio.channels <= 0:
        raise RuntimeError("PHASE 8 output AAC stream metadata is invalid")
    durations = (info.duration, info.video.duration, info.audio.duration)
    if any(not math.isfinite(value) or value <= 0 for value in durations):
        raise RuntimeError("PHASE 8 output contains an invalid duration")
    if min(durations) + (1 / OUTPUT_FPS) + 1e-9 < target_duration:
        raise RuntimeError(
            "PHASE 8 output is shorter than the Shot by more than one frame: "
            f"video={info.video.duration:.4f}s audio={info.audio.duration:.4f}s "
            f"target={target_duration:.4f}s"
        )
    if abs(info.video.duration - info.audio.duration) > 0.08 + 1e-9:
        raise RuntimeError(
            "PHASE 8 output A/V synchronization exceeds 80ms: "
            f"video={info.video.duration:.4f}s audio={info.audio.duration:.4f}s"
        )


def decode_media(
    path: Path,
    *,
    ffmpeg: str = "ffmpeg",
    runner: Callable[..., Any] = subprocess.run,
) -> None:
    command = [
        str(ffmpeg),
        "-hide_banner",
        "-loglevel",
        "error",
        "-xerror",
        "-nostdin",
        "-i",
        str(Path(path).resolve()),
        "-map",
        "0:v:0",
        "-map",
        "0:a:0",
        "-f",
        "null",
        os.devnull,
    ]
    try:
        result = runner(
            command,
            capture_output=True,
            text=True,
            timeout=180,
            shell=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError(f"FFmpeg full decode could not complete: {path}") from exc
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "")[-1000:]
        raise RuntimeError(f"PHASE 8 output failed full A/V decode: {detail}")


def _json_object(path: Path, label: str) -> dict[str, Any]:
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"{label} is not valid JSON: {path}") from exc
    if not isinstance(payload, dict):
        raise RuntimeError(f"{label} must be a JSON object: {path}")
    return payload


def _same_path(value: object, expected: Path, label: str) -> None:
    if not isinstance(value, str):
        raise RuntimeError(f"{label} path is missing")
    try:
        measured = Path(value).resolve()
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        raise RuntimeError(f"{label} path is invalid") from exc
    if measured != expected:
        raise RuntimeError(f"{label} path disagrees: {measured} != {expected}")


def validate_database_evidence(
    database_path: Path,
    project_id: str,
    shot_id: str,
    output_path: Path,
    provider_manifest_path: Path,
) -> dict[str, Any]:
    """Independently prove the exact Asset/Manifest/Shot/provenance graph."""
    database_path = Path(database_path).resolve()
    output_path = Path(output_path).resolve()
    provider_manifest_path = Path(provider_manifest_path).resolve()
    for label, path in (
        ("PHASE 8 database", database_path),
        ("PHASE 8 output", output_path),
        ("MuseTalk provider manifest", provider_manifest_path),
    ):
        if not path.is_file() or path.stat().st_size <= 0:
            raise RuntimeError(f"{label} is missing or empty: {path}")
    output_sha256 = sha256_file(output_path)
    provider_manifest_sha256 = sha256_file(provider_manifest_path)
    provider_payload = _json_object(provider_manifest_path, "MuseTalk provider manifest")

    with session_scope(str(database_path)) as session:
        shot = session.get(Shot, shot_id)
        if shot is None:
            raise RuntimeError("PHASE 8 Shot is missing from the evidence database")
        scene = session.get(Scene, shot.scene_id)
        if scene is None or scene.project_id != project_id:
            raise RuntimeError("PHASE 8 Shot does not belong to the evidence project")
        if not shot.requires_lip_sync or not shot.speaker_visible:
            raise RuntimeError("PHASE 8 Shot eligibility gates are not both true")
        if abs(float(shot.duration) - SHOT_DURATION) > 1e-9:
            raise RuntimeError("PHASE 8 Shot duration is not exactly 2.78 seconds")
        if shot.status != "LIPSYNC_GENERATED" or not shot.lipsync_asset_id:
            raise RuntimeError("PHASE 8 Shot has no successful lip-sync link")

        source_video = session.get(Asset, shot.video_asset_id)
        if (
            source_video is None
            or source_video.project_id != project_id
            or source_video.kind != "VIDEO"
        ):
            raise RuntimeError("PHASE 8 source VIDEO Asset link is invalid")
        dialogues = (
            session.query(Dialogue)
            .filter_by(shot_id=shot.id)
            .order_by(Dialogue.order)
            .all()
        )
        if len(dialogues) != 1:
            raise RuntimeError("PHASE 8 evidence must contain exactly one Dialogue")
        dialogue = dialogues[0]
        if abs(float(dialogue.duration or 0) - DIALOGUE_DURATION) > 0.02:
            raise RuntimeError("PHASE 8 Dialogue duration does not match verified speech")
        source_audio = session.get(Asset, dialogue.audio_asset_id)
        if (
            source_audio is None
            or source_audio.project_id != project_id
            or source_audio.kind != "AUDIO"
        ):
            raise RuntimeError("PHASE 8 dialogue AUDIO Asset link is invalid")
        expected_inputs = [source_video.id, source_audio.id]

        lipsync = session.get(Asset, shot.lipsync_asset_id)
        if (
            lipsync is None
            or lipsync.project_id != project_id
            or lipsync.kind != "LIPSYNC"
            or lipsync.mime_type != "video/mp4"
        ):
            raise RuntimeError("PHASE 8 LIPSYNC Asset link is invalid")
        _same_path(lipsync.path, output_path, "LIPSYNC Asset")
        manifests = session.query(GenerationManifest).filter_by(asset_id=lipsync.id).all()
        if len(manifests) != 1:
            raise RuntimeError("PHASE 8 LIPSYNC Asset must have exactly one GenerationManifest")
        manifest = manifests[0]
        if manifest.input_assets != expected_inputs:
            raise RuntimeError("PHASE 8 GenerationManifest input assets are not exact or ordered")
        if (
            manifest.output_asset != lipsync.id
            or manifest.provider != "musetalk"
            or manifest.model_name != "musetalk-v1.5"
            or manifest.workflow_name != "musetalk_lipsync"
        ):
            raise RuntimeError("PHASE 8 GenerationManifest provenance or output link is invalid")

        metadata = lipsync.metadata_json
        if not isinstance(metadata, dict):
            raise RuntimeError("PHASE 8 LIPSYNC Asset metadata is missing")
        _same_path(metadata.get("manifest_path"), provider_manifest_path, "Asset manifest")
        source_video_path = Path(source_video.path).resolve()
        source_audio_path = Path(source_audio.path).resolve()
        source_video_sha256 = sha256_file(source_video_path)
        source_audio_sha256 = sha256_file(source_audio_path)
        expected_metadata = {
            "provider": "musetalk",
            "provider_version": manifest.provider_version,
            "model_name": "musetalk-v1.5",
            "workflow_name": "musetalk_lipsync",
            "source_video_asset_id": source_video.id,
            "source_video_sha256": source_video_sha256,
            "audio_asset_id": source_audio.id,
            "source_audio_sha256": source_audio_sha256,
            "input_assets": expected_inputs,
            "target_duration": SHOT_DURATION,
            "output_sha256": output_sha256,
        }
        for key, expected in expected_metadata.items():
            if metadata.get(key) != expected:
                raise RuntimeError(f"PHASE 8 Asset metadata {key} disagrees with evidence")

        ids = {
            "project_id": project_id,
            "shot_id": shot.id,
            "dialogue_id": dialogue.id,
            "video_asset_id": source_video.id,
            "audio_asset_id": source_audio.id,
            "lipsync_asset_id": lipsync.id,
            "generation_manifest_id": manifest.id,
            "input_assets": expected_inputs,
            "provider_version": manifest.provider_version,
            "generation_time": manifest.generation_time,
            "source_video_path": str(source_video_path),
            "source_audio_path": str(source_audio_path),
            "source_video_sha256": source_video_sha256,
            "source_audio_sha256": source_audio_sha256,
            "output_sha256": output_sha256,
            "provider_manifest_sha256": provider_manifest_sha256,
        }

    metadata = provider_payload.get("metadata")
    if metadata != {
        "project_id": project_id,
        "shot_id": shot_id,
        "input_assets": ids["input_assets"],
    }:
        raise RuntimeError("MuseTalk provider manifest request metadata disagrees with database")
    if provider_payload.get("provider") != "musetalk":
        raise RuntimeError("MuseTalk provider manifest provider provenance is invalid")
    if provider_payload.get("provider_version") != ids["provider_version"]:
        raise RuntimeError("MuseTalk provider manifest version disagrees with database")
    if provider_payload.get("model", provider_payload.get("model_name")) != "musetalk-v1.5":
        raise RuntimeError("MuseTalk provider manifest model provenance is invalid")
    if provider_payload.get("workflow", provider_payload.get("workflow_name")) != "musetalk_lipsync":
        raise RuntimeError("MuseTalk provider manifest workflow provenance is invalid")
    if provider_payload.get("target_duration") != SHOT_DURATION:
        raise RuntimeError("MuseTalk provider manifest target duration disagrees")
    for key, expected_path, expected_hash in (
        ("source_video", Path(ids["source_video_path"]), ids["source_video_sha256"]),
        ("source_audio", Path(ids["source_audio_path"]), ids["source_audio_sha256"]),
    ):
        item = provider_payload.get(key)
        if not isinstance(item, dict):
            raise RuntimeError(f"MuseTalk provider manifest {key} provenance is missing")
        _same_path(item.get("path"), expected_path, f"provider {key}")
        if item.get("sha256") != expected_hash:
            raise RuntimeError(f"MuseTalk provider manifest {key} SHA256 disagrees")
    _same_path(provider_payload.get("output_path"), output_path, "provider output")
    if provider_payload.get("output_sha256") != output_sha256:
        raise RuntimeError("MuseTalk provider manifest output SHA256 disagrees")
    return ids


def build_review_commands(
    output_path: Path,
    review_dir: Path,
    *,
    duration: float,
    ffmpeg: str = "ffmpeg",
) -> tuple[list[list[str]], list[Path]]:
    output_path = Path(output_path).resolve()
    review_dir = Path(review_dir).resolve()
    frame_times = (0.0, duration / 2, max(0.0, duration - (1 / OUTPUT_FPS)))
    artifacts = [
        review_dir / "start.png",
        review_dir / "middle.png",
        review_dir / "end.png",
        review_dir / "mouth-contact-sheet.png",
    ]
    commands: list[list[str]] = []
    for timestamp, destination in zip(frame_times, artifacts[:3]):
        commands.append(
            [
                str(ffmpeg),
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-i",
                str(output_path),
                "-ss",
                f"{timestamp:.6f}",
                "-frames:v",
                "1",
                str(destination),
            ]
        )
    sample_fps = 12 / duration
    filter_graph = (
        f"fps={sample_fps:.9f},"
        "crop=iw*0.45:ih*0.42:iw*0.275:ih*0.36,"
        "scale=288:154,tile=4x3:padding=4:margin=4"
    )
    commands.append(
        [
            str(ffmpeg),
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(output_path),
            "-vf",
            filter_graph,
            "-frames:v",
            "1",
            str(artifacts[3]),
        ]
    )
    return commands, artifacts


def extract_review_artifacts(
    output_path: Path,
    review_dir: Path,
    *,
    duration: float,
    ffmpeg: str = "ffmpeg",
) -> list[Path]:
    review_dir = Path(review_dir).resolve()
    review_dir.mkdir(parents=True, exist_ok=True)
    commands, artifacts = build_review_commands(
        output_path,
        review_dir,
        duration=duration,
        ffmpeg=ffmpeg,
    )
    for command, destination in zip(commands, artifacts):
        try:
            result = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=120,
                shell=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise RuntimeError(f"could not extract visual review evidence: {destination}") from exc
        if result.returncode != 0 or not destination.is_file() or destination.stat().st_size <= 0:
            detail = (result.stderr or result.stdout or "")[-1000:]
            raise RuntimeError(f"visual review extraction failed for {destination}: {detail}")
    return artifacts


def windows_commit_mib() -> int:
    if os.name != "nt" or not hasattr(ctypes, "windll"):
        return 0
    status = _MemoryStatus()
    status.dwLength = ctypes.sizeof(_MemoryStatus)
    if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
        raise RuntimeError("Windows GlobalMemoryStatusEx failed")
    return int((status.ullTotalPageFile - status.ullAvailPageFile) / 1024 / 1024)


def parse_nvidia_smi(value: str) -> dict[str, int]:
    try:
        fields = [int(item.strip()) for item in value.strip().splitlines()[0].split(",")]
    except (IndexError, TypeError, ValueError) as exc:
        raise RuntimeError("nvidia-smi returned malformed resource data") from exc
    if len(fields) != 3 or any(field < 0 for field in fields):
        raise RuntimeError("nvidia-smi returned malformed resource data")
    return {
        "vram_mib": fields[0],
        "gpu_percent": fields[1],
        "temperature_c": fields[2],
    }


def sample_resources(started: float) -> dict[str, int | float]:
    try:
        result = subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=memory.used,utilization.gpu,temperature.gpu",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            text=True,
            timeout=10,
            shell=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError("nvidia-smi resource sampling failed") from exc
    if result.returncode != 0:
        raise RuntimeError(f"nvidia-smi resource sampling failed: {(result.stderr or '')[-500:]}")
    sample: dict[str, int | float] = {
        "elapsed_seconds": round(time.monotonic() - started, 3),
        **parse_nvidia_smi(result.stdout),
        "ram_mib": int(psutil.virtual_memory().used / 1024 / 1024),
        "commit_mib": windows_commit_mib(),
    }
    if sample["ram_mib"] <= 0 or sample["commit_mib"] <= 0:
        raise RuntimeError("RAM or Windows commit resource sampling returned no data")
    return sample


async def monitor_resources(
    stop: asyncio.Event,
    samples: list[dict[str, int | float]],
    *,
    started: float,
) -> None:
    while True:
        samples.append(await asyncio.to_thread(sample_resources, started))
        if stop.is_set():
            return
        try:
            await asyncio.wait_for(stop.wait(), timeout=1.0)
        except asyncio.TimeoutError:
            pass


def resource_peaks(samples: Sequence[dict[str, Any]]) -> dict[str, int]:
    keys = ("vram_mib", "gpu_percent", "temperature_c", "ram_mib", "commit_mib")
    if not samples:
        raise RuntimeError("PHASE 8 resource monitor captured no samples")
    peaks: dict[str, int] = {}
    for key in keys:
        values = [sample.get(key) for sample in samples]
        if any(isinstance(value, bool) or not isinstance(value, (int, float)) for value in values):
            raise RuntimeError(f"PHASE 8 resource samples are missing {key}")
        peaks[key] = int(max(values))
    if peaks["vram_mib"] <= 0 or peaks["ram_mib"] <= 0 or peaks["commit_mib"] <= 0:
        raise RuntimeError("PHASE 8 resource samples contain no GPU/RAM/commit evidence")
    return peaks


def write_json_atomic(path: Path, payload: dict[str, Any]) -> Path:
    path = Path(path).resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        if temporary.stat().st_size <= 0:
            raise RuntimeError(f"evidence JSON serialization was empty: {path}")
        os.replace(temporary, path)
        return path
    finally:
        temporary.unlink(missing_ok=True)


def _start_service(
    service_python: Path,
    musetalk_repo: Path,
    ffmpeg_bin: Path,
    repo_commit: str,
    log_handle,
) -> subprocess.Popen:
    service_module = ROOT / "ai_services" / "musetalk" / "service.py"
    for label, path in (
        ("MuseTalk service Python", service_python),
        ("MuseTalk inference repository", musetalk_repo),
        ("MuseTalk service module", service_module),
    ):
        if not path.is_file() if label != "MuseTalk inference repository" else not path.is_dir():
            raise FileNotFoundError(f"{label} is missing: {path}")
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(ROOT) + (
        os.pathsep + environment["PYTHONPATH"] if environment.get("PYTHONPATH") else ""
    )
    environment["LOCALDRAMA_MUSETALK_REPO"] = str(musetalk_repo.resolve())
    environment["LOCALDRAMA_MUSETALK_PYTHON"] = str(service_python.resolve())
    environment["LOCALDRAMA_MUSETALK_FFMPEG_BIN"] = str(ffmpeg_bin.resolve())
    environment["LOCALDRAMA_MUSETALK_REPO_COMMIT"] = repo_commit
    kwargs: dict[str, Any] = {
        "cwd": str(ROOT),
        "env": environment,
        "stdin": subprocess.DEVNULL,
        "stdout": log_handle,
        "stderr": subprocess.STDOUT,
        "shell": False,
    }
    if os.name == "nt":
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    else:
        kwargs["start_new_session"] = True
    return subprocess.Popen(
        [
            str(service_python.resolve()),
            "-m",
            "uvicorn",
            "ai_services.musetalk.service:app",
            "--host",
            "127.0.0.1",
            "--port",
            "8030",
        ],
        **kwargs,
    )


def capture_owned_descendants(
    process: subprocess.Popen | None,
    captured: set[tuple[int, float]],
) -> None:
    if process is None:
        return
    try:
        descendants = psutil.Process(process.pid).children(recursive=True)
    except (psutil.Error, OSError):
        return
    for descendant in descendants:
        try:
            captured.add((descendant.pid, descendant.create_time()))
        except (psutil.Error, OSError):
            continue


async def monitor_owned_descendants(
    process: subprocess.Popen,
    captured: set[tuple[int, float]],
    stop: asyncio.Event,
) -> None:
    while not stop.is_set():
        capture_owned_descendants(process, captured)
        await asyncio.sleep(0.1)
    capture_owned_descendants(process, captured)


def stop_process_tree(
    process: subprocess.Popen | None,
    captured_descendants: Iterable[tuple[int, float]] = (),
) -> None:
    """Stop only the launched parent and descendants proven to have belonged to it."""
    cleanup_errors: list[str] = []
    parent_alive = process is not None and process.poll() is None
    if parent_alive and process is not None:
        if os.name == "nt":
            taskkill_failed = False
            try:
                result = subprocess.run(
                    ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                    capture_output=True,
                    text=True,
                    timeout=30,
                    shell=False,
                )
                taskkill_failed = result.returncode != 0
            except (OSError, subprocess.TimeoutExpired):
                taskkill_failed = True
            if taskkill_failed and process.poll() is None:
                try:
                    process.terminate()
                except (OSError, subprocess.SubprocessError) as exc:
                    cleanup_errors.append(f"terminate parent PID {process.pid}: {exc}")
        else:
            try:
                process.terminate()
            except (OSError, subprocess.SubprocessError) as exc:
                cleanup_errors.append(f"terminate parent PID {process.pid}: {exc}")
        try:
            process.wait(timeout=30)
        except subprocess.TimeoutExpired:
            try:
                process.kill()
                process.wait(timeout=10)
            except (OSError, subprocess.SubprocessError) as exc:
                cleanup_errors.append(f"kill parent PID {process.pid}: {exc}")
        except (OSError, subprocess.SubprocessError) as exc:
            cleanup_errors.append(f"wait for parent PID {process.pid}: {exc}")
        try:
            if process.poll() is None:
                cleanup_errors.append(f"owned MuseTalk parent still alive: {process.pid}")
        except (OSError, subprocess.SubprocessError) as exc:
            cleanup_errors.append(f"verify parent PID {process.pid}: {exc}")

    owned: list[psutil.Process] = []
    captured_create_times: dict[int, set[float]] = {}
    for pid, create_time in set(captured_descendants):
        captured_create_times.setdefault(pid, set()).add(create_time)
    owned_create_times: dict[int, float] = {}
    for pid, create_times in captured_create_times.items():
        try:
            candidate = psutil.Process(pid)
            candidate_create_time = candidate.create_time()
            if (
                any(abs(candidate_create_time - value) <= 1e-6 for value in create_times)
                and candidate.is_running()
            ):
                owned.append(candidate)
                owned_create_times[pid] = candidate_create_time
        except psutil.NoSuchProcess:
            continue
        except (psutil.Error, OSError) as exc:
            cleanup_errors.append(f"inspect owned descendant PID {pid}: {exc}")
    for descendant in owned:
        try:
            descendant.terminate()
        except (psutil.Error, OSError) as exc:
            cleanup_errors.append(f"terminate PID {descendant.pid}: {exc}")
    try:
        _, alive = psutil.wait_procs(owned, timeout=5)
    except (psutil.Error, OSError) as exc:
        cleanup_errors.append(f"wait for owned descendants: {exc}")
        alive = owned
    for descendant in alive:
        try:
            descendant.kill()
        except (psutil.Error, OSError) as exc:
            cleanup_errors.append(f"kill PID {descendant.pid}: {exc}")
    if alive:
        try:
            _, alive = psutil.wait_procs(alive, timeout=5)
        except (psutil.Error, OSError) as exc:
            cleanup_errors.append(f"final wait for owned descendants: {exc}")

    survivors: list[int] = []
    for descendant in alive:
        try:
            candidate = psutil.Process(descendant.pid)
            if (
                abs(candidate.create_time() - owned_create_times[descendant.pid]) <= 1e-6
                and candidate.is_running()
            ):
                survivors.append(descendant.pid)
        except psutil.NoSuchProcess:
            continue
        except (psutil.Error, OSError, KeyError) as exc:
            cleanup_errors.append(
                f"verify owned descendant PID {descendant.pid}: {exc}"
            )
    if survivors:
        cleanup_errors.append(
            "owned MuseTalk descendants still alive: "
            + ", ".join(str(pid) for pid in sorted(set(survivors)))
        )
    if cleanup_errors:
        raise RuntimeError("; ".join(cleanup_errors))


async def _wait_for_health(provider: MuseTalkProvider, timeout: float = 180.0) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    last: dict[str, Any] | None = None
    while time.monotonic() < deadline:
        try:
            last = await provider.health()
            if last.get("status") == "ONLINE" and last.get("ready") is True:
                return last
        except RuntimeError:
            pass
        await asyncio.sleep(1)
    raise RuntimeError(
        "MuseTalk service did not become ready on 127.0.0.1:8030; "
        f"last health={json.dumps(last, ensure_ascii=True, sort_keys=True)}"
    )


def _new_run_id() -> str:
    return f"{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:8]}"


def _file_evidence(path: Path) -> dict[str, str]:
    path = Path(path).resolve()
    return {"path": str(path), "sha256": sha256_file(path)}


async def run_smoke(args: argparse.Namespace) -> dict[str, Any]:
    base_url = validate_musetalk_url(args.musetalk_url)
    evidence_root = Path(args.evidence_root).resolve()
    run_id = validate_run_id(args.run_id or _new_run_id())
    paths = build_evidence_paths(evidence_root, run_id)
    run_dir = paths["run_dir"]
    output_dir = paths["output_dir"]
    review_dir = paths["review_dir"]
    database_path = paths["database"]
    resources_path = paths["resources"]
    evidence_path = paths["evidence"]
    log_path = paths["log"]
    runtime_identity = attest_musetalk_runtime(
        Path(args.musetalk_repo),
        args.repo_commit,
        Path(args.model_lock),
    )
    assert_ports_free()
    for directory in (output_dir, review_dir, database_path.parent, log_path.parent):
        directory.mkdir(parents=True, exist_ok=True)
    seeded = seed_phase8_database(database_path)

    service_process: subprocess.Popen | None = None
    provider: MuseTalkProvider | None = None
    log_handle = log_path.open("w", encoding="utf-8")
    stop_monitor = asyncio.Event()
    samples: list[dict[str, int | float]] = []
    started = time.monotonic()
    monitor = asyncio.create_task(monitor_resources(stop_monitor, samples, started=started))
    primary_error: BaseException | None = None
    cleanup_errors: list[Exception] = []
    owned_descendants: set[tuple[int, float]] = set()
    descendant_monitor: asyncio.Task[None] | None = None
    result: dict[str, Any] | None = None
    try:
        service_process = _start_service(
            Path(args.service_python),
            Path(args.musetalk_repo),
            Path(args.ffmpeg_bin),
            runtime_identity["repo_commit"],
            log_handle,
        )
        descendant_monitor = asyncio.create_task(
            monitor_owned_descendants(service_process, owned_descendants, stop_monitor)
        )
        provider = MuseTalkProvider(base_url, timeout=args.generation_timeout)
        health = await _wait_for_health(provider, timeout=args.health_timeout)
        asset = await generate_shot_lipsync(
            str(database_path),
            seeded.project_id,
            seeded.shot_id,
            provider,
            output_dir,
        )
        if asset is None:
            raise RuntimeError("eligible PHASE 8 Shot was unexpectedly skipped")
        output_path = Path(asset.path).resolve()
        metadata = asset.metadata_json if isinstance(asset.metadata_json, dict) else {}
        provider_manifest_path = Path(metadata.get("manifest_path", "")).resolve()
        media = probe_av(output_path, executable=args.ffprobe)
        validate_media_profile(media, target_duration=SHOT_DURATION)
        decode_media(output_path, ffmpeg=args.ffmpeg)
        database_evidence = validate_database_evidence(
            database_path,
            seeded.project_id,
            seeded.shot_id,
            output_path,
            provider_manifest_path,
        )
        review_artifacts = extract_review_artifacts(
            output_path,
            review_dir,
            duration=media.video.duration,
            ffmpeg=args.ffmpeg,
        )
        if args.visual_review == "rejected":
            raise RuntimeError("operator rejected PHASE 8 visual review evidence")
        result = {
            "phase": 8,
            "run_id": run_id,
            "project_id": seeded.project_id,
            "shot_id": seeded.shot_id,
            "target_duration": SHOT_DURATION,
            "database": str(database_path),
            "source_video": _file_evidence(PHASE7_VIDEO),
            "source_audio": _file_evidence(PHASE7_AUDIO),
            "output": _file_evidence(output_path),
            "provider_manifest": _file_evidence(provider_manifest_path),
            "database_links": database_evidence,
            "media": asdict(media),
            "review": {
                "status": args.visual_review,
                "checklist": list(_VISUAL_REVIEW_CHECKLIST),
                "start": _file_evidence(review_artifacts[0]),
                "middle": _file_evidence(review_artifacts[1]),
                "end": _file_evidence(review_artifacts[2]),
                "mouth_contact_sheet": _file_evidence(review_artifacts[3]),
            },
            "health": health,
            "musetalk_runtime": runtime_identity,
            "elapsed_seconds": round(time.monotonic() - started, 3),
            "service_log": str(log_path.resolve()),
        }
    except BaseException as exc:
        primary_error = exc
    finally:
        if provider is not None:
            try:
                await provider.unload()
            except Exception as exc:
                cleanup_errors.append(exc)
        stop_monitor.set()
        if descendant_monitor is not None:
            try:
                await descendant_monitor
            except Exception as exc:
                cleanup_errors.append(exc)
        try:
            await monitor
        except Exception as exc:
            cleanup_errors.append(exc)
        if samples:
            try:
                write_json_atomic(
                    resources_path,
                    {"samples": samples, "peaks": resource_peaks(samples)},
                )
            except Exception as exc:
                cleanup_errors.append(exc)
        try:
            stop_process_tree(service_process, owned_descendants)
        except Exception as exc:
            cleanup_errors.append(exc)
        try:
            log_handle.close()
        except Exception as exc:
            cleanup_errors.append(exc)
        try:
            await wait_ports_free()
            assert_ports_free()
        except Exception as exc:
            cleanup_errors.append(exc)

    if primary_error is not None:
        for cleanup_error in cleanup_errors:
            primary_error.add_note(f"cleanup failure: {cleanup_error}")
        raise primary_error
    if cleanup_errors:
        raise RuntimeError(
            "PHASE 8 cleanup or evidence capture failed: "
            + "; ".join(str(error) for error in cleanup_errors)
        )
    if result is None:
        raise RuntimeError("PHASE 8 smoke produced no result")
    resource_payload = _json_object(resources_path, "PHASE 8 resources")
    result["resources"] = {
        **_file_evidence(resources_path),
        "peaks": resource_payload["peaks"],
    }
    write_json_atomic(evidence_path, result)
    result["evidence"] = _file_evidence(evidence_path)
    return result


def _positive_float(value: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed) or parsed <= 0:
        raise argparse.ArgumentTypeError("value must be a positive finite number")
    return parsed


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--evidence-root",
        type=Path,
        default=ROOT,
        help="Root under which artifacts, database, logs, and review evidence are written.",
    )
    parser.add_argument("--run-id", help="Optional stable run identifier; must name a fresh database.")
    parser.add_argument(
        "--musetalk-url",
        default=os.environ.get("LOCALDRAMA_MUSETALK_URL", DEFAULT_MUSETALK_URL),
    )
    parser.add_argument("--service-python", type=Path, default=DEFAULT_MUSETALK_PYTHON)
    parser.add_argument("--musetalk-repo", type=Path, default=DEFAULT_MUSETALK_REPO)
    parser.add_argument("--model-lock", type=Path, default=DEFAULT_MODEL_LOCK)
    parser.add_argument("--ffmpeg-bin", type=Path, default=DEFAULT_FFMPEG_BIN)
    parser.add_argument(
        "--repo-commit",
        default=os.environ.get(
            "LOCALDRAMA_MUSETALK_REPO_COMMIT",
            EXPECTED_MUSETALK_COMMIT,
        ),
    )
    parser.add_argument("--ffmpeg", default="ffmpeg")
    parser.add_argument("--ffprobe", default="ffprobe")
    parser.add_argument("--health-timeout", type=_positive_float, default=180.0)
    parser.add_argument("--generation-timeout", type=_positive_float, default=1800.0)
    parser.add_argument(
        "--visual-review",
        choices=("pending", "approved", "rejected"),
        default="pending",
        help="Operator attestation after inspecting the generated review sheet.",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    result = asyncio.run(run_smoke(args))
    print("PHASE8_OUTPUT", result["output"]["path"])
    print("PHASE8_OUTPUT_SHA256", result["output"]["sha256"])
    print("PHASE8_DATABASE", result["database"])
    print("PHASE8_PROVIDER_MANIFEST", result["provider_manifest"]["path"])
    print("PHASE8_EVIDENCE", result["evidence"]["path"])
    print("PHASE8_EVIDENCE_SHA256", result["evidence"]["sha256"])
    print("PHASE8_VISUAL_REVIEW", result["review"]["status"])
    print("PHASE8_PEAK_RESOURCES", json.dumps(result["resources"]["peaks"], sort_keys=True))
    if result["review"]["status"] == "pending":
        print("MANUAL_VISUAL_REVIEW_REQUIRED", result["review"]["mouth_contact_sheet"]["path"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
