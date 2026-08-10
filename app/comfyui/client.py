import asyncio, time, uuid
from pathlib import Path
import httpx

class ComfyUIClient:
    def __init__(self, base_url: str, transport=None, timeout: float = 30):
        self.base_url = base_url.rstrip("/"); self.transport = transport; self.timeout = timeout

    async def _client(self): return httpx.AsyncClient(base_url=self.base_url, transport=self.transport, timeout=self.timeout)
    async def health(self):
        async with await self._client() as client:
            response = await client.get("/system_stats")
            return response.is_success
    async def submit_prompt(self, workflow: dict, client_id: str | None = None):
        async with await self._client() as client:
            response = await client.post("/prompt", json={"prompt": workflow, "client_id": client_id or str(uuid.uuid4())})
            response.raise_for_status(); return response.json()["prompt_id"]
    async def wait_for_completion(self, prompt_id: str, poll_seconds: float = 1.0):
        async with await self._client() as client:
            while True:
                response = await client.get(f"/history/{prompt_id}"); response.raise_for_status()
                history = response.json().get(prompt_id)
                if history:
                    status = history.get("status", {})
                    if status.get("status_str") == "error":
                        message = next((m[1].get("exception_message") for m in status.get("messages", []) if m[0] == "execution_error"), "ComfyUI execution failed")
                        raise RuntimeError(message)
                    if status.get("completed"):
                        return history
                await asyncio.sleep(poll_seconds)
    async def download_output(self, filename: str, subfolder: str, output_dir: Path):
        output_dir.mkdir(parents=True, exist_ok=True); temp = output_dir / f"{filename}.tmp"; final = output_dir / filename
        async with await self._client() as client:
            response = await client.get("/view", params={"filename": filename, "subfolder": subfolder, "type": "output"}); response.raise_for_status()
            temp.write_bytes(response.content)
        temp.replace(final); return final
