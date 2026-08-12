import hashlib
import json
import subprocess
import wave
from dataclasses import replace
from pathlib import Path

import pytest
import yaml

from app.db.session import session_scope
from app.models import Asset, Dialogue, GenerationManifest, Scene, Shot
from app.services.audio_probe import WavInfo
from app.services.media_probe import AVInfo, AudioStreamInfo, VideoStreamInfo
from app.services.video_probe import VideoInfo


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _locked_runtime(tmp_path: Path, *, lock_path: Path | None = None):
    repository = tmp_path / "MuseTalk"
    repository.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(repository)], check=True)
    subprocess.run(["git", "-C", str(repository), "config", "user.email", "phase8@example.invalid"], check=True)
    subprocess.run(["git", "-C", str(repository), "config", "user.name", "PHASE8 Test"], check=True)
    (repository / "README.md").write_text("official checkout\n", encoding="utf-8")
    (repository / ".gitignore").write_text("models/\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repository), "add", "README.md", ".gitignore"], check=True)
    subprocess.run(["git", "-C", str(repository), "commit", "-q", "-m", "fixture"], check=True)
    commit = subprocess.run(
        ["git", "-C", str(repository), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    records = []
    manifest = []
    for index in range(11):
        relative = f"models/group-{index}/model-{index}.bin"
        path = repository / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        contents = f"model-{index}".encode()
        path.write_bytes(contents)
        record = {
            "repository": f"owner/model-{index}",
            "revision": f"{index:x}" * 40,
            "source": f"model-{index}.bin",
            "path": relative,
            "bytes": len(contents),
            "sha256": hashlib.sha256(contents).hexdigest(),
        }
        records.append(record)
        manifest.append({key: value for key, value in record.items() if key != "source"})
    lock_path = tmp_path / "musetalk-models.lock.json" if lock_path is None else lock_path
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path.write_text(json.dumps(records), encoding="utf-8")
    manifest_path = repository / "models" / "model-hashes.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    return repository, commit, lock_path, manifest_path, records


def _source_files(tmp_path: Path) -> tuple[Path, Path]:
    video = tmp_path / "phase7.mp4"
    video.write_bytes(b"verified phase 7 video")
    audio = tmp_path / "phase7.wav"
    with wave.open(str(audio), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(100)
        output.writeframes(b"\0\0" * 248)
    return video, audio


def _valid_av() -> AVInfo:
    return AVInfo(
        video=VideoStreamInfo(
            codec="h264",
            pixel_format="yuv420p",
            width=640,
            height=368,
            fps=25.0,
            frames=70,
            duration=2.8,
        ),
        audio=AudioStreamInfo(
            codec="aac",
            sample_rate=16000,
            channels=1,
            duration=2.8,
        ),
        duration=2.8,
        format_name="mov,mp4,m4a,3gp,3g2,mj2",
    )


def _seed(monkeypatch, tmp_path):
    from scripts import smoke_phase8

    video, audio = _source_files(tmp_path)
    monkeypatch.setattr(
        smoke_phase8,
        "probe_video",
        lambda _: VideoInfo("h264", 640, 368, 16.0, 49, 3.0625),
    )
    monkeypatch.setattr(
        smoke_phase8,
        "probe_wav",
        lambda _: WavInfo(24000, 1, 2, 59520, 2.48),
    )
    seeded = smoke_phase8.seed_phase8_database(
        tmp_path / "phase8.db",
        video,
        audio,
        expected_video_sha256=_sha256(video),
        expected_audio_sha256=_sha256(audio),
    )
    return seeded, video.resolve(), audio.resolve()


def _persist_success(tmp_path, seeded, video, audio):
    output = (tmp_path / "artifacts" / "phase8" / "run" / "musetalk.mp4").resolve()
    output.parent.mkdir(parents=True)
    output.write_bytes(b"canonical audiovisual output")
    provider_manifest = output.with_suffix(".manifest.json")
    provider_payload = {
        "provider": "musetalk",
        "provider_version": "0a89dec",
        "model": "musetalk-v1.5",
        "workflow": "musetalk_lipsync",
        "generation_time": 12.5,
        "target_duration": 2.78,
        "metadata": {
            "project_id": seeded.project_id,
            "shot_id": seeded.shot_id,
            "input_assets": [seeded.video_asset_id, seeded.audio_asset_id],
        },
        "source_video": {"path": str(video), "sha256": _sha256(video)},
        "source_audio": {"path": str(audio), "sha256": _sha256(audio)},
        "output_path": str(output),
        "output_sha256": _sha256(output),
        "sha256": _sha256(output),
    }
    provider_manifest.write_text(json.dumps(provider_payload), encoding="utf-8")

    with session_scope(str(seeded.database_path)) as session:
        asset = Asset(
            project_id=seeded.project_id,
            kind="LIPSYNC",
            path=str(output),
            mime_type="video/mp4",
            metadata_json={
                "manifest_path": str(provider_manifest),
                "provider": "musetalk",
                "provider_version": "0a89dec",
                "model_name": "musetalk-v1.5",
                "workflow_name": "musetalk_lipsync",
                "source_video_asset_id": seeded.video_asset_id,
                "source_video_sha256": _sha256(video),
                "audio_asset_id": seeded.audio_asset_id,
                "source_audio_sha256": _sha256(audio),
                "input_assets": [seeded.video_asset_id, seeded.audio_asset_id],
                "target_duration": 2.78,
                "output_sha256": _sha256(output),
            },
        )
        session.add(asset)
        session.flush()
        session.add(
            GenerationManifest(
                asset_id=asset.id,
                provider="musetalk",
                provider_version="0a89dec",
                model_name="musetalk-v1.5",
                workflow_name="musetalk_lipsync",
                generation_time=12.5,
                input_assets=[seeded.video_asset_id, seeded.audio_asset_id],
                output_asset=asset.id,
            )
        )
        shot = session.get(Shot, seeded.shot_id)
        shot.lipsync_asset_id = asset.id
        shot.status = "LIPSYNC_GENERATED"
        asset_id = asset.id
    return output, provider_manifest, asset_id


def test_phase7_inputs_are_exactly_locked():
    from scripts.smoke_phase8 import (
        PHASE7_AUDIO,
        PHASE7_AUDIO_SHA256,
        PHASE7_VIDEO,
        PHASE7_VIDEO_SHA256,
    )

    assert PHASE7_VIDEO.as_posix() == (
        "E:/kang/github/Movie/artifacts/phase7/1786342910/video/"
        "0753ed98-8ccc-4c4c-99dc-f9fc96b822ac_00001_.mp4"
    )
    assert PHASE7_AUDIO.as_posix() == (
        "E:/kang/github/Movie/artifacts/phase7/1786342910/audio/"
        "79002d71-d985-4658-aa8e-f30731dc0291.wav"
    )
    assert PHASE7_VIDEO_SHA256 == "009cf772e2ba8f36604cc7f72c6799c15f093699aecb51c8cbea89f1db9a95f6"
    assert PHASE7_AUDIO_SHA256 == "c77af7486266d780b2d9a8bc30e7c064cb244502d14d8f132485c596a5c72d49"


def test_seed_phase8_database_preserves_verified_inputs_and_eligibility(monkeypatch, tmp_path):
    seeded, video, audio = _seed(monkeypatch, tmp_path)

    with session_scope(str(seeded.database_path)) as session:
        shot = session.get(Shot, seeded.shot_id)
        scene = session.get(Scene, shot.scene_id)
        source = session.get(Asset, shot.video_asset_id)
        dialogues = session.query(Dialogue).filter_by(shot_id=shot.id).all()
        speech = session.get(Asset, dialogues[0].audio_asset_id)

        assert scene.project_id == seeded.project_id
        assert shot.duration == pytest.approx(2.78)
        assert shot.requires_lip_sync is True
        assert shot.speaker_visible is True
        assert shot.video_asset_id == seeded.video_asset_id
        assert shot.lipsync_asset_id is None
        assert shot.status == "VIDEO_GENERATED"
        assert source.kind == "VIDEO" and Path(source.path) == video
        assert len(dialogues) == 1
        assert dialogues[0].duration == pytest.approx(2.48)
        assert speech.kind == "AUDIO" and Path(speech.path) == audio


def test_seed_rejects_changed_phase7_hash_before_writing_database(monkeypatch, tmp_path):
    from scripts import smoke_phase8

    video, audio = _source_files(tmp_path)
    monkeypatch.setattr(
        smoke_phase8,
        "probe_video",
        lambda _: VideoInfo("h264", 640, 368, 16.0, 49, 3.0625),
    )
    monkeypatch.setattr(
        smoke_phase8,
        "probe_wav",
        lambda _: WavInfo(24000, 1, 2, 59520, 2.48),
    )

    with pytest.raises(RuntimeError, match="SHA256"):
        smoke_phase8.seed_phase8_database(
            tmp_path / "phase8.db",
            video,
            audio,
            expected_video_sha256="0" * 64,
            expected_audio_sha256=_sha256(audio),
        )
    assert not (tmp_path / "phase8.db").exists()


@pytest.mark.parametrize(
    "value",
    [
        "https://127.0.0.1:8030",
        "http://0.0.0.0:8030",
        "http://localhost:8030",
        "http://127.0.0.1:8031",
        "http://user:secret@127.0.0.1:8030",
        "http://127.0.0.1:8030/api",
        "http://127.0.0.1:8030?token=secret",
    ],
)
def test_musetalk_url_accepts_only_exact_ipv4_loopback_endpoint(value):
    from scripts.smoke_phase8 import validate_musetalk_url

    with pytest.raises(ValueError, match="127.0.0.1:8030"):
        validate_musetalk_url(value)


def test_port_preflight_refuses_any_ai_service_listener():
    from scripts.smoke_phase8 import assert_ports_free

    with pytest.raises(RuntimeError, match="8188"):
        assert_ports_free((8020, 8030, 8188), is_listening=lambda port: port == 8188)


def test_runtime_attestation_uses_actual_clean_git_head_and_all_model_tuples(tmp_path):
    from scripts.smoke_phase8 import attest_musetalk_runtime

    repository, commit, lock_path, manifest_path, records = _locked_runtime(tmp_path)
    identity = attest_musetalk_runtime(repository, commit, lock_path)

    assert identity["repo_commit"] == commit
    assert identity["repo_clean"] is True
    assert identity["model_count"] == 11
    assert identity["model_lock"]["path"] == str(lock_path.resolve())
    assert identity["model_manifest"]["path"] == str(manifest_path.resolve())
    assert identity["models"] == [
        {
            "path": record["path"],
            "repository": record["repository"],
            "revision": record["revision"],
            "bytes": record["bytes"],
            "sha256": record["sha256"],
        }
        for record in records
    ]


def test_runtime_attestation_rejects_bogus_expected_commit_and_dirty_checkout(tmp_path):
    from scripts.smoke_phase8 import attest_musetalk_runtime

    repository, commit, lock_path, _, _ = _locked_runtime(tmp_path)
    with pytest.raises(RuntimeError, match="Git HEAD"):
        attest_musetalk_runtime(repository, "0" * 40, lock_path)

    (repository / "README.md").write_text("dirty\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match="clean"):
        attest_musetalk_runtime(repository, commit, lock_path)


def test_runtime_attestation_rejects_altered_model_and_manifest(tmp_path):
    from scripts.smoke_phase8 import attest_musetalk_runtime

    repository, commit, lock_path, manifest_path, records = _locked_runtime(tmp_path)
    model = repository / records[0]["path"]
    model.write_bytes(b"tampered")
    with pytest.raises(RuntimeError, match="model (byte count|SHA256)"):
        attest_musetalk_runtime(repository, commit, lock_path)

    model.write_bytes(b"model-0")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest[0]["revision"] = "f" * 40
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(RuntimeError, match="manifest tuple"):
        attest_musetalk_runtime(repository, commit, lock_path)


@pytest.mark.parametrize(
    "run_id",
    [
        "../escape",
        "..\\escape",
        "C:\\absolute",
        "/absolute",
        "\\root-relative",
        ".",
        "..",
        "CON",
        "con.txt",
        "NUL",
        "AUX.json",
        "bad id",
        "x" * 129,
        "",
    ],
)
def test_run_id_rejects_traversal_absolute_reserved_and_unsafe_values(run_id):
    from scripts.smoke_phase8 import validate_run_id

    with pytest.raises(ValueError, match="run ID"):
        validate_run_id(run_id)


def test_run_id_and_evidence_destinations_accept_safe_contained_paths(tmp_path):
    from scripts.smoke_phase8 import build_evidence_paths, validate_run_id

    assert validate_run_id("20260812-203657_ok.1") == "20260812-203657_ok.1"
    paths = build_evidence_paths(tmp_path, "run-01.ok")
    assert paths["run_dir"] == (tmp_path / "artifacts" / "phase8" / "run-01.ok").resolve()
    assert paths["database"] == (tmp_path / ".runtime" / "phase8" / "phase8-smoke-run-01.ok.db").resolve()
    assert paths["log"] == (tmp_path / "logs" / "musetalk-phase8-run-01.ok.log").resolve()
    assert all(path.is_relative_to(tmp_path.resolve()) for path in paths.values())


def test_stop_process_tree_cleans_captured_descendant_after_parent_exits(monkeypatch):
    from scripts import smoke_phase8

    events = []

    class Parent:
        pid = 101

        def poll(self):
            return 1

        def wait(self, timeout):
            events.append(("parent-wait", timeout))

    class Child:
        pid = 202

        def create_time(self):
            return 12.5

        def is_running(self):
            return True

        def terminate(self):
            events.append("child-terminate")

        def kill(self):
            events.append("child-kill")

    child = Child()
    monkeypatch.setattr(smoke_phase8.psutil, "Process", lambda pid: child)
    monkeypatch.setattr(
        smoke_phase8.psutil,
        "wait_procs",
        lambda processes, timeout: ([], processes),
    )

    smoke_phase8.stop_process_tree(Parent(), {(202, 12.5)})

    assert events == ["child-terminate", "child-kill"]


@pytest.mark.parametrize(
    ("mutator", "message"),
    [
        (lambda info: replace(info, format_name="matroska,webm"), "MP4/MOV"),
        (lambda info: replace(info, video=replace(info.video, codec="hevc")), "H.264"),
        (lambda info: replace(info, video=replace(info.video, pixel_format="yuv444p")), "yuv420p"),
        (lambda info: replace(info, video=replace(info.video, width=320)), "640x368"),
        (lambda info: replace(info, video=replace(info.video, fps=24)), "25 FPS"),
        (lambda info: replace(info, audio=replace(info.audio, codec="mp3")), "AAC"),
        (lambda info: replace(info, video=replace(info.video, duration=2.70)), "shorter"),
        (
            lambda info: replace(
                info,
                video=replace(info.video, duration=2.83),
                audio=replace(info.audio, duration=2.74),
                duration=2.83,
            ),
            "synchronization",
        ),
    ],
)
def test_machine_media_validation_rejects_every_profile_gap(mutator, message):
    from scripts.smoke_phase8 import validate_media_profile

    with pytest.raises(RuntimeError, match=message):
        validate_media_profile(mutator(_valid_av()), target_duration=2.78)


def test_database_validation_checks_exact_links_hashes_and_provenance(monkeypatch, tmp_path):
    from scripts.smoke_phase8 import validate_database_evidence

    seeded, video, audio = _seed(monkeypatch, tmp_path)
    output, provider_manifest, asset_id = _persist_success(tmp_path, seeded, video, audio)

    evidence = validate_database_evidence(
        seeded.database_path,
        seeded.project_id,
        seeded.shot_id,
        output,
        provider_manifest,
    )

    assert evidence["lipsync_asset_id"] == asset_id
    assert evidence["video_asset_id"] == seeded.video_asset_id
    assert evidence["audio_asset_id"] == seeded.audio_asset_id
    assert evidence["input_assets"] == [seeded.video_asset_id, seeded.audio_asset_id]
    assert evidence["output_sha256"] == _sha256(output)


def test_database_validation_rejects_manifest_input_order(monkeypatch, tmp_path):
    from scripts.smoke_phase8 import validate_database_evidence

    seeded, video, audio = _seed(monkeypatch, tmp_path)
    output, provider_manifest, _ = _persist_success(tmp_path, seeded, video, audio)
    with session_scope(str(seeded.database_path)) as session:
        manifest = session.query(GenerationManifest).one()
        manifest.input_assets = list(reversed(manifest.input_assets))

    with pytest.raises(RuntimeError, match="input assets"):
        validate_database_evidence(
            seeded.database_path,
            seeded.project_id,
            seeded.shot_id,
            output,
            provider_manifest,
        )


def test_review_commands_extract_three_frames_and_mouth_contact_sheet(tmp_path):
    from scripts.smoke_phase8 import build_review_commands

    commands, artifacts = build_review_commands(
        tmp_path / "output.mp4",
        tmp_path / "review",
        duration=2.8,
        ffmpeg="ffmpeg",
    )

    assert [path.name for path in artifacts] == [
        "start.png",
        "middle.png",
        "end.png",
        "mouth-contact-sheet.png",
    ]
    assert len(commands) == 4
    assert all(command[0] == "ffmpeg" and command[-1].endswith(".png") for command in commands)
    assert "1.400000" in commands[1]
    assert "2.760000" in commands[2]
    assert "crop=" in " ".join(commands[3]) and "tile=4x3" in " ".join(commands[3])


def test_resource_parser_and_peaks_require_gpu_ram_and_windows_commit():
    from scripts.smoke_phase8 import parse_nvidia_smi, resource_peaks

    gpu = parse_nvidia_smi("1024, 77, 61\n")
    assert gpu == {"vram_mib": 1024, "gpu_percent": 77, "temperature_c": 61}
    peaks = resource_peaks(
        [
            {**gpu, "ram_mib": 8000, "commit_mib": 9000},
            {"vram_mib": 2048, "gpu_percent": 55, "temperature_c": 65, "ram_mib": 8100, "commit_mib": 9200},
        ]
    )
    assert peaks == {
        "vram_mib": 2048,
        "gpu_percent": 77,
        "temperature_c": 65,
        "ram_mib": 8100,
        "commit_mib": 9200,
    }


def _write_locked_evidence(tmp_path, seeded, video, audio, output, manifest):
    from scripts.smoke_phase8 import attest_musetalk_runtime, validate_database_evidence

    repository, commit, model_lock_path, model_manifest_path, _ = _locked_runtime(
        tmp_path / "runtime-fixture",
        lock_path=tmp_path / "scripts" / "musetalk-models.lock.json",
    )
    runtime_identity = attest_musetalk_runtime(repository, commit, model_lock_path)

    run_dir = output.parent
    review = run_dir / "review"
    review.mkdir()
    review_paths = []
    for name in ("start.png", "middle.png", "end.png", "mouth-contact-sheet.png"):
        path = review / name
        path.write_bytes(name.encode())
        review_paths.append(path)
    resource_path = run_dir / "resources.json"
    resource_path.write_text(
        json.dumps(
            {
                "samples": [
                    {
                        "vram_mib": 1024,
                        "gpu_percent": 50,
                        "temperature_c": 60,
                        "ram_mib": 8000,
                        "commit_mib": 9000,
                    }
                ],
                "peaks": {
                    "vram_mib": 1024,
                    "gpu_percent": 50,
                    "temperature_c": 60,
                    "ram_mib": 8000,
                    "commit_mib": 9000,
                },
            }
        ),
        encoding="utf-8",
    )
    evidence_path = run_dir / "evidence.json"
    database_links = validate_database_evidence(
        seeded.database_path,
        seeded.project_id,
        seeded.shot_id,
        output,
        manifest,
    )
    evidence_payload = {
        "phase": 8,
        "project_id": seeded.project_id,
        "shot_id": seeded.shot_id,
        "target_duration": 2.78,
        "database": str(seeded.database_path),
        "source_video": {"path": str(video), "sha256": _sha256(video)},
        "source_audio": {"path": str(audio), "sha256": _sha256(audio)},
        "output": {"path": str(output), "sha256": _sha256(output)},
        "provider_manifest": {"path": str(manifest), "sha256": _sha256(manifest)},
        "database_links": database_links,
        "review": {
            "status": "approved",
            "start": {"path": str(review_paths[0]), "sha256": _sha256(review_paths[0])},
            "middle": {"path": str(review_paths[1]), "sha256": _sha256(review_paths[1])},
            "end": {"path": str(review_paths[2]), "sha256": _sha256(review_paths[2])},
            "mouth_contact_sheet": {"path": str(review_paths[3]), "sha256": _sha256(review_paths[3])},
        },
        "resources": {
            "path": str(resource_path),
            "sha256": _sha256(resource_path),
            "peaks": {
                "vram_mib": 1024,
                "gpu_percent": 50,
                "temperature_c": 60,
                "ram_mib": 8000,
                "commit_mib": 9000,
            },
        },
        "musetalk_runtime": runtime_identity,
    }
    evidence_path.write_text(json.dumps(evidence_payload), encoding="utf-8")
    lock = {
        "musetalk": {
            "path": str(repository),
            "commit_or_release": commit,
            "model_hash_manifest": str(model_manifest_path),
            "model_hash_manifest_sha256": _sha256(model_manifest_path),
        },
        "verification": {
            "phase": "8",
            "phase8_lipsync": str(output),
            "phase8_lipsync_sha256": _sha256(output),
            "phase8_database": str(seeded.database_path),
            "phase8_provider_manifest": str(manifest),
            "phase8_provider_manifest_sha256": _sha256(manifest),
            "phase8_evidence": str(evidence_path),
            "phase8_evidence_sha256": _sha256(evidence_path),
            "phase8_visual_review": "approved",
        }
    }
    lock_path = tmp_path / "runtime-lock.yaml"
    lock_path.write_text(yaml.safe_dump(lock), encoding="utf-8")
    return lock_path


def test_verify_phase8_checks_lock_hash_decode_database_evidence_and_free_ports(monkeypatch, tmp_path):
    from scripts.verify_phase8 import verify_phase8

    seeded, video, audio = _seed(monkeypatch, tmp_path)
    output, manifest, _ = _persist_success(tmp_path, seeded, video, audio)
    lock_path = _write_locked_evidence(tmp_path, seeded, video, audio, output, manifest)
    decoded = []

    result = verify_phase8(
        lock_path,
        repo_root=tmp_path,
        evidence_root=tmp_path,
        probe=lambda _: _valid_av(),
        decoder=lambda path: decoded.append(path),
        is_listening=lambda _: False,
    )

    assert decoded == [output]
    assert result["output_sha256"] == _sha256(output)
    assert result["visual_review"] == "approved"
    assert result["musetalk_runtime"]["repo_commit"] == yaml.safe_load(
        lock_path.read_text(encoding="utf-8")
    )["musetalk"]["commit_or_release"]
    assert result["musetalk_runtime"]["model_count"] == 11


def test_verify_phase8_rejects_runtime_lock_bogus_commit_before_decode(monkeypatch, tmp_path):
    from scripts.verify_phase8 import verify_phase8

    seeded, video, audio = _seed(monkeypatch, tmp_path)
    output, manifest, _ = _persist_success(tmp_path, seeded, video, audio)
    lock_path = _write_locked_evidence(tmp_path, seeded, video, audio, output, manifest)
    lock = yaml.safe_load(lock_path.read_text(encoding="utf-8"))
    lock["musetalk"]["commit_or_release"] = "0" * 40
    lock_path.write_text(yaml.safe_dump(lock), encoding="utf-8")

    with pytest.raises(RuntimeError, match="Git HEAD"):
        verify_phase8(
            lock_path,
            repo_root=tmp_path,
            evidence_root=tmp_path,
            probe=lambda _: _valid_av(),
            decoder=lambda _: pytest.fail("decode must not run after runtime mismatch"),
            is_listening=lambda _: False,
        )


def test_verify_phase8_rejects_evidence_runtime_identity_tampering(monkeypatch, tmp_path):
    from scripts.verify_phase8 import verify_phase8

    seeded, video, audio = _seed(monkeypatch, tmp_path)
    output, manifest, _ = _persist_success(tmp_path, seeded, video, audio)
    lock_path = _write_locked_evidence(tmp_path, seeded, video, audio, output, manifest)
    lock = yaml.safe_load(lock_path.read_text(encoding="utf-8"))
    evidence_path = Path(lock["verification"]["phase8_evidence"])
    evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
    evidence["musetalk_runtime"]["model_identity_sha256"] = "0" * 64
    evidence_path.write_text(json.dumps(evidence), encoding="utf-8")
    lock["verification"]["phase8_evidence_sha256"] = _sha256(evidence_path)
    lock_path.write_text(yaml.safe_dump(lock), encoding="utf-8")

    with pytest.raises(RuntimeError, match="runtime identity"):
        verify_phase8(
            lock_path,
            repo_root=tmp_path,
            evidence_root=tmp_path,
            probe=lambda _: _valid_av(),
            decoder=lambda _: pytest.fail("decode must not run after identity mismatch"),
            is_listening=lambda _: False,
        )


def test_verify_phase8_rejects_altered_model_manifest_before_decode(monkeypatch, tmp_path):
    from scripts.verify_phase8 import verify_phase8

    seeded, video, audio = _seed(monkeypatch, tmp_path)
    output, manifest, _ = _persist_success(tmp_path, seeded, video, audio)
    lock_path = _write_locked_evidence(tmp_path, seeded, video, audio, output, manifest)
    lock = yaml.safe_load(lock_path.read_text(encoding="utf-8"))
    model_manifest = Path(lock["musetalk"]["model_hash_manifest"])
    records = json.loads(model_manifest.read_text(encoding="utf-8"))
    records[0]["sha256"] = "0" * 64
    model_manifest.write_text(json.dumps(records), encoding="utf-8")
    lock["musetalk"]["model_hash_manifest_sha256"] = _sha256(model_manifest)
    lock_path.write_text(yaml.safe_dump(lock), encoding="utf-8")

    with pytest.raises(RuntimeError, match="manifest tuple"):
        verify_phase8(
            lock_path,
            repo_root=tmp_path,
            evidence_root=tmp_path,
            probe=lambda _: _valid_av(),
            decoder=lambda _: pytest.fail("decode must not run after model mismatch"),
            is_listening=lambda _: False,
        )


def test_verify_phase8_rejects_path_outside_repo_and_evidence_root(tmp_path):
    from scripts.verify_phase8 import resolve_locked_path

    outside = tmp_path.parent / "outside-phase8.mp4"
    outside.write_bytes(b"outside")
    with pytest.raises(RuntimeError, match="outside"):
        resolve_locked_path(str(outside), (tmp_path,), "output")


def test_verify_phase8_rejects_hash_mismatch_before_decode(monkeypatch, tmp_path):
    from scripts.verify_phase8 import verify_phase8

    seeded, video, audio = _seed(monkeypatch, tmp_path)
    output, manifest, _ = _persist_success(tmp_path, seeded, video, audio)
    lock_path = _write_locked_evidence(tmp_path, seeded, video, audio, output, manifest)
    lock = yaml.safe_load(lock_path.read_text(encoding="utf-8"))
    lock["verification"]["phase8_lipsync_sha256"] = "0" * 64
    lock_path.write_text(yaml.safe_dump(lock), encoding="utf-8")

    with pytest.raises(RuntimeError, match="SHA256"):
        verify_phase8(
            lock_path,
            repo_root=tmp_path,
            evidence_root=tmp_path,
            probe=lambda _: _valid_av(),
            decoder=lambda _: pytest.fail("decode must not run after hash mismatch"),
            is_listening=lambda _: False,
        )


def test_verify_phase8_rejects_listening_ai_port(monkeypatch, tmp_path):
    from scripts.verify_phase8 import verify_phase8

    seeded, video, audio = _seed(monkeypatch, tmp_path)
    output, manifest, _ = _persist_success(tmp_path, seeded, video, audio)
    lock_path = _write_locked_evidence(tmp_path, seeded, video, audio, output, manifest)

    with pytest.raises(RuntimeError, match="8030"):
        verify_phase8(
            lock_path,
            repo_root=tmp_path,
            evidence_root=tmp_path,
            probe=lambda _: _valid_av(),
            decoder=lambda _: None,
            is_listening=lambda port: port == 8030,
        )
