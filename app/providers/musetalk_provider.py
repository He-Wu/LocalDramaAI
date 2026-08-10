import math
import re
from numbers import Real
from pathlib import Path
from typing import Any

import httpx


_MAX_ERROR_DETAIL_BYTES = 384
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
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self._transport = transport

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=self.base_url,
            timeout=self.timeout,
            transport=self._transport,
        )

    @staticmethod
    def _error_detail(response: httpx.Response) -> str:
        def bounded_detail(value: bytes) -> str:
            truncated = len(value) > _MAX_ERROR_DETAIL_BYTES
            detail = value[:_MAX_ERROR_DETAIL_BYTES].decode(
                "utf-8",
                errors="replace",
            )
            detail = " ".join(detail.split())
            detail = _SENSITIVE_DETAIL_VALUE.sub(r"\1<redacted>", detail)
            if truncated:
                detail += "..."
            return detail

        detail = bounded_detail(response.content)
        if detail:
            return detail
        reason_phrase = response.extensions.get("reason_phrase")
        if not isinstance(reason_phrase, bytes):
            reason_phrase = response.reason_phrase.encode("utf-8", errors="replace")
        return bounded_detail(reason_phrase)

    async def _request_object(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
    ) -> dict:
        try:
            async with self._client() as client:
                if payload is None:
                    response = await client.request(method, path)
                else:
                    response = await client.request(method, path, json=payload)
        except httpx.TimeoutException as exc:
            raise RuntimeError(
                f"MuseTalk request timed out for {method} {path}"
            ) from exc
        except httpx.RequestError as exc:
            raise RuntimeError(
                f"MuseTalk request failed for {method} {path} "
                f"({type(exc).__name__})"
            ) from exc

        if not response.is_success:
            detail = self._error_detail(response)
            raise RuntimeError(
                f"MuseTalk service returned HTTP {response.status_code} for "
                f"{method} {path}: {detail}"
            )

        try:
            result = response.json()
        except (ValueError, UnicodeError) as exc:
            raise RuntimeError(
                f"MuseTalk service returned malformed JSON for {method} {path}"
            ) from exc
        if not isinstance(result, dict):
            raise RuntimeError(
                f"MuseTalk service returned non-object JSON for {method} {path}"
            )
        return result

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
            return path.resolve()
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            raise RuntimeError(f"MuseTalk response contains an invalid {key}") from exc

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
            },
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
