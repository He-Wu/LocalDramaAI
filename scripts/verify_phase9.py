"""Read-only verification of one Phase 9 evidence bundle."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
from pathlib import Path
from typing import Callable

import psutil
import yaml

from app.services.media_probe import AVInfo, probe_av
PHASE8_LIPSYNC = Path(
    "E:/kang/github/Movie/artifacts/phase8/20260812-205621-fad213b5/output/"
    "musetalk-e0eac769dadc4242ae8b6ac2f9ea55ab.mp4"
)
PHASE7_AUDIO = Path(
    "E:/kang/github/Movie/artifacts/phase7/1786342910/audio/"
    "79002d71-d985-4658-aa8e-f30731dc0291.wav"
)
PHASE5_VIDEO = Path("E:/kang/github/Movie/artifacts/phase5/1786277293/phase5_00002_.mp4")
PHASE8_LIPSYNC_SHA256 = "58029ed0c9f539daed13faf643fba3c03f0c93e23d2814bfd36a44c144d09f98"
PHASE7_AUDIO_SHA256 = "c77af7486266d780b2d9a8bc30e7c064cb244502d14d8f132485c596a5c72d49"
PHASE5_VIDEO_SHA256 = "8382967b7b092afa3a1948d1374b0e94496db7bf90c83c0233af7c57ee10b87e"
DIALOGUE_TEXT = "别怕，我已经找到回家的路了。"
ROOT = Path(__file__).resolve().parents[1]


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def git_identity() -> dict[str, str]:
    common = subprocess.run(
        ["git", "-C", str(ROOT), "rev-parse", "--path-format=absolute", "--git-common-dir"],
        capture_output=True, text=True, timeout=30, shell=False,
    )
    head = subprocess.run(
        ["git", "-C", str(ROOT), "rev-parse", "HEAD"],
        capture_output=True, text=True, timeout=30, shell=False,
    )
    if common.returncode != 0 or head.returncode != 0:
        raise RuntimeError("cannot attest LocalDramaAI Git identity")
    common_dir = Path(common.stdout.strip()).resolve(strict=True)
    return {"repository": str(common_dir.parent), "commit": head.stdout.strip()}


def _verify_git_identity(record: object) -> None:
    expected = _require_record(record, "git")
    current = git_identity()
    if Path(expected.get("repository", "")).resolve() != Path(current["repository"]):
        raise RuntimeError("Phase 9 Git repository identity mismatch")
    commit = expected.get("commit")
    if not isinstance(commit, str) or not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise RuntimeError("Phase 9 Git commit identity mismatch")
    ancestor = subprocess.run(
        ["git", "-C", str(ROOT), "merge-base", "--is-ancestor", commit, "HEAD"],
        capture_output=True, timeout=30, shell=False,
    )
    if ancestor.returncode != 0:
        raise RuntimeError("Phase 9 Git commit is not an ancestor of the current checkout")


def _verify_runtime_lock(
    evidence_path: Path, evidence: dict, runtime_lock: Path
) -> None:
    try:
        lock = yaml.safe_load(Path(runtime_lock).read_text(encoding="utf-8"))
        verification = lock["verification"]
    except (OSError, UnicodeError, yaml.YAMLError, KeyError, TypeError) as exc:
        raise RuntimeError("Phase 9 runtime lock is missing or invalid") from exc
    if str(verification.get("phase")) != "9":
        raise RuntimeError("Phase 9 runtime lock phase mismatch")

    def require_path(key: str, actual: object) -> Path:
        try:
            locked = Path(verification[key]).resolve(strict=True)
            measured = Path(actual).resolve(strict=True)
        except (KeyError, OSError, TypeError, ValueError) as exc:
            raise RuntimeError(f"Phase 9 runtime lock {key} path mismatch") from exc
        if locked != measured:
            raise RuntimeError(f"Phase 9 runtime lock {key} path mismatch")
        return measured

    def require_hash(key: str, path: Path) -> None:
        if verification.get(key) != _sha256(path):
            raise RuntimeError(f"Phase 9 runtime lock {key} mismatch")

    output = _require_record(evidence.get("output"), "output")
    subtitle = _require_record(evidence.get("subtitle"), "subtitle")
    database = _require_record(evidence.get("database"), "database")
    manifest = _require_record(evidence.get("provider_manifest"), "provider manifest")
    resources = _require_record(evidence.get("resources"), "resources")
    review = _require_record(evidence.get("review"), "review")
    git = _require_record(evidence.get("git"), "git")

    locked_evidence = require_path("phase9_evidence", evidence_path)
    immutable = require_path("phase9_output", output.get("immutable_path"))
    alias = require_path("phase9_alias", output.get("alias_path"))
    locked_subtitle = require_path("phase9_subtitle", subtitle.get("path"))
    locked_database = require_path("phase9_database", database.get("path"))
    locked_manifest = require_path("phase9_provider_manifest", manifest.get("path"))
    locked_resources = require_path("phase9_resources", resources.get("path"))
    contact = _require_record(review.get("contact_sheet"), "review contact_sheet")
    locked_contact = require_path("phase9_contact_sheet", contact.get("path"))

    require_hash("phase9_evidence_sha256", locked_evidence)
    require_hash("phase9_output_sha256", immutable)
    if verification.get("phase9_output_sha256") != _sha256(alias):
        raise RuntimeError("Phase 9 runtime lock alias hash mismatch")
    require_hash("phase9_subtitle_sha256", locked_subtitle)
    require_hash("phase9_database_sha256", locked_database)
    require_hash("phase9_provider_manifest_sha256", locked_manifest)
    require_hash("phase9_resources_sha256", locked_resources)
    require_hash("phase9_contact_sheet_sha256", locked_contact)
    if (
        verification.get("phase9_run_id") != evidence.get("run_id")
        or verification.get("phase9_code_commit") != git.get("commit")
        or verification.get("phase9_alias_status") != evidence.get("alias_status")
        or verification.get("phase9_visual_review") != evidence.get("visual_review")
    ):
        raise RuntimeError("Phase 9 runtime lock evidence identity mismatch")


def _require_record(record: object, label: str) -> dict:
    if not isinstance(record, dict):
        raise RuntimeError(f"Phase 9 evidence {label} is invalid")
    return record


def _bound_file(record: object, roots: tuple[Path, ...], label: str) -> Path:
    record = _require_record(record, label)
    path = resolve_approved_path(record.get("path"), roots, label)
    expected = record.get("sha256")
    if not isinstance(expected, str) or len(expected) != 64 or _sha256(path) != expected:
        raise RuntimeError(f"Phase 9 {label} SHA256 mismatch")
    return path


def _decode(path: Path, executable: str = "ffmpeg") -> None:
    result = subprocess.run(
        [
            executable, "-hide_banner", "-loglevel", "error", "-xerror", "-nostdin",
            "-i", str(path), "-map", "0:v:0", "-map", "0:a:0", "-f", "null", os.devnull,
        ],
        capture_output=True,
        timeout=120,
        shell=False,
    )
    if result.returncode != 0:
        raise RuntimeError("Phase 9 final output failed full decode")


def _verify_profile(info: AVInfo, evidence: dict) -> None:
    if (
        info.video.codec != "h264"
        or info.video.pixel_format != "yuv420p"
        or (info.video.width, info.video.height) != (640, 368)
        or abs(info.video.fps - 25.0) > 0.01
        or info.video.frames != 145
        or info.audio.codec != "aac"
        or info.audio.sample_rate != 48_000
        or info.audio.channels != 2
        or abs(info.video.duration - info.audio.duration) > 0.080000001
    ):
        raise RuntimeError("Phase 9 final output profile/frame/audio mismatch")
    profile = _require_record(evidence.get("profile"), "profile")
    actual = {
        "video_codec": info.video.codec,
        "pixel_format": info.video.pixel_format,
        "width": info.video.width,
        "height": info.video.height,
        "fps": info.video.fps,
        "frames": info.video.frames,
        "audio_codec": info.audio.codec,
        "sample_rate": info.audio.sample_rate,
        "channels": info.audio.channels,
    }
    if profile != actual:
        raise RuntimeError("Phase 9 evidence media profile mismatch")


def _read_database(database: Path, evidence: dict, subtitle: Path, immutable: Path) -> None:
    uri = f"file:{database.as_posix()}?mode=ro"
    try:
        connection = sqlite3.connect(uri, uri=True)
        connection.row_factory = sqlite3.Row
        project = connection.execute(
            "SELECT id, subtitle_asset_id, final_video_asset_id FROM projects WHERE id = ?",
            (evidence.get("project_id"),),
        ).fetchone()
        if project is None:
            raise RuntimeError("Phase 9 database Project is missing")
        subtitle_record = _require_record(evidence.get("subtitle"), "subtitle")
        output_record = _require_record(evidence.get("output"), "output")
        if (
            project["subtitle_asset_id"] != subtitle_record.get("asset_id")
            or project["final_video_asset_id"] != output_record.get("asset_id")
        ):
            raise RuntimeError("Phase 9 database Project pointers mismatch")
        assets = {
            row["id"]: row
            for row in connection.execute(
                "SELECT id, project_id, kind, path, mime_type, metadata_json FROM assets "
                "WHERE id IN (?, ?)",
                (project["subtitle_asset_id"], project["final_video_asset_id"]),
            )
        }
        if set(assets) != {project["subtitle_asset_id"], project["final_video_asset_id"]}:
            raise RuntimeError("Phase 9 database output Assets are missing")
        subtitle_asset = assets[project["subtitle_asset_id"]]
        final_asset = assets[project["final_video_asset_id"]]
        if (
            subtitle_asset["project_id"] != evidence["project_id"]
            or subtitle_asset["kind"] != "SUBTITLE"
            or Path(subtitle_asset["path"]).resolve() != subtitle
            or subtitle_asset["mime_type"] != "application/x-subrip"
            or final_asset["project_id"] != evidence["project_id"]
            or final_asset["kind"] != "FINAL_VIDEO"
            or Path(final_asset["path"]).resolve() != immutable
            or final_asset["mime_type"] != "video/mp4"
        ):
            raise RuntimeError("Phase 9 database Asset binding mismatch")
        subtitle_metadata = json.loads(subtitle_asset["metadata_json"])
        final_metadata = json.loads(final_asset["metadata_json"])
        if subtitle_metadata.get("sha256") != _sha256(subtitle):
            raise RuntimeError("Phase 9 subtitle Asset metadata hash mismatch")
        if final_metadata.get("sha256") != _sha256(immutable):
            raise RuntimeError("Phase 9 final Asset metadata hash mismatch")
        timeline = final_metadata.get("timeline")
        if not isinstance(timeline, list) or len(timeline) != 2:
            raise RuntimeError("Phase 9 final Asset timeline metadata mismatch")
        shots = list(
            connection.execute(
                "SELECT shots.id, shots.[order] AS shot_order, shots.duration, shots.requires_lip_sync, "
                "shots.speaker_visible, shots.video_asset_id, shots.lipsync_asset_id "
                "FROM shots JOIN scenes ON shots.scene_id = scenes.id "
                "WHERE scenes.project_id = ? ORDER BY scenes.[order], shots.[order]",
                (evidence["project_id"],),
            )
        )
        if (
            len(shots) != 2
            or [row["id"] for row in shots] != evidence.get("shot_ids")
            or [row["shot_order"] for row in shots] != [1, 2]
            or [row["duration"] for row in shots] != [2.8, 3.0]
            or not shots[0]["requires_lip_sync"]
            or not shots[0]["speaker_visible"]
            or shots[0]["lipsync_asset_id"] != timeline[0].get("asset_id")
            or shots[1]["requires_lip_sync"]
            or shots[1]["video_asset_id"] != timeline[1].get("asset_id")
        ):
            raise RuntimeError("Phase 9 database Shot timeline mismatch")
        dialogue = connection.execute(
            "SELECT text, duration, start_time, end_time, audio_asset_id FROM dialogues "
            "WHERE shot_id = ?",
            (shots[0]["id"],),
        ).fetchone()
        if (
            dialogue is None
            or dialogue["text"] != DIALOGUE_TEXT
            or dialogue["duration"] != 2.48
            or dialogue["start_time"] != 0.0
            or dialogue["end_time"] != 2.48
            or dialogue["audio_asset_id"] != timeline[0]["audio"][0].get("asset_id")
        ):
            raise RuntimeError("Phase 9 database Dialogue binding mismatch")
        if connection.execute(
            "SELECT COUNT(*) FROM dialogues WHERE shot_id IN (?, ?)",
            (shots[0]["id"], shots[1]["id"]),
        ).fetchone()[0] != 1:
            raise RuntimeError("Phase 9 database Dialogue count mismatch")
        source_ids = [
            timeline[0]["asset_id"],
            timeline[0]["audio"][0]["asset_id"],
            timeline[1]["asset_id"],
        ]
        source_assets = {
            row["id"]: row
            for row in connection.execute(
                "SELECT id, project_id, kind, path, metadata_json FROM assets "
                "WHERE id IN (?, ?, ?)",
                source_ids,
            )
        }
        source_evidence = evidence["sources"]
        expected_sources = [
            (source_ids[0], "LIPSYNC", source_evidence["phase8_lipsync"]),
            (source_ids[1], "AUDIO", source_evidence["phase7_audio"]),
            (source_ids[2], "VIDEO", source_evidence["phase5_video"]),
        ]
        for asset_id, kind, source in expected_sources:
            asset = source_assets.get(asset_id)
            metadata = json.loads(asset["metadata_json"]) if asset is not None else {}
            if (
                asset is None
                or asset["project_id"] != evidence["project_id"]
                or asset["kind"] != kind
                or Path(asset["path"]).resolve() != Path(source["path"]).resolve()
                or metadata.get("sha256") != source["sha256"]
                or metadata.get("locked") is not True
            ):
                raise RuntimeError("Phase 9 database locked source Asset mismatch")
        manifests = {
            row["asset_id"]: row
            for row in connection.execute(
                "SELECT asset_id, provider, workflow_name, workflow_hash, input_assets, output_asset "
                "FROM generation_manifests WHERE asset_id IN (?, ?)",
                (project["subtitle_asset_id"], project["final_video_asset_id"]),
            )
        }
        if set(manifests) != set(assets):
            raise RuntimeError("Phase 9 database GenerationManifests are missing")
        subtitle_manifest = manifests[project["subtitle_asset_id"]]
        final_manifest = manifests[project["final_video_asset_id"]]
        first_timeline_shot = timeline[0]
        expected_subtitle_inputs = [first_timeline_shot["audio"][0]["asset_id"]]
        expected_final_inputs = [
            first_timeline_shot["asset_id"],
            first_timeline_shot["audio"][0]["asset_id"],
            timeline[1]["asset_id"],
            project["subtitle_asset_id"],
        ]
        if (
            subtitle_manifest["provider"] != "local"
            or subtitle_manifest["workflow_name"] != "subtitle_srt_v1"
            or json.loads(subtitle_manifest["input_assets"]) != expected_subtitle_inputs
            or subtitle_manifest["output_asset"] != project["subtitle_asset_id"]
            or final_manifest["provider"] != "ffmpeg"
            or final_manifest["workflow_name"] != "final_render_v1"
            or json.loads(final_manifest["input_assets"]) != expected_final_inputs
            or final_manifest["output_asset"] != project["final_video_asset_id"]
            or final_manifest["workflow_hash"] != subtitle_manifest["workflow_hash"]
        ):
            raise RuntimeError("Phase 9 database manifest provenance/input order mismatch")
    finally:
        try:
            connection.close()
        except UnboundLocalError:
            pass


def _reject_temps_and_owned_processes(run_dir: Path) -> None:
    if any(path.is_file() for path in run_dir.rglob("*.tmp")):
        raise RuntimeError("Phase 9 temporary output remains")
    marker = str(run_dir).lower()
    for process in psutil.process_iter(["name", "cmdline"]):
        try:
            name = (process.info.get("name") or "").lower()
            command = " ".join(process.info.get("cmdline") or []).lower()
        except psutil.NoSuchProcess:
            continue
        except (psutil.AccessDenied, OSError) as exc:
            raise RuntimeError(
                f"Phase 9 could not inspect running processes: {process.pid}"
            ) from exc
        if name.startswith("ffmpeg") and marker in command:
            raise RuntimeError(f"Phase 9 owned FFmpeg process remains: {process.pid}")


def resolve_approved_path(value: str, roots: tuple[Path, ...], label: str) -> Path:
    try:
        path = Path(value).resolve(strict=True)
    except (OSError, TypeError, ValueError) as exc:
        raise RuntimeError(f"{label} path is missing or invalid") from exc
    for root in roots:
        try:
            path.relative_to(Path(root).resolve())
            return path
        except ValueError:
            continue
    raise RuntimeError(f"{label} path is outside approved roots: {path}")


def verify_phase9(
    evidence_path: Path,
    *,
    evidence_root: Path,
    decoder: Callable[[Path], None] | None = None,
    probe: Callable[[Path], AVInfo] | None = None,
    runtime_lock: Path | None = ROOT / "runtime" / "runtime-lock.yaml",
) -> dict:
    root = Path(evidence_root).resolve()
    evidence_path = resolve_approved_path(str(evidence_path), (root,), "evidence")
    try:
        evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise FileNotFoundError(f"Phase 9 evidence is missing: {evidence_path}") from exc
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Phase 9 evidence is invalid: {evidence_path}") from exc
    if not isinstance(evidence, dict) or evidence.get("phase") != 9:
        raise RuntimeError("Phase 9 evidence payload is invalid")
    if evidence.get("alias_status") != "READY":
        raise RuntimeError("Phase 9 evidence requires alias_status READY")
    if evidence.get("visual_review") != "approved":
        raise RuntimeError("Phase 9 evidence requires approved visual review")
    if runtime_lock is not None:
        _verify_runtime_lock(evidence_path, evidence, runtime_lock)
    artifact_root = (root / "artifacts" / "phase9").resolve()
    runtime_root = (root / ".runtime" / "phase9").resolve()
    source_roots = (Path("E:/kang/github/Movie/artifacts").resolve(),)

    sources = _require_record(evidence.get("sources"), "sources")
    locked = {
        "phase8_lipsync": (PHASE8_LIPSYNC, PHASE8_LIPSYNC_SHA256),
        "phase7_audio": (PHASE7_AUDIO, PHASE7_AUDIO_SHA256),
        "phase5_video": (PHASE5_VIDEO, PHASE5_VIDEO_SHA256),
    }
    for label, (locked_path, locked_hash) in locked.items():
        path = _bound_file(sources.get(label), source_roots, label)
        if path != locked_path.resolve() or _sha256(path) != locked_hash:
            raise RuntimeError(f"Phase 9 {label} does not match locked source")

    database = _bound_file(evidence.get("database"), (runtime_root,), "database")
    subtitle_record = _require_record(evidence.get("subtitle"), "subtitle")
    subtitle = _bound_file(subtitle_record, (artifact_root,), "subtitle")
    try:
        subtitle_text = subtitle.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise RuntimeError("Phase 9 SRT is invalid UTF-8") from exc
    expected_srt = f"1\n00:00:00,000 --> 00:00:02,480\n{DIALOGUE_TEXT}\n"
    if subtitle_text.replace("\r\n", "\n") != expected_srt:
        raise RuntimeError("Phase 9 SRT timing/text is invalid")
    if subtitle_record.get("text") != DIALOGUE_TEXT or subtitle_record.get("cue") != "00:00:00,000 --> 00:00:02,480":
        raise RuntimeError("Phase 9 evidence SRT binding mismatch")

    output = _require_record(evidence.get("output"), "output")
    immutable = resolve_approved_path(output.get("immutable_path"), (artifact_root,), "immutable output")
    alias = resolve_approved_path(output.get("alias_path"), (artifact_root,), "canonical alias")
    expected_hash = output.get("sha256")
    if _sha256(immutable) != expected_hash or _sha256(alias) != expected_hash:
        raise RuntimeError("Phase 9 immutable output/canonical alias SHA256 mismatch")
    manifest = _bound_file(evidence.get("provider_manifest"), (artifact_root,), "provider manifest")
    provider_payload = json.loads(manifest.read_text(encoding="utf-8"))
    if (
        provider_payload.get("provider") != "ffmpeg"
        or provider_payload.get("workflow") != "final_render_v1"
        or provider_payload.get("project_id") != evidence.get("project_id")
        or provider_payload.get("output_path") != str(immutable)
        or provider_payload.get("output_sha256") != expected_hash
        or provider_payload.get("srt_sha256") != _sha256(subtitle)
        or provider_payload.get("cue_count") != 1
    ):
        raise RuntimeError("Phase 9 provider manifest binding mismatch")

    ffmpeg = _require_record(evidence.get("ffmpeg"), "ffmpeg")
    resolved_ffmpeg = shutil.which("ffmpeg")
    if resolved_ffmpeg is None:
        raise RuntimeError("Phase 9 FFmpeg runtime is unavailable")
    ffmpeg_path = Path(resolved_ffmpeg).resolve(strict=True)
    if Path(ffmpeg.get("executable", "")).resolve() != ffmpeg_path:
        raise RuntimeError("Phase 9 FFmpeg executable identity mismatch")
    version = subprocess.run(
        [str(ffmpeg_path), "-hide_banner", "-version"], capture_output=True, text=True,
        timeout=30, shell=False,
    )
    if version.returncode != 0 or ffmpeg.get("version") not in version.stdout or ffmpeg.get("configuration") not in version.stdout:
        raise RuntimeError("Phase 9 FFmpeg runtime identity mismatch")
    font = _require_record(evidence.get("font"), "font")
    font_path = resolve_approved_path(font.get("path"), (Path("C:/Windows/Fonts"),), "font")
    if font_path != Path("C:/Windows/Fonts/msyh.ttc").resolve():
        raise RuntimeError("Phase 9 locked font path mismatch")
    if font.get("size") != font_path.stat().st_size or font.get("sha256") != _sha256(font_path):
        raise RuntimeError("Phase 9 locked font identity mismatch")

    info = (
        probe(alias)
        if probe is not None
        else probe_av(alias, executable=str(ffmpeg_path.with_name("ffprobe.exe")))
    )
    _verify_profile(info, evidence)
    if output.get("frames") != info.video.frames:
        raise RuntimeError("Phase 9 evidence frame count mismatch")
    if (
        abs(output.get("video_duration", -1) - info.video.duration) > 1e-9
        or abs(output.get("audio_duration", -1) - info.audio.duration) > 1e-9
    ):
        raise RuntimeError("Phase 9 evidence A/V duration mismatch")
    (decoder or (lambda path: _decode(path, str(ffmpeg_path))))(alias)
    _read_database(database, evidence, subtitle, immutable)

    review = _require_record(evidence.get("review"), "review")
    for label in ("pre_cue", "in_cue", "post_cue", "pre_boundary", "post_boundary", "contact_sheet"):
        _bound_file(review.get(label), (artifact_root,), f"review {label}")
    if review["pre_boundary"]["sha256"] == review["post_boundary"]["sha256"]:
        raise RuntimeError("Phase 9 review does not prove Shot transition")
    resources = _bound_file(evidence.get("resources"), (artifact_root,), "resources")
    resource_payload = json.loads(resources.read_text(encoding="utf-8"))
    if resource_payload.get("peaks") != evidence["resources"].get("peaks"):
        raise RuntimeError("Phase 9 resource evidence mismatch")
    _verify_git_identity(evidence.get("git"))
    _reject_temps_and_owned_processes(evidence_path.parent)
    return {
        "status": "VERIFIED",
        "output_sha256": expected_hash,
        "frames": info.video.frames,
        "alias_status": "READY",
        "visual_review": evidence.get("visual_review"),
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("evidence")
    parser.add_argument("--evidence-root", required=True)
    return parser


def main() -> None:
    args = _parser().parse_args()
    result = verify_phase9(Path(args.evidence), evidence_root=Path(args.evidence_root))
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
