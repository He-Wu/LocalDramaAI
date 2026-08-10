import math


def frame_count_for_duration(duration: float, fps: int = 16, minimum: int = 49, maximum: int = 121) -> int:
    """Return the smallest Wan-compatible 4n+1 frame count covering duration."""
    if duration <= 0:
        raise ValueError("video duration must be positive")
    if fps <= 0:
        raise ValueError("fps must be positive")
    frames = max(minimum, math.ceil(duration * fps) + 1)
    remainder = (frames - 1) % 4
    if remainder:
        frames += 4 - remainder
    if frames > maximum:
        raise ValueError(f"required duration exceeds the locked {maximum}-frame video profile")
    return frames


def validate_video_profile(profile: dict):
    width, height, frames = profile["width"], profile["height"], profile["frames"]
    if width % 16 or height % 16: raise ValueError("video dimensions must be divisible by 16")
    if frames < 1 or frames > 121: raise ValueError("video frame count must be 1..121")
    if profile.get("fps", 16) <= 0: raise ValueError("fps must be positive")

def bind_video_workflow(workflow: dict, image_name: str, prompt: str, seed: int, width: int, height: int, frames: int, filename_prefix: str | None = None, fps: int = 16):
    validate_video_profile({"width":width,"height":height,"frames":frames,"fps":fps})
    bound = {node_id: {**node, "inputs": dict(node.get("inputs", {}))} for node_id, node in workflow.items()}
    for node in bound.values():
        if node.get("class_type") == "LoadImage": node["inputs"]["image"] = image_name
        if node.get("class_type") == "CLIPTextEncode" and node["inputs"].get("text") in {"PROMPT", "PHASE5_PROMPT"}:
            node["inputs"]["text"] = prompt
        if node.get("class_type") == "KSampler": node["inputs"]["seed"] = seed
        if node.get("class_type") == "Wan22ImageToVideoLatent":
            node["inputs"].update({"width":width,"height":height,"length":frames})
        if node.get("class_type") == "SaveWEBM" and filename_prefix:
            node["inputs"]["filename_prefix"] = filename_prefix
        if node.get("class_type") == "SaveWEBM":
            node["inputs"]["fps"] = fps
    return bound
