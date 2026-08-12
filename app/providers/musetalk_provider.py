import asyncio
import json
import math
import re
from numbers import Real
from pathlib import Path
from typing import Any

import httpx


_MAX_ERROR_DETAIL_BYTES = 384
_MAX_SUCCESS_JSON_BYTES = 64 * 1024
_SHORT_IO_TIMEOUT_SECONDS = 10.0
_CONTROL_DEADLINE_SECONDS = 30.0
_AUTHORIZATION_CREDENTIAL = re.compile(
    r"(?i)([\"']?authorization[\"']?\s*[:=]\s*[\"']?)"
    r"(?:bearer|basic)\s+[^,\s\"'}\]]+"
)
_SENSITIVE_DETAIL_VALUE = re.compile(
    r"(?i)([\"']?(?:api[_-]?key|access[_-]?token|refresh[_-]?token|token|secret|"
    r"password|authorization|cookie)[\"']?\s*[:=]\s*)"
    r"(?:\"[^\"]*\"|'[^']*'|[^,\s}\]]+)"
)


class MuseTalkProvider:
    def __init__(
        self,
        base_url: str = "http://127.0.0.1:8030",
        timeout: float = 1800,
        *,
        transport=None,
    ):
        if (
            isinstance(timeout, bool)
            or not isinstance(timeout, Real)
            or not math.isfinite(float(timeout))
            or timeout <= 0
        ):
            raise ValueError("MuseTalk timeout must be a positive number")
        self.base_url = base_url.rstrip("/")
        self.timeout = float(timeout)
        self._transport = transport

    def _client(self, timeout: httpx.Timeout) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=self.base_url,
            timeout=timeout,
            transport=self._transport,
        )

    @staticmethod
    async def _read_limited(
        response: httpx.Response,
        max_bytes: int,
    ) -> tuple[bytes, bool]:
        content = bytearray()
        async for chunk in response.aiter_bytes():
            remaining = max_bytes + 1 - len(content)
            if remaining <= 0:
                break
            content.extend(chunk[:remaining])
            if len(content) > max_bytes:
                return bytes(content), True
        return bytes(content), False

    @staticmethod
    def _error_detail(response: httpx.Response, content: bytes) -> str:
        def bounded_detail(value: bytes) -> str:
            truncated = len(value) > _MAX_ERROR_DETAIL_BYTES
            detail = value[:_MAX_ERROR_DETAIL_BYTES].decode(
                "utf-8",
                errors="replace",
            )
            detail = " ".join(detail.split())
            detail = _AUTHORIZATION_CREDENTIAL.sub(r"\1<redacted>", detail)
            detail = _SENSITIVE_DETAIL_VALUE.sub(r"\1<redacted>", detail)
            if truncated:
                detail += "..."
            return detail

        detail = bounded_detail(content)
        if detail:
            return detail
        reason_phrase = response.extensions.get("reason_phrase")
        if not isinstance(reason_phrase, bytes):
            reason_phrase = response.reason_phrase.encode("utf-8", errors="replace")
        return bounded_detail(reason_phrase)

    def _timeout_policy(self, long_running: bool) -> tuple[httpx.Timeout, float]:
        overall_timeout = (
            self.timeout
            if long_running
            else min(self.timeout, _CONTROL_DEADLINE_SECONDS)
        )
        short_timeout = min(_SHORT_IO_TIMEOUT_SECONDS, overall_timeout)
        read_timeout = self.timeout if long_running else short_timeout
        return (
            httpx.Timeout(
                connect=short_timeout,
                read=read_timeout,
                write=short_timeout,
                pool=short_timeout,
            ),
            overall_timeout,
        )

    async def _request_object(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
        *,
        long_running: bool = False,
    ) -> dict:
        request_timeout, overall_timeout = self._timeout_policy(long_running)
        try:
            async with asyncio.timeout(overall_timeout):
                async with self._client(request_timeout) as client:
                    request_kwargs = {} if payload is None else {"json": payload}
                    async with client.stream(
                        method,
                        path,
                        **request_kwargs,
                    ) as response:
                        if not response.is_success:
                            content, _ = await self._read_limited(
                                response,
                                _MAX_ERROR_DETAIL_BYTES,
                            )
                            detail = self._error_detail(response, content)
                            raise RuntimeError(
                                "MuseTalk service returned HTTP "
                                f"{response.status_code} for {method} {path}: "
                                f"{detail}"
                            )

                        content, truncated = await self._read_limited(
                            response,
                            _MAX_SUCCESS_JSON_BYTES,
                        )
                        if truncated:
                            raise RuntimeError(
                                "MuseTalk service JSON response exceeded "
                                f"{_MAX_SUCCESS_JSON_BYTES} bytes for "
                                f"{method} {path}"
                            )
                        try:
                            result = json.loads(content)
                        except (ValueError, UnicodeError) as exc:
                            raise RuntimeError(
                                "MuseTalk service returned malformed JSON for "
                                f"{method} {path}"
                            ) from exc
                        if not isinstance(result, dict):
                            raise RuntimeError(
                                "MuseTalk service returned non-object JSON for "
                                f"{method} {path}"
                            )
                        return result
        except (TimeoutError, httpx.TimeoutException) as exc:
            raise RuntimeError(
                f"MuseTalk request timed out for {method} {path}"
            ) from exc
        except httpx.RequestError as exc:
            raise RuntimeError(
                f"MuseTalk request failed for {method} {path} "
                f"({type(exc).__name__})"
            ) from exc

    @staticmethod
    def _source_path(path: Path, label: str) -> Path:
        try:
            resolved = Path(path).expanduser().resolve()
            is_nonempty_file = resolved.is_file() and resolved.stat().st_size > 0
        except (OSError, RuntimeError, TypeError, ValueError):
            is_nonempty_file = False
            resolved = Path(path)
        if not is_nonempty_file:
            raise ValueError(
                f"MuseTalk source {label} must be an existing non-empty file: "
                f"{resolved}"
            )
        return resolved

    @staticmethod
    def _target_duration(metadata: dict) -> float:
        if not isinstance(metadata, dict):
            raise ValueError("MuseTalk metadata must be a dictionary")
        duration = metadata.get("target_duration")
        if (
            isinstance(duration, bool)
            or not isinstance(duration, Real)
            or not math.isfinite(float(duration))
            or duration <= 0
        ):
            raise ValueError("MuseTalk target_duration must be a positive number")
        return float(duration)

    @staticmethod
    def _service_metadata(metadata: dict) -> dict:
        project_id = metadata.get("project_id")
        if not isinstance(project_id, str) or not project_id.strip():
            raise ValueError(
                "MuseTalk metadata project_id must be a non-empty string"
            )
        shot_id = metadata.get("shot_id")
        if not isinstance(shot_id, str) or not shot_id.strip():
            raise ValueError("MuseTalk metadata shot_id must be a non-empty string")
        input_assets = metadata.get("input_assets")
        if (
            not isinstance(input_assets, list)
            or len(input_assets) != 2
            or any(
                not isinstance(asset_id, str) or not asset_id.strip()
                for asset_id in input_assets
            )
        ):
            raise ValueError(
                "MuseTalk metadata input_assets must contain exactly two "
                "non-empty string asset IDs"
            )
        return {
            "project_id": project_id,
            "shot_id": shot_id,
            "input_assets": list(input_assets),
        }

    @staticmethod
    def _output_directory(output_dir: Path) -> Path:
        try:
            resolved = Path(output_dir).expanduser().resolve()
            resolved.mkdir(parents=True, exist_ok=True)
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            raise RuntimeError(
                "MuseTalk output_dir could not be created or resolved"
            ) from exc
        if not resolved.is_dir():
            raise RuntimeError("MuseTalk output_dir is not a directory")
        return resolved

    @staticmethod
    def _returned_path(result: dict, key: str, output_dir: Path) -> Path:
        value = result.get(key)
        if not isinstance(value, str) or not value.strip():
            raise RuntimeError(f"MuseTalk response is missing {key}")
        try:
            path = Path(value).expanduser()
            if not path.is_absolute():
                path = output_dir / path
            resolved = path.resolve()
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            raise RuntimeError(f"MuseTalk response contains an invalid {key}") from exc
        try:
            relative_path = resolved.relative_to(output_dir)
        except ValueError as exc:
            raise RuntimeError(
                f"MuseTalk {key} must be inside output_dir: {resolved}"
            ) from exc
        if not relative_path.parts:
            raise RuntimeError(
                f"MuseTalk {key} must be inside output_dir: {resolved}"
            )
        return resolved

    @staticmethod
    def _require_nonempty_file(path: Path, key: str) -> None:
        try:
            is_nonempty_file = path.is_file() and path.stat().st_size > 0
        except (OSError, ValueError):
            is_nonempty_file = False
        if not is_nonempty_file:
            raise RuntimeError(
                f"MuseTalk {key} is not an existing non-empty file: {path}"
            )

    async def health(self) -> dict:
        return await self._request_object("GET", "/health")

    async def generate(
        self,
        video_path: Path,
        audio_path: Path,
        output_dir: Path,
        metadata: dict,
    ) -> tuple[Path, Path]:
        resolved_video = self._source_path(video_path, "video")
        resolved_audio = self._source_path(audio_path, "audio")
        target_duration = self._target_duration(metadata)
        service_metadata = self._service_metadata(metadata)
        resolved_output_dir = self._output_directory(output_dir)

        result = await self._request_object(
            "POST",
            "/generate",
            {
                "video_path": str(resolved_video),
                "audio_path": str(resolved_audio),
                "output_dir": str(resolved_output_dir),
                "target_duration": target_duration,
                "batch_size": 4,
                "use_float16": True,
                "metadata": service_metadata,
            },
            long_running=True,
        )

        output_path = self._returned_path(result, "output_path", resolved_output_dir)
        manifest_path = self._returned_path(
            result,
            "manifest_path",
            resolved_output_dir,
        )
        if output_path.suffix.lower() != ".mp4":
            raise RuntimeError(
                f"MuseTalk output_path must be an MP4 file: {output_path}"
            )
        self._require_nonempty_file(output_path, "output_path")
        self._require_nonempty_file(manifest_path, "manifest_path")
        return output_path, manifest_path

    async def unload(self) -> dict:
        return await self._request_object("POST", "/unload")
