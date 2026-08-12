from __future__ import annotations

import json
import shutil
import sqlite3
import subprocess
from argparse import Namespace
from dataclasses import replace
from pathlib import Path

import pytest

from scripts.smoke_phase9 import (
    PHASE5_VIDEO_SHA256,
    PHASE7_AUDIO_SHA256,
    PHASE8_LIPSYNC_SHA256,
    build_evidence_paths,
    seed_phase9_database,
    run_smoke,
    validate_run_id,
)
from scripts.verify_phase9 import git_identity, resolve_approved_path, verify_phase9


def verify_candidate(*args, **kwargs):
    kwargs["runtime_lock"] = None
    return verify_phase9(*args, **kwargs)


@pytest.fixture(scope="module")
def real_evidence(tmp_path_factory):
    root = tmp_path_factory.mktemp("phase9-real-smoke")
    result = run_smoke(
        Namespace(evidence_root=str(root), run_id="pytest-real", visual_review="approved")
    )
    return root, Path(result["evidence_path"]), result


@pytest.mark.parametrize(
    "run_id",
    ["../escape", "a/b", "a\\b", "C:drive", ".", "..", "CON", "x" * 129, "中文"],
)
def test_run_id_rejects_unsafe_destinations(run_id: str) -> None:
    with pytest.raises(ValueError, match="run ID|reserved"):
        validate_run_id(run_id)


def test_evidence_paths_are_contained_and_separate(tmp_path: Path) -> None:
    paths = build_evidence_paths(tmp_path, "safe-run-01")

    assert paths["run_dir"] == (tmp_path / "artifacts" / "phase9" / "safe-run-01").resolve()
    assert paths["database"] == (
        tmp_path / ".runtime" / "phase9" / "phase9-smoke-safe-run-01.db"
    ).resolve()
    assert all(path.is_absolute() for path in paths.values())
    assert paths["database"] != paths["evidence"]


def test_resolve_approved_path_rejects_outside_root(tmp_path: Path) -> None:
    outside = tmp_path.parent / "outside.mp4"
    outside.write_bytes(b"outside")

    with pytest.raises(RuntimeError, match="outside approved roots"):
        resolve_approved_path(str(outside), (tmp_path,), "output")


def test_real_seed_requires_and_records_locked_sources(tmp_path: Path) -> None:
    seeded = seed_phase9_database(tmp_path / "phase9.db")

    assert seeded.source_hashes == {
        "phase8_lipsync": PHASE8_LIPSYNC_SHA256,
        "phase7_audio": PHASE7_AUDIO_SHA256,
        "phase5_video": PHASE5_VIDEO_SHA256,
    }
    assert seeded.database_path.is_file()
    assert len(seeded.shot_ids) == 2


def test_seed_rejects_unlocked_source_override(tmp_path: Path) -> None:
    unlocked = tmp_path / "unlocked.mp4"
    unlocked.write_bytes(b"not locked")

    with pytest.raises((TypeError, RuntimeError), match="locked|SHA256"):
        seed_phase9_database(tmp_path / "phase9.db", phase8_lipsync=unlocked)


def test_verifier_rejects_missing_evidence(tmp_path: Path) -> None:
    with pytest.raises((FileNotFoundError, RuntimeError), match="evidence"):
        verify_candidate(tmp_path / "missing-evidence.json", evidence_root=tmp_path)


def test_verifier_rejects_degraded_before_decode(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    evidence = tmp_path / "evidence.json"
    evidence.write_text(json.dumps({"phase": 9, "alias_status": "DEGRADED"}), encoding="utf-8")
    decoded = []

    with pytest.raises(RuntimeError, match="READY"):
        verify_candidate(
            evidence,
            evidence_root=tmp_path,
            decoder=lambda path: decoded.append(path),
        )

    assert decoded == []


def test_real_smoke_and_read_only_verifier_pass_full_decode(real_evidence) -> None:
    root, evidence_path, result = real_evidence
    database = Path(result["database"]["path"])
    before = (database.read_bytes(), evidence_path.read_bytes())

    verified = verify_phase9(evidence_path, evidence_root=root, runtime_lock=None)

    assert verified == {
        "status": "VERIFIED",
        "output_sha256": result["output"]["sha256"],
        "frames": 145,
        "alias_status": "READY",
        "visual_review": "approved",
    }
    assert (database.read_bytes(), evidence_path.read_bytes()) == before


def test_verifier_rejects_internally_valid_but_unlocked_bundle(real_evidence) -> None:
    root, evidence_path, _ = real_evidence

    with pytest.raises(RuntimeError, match="runtime lock"):
        verify_phase9(evidence_path, evidence_root=root)


def test_git_identity_uses_shared_repository_root_not_worktree() -> None:
    identity = git_identity()

    assert Path(identity["repository"]).resolve() == Path("E:/kang/github/Movie").resolve()
    assert len(identity["commit"]) == 40


def _mutated_evidence(real_evidence, tmp_path: Path, mutate) -> tuple[Path, Path]:
    root, evidence_path, _ = real_evidence
    payload = json.loads(evidence_path.read_text(encoding="utf-8"))
    mutate(payload)
    path = evidence_path.parent / f"mutated-{tmp_path.name}.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return root, path


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (lambda evidence: evidence["output"].update(sha256="0" * 64), "SHA256"),
        (lambda evidence: evidence["subtitle"].update(cue="00:00:00,100 --> 00:00:02,480"), "SRT binding"),
        (lambda evidence: evidence["profile"].update(frames=144), "profile"),
        (lambda evidence: evidence["font"].update(sha256="0" * 64), "font"),
        (lambda evidence: evidence["resources"].update(sha256="0" * 64), "resources SHA256"),
        (lambda evidence: evidence["review"]["in_cue"].update(sha256="0" * 64), "review in_cue SHA256"),
    ],
)
def test_verifier_rejects_evidence_binding_tampering(
    real_evidence,
    tmp_path: Path,
    mutation,
    message: str,
) -> None:
    root, evidence = _mutated_evidence(real_evidence, tmp_path, mutation)

    with pytest.raises(RuntimeError, match=message):
        verify_candidate(evidence, evidence_root=root, decoder=lambda _path: None)


def test_verifier_rejects_invalid_srt_bytes(real_evidence) -> None:
    root, evidence_path, result = real_evidence
    subtitle = Path(result["subtitle"]["path"])
    original = subtitle.read_bytes()
    try:
        subtitle.write_bytes(b"invalid srt")
        with pytest.raises(RuntimeError, match="subtitle SHA256|SRT"):
            verify_candidate(evidence_path, evidence_root=root, decoder=lambda _path: None)
    finally:
        subtitle.write_bytes(original)


def test_verifier_rejects_alias_mismatch(real_evidence) -> None:
    root, evidence_path, result = real_evidence
    alias = Path(result["output"]["alias_path"])
    original = alias.read_bytes()
    try:
        alias.write_bytes(b"stale alias")
        with pytest.raises(RuntimeError, match="alias SHA256"):
            verify_candidate(evidence_path, evidence_root=root, decoder=lambda _path: None)
    finally:
        alias.write_bytes(original)


def test_verifier_rejects_database_pointer_mismatch(real_evidence, tmp_path: Path) -> None:
    root, evidence_path, result = real_evidence
    original = Path(result["database"]["path"])
    copied = original.parent / "tampered-pointer.db"
    shutil.copyfile(original, copied)
    with sqlite3.connect(copied) as connection:
        connection.execute("UPDATE projects SET final_video_asset_id = NULL")
    payload = json.loads(evidence_path.read_text(encoding="utf-8"))
    import hashlib

    payload["database"] = {
        "path": str(copied),
        "sha256": hashlib.sha256(copied.read_bytes()).hexdigest(),
    }
    mutated = evidence_path.parent / "tampered-db-evidence.json"
    mutated.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(RuntimeError, match="pointers"):
        verify_candidate(mutated, evidence_root=root, decoder=lambda _path: None)


def test_verifier_rejects_wrong_probed_frame_count(real_evidence) -> None:
    root, evidence_path, result = real_evidence
    from app.services.media_probe import probe_av

    info = probe_av(Path(result["output"]["alias_path"]))
    wrong = replace(info, video=replace(info.video, frames=144))

    with pytest.raises(RuntimeError, match="profile/frame"):
        verify_candidate(
            evidence_path,
            evidence_root=root,
            probe=lambda _path: wrong,
            decoder=lambda _path: None,
        )


def test_verifier_propagates_full_decode_failure(real_evidence) -> None:
    root, evidence_path, _ = real_evidence

    with pytest.raises(RuntimeError, match="decode sentinel"):
        verify_candidate(
            evidence_path,
            evidence_root=root,
            decoder=lambda _path: (_ for _ in ()).throw(RuntimeError("decode sentinel")),
        )


def test_full_decode_escalates_decoder_warnings_to_errors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from scripts import verify_phase9 as verifier

    captured = []

    def record_run(argv, **_kwargs):
        captured.append(argv)
        return subprocess.CompletedProcess(argv, 0, b"", b"")

    monkeypatch.setattr(verifier.subprocess, "run", record_run)
    verifier._decode(tmp_path / "candidate.mp4", "ffmpeg.exe")

    assert captured
    assert "-xerror" in captured[0]


def test_verifier_rejects_owned_temp(real_evidence) -> None:
    root, evidence_path, _ = real_evidence
    temporary = evidence_path.parent / "owned-output.tmp"
    temporary.write_bytes(b"partial")
    try:
        with pytest.raises(RuntimeError, match="temporary"):
            verify_candidate(evidence_path, evidence_root=root, decoder=lambda _path: None)
    finally:
        temporary.unlink()


def test_verifier_rejects_live_owned_ffmpeg(real_evidence, monkeypatch) -> None:
    root, evidence_path, _ = real_evidence
    from scripts import verify_phase9 as verifier

    class Process:
        pid = 123
        info = {"name": "ffmpeg.exe", "cmdline": ["ffmpeg", str(evidence_path.parent)]}

    monkeypatch.setattr(verifier.psutil, "process_iter", lambda _attrs: [Process()])

    with pytest.raises(RuntimeError, match="owned FFmpeg"):
        verify_candidate(evidence_path, evidence_root=root, decoder=lambda _path: None)


def test_owned_process_inspection_failure_is_not_treated_as_clean(
    real_evidence,
    monkeypatch,
) -> None:
    root, evidence_path, _ = real_evidence
    from scripts import verify_phase9 as verifier

    class Process:
        pid = 456

        @property
        def info(self):
            raise verifier.psutil.AccessDenied(pid=self.pid)

    monkeypatch.setattr(verifier.psutil, "process_iter", lambda _attrs: [Process()])

    with pytest.raises(RuntimeError, match="inspect running processes"):
        verify_candidate(evidence_path, evidence_root=root, decoder=lambda _path: None)


def test_verifier_rejects_provider_manifest_binding_mismatch(real_evidence) -> None:
    root, evidence_path, result = real_evidence
    manifest = Path(result["provider_manifest"]["path"])
    original = manifest.read_bytes()
    try:
        payload = json.loads(original)
        payload["cue_count"] = 0
        manifest.write_text(json.dumps(payload), encoding="utf-8")
        with pytest.raises(RuntimeError, match="provider manifest SHA256|binding"):
            verify_candidate(evidence_path, evidence_root=root, decoder=lambda _path: None)
    finally:
        manifest.write_bytes(original)


def test_verifier_rejects_pending_visual_review_before_decode(
    real_evidence,
    tmp_path: Path,
) -> None:
    root, evidence = _mutated_evidence(
        real_evidence,
        tmp_path,
        lambda payload: payload.update(visual_review="pending"),
    )
    decoded = []

    with pytest.raises(RuntimeError, match="approved visual review"):
        verify_candidate(evidence, evidence_root=root, decoder=lambda path: decoded.append(path))

    assert decoded == []
