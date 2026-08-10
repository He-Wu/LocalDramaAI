import json
import httpx
import pytest
from pydantic import ValidationError
from app.providers.ollama_provider import OllamaProvider
from app.schemas.drama import StructuredDrama


@pytest.mark.anyio
async def test_ollama_provider_parses_structured_drama_and_unloads():
    calls = []
    def handler(request: httpx.Request):
        calls.append((request.url.path, json.loads(request.content)))
        return httpx.Response(200, json={"message": {"content": json.dumps({
            "title": "测试", "characters": [{"name":"阿明"}],
            "scenes": [{"order":1,"title":"场景","description":"描述"}],
            "shots": [{"order":1,"title":"镜头","description":"描述"}],
            "dialogues": [{"shot_order":1,"character_name":"阿明","text":"你好"}]
        })}})
    transport = httpx.MockTransport(handler)
    provider = OllamaProvider("http://ollama", transport=transport)
    drama = await provider.generate_drama("一个故事")
    assert isinstance(drama, StructuredDrama)
    assert calls[0][1]["keep_alive"] == 0
    assert "properties" in calls[0][1]["format"]

@pytest.mark.anyio
async def test_ollama_provider_retries_truncated_json():
    attempts = 0
    def handler(request: httpx.Request):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return httpx.Response(200, json={"message": {"content": '{"title":"截断'}})
        return httpx.Response(200, json={"message": {"content": json.dumps({
            "title":"成功", "characters":[{"name":"阿明"}], "scenes":[{"order":1,"title":"场景","description":"描述"}],
            "shots":[{"order":1,"title":"镜头","description":"描述"}], "dialogues":[{"shot_order":1,"text":"你好"}]})}})
    provider = OllamaProvider("http://ollama", transport=httpx.MockTransport(handler))
    result = await provider.generate_drama("故事")
    assert result.title == "成功" and attempts == 2

def test_structured_drama_requires_all_phase_two_entities():
    with pytest.raises(ValidationError):
        StructuredDrama.model_validate({"title":"空","characters":[],"scenes":[],"shots":[],"dialogues":[]})
