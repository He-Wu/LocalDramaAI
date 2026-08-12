"""Verify locked PHASE 8 evidence without starting any AI service."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any, Callable, Sequence

import yaml

from app.services.media_probe import AVInfo, probe_av
from scripts.smoke_phase8 import (
    AI_PORTS,
    attest_musetalk_runtime,
    assert_ports_free,
    decode_media,
    resource_peaks,
    sha256_file,
    validate_database_evidence,
    validate_media_profile,
)


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_LOCK = ROOT / "runtime" / "runtime-lock.yaml"
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


def resolve_locked_path(value: object, allowed_roots: Sequence[Path], label: str) -> Path:
    """Resolve a nonempty evidence file and reject traversal/symlink escapes."""
    if not isinstance(value, str) or not value.strip():
        raise RuntimeError(f"PHASE 8 runtime lock is missing {label} path")
    roots = tuple(Path(root).resolve() for root in allowed_roots)
    candidate = Path(value)
    if not candidate.is_absolute():
        candidate = roots[0] / candidate
    try:
        resolved = candidate.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise RuntimeError(f"locked {label} path is missing: {candidate}") from exc
    if not any(_is_relative_to(resolved, root) for root in roots):
        raise RuntimeError(f"locked {label} path is outside approved evidence roots: {resolved}")
    if not resolved.is_file() or resolved.stat().st_size <= 0:
        raise RuntimeError(f"locked {label} path is not a nonempty file: {resolved}")
    return resolved


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _locked_sha256(path: Path, expected: object, label: str) -> str:
    if not isinstance(expected, str) or _SHA256.fullmatch(expected.lower()) is None:
        raise RuntimeError(f"PHASE 8 runtime lock has an invalid {label} SHA256")
    measured = sha256_file(path)
    if measured != expected.lower():
        raise RuntimeError(
            f"locked {label} SHA256 mismatch: expected {expected.lower()}, got {measured}"
        )
    return measured


def _load_yaml_object(path: Path) -> dict[str, Any]:
    try:
        payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise RuntimeError(f"cannot read runtime lock: {path}") from exc
    if not isinstance(payload, dict):
        raise RuntimeError("runtime lock must contain a YAML mapping")
    return payload


def _load_json_object(path: Path, label: str) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"locked {label} is not valid JSON: {path}") from exc
    if not isinstance(payload, dict):
        raise RuntimeError(f"locked {label} must be a JSON object")
    return payload


def _verify_file_descriptor(
    value: object,
    allowed_roots: Sequence[Path],
    label: str,
) -> Path:
    if not isinstance(value, dict):
        raise RuntimeError(f"PHASE 8 evidence is missing {label}")
    path = resolve_locked_path(value.get("path"), allowed_roots, label)
    _locked_sha256(path, value.get("sha256"), label)
    return path


def _same_path(left: Path, right: Path, label: str) -> None:
    if left != right:
        raise RuntimeError(f"locked {label} paths disagree: {left} != {right}")


def _runtime_identity_matches(evidence: dict[str, Any], actual: dict[str, Any]) -> bool:
    """Compare identity, allowing only the project worktree's lock path to move."""
    evidence_copy = dict(evidence)
    actual_copy = dict(actual)
    evidence_lock = evidence_copy.get("model_lock")
    actual_lock = actual_copy.get("model_lock")
    if not isinstance(evidence_lock, dict) or not isinstance(actual_lock, dict):
        return False
    evidence_copy["model_lock"] = {"sha256": evidence_lock.get("sha256")}
    actual_copy["model_lock"] = {"sha256": actual_lock.get("sha256")}
    return evidence_copy == actual_copy


def verify_phase8(
    lock_path: Path = DEFAULT_LOCK,
    *,
    repo_root: Path = ROOT,
    evidence_root: Path | None = None,
    probe: Callable[[Path], AVInfo] = probe_av,
    decoder: Callable[[Path], None] = decode_media,
    is_listening: Callable[[int], bool],
) -> dict[str, Any]:
    repo_root = Path(repo_root).resolve()
    evidence_root = repo_root if evidence_root is None else Path(evidence_root).resolve()
    allowed_roots = tuple(dict.fromkeys((repo_root, evidence_root)))
    payload = _load_yaml_object(Path(lock_path).resolve())
    musetalk = payload.get("musetalk")
    if not isinstance(musetalk, dict):
        raise RuntimeError("runtime lock has no MuseTalk runtime mapping")
    repository_value = musetalk.get("path")
    if not isinstance(repository_value, str) or not repository_value.strip():
        raise RuntimeError("runtime lock has no MuseTalk repository path")
    try:
        musetalk_repository = Path(repository_value).resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise RuntimeError("runtime lock MuseTalk repository path is missing") from exc
    if not musetalk_repository.is_dir():
        raise RuntimeError("runtime lock MuseTalk repository path is not a directory")
    expected_commit = musetalk.get("commit_or_release")
    model_lock_path = (repo_root / "scripts" / "musetalk-models.lock.json").resolve()
    runtime_identity = attest_musetalk_runtime(
        musetalk_repository,
        expected_commit,
        model_lock_path,
    )
    configured_manifest_value = musetalk.get("model_hash_manifest")
    if not isinstance(configured_manifest_value, str) or not configured_manifest_value.strip():
        raise RuntimeError("runtime lock has no MuseTalk model hash manifest path")
    try:
        configured_manifest = Path(configured_manifest_value).resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise RuntimeError("runtime lock MuseTalk model hash manifest is missing") from exc
    if configured_manifest != Path(runtime_identity["model_manifest"]["path"]):
        raise RuntimeError("runtime lock MuseTalk model manifest path disagrees with attestation")
    configured_manifest_sha256 = musetalk.get("model_hash_manifest_sha256")
    if configured_manifest_sha256 != runtime_identity["model_manifest"]["sha256"]:
        raise RuntimeError("runtime lock MuseTalk model manifest SHA256 disagrees with attestation")

    verification = payload.get("verification")
    if not isinstance(verification, dict):
        raise RuntimeError("runtime lock has no verification mapping")
    if str(verification.get("phase")) != "8":
        raise RuntimeError("runtime lock verification phase is not 8")
    if verification.get("phase8_visual_review") != "approved":
        raise RuntimeError("PHASE 8 visual review is not approved in the runtime lock")

    output_path = resolve_locked_path(
        verification.get("phase8_lipsync"), allowed_roots, "lip-sync output"
    )
    output_sha256 = _locked_sha256(
        output_path,
        verification.get("phase8_lipsync_sha256"),
        "lip-sync output",
    )
    database_path = resolve_locked_path(
        verification.get("phase8_database"), allowed_roots, "database"
    )
    provider_manifest_path = resolve_locked_path(
        verification.get("phase8_provider_manifest"),
        allowed_roots,
        "provider manifest",
    )
    provider_manifest_sha256 = _locked_sha256(
        provider_manifest_path,
        verification.get("phase8_provider_manifest_sha256"),
        "provider manifest",
    )
    evidence_path = resolve_locked_path(
        verification.get("phase8_evidence"), allowed_roots, "evidence JSON"
    )
    evidence_sha256 = _locked_sha256(
        evidence_path,
        verification.get("phase8_evidence_sha256"),
        "evidence JSON",
    )

    evidence = _load_json_object(evidence_path, "evidence JSON")
    if evidence.get("phase") != 8:
        raise RuntimeError("locked evidence JSON phase is not 8")
    if evidence.get("target_duration") != 2.78:
        raise RuntimeError("locked evidence JSON target duration is not 2.78 seconds")
    project_id = evidence.get("project_id")
    shot_id = evidence.get("shot_id")
    if not isinstance(project_id, str) or not project_id:
        raise RuntimeError("locked evidence JSON project_id is missing")
    if not isinstance(shot_id, str) or not shot_id:
        raise RuntimeError("locked evidence JSON shot_id is missing")
    evidence_runtime = evidence.get("musetalk_runtime")
    if not isinstance(evidence_runtime, dict) or not _runtime_identity_matches(
        evidence_runtime, runtime_identity
    ):
        raise RuntimeError("locked evidence MuseTalk runtime identity disagrees with current attestation")
    evidence_database = resolve_locked_path(
        evidence.get("database"), allowed_roots, "evidence database"
    )
    _same_path(evidence_database, database_path, "database")

    evidence_output = _verify_file_descriptor(
        evidence.get("output"), allowed_roots, "evidence output"
    )
    _same_path(evidence_output, output_path, "output")
    if evidence["output"].get("sha256") != output_sha256:
        raise RuntimeError("runtime lock and evidence JSON output SHA256 disagree")
    evidence_manifest = _verify_file_descriptor(
        evidence.get("provider_manifest"), allowed_roots, "evidence provider manifest"
    )
    _same_path(evidence_manifest, provider_manifest_path, "provider manifest")
    if evidence["provider_manifest"].get("sha256") != provider_manifest_sha256:
        raise RuntimeError("runtime lock and evidence JSON provider manifest SHA256 disagree")
    _verify_file_descriptor(evidence.get("source_video"), allowed_roots, "source video")
    _verify_file_descriptor(evidence.get("source_audio"), allowed_roots, "source audio")

    review = evidence.get("review")
    if not isinstance(review, dict) or review.get("status") != "approved":
        raise RuntimeError("locked evidence JSON visual review is not approved")
    for key in ("start", "middle", "end", "mouth_contact_sheet"):
        _verify_file_descriptor(review.get(key), allowed_roots, f"review {key}")

    resources = evidence.get("resources")
    if not isinstance(resources, dict):
        raise RuntimeError("locked evidence JSON resource evidence is missing")
    resources_path = _verify_file_descriptor(resources, allowed_roots, "resources")
    resources_payload = _load_json_object(resources_path, "resources")
    samples = resources_payload.get("samples")
    if not isinstance(samples, list):
        raise RuntimeError("locked resource evidence has no samples")
    measured_peaks = resource_peaks(samples)
    if resources_payload.get("peaks") != measured_peaks or resources.get("peaks") != measured_peaks:
        raise RuntimeError("locked resource peaks disagree with samples")

    assert_ports_free(AI_PORTS, is_listening=is_listening)
    media = probe(output_path)
    validate_media_profile(media, target_duration=2.78)
    decoder(output_path)
    database = validate_database_evidence(
        database_path,
        project_id,
        shot_id,
        output_path,
        provider_manifest_path,
    )
    database_links = evidence.get("database_links")
    if not isinstance(database_links, dict):
        raise RuntimeError("locked evidence JSON database links are missing")
    for key in (
        "project_id",
        "shot_id",
        "dialogue_id",
        "video_asset_id",
        "audio_asset_id",
        "lipsync_asset_id",
        "generation_manifest_id",
        "input_assets",
        "provider_version",
        "source_video_sha256",
        "source_audio_sha256",
        "output_sha256",
        "provider_manifest_sha256",
    ):
        if database_links.get(key) != database.get(key):
            raise RuntimeError(f"locked database link evidence disagrees for {key}")
    assert_ports_free(AI_PORTS, is_listening=is_listening)
    return {
        "phase": 8,
        "output": str(output_path),
        "output_sha256": output_sha256,
        "database": str(database_path),
        "provider_manifest": str(provider_manifest_path),
        "provider_manifest_sha256": provider_manifest_sha256,
        "evidence": str(evidence_path),
        "evidence_sha256": evidence_sha256,
        "visual_review": "approved",
        "media": {
            "format_name": media.format_name,
            "video_codec": media.video.codec,
            "pixel_format": media.video.pixel_format,
            "width": media.video.width,
            "height": media.video.height,
            "fps": media.video.fps,
            "video_duration": media.video.duration,
            "audio_codec": media.audio.codec,
            "audio_duration": media.audio.duration,
        },
        "database_links": database,
        "resource_peaks": measured_peaks,
        "musetalk_runtime": runtime_identity,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime-lock", type=Path, default=DEFAULT_LOCK)
    parser.add_argument(
        "--evidence-root",
        type=Path,
        default=ROOT,
        help="Explicit second approved root for ignored runtime evidence.",
    )
    parser.add_argument("--ffmpeg", default="ffmpeg")
    parser.add_argument("--ffprobe", default="ffprobe")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    result = verify_phase8(
        args.runtime_lock,
        repo_root=ROOT,
        evidence_root=args.evidence_root,
        probe=lambda path: probe_av(path, executable=args.ffprobe),
        decoder=lambda path: decode_media(path, ffmpeg=args.ffmpeg),
        is_listening=_default_port_check,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


def _default_port_check(port: int) -> bool:
    from scripts.smoke_phase8 import is_port_listening

    return is_port_listening(port)


if __name__ == "__main__":
    raise SystemExit(main())
