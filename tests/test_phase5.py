import pytest
from PIL import Image
from app.providers.comfyui_video_provider import find_video_output
from app.providers.ffmpeg_provider import FFmpegProvider
from app.comfyui.video_workflow import bind_video_workflow, frame_count_for_duration, validate_video_profile


def test_video_output_is_found_in_comfy_history():
    history = {"outputs": {"47": {"images": [{"filename": "LocalDramaAI/phase5_00001_.webm", "subfolder": "", "type": "output"}]}}}
    result = find_video_output(history)
    assert result["filename"].endswith(".webm")


def test_missing_video_output_is_rejected():
    with pytest.raises(RuntimeError): find_video_output({"outputs": {"8": {"images": [{"filename": "still.png"}]}}})


def test_ffmpeg_converts_real_frame_to_atomic_mp4(tmp_path):
    frame = tmp_path / "frame.png"; Image.new("RGB", (64, 64), (30, 80, 120)).save(frame)
    out = FFmpegProvider().image_to_mp4(frame, tmp_path / "clip.mp4", duration=0.25, fps=8)
    assert out.exists() and out.stat().st_size > 1000


def test_video_workflow_binding_sets_first_frame_prompt_seed_and_profile():
    workflow = {"3": {"class_type":"KSampler", "inputs":{"seed":0}}, "6":{"class_type":"CLIPTextEncode", "inputs":{"text":"PROMPT"}}, "57":{"class_type":"LoadImage", "inputs":{"image":"IMAGE"}}, "55":{"class_type":"Wan22ImageToVideoLatent", "inputs":{"width":1280,"height":704,"length":81}}, "47":{"class_type":"SaveWEBM", "inputs":{"filename_prefix":"LocalDramaAI/phase5"}}}
    validate_video_profile({"width":640,"height":368,"frames":41,"fps":16})
    bound = bind_video_workflow(workflow, image_name="first.png", prompt="a moving shot", seed=42, width=640, height=368, frames=41, filename_prefix="LocalDramaAI/phase7/shot-42")
    assert bound["57"]["inputs"]["image"] == "first.png"
    assert bound["6"]["inputs"]["text"] == "a moving shot"
    assert bound["3"]["inputs"]["seed"] == 42
    assert bound["55"]["inputs"]["length"] == 41
    assert bound["47"]["inputs"]["filename_prefix"] == "LocalDramaAI/phase7/shot-42"


def test_frame_count_covers_duration_and_uses_wan_cadence():
    assert frame_count_for_duration(2.78, fps=16) == 49
    frames = frame_count_for_duration(4.0, fps=16)
    assert frames == 65
    assert (frames - 1) / 16 >= 4.0
    assert (frames - 1) % 4 == 0


def test_frame_count_rejects_duration_beyond_locked_profile():
    with pytest.raises(ValueError, match="121"):
        frame_count_for_duration(8.0, fps=16)
