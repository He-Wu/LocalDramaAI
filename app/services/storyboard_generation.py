import hashlib
from app.schemas.character import VisualBible
from app.services.character_generation import build_identity_prompt

def build_storyboard_prompt(bible: VisualBible, shot_description: str) -> str:
    locks = ", ".join(bible.forbidden_changes)
    return build_identity_prompt(bible) + f"Storyboard first frame: {shot_description}. Keep the exact same person and wardrobe. 禁止改变: {locks}. Cinematic composition, consistent face."

def derive_shot_seed(character_id: str, shot_order: int) -> int:
    return int(hashlib.sha256(f"{character_id}:shot:{shot_order}".encode()).hexdigest()[:8], 16)
