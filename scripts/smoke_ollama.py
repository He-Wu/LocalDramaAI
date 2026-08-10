import asyncio
from app.providers.ollama_provider import OllamaProvider

async def main():
    provider = OllamaProvider("http://127.0.0.1:11434", "qwen2.5:0.5b", timeout=180)
    print("MODELS", await provider.list_models())
    drama = await provider.generate_drama("雨夜，一名快递员发现包裹会说话。只生成一个场景、两个镜头和一句对白。")
    print("DRAMA", drama.model_dump_json())
    await provider.unload()
    print("UNLOADED")

if __name__ == "__main__":
    asyncio.run(main())
