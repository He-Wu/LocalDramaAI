from pathlib import Path
import tomllib


def test_setuptools_only_discovers_python_packages():
    config = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))
    find = config["tool"]["setuptools"]["packages"]["find"]
    assert find["include"] == ["app*", "ai_services*"]
    assert find["exclude"] == ["tests*", "scripts*", "comfyui*", "runtime*", "models*"]
