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
            "title": "测试", "characters": [{
                "name":"阿明", "age":"成年", "gender":"男", "face":"自然脸型",
                "eyes":"自然眼型", "hair":"黑色短发", "body":"自然体型",
                "clothes":"日常服装", "visual_style":"写实",
            }],
            "scenes": [{"order":1,"title":"场景","description":"描述"}],
            "shots": [{
                "order":1,"scene_order":1,"character_name":"阿明",
                "title":"镜头","description":"描述",
            }],
            "dialogues": [{"shot_order":1,"character_name":"阿明","text":"你好"}]
        })}})
    transport = httpx.MockTransport(handler)
    provider = OllamaProvider("http://ollama", transport=transport)
    drama = await provider.generate_drama("一个故事")
    assert isinstance(drama, StructuredDrama)
    assert calls[0][1]["keep_alive"] == 0
    assert "properties" in calls[0][1]["format"]
    system_prompt = calls[0][1]["messages"][0]["content"]
    assert "shots.scene_order" in system_prompt
    assert "shots.character_name" in system_prompt
    required_character_fields = (
        "name", "age", "gender", "face", "eyes", "nose", "mouth", "hair", "body",
        "clothes", "accessories", "visual_style", "personality",
    )
    assert (
        f"每个 character 必须给出 {'、'.join(required_character_fields)}"
        in system_prompt
    )

@pytest.mark.anyio
async def test_ollama_provider_retries_truncated_json():
    attempts = 0
    def handler(request: httpx.Request):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return httpx.Response(200, json={"message": {"content": '{"title":"截断'}})
        return httpx.Response(200, json={"message": {"content": json.dumps({
            "title":"成功", "characters":[{
                "name":"阿明", "age":"成年", "gender":"男", "face":"自然脸型",
                "eyes":"自然眼型", "hair":"黑色短发", "body":"自然体型",
                "clothes":"日常服装", "visual_style":"写实",
            }], "scenes":[{"order":1,"title":"场景","description":"描述"}],
            "shots":[{"order":1,"scene_order":1,"character_name":"阿明","title":"镜头","description":"描述"}],
            "dialogues":[{"shot_order":1,"text":"你好"}]})}})
    provider = OllamaProvider("http://ollama", transport=httpx.MockTransport(handler))
    result = await provider.generate_drama("故事")
    assert result.title == "成功" and attempts == 2

def test_structured_drama_requires_all_phase_two_entities():
    with pytest.raises(ValidationError):
        StructuredDrama.model_validate({"title":"空","characters":[],"scenes":[],"shots":[],"dialogues":[]})
