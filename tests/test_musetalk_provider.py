import json
from pathlib import Path

import httpx
import pytest

from app.core.config import Settings
from app.providers.musetalk_provider import MuseTalkProvider


def _write_nonempty(path: Path, content: bytes = b"fixture") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


def _source_files(tmp_path: Path) -> tuple[Path, Path]:
    video_path = _write_nonempty(tmp_path / "source video.mp4", b"video")
    audio_path = _write_nonempty(tmp_path / "source audio.wav", b"audio")
    return video_path, audio_path


def test_settings_default_to_local_musetalk_service(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("LOCALDRAMA_MUSETALK_URL", raising=False)

    configured = Settings(_env_file=None)

    assert configured.musetalk_url == "http://127.0.0.1:8030"


def test_settings_read_prefixed_musetalk_url(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("LOCALDRAMA_MUSETALK_URL", "http://musetalk.test:9030")

    configured = Settings(_env_file=None)

    assert configured.musetalk_url == "http://musetalk.test:9030"


@pytest.mark.anyio
async def test_health_gets_endpoint_and_returns_object():
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"status": "ok", "busy": False})

    provider = MuseTalkProvider(
        "http://musetalk.test/",
        transport=httpx.MockTransport(handler),
    )

    result = await provider.health()

    assert result == {"status": "ok", "busy": False}
    assert [(request.method, request.url.path) for request in requests] == [
        ("GET", "/health")
    ]


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("response", "message"),
    [
        (httpx.Response(200, content=b"not-json"), "malformed JSON"),
        (httpx.Response(200, json=["ok"]), "non-object JSON"),
    ],
)
async def test_health_rejects_invalid_json_responses(
    response: httpx.Response,
    message: str,
):
    provider = MuseTalkProvider(
        transport=httpx.MockTransport(lambda request: response),
    )

    with pytest.raises(RuntimeError, match=message):
        await provider.health()


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("error_factory", "message"),
    [
        (
            lambda request: httpx.ReadTimeout("too slow", request=request),
            "MuseTalk request timed out for GET /health",
        ),
        (
            lambda request: httpx.ConnectError("connection refused", request=request),
            r"MuseTalk request failed for GET /health \(ConnectError\)",
        ),
    ],
)
async def test_health_converts_transport_errors_to_stable_runtime_errors(
    error_factory,
    message: str,
):
    def handler(request: httpx.Request) -> httpx.Response:
        raise error_factory(request)

    provider = MuseTalkProvider(transport=httpx.MockTransport(handler))

    with pytest.raises(RuntimeError, match=message):
        await provider.health()


@pytest.mark.anyio
@pytest.mark.parametrize("status_code", [400, 503])
async def test_health_reports_bounded_sanitized_http_errors(status_code: int):
    secret = "do-not-leak-this-token"
    response_body = json.dumps(
        {
            "detail": "service unavailable " + ("x" * 2_000),
            "token": secret,
        }
    )
    provider = MuseTalkProvider(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(status_code, text=response_body)
        )
    )

    with pytest.raises(RuntimeError) as caught:
        await provider.health()

    message = str(caught.value)
    assert f"HTTP {status_code}" in message
    assert "service unavailable" in message
    assert secret not in message
    assert len(message) < 700


@pytest.mark.anyio
async def test_health_bounds_and_sanitizes_an_empty_body_reason_phrase():
    secret = "do-not-leak-this-password"
    reason_phrase = (
        f"  upstream 上游\tfailed password={secret}\n"
        + ("理由填充 reason-phrase-padding " * 100)
    )
    provider = MuseTalkProvider(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                599,
                content=b"",
                extensions={"reason_phrase": reason_phrase.encode("utf-8")},
            )
        )
    )

    with pytest.raises(RuntimeError) as caught:
        await provider.health()

    message = str(caught.value)
    assert "HTTP 599" in message
    assert "upstream 上游 failed" in message
    assert secret not in message
    assert message.endswith("...")
    assert len(message) < 700


@pytest.mark.anyio
async def test_generate_posts_absolute_paths_and_fixed_service_options(tmp_path: Path):
    video_path, audio_path = _source_files(tmp_path)
    output_dir = tmp_path / "published output"
    output_path = _write_nonempty(output_dir / "lip synced.mp4", b"mp4")
    manifest_path = _write_nonempty(
        output_dir / "lip synced.manifest.json",
        b"{}",
    )
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["path"] = request.url.path
        captured["payload"] = json.loads(request.content)
        captured["timeout"] = request.extensions["timeout"]
        return httpx.Response(
            200,
            json={
                "output_path": str(output_path),
                "manifest_path": str(manifest_path),
            },
        )

    provider = MuseTalkProvider(
        "http://musetalk.test/",
        timeout=17.5,
        transport=httpx.MockTransport(handler),
    )

    returned_output, returned_manifest = await provider.generate(
        video_path,
        audio_path,
        output_dir,
        {"target_duration": 2.75, "project_id": "not-forwarded"},
    )

    assert captured["method"] == "POST"
    assert captured["path"] == "/generate"
    assert captured["payload"] == {
        "video_path": str(video_path.resolve()),
        "audio_path": str(audio_path.resolve()),
        "output_dir": str(output_dir.resolve()),
        "target_duration": 2.75,
        "batch_size": 4,
        "use_float16": True,
    }
    assert all(Path(captured["payload"][key]).is_absolute() for key in (
        "video_path",
        "audio_path",
        "output_dir",
    ))
    assert captured["timeout"] == {
        "connect": 17.5,
        "read": 17.5,
        "write": 17.5,
        "pool": 17.5,
    }
    assert returned_output == output_path.resolve()
    assert returned_manifest == manifest_path.resolve()


@pytest.mark.anyio
@pytest.mark.parametrize(
    "metadata",
    [
        {},
        {"target_duration": None},
        {"target_duration": 0},
        {"target_duration": -0.01},
        {"target_duration": float("inf")},
        {"target_duration": True},
    ],
)
async def test_generate_requires_a_positive_finite_target_duration(
    tmp_path: Path,
    metadata: dict,
):
    video_path, audio_path = _source_files(tmp_path)
    transport_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal transport_calls
        transport_calls += 1
        return httpx.Response(500)

    provider = MuseTalkProvider(transport=httpx.MockTransport(handler))

    with pytest.raises(ValueError, match="target_duration must be a positive number"):
        await provider.generate(video_path, audio_path, tmp_path / "output", metadata)

    assert transport_calls == 0


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("source_name", "source_state"),
    [
        ("video", "missing"),
        ("video", "empty"),
        ("audio", "missing"),
        ("audio", "empty"),
    ],
)
async def test_generate_rejects_missing_or_empty_sources_before_transport(
    tmp_path: Path,
    source_name: str,
    source_state: str,
):
    video_path, audio_path = _source_files(tmp_path)
    invalid_path = tmp_path / f"invalid-{source_name}"
    if source_state == "empty":
        invalid_path.touch()
    if source_name == "video":
        video_path = invalid_path
    else:
        audio_path = invalid_path
    transport_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal transport_calls
        transport_calls += 1
        return httpx.Response(500)

    provider = MuseTalkProvider(transport=httpx.MockTransport(handler))

    with pytest.raises(
        ValueError,
        match=rf"source {source_name} must be an existing non-empty file",
    ):
        await provider.generate(
            video_path,
            audio_path,
            tmp_path / "output",
            {"target_duration": 2.5},
        )

    assert transport_calls == 0


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("response_json", "message"),
    [
        ({"manifest_path": "manifest.json"}, "missing output_path"),
        ({"output_path": "output.mp4"}, "missing manifest_path"),
        ({"output_path": "", "manifest_path": "manifest.json"}, "missing output_path"),
        ({"output_path": "output.mp4", "manifest_path": ""}, "missing manifest_path"),
    ],
)
async def test_generate_rejects_missing_response_paths(
    tmp_path: Path,
    response_json: dict,
    message: str,
):
    video_path, audio_path = _source_files(tmp_path)
    provider = MuseTalkProvider(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json=response_json)
        )
    )

    with pytest.raises(RuntimeError, match=message):
        await provider.generate(
            video_path,
            audio_path,
            tmp_path / "output",
            {"target_duration": 2.5},
        )


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("output_state", "message"),
    [
        ("missing", "output_path is not an existing non-empty file"),
        ("empty", "output_path is not an existing non-empty file"),
        ("wrong-extension", "output_path must be an MP4 file"),
    ],
)
async def test_generate_rejects_invalid_returned_output(
    tmp_path: Path,
    output_state: str,
    message: str,
):
    video_path, audio_path = _source_files(tmp_path)
    output_dir = tmp_path / "output"
    manifest_path = _write_nonempty(output_dir / "result.manifest.json", b"{}")
    output_path = output_dir / "result.mp4"
    if output_state == "empty":
        output_path.touch()
    elif output_state == "wrong-extension":
        output_path = _write_nonempty(output_dir / "result.webm", b"video")
    provider = MuseTalkProvider(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                json={
                    "output_path": str(output_path),
                    "manifest_path": str(manifest_path),
                },
            )
        )
    )

    with pytest.raises(RuntimeError, match=message):
        await provider.generate(
            video_path,
            audio_path,
            output_dir,
            {"target_duration": 2.5},
        )


@pytest.mark.anyio
@pytest.mark.parametrize("manifest_state", ["missing", "empty"])
async def test_generate_rejects_invalid_returned_manifest(
    tmp_path: Path,
    manifest_state: str,
):
    video_path, audio_path = _source_files(tmp_path)
    output_dir = tmp_path / "output"
    output_path = _write_nonempty(output_dir / "result.mp4", b"video")
    manifest_path = output_dir / "result.manifest.json"
    if manifest_state == "empty":
        manifest_path.touch()
    provider = MuseTalkProvider(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                json={
                    "output_path": str(output_path),
                    "manifest_path": str(manifest_path),
                },
            )
        )
    )

    with pytest.raises(
        RuntimeError,
        match="manifest_path is not an existing non-empty file",
    ):
        await provider.generate(
            video_path,
            audio_path,
            output_dir,
            {"target_duration": 2.5},
        )


@pytest.mark.anyio
async def test_unload_posts_endpoint_and_returns_object():
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"unloaded": True, "busy": False})

    provider = MuseTalkProvider(
        "http://musetalk.test",
        transport=httpx.MockTransport(handler),
    )

    result = await provider.unload()

    assert result == {"unloaded": True, "busy": False}
    assert [(request.method, request.url.path) for request in requests] == [
        ("POST", "/unload")
    ]
    assert requests[0].content == b""
