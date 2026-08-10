import json
import httpx
from pydantic import BaseModel
from app.schemas.drama import StructuredDrama

class OllamaProvider:
    def __init__(self, base_url: str, model: str = "qwen2.5:7b", transport=None, timeout: float = 120):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self._transport = transport
        self.timeout = timeout

    async def _client(self):
        return httpx.AsyncClient(base_url=self.base_url, transport=self._transport, timeout=self.timeout)

    async def health(self) -> bool:
        async with await self._client() as client:
            response = await client.get("/api/tags")
            return response.is_success

    async def list_models(self) -> list[str]:
        async with await self._client() as client:
            response = await client.get("/api/tags"); response.raise_for_status()
            return [m["name"] for m in response.json().get("models", [])]

    async def generate_drama(self, story: str) -> StructuredDrama:
        schema = StructuredDrama.model_json_schema()
        payload = {"model": self.model, "stream": False, "keep_alive": 0, "options": {"temperature": 0, "num_predict": 1024},
                   "format": schema,
                   "messages": [{"role": "system", "content": "只输出符合 JSON Schema 的中文短剧结构，不要 Markdown。characters、scenes、shots、dialogues 四个数组都必须至少有一个元素；每个 character 必须给出 name、age、gender、face、eyes、nose、mouth、hair、body、clothes、accessories、visual_style、personality；shots 使用全剧扁平镜头列表，每个 shot 必须给出 shots.scene_order 和 shots.character_name；dialogues 的 shot_order 必须对应镜头。"},
                                {"role": "user", "content": story}]}
        async with await self._client() as client:
            last_error = None
            for attempt in range(2):
                response = await client.post("/api/chat", json=payload); response.raise_for_status()
                content = response.json().get("message", {}).get("content", "")
                try:
                    return StructuredDrama.model_validate(json.loads(content))
                except (json.JSONDecodeError, ValueError) as exc:
                    last_error = exc
                    payload["messages"].append({"role": "user", "content": "上一次输出不完整。请重新输出完整、紧凑的 JSON，所有数组至少一个元素，不要解释。"})
            raise ValueError(f"Ollama returned invalid structured JSON after retry: {last_error}")

    async def unload(self):
        # Ollama unload is a zero-duration generate request.
        async with await self._client() as client:
            response = await client.post("/api/generate", json={"model": self.model, "prompt": "", "stream": False, "keep_alive": 0})
            response.raise_for_status()
