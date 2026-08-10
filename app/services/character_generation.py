import hashlib
from app.schemas.character import VisualBible

def build_identity_prompt(bible: VisualBible) -> str:
    return (
        f"Character identity, {bible.name}, {bible.age}, {bible.gender}. "
        f"Face: {bible.face}; eyes: {bible.eyes}; nose: {bible.nose}; mouth: {bible.mouth}; "
        f"hair: {bible.hair}; body: {bible.body}; clothes: {bible.clothes}; accessories: {bible.accessories}. "
        f"Visual style: {bible.visual_style}. "
    )

def build_master_prompt(bible: VisualBible) -> str:
    locks = ", ".join(bible.forbidden_changes)
    return build_identity_prompt(bible) + f"One single person only, front-facing neutral full-body portrait, clean background, no collage, no split screen, no duplicate person. 禁止改变: {locks}."

def derive_character_seed(character_id: str) -> int:
    return int(hashlib.sha256(f"{character_id}:master".encode()).hexdigest()[:8], 16)
