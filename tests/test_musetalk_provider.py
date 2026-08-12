import asyncio
import json
import time
from pathlib import Path

import httpx
import pytest

from app.core.config import Settings
from app.providers import musetalk_provider as musetalk_provider_module
from app.providers.musetalk_provider import MuseTalkProvider


def _write_nonempty(path: Path, content: bytes = b"fixture") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


def _source_files(tmp_path: Path) -> tuple[Path, Path]:
    video_path = _write_nonempty(tmp_path / "source video.mp4", b"video")
    audio_path = _write_nonempty(tmp_path / "source audio.wav", b"audio")
    return video_path, audio_path


def _metadata(**overrides) -> dict:
    metadata = {
        "project_id": "project-1",
        "shot_id": "shot-1",
        "input_assets": ["video-asset", "audio-asset"],
        "target_duration": 2.5,
    }
    metadata.update(overrides)
    return metadata


class CountingAsyncByteStream(httpx.AsyncByteStream):
    def __init__(self, chunks: list[bytes], *, delay: float = 0):
        self.chunks = chunks
        self.delay = delay
        self.chunks_yielded = 0
        self.bytes_yielded = 0
        self.closed = False

    async def __aiter__(self):
        for chunk in self.chunks:
            if self.delay:
                await asyncio.sleep(self.delay)
            self.chunks_yielded += 1
            self.bytes_yielded += len(chunk)
            yield chunk

    async def aclose(self) -> None:
        self.closed = True


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
async def test_health_streams_only_a_bounded_error_body():
    secret = b"actual-error-bearer-token"
    stream = CountingAsyncByteStream(
        [b"Authorization: Bearer " + secret + b"\nservice unavailable "]
        + ([b"x" * 64] * 30)
    )
    provider = MuseTalkProvider(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(503, stream=stream)
        )
    )

    with pytest.raises(RuntimeError) as caught:
        await provider.health()

    message = str(caught.value)
    assert "HTTP 503" in message
    assert secret.decode() not in message
    assert stream.bytes_yielded <= 512
    assert stream.chunks_yielded < len(stream.chunks)
    assert stream.closed


@pytest.mark.anyio
async def test_health_rejects_an_oversized_streamed_success_object():
    stream = CountingAsyncByteStream(
        [b'{"payload":"'] + ([b"x" * 1024] * 70) + [b'"}']
    )
    provider = MuseTalkProvider(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, stream=stream)
        )
    )

    with pytest.raises(RuntimeError, match="JSON response exceeded 65536 bytes"):
        await provider.health()

    assert stream.bytes_yielded <= 66 * 1024
    assert stream.chunks_yielded < len(stream.chunks)
    assert stream.closed


@pytest.mark.anyio
async def test_health_fully_redacts_bearer_and_basic_authorization_credentials():
    bearer_token = "eyJhbGciOiJIUzI1NiJ9.real-signature-segment"
    basic_token = "dXNlcjpzdXBlci1zZWNyZXQtcGFzc3dvcmQ="
    body = (
        f"Authorization: Bearer {bearer_token}\n"
        f"authorization=Basic {basic_token}\n"
        "access denied"
    )
    provider = MuseTalkProvider(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(401, text=body)
        )
    )

    with pytest.raises(RuntimeError) as caught:
        await provider.health()

    message = str(caught.value)
    assert bearer_token not in message
    assert basic_token not in message
    assert "Bearer" not in message
    assert "Basic" not in message
    assert message.count("<redacted>") >= 2


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
async def test_health_and_unload_use_short_phase_timeouts():
    captured: dict[str, dict] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured[request.url.path] = request.extensions["timeout"]
        return httpx.Response(200, json={"ok": True})

    provider = MuseTalkProvider(
        timeout=1800,
        transport=httpx.MockTransport(handler),
    )

    await provider.health()
    await provider.unload()

    expected = {
        "connect": 10.0,
        "read": 10.0,
        "write": 10.0,
        "pool": 10.0,
    }
    assert captured == {"/health": expected, "/unload": expected}


@pytest.mark.anyio
@pytest.mark.parametrize("method_name", ["health", "unload"])
async def test_control_endpoints_enforce_a_short_wall_clock_deadline(
    monkeypatch: pytest.MonkeyPatch,
    method_name: str,
):
    monkeypatch.setattr(
        musetalk_provider_module,
        "_CONTROL_DEADLINE_SECONDS",
        0.08,
        raising=False,
    )
    stream = CountingAsyncByteStream(
        [b'{"ok"', b":", b"true", b"}"],
        delay=0.03,
    )
    provider = MuseTalkProvider(
        timeout=1800,
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, stream=stream)
        ),
    )

    started = time.perf_counter()
    with pytest.raises(RuntimeError, match=rf"timed out for .* /{method_name}"):
        await getattr(provider, method_name)()

    assert time.perf_counter() - started < 0.25
    assert stream.closed


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
        _metadata(
            target_duration=2.75,
            project_id="project-42",
            shot_id="shot-17",
            input_assets=["video-9", "audio-4"],
            ignored="not-forwarded",
        ),
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
        "metadata": {
            "project_id": "project-42",
            "shot_id": "shot-17",
            "input_assets": ["video-9", "audio-4"],
        },
    }
    assert all(Path(captured["payload"][key]).is_absolute() for key in (
        "video_path",
        "audio_path",
        "output_dir",
    ))
    assert captured["timeout"] == {
        "connect": 10.0,
        "read": 17.5,
        "write": 10.0,
        "pool": 10.0,
    }
    assert returned_output == output_path.resolve()
    assert returned_manifest == manifest_path.resolve()


@pytest.mark.anyio
async def test_generate_enforces_an_overall_wall_clock_deadline(tmp_path: Path):
    video_path, audio_path = _source_files(tmp_path)
    output_dir = tmp_path / "output"
    output_path = _write_nonempty(output_dir / "result.mp4", b"video")
    manifest_path = _write_nonempty(output_dir / "result.manifest.json", b"{}")
    body = json.dumps(
        {
            "output_path": str(output_path),
            "manifest_path": str(manifest_path),
        }
    ).encode()
    chunk_size = max(1, len(body) // 4)
    stream = CountingAsyncByteStream(
        [body[index : index + chunk_size] for index in range(0, len(body), chunk_size)],
        delay=0.03,
    )
    provider = MuseTalkProvider(
        timeout=0.08,
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, stream=stream)
        ),
    )

    started = time.perf_counter()
    with pytest.raises(RuntimeError, match="timed out for POST /generate"):
        await provider.generate(
            video_path,
            audio_path,
            output_dir,
            _metadata(),
        )

    assert time.perf_counter() - started < 0.25
    assert stream.closed


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
    "metadata",
    [
        {"shot_id": "shot-1", "input_assets": ["video", "audio"], "target_duration": 2.5},
        _metadata(project_id=""),
        {"project_id": "project-1", "input_assets": ["video", "audio"], "target_duration": 2.5},
        _metadata(shot_id=""),
        {"project_id": "project-1", "shot_id": "shot-1", "target_duration": 2.5},
        _metadata(input_assets=[]),
        _metadata(input_assets=["video-only"]),
        _metadata(input_assets=["video", "audio", "extra"]),
        _metadata(input_assets=["video", ""]),
        _metadata(input_assets=["video", 42]),
    ],
)
async def test_generate_rejects_invalid_provenance_metadata_before_transport(
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

    with pytest.raises(ValueError, match="MuseTalk metadata"):
        await provider.generate(
            video_path,
            audio_path,
            tmp_path / "output",
            metadata,
        )

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
            _metadata(),
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
            _metadata(),
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
            _metadata(),
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
            _metadata(),
        )


@pytest.mark.anyio
@pytest.mark.parametrize("returned_key", ["output_path", "manifest_path"])
@pytest.mark.parametrize("escape_style", ["absolute", "traversal", "root-relative"])
async def test_generate_rejects_returned_paths_outside_output_dir(
    tmp_path: Path,
    returned_key: str,
    escape_style: str,
):
    video_path, audio_path = _source_files(tmp_path)
    output_dir = tmp_path / "requested" / "nested"
    inside_output = _write_nonempty(output_dir / "inside.mp4", b"video")
    inside_manifest = _write_nonempty(output_dir / "inside.manifest.json", b"{}")
    outside_suffix = ".mp4" if returned_key == "output_path" else ".manifest.json"
    outside_path = _write_nonempty(
        tmp_path / f"outside-{returned_key}{outside_suffix}",
        b"outside",
    ).resolve()
    if escape_style == "absolute":
        escaped_value = str(outside_path)
    elif escape_style == "traversal":
        escaped_value = str(Path("..") / ".." / outside_path.name)
    else:
        if not outside_path.drive:
            pytest.skip("Windows root-relative paths require a drive")
        escaped_value = str(outside_path)[len(outside_path.drive) :]
        assert escaped_value.startswith(("\\", "/"))

    response_json = {
        "output_path": str(inside_output),
        "manifest_path": str(inside_manifest),
    }
    response_json[returned_key] = escaped_value
    provider = MuseTalkProvider(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json=response_json)
        )
    )

    with pytest.raises(
        RuntimeError,
        match=rf"{returned_key} must be inside output_dir",
    ):
        await provider.generate(
            video_path,
            audio_path,
            output_dir,
            _metadata(),
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
