import json
import re
import requests
from config import LLM_URL, LLM_MODEL


def check_alive(timeout=5) -> dict:
    r = requests.get(f"{LLM_URL}/api/tags", timeout=timeout)
    r.raise_for_status()
    return r.json()


SYSTEM_PROMPT = """You translate a user's natural language image request into a JSON generation spec for a Stable Diffusion pipeline.

Available checkpoints: {checkpoints}

Available LoRAs (style/detail adapters that stack on top of the checkpoint): {loras}

Output ONLY a JSON object with these fields:
{{
  "positive_prompt": "the user's AFFIRMATIVE original request content, verbatim and unmodified, followed by a comma and additional style/quality/lighting tags you append",
  "negative_prompt": "things to avoid, e.g. low quality, blurry, deformed, PLUS anything the user explicitly excluded or negated (see rules below)",
  "checkpoint": "one of the available checkpoints above, pick the best fit",
  "orientation": "square" or "portrait" or "landscape" — pick based on the subject
    (a single person/object close-up -> portrait; a scene/environment/group -> landscape;
    ambiguous or centered subject -> square). This selects a proper SDXL-trained
    resolution bucket in code, not raw pixel dimensions.
  "steps": integer 15-40,
  "cfg": float 4.0-9.0,
  "sampler_name": one of "euler", "euler_ancestral", "dpmpp_2m", "dpmpp_sde",
  "seed": integer, use -1 for random unless user specifies one,
  "hires": true or false — set true only if the user asks for extra detail/quality
    (e.g. "highly detailed", "intricate", "high resolution", "8k", "sharp fine detail",
    "large print/poster quality"). This triggers a real second refinement pass, not just
    bigger base dimensions, so reserve it for requests that actually ask for it — default
    false for ordinary requests.
  "face_fix": true or false — set true whenever the image will contain a recognizable
    face that would benefit from refinement (a person, portrait, selfie, group photo,
    a named character, or a close-up animal/pet face). Set false for landscapes,
    objects, abstract scenes, or anything without a clear face in frame. This runs a
    dedicated face-detection-and-refine pass; it costs extra time, so only set it when
    a face is actually likely to be in the image.
  "loras": a list of 0-2 objects {{"name": "<one of the available LoRAs above>", "strength": float}}.
    Only include a LoRA when the request clearly calls for what it does — an
    "add-detail"/"detail-tweaker" LoRA for requests emphasizing extreme detail/texture
    (strength 1.0-1.5), a "cinematic"/"style" LoRA for film-look/cinematic-lighting
    requests (strength 0.6-1.0), an "anime"/"enhancer" LoRA for anime/manga-style
    requests (strength 0.6-1.0). Default to an empty list for ordinary requests — do
    not stack LoRAs speculatively.
}}

Rules:
- NEVER drop, replace, summarize, or reword any subject, object, action, or detail the user AFFIRMATIVELY named. Every noun and verb the user states as present must appear in positive_prompt exactly as they wrote it. This verbatim rule applies ONLY to affirmative content — do not apply it to anything the user is negating or excluding.
- Food item disambiguation: When the user mentions a food item (dumpling, bun, bread, fruit, vegetable, pastry, etc.) and does NOT explicitly ask for a character, mascot, or humanoid version, assume they want the actual FOOD OBJECT itself. Clarify in positive_prompt: "dumpling (food, not a character)" or "steamed dumpling" or "mushroom (fungus, not an anthropomorphic character)". Add to negative_prompt: "humanoid, character, face, eyes" to prevent the diffusion model from rendering food as a cute creature.
- Field routing for negations: if the user states an exclusion or negation (e.g. "no legs", "without a face", "not wearing shoes", "X instead of Y" where Y is excluded), that excluded noun/phrase must NEVER appear in positive_prompt. Instead, convert it to positive form and add it to negative_prompt (user says "no human face" -> negative_prompt contains "human face"; user says "a single stem instead of legs" -> negative_prompt contains "legs").
- positive_prompt MUST start with the user's affirmative request content unchanged (with negated phrases removed), then ", " plus your added tags (style/composition/lighting/quality). Only ADD to the affirmative content, never rewrite it.
- If checkpoint name suggests "lightning" or "turbo", prefer steps 6-10 and cfg 1.5-2.5.
- Return ONLY the JSON object, no explanation, no markdown fences.
"""

AMBIGUITY_SYSTEM_PROMPT = """You are a pre-flight checker for an image generation pipeline. Given a user's natural language image request, decide whether it contains a genuinely ambiguous, compound, or self-conflicting PHYSICAL description that would likely cause an inconsistent or broken render if generated as-is.

Only flag real problems, such as:
- Anatomy substitutions or conflicts (e.g. "a stem instead of legs" without saying how many limbs, how it stands, etc.)
- Conflicting counts (e.g. "three arms but only two hands")
- Style conflicts that cannot coexist in one image (e.g. "photorealistic cartoon")
- Underspecified compound creature/object anatomy that a generic model is likely to render inconsistently

Do NOT flag ordinary, everyday requests as ambiguous, even if they are vague. Vague-but-fine requests (e.g. "a nice landscape", "a golden retriever at sunset", "a portrait of a woman") should always return needs_clarification: false. Bias strongly toward false — most requests are fine as-is. Only flag the rare case where the ambiguity would plausibly cause a broken or inconsistent render.

Output ONLY a JSON object with these fields:
{
  "needs_clarification": true or false,
  "question": "ONE short, targeted clarifying question if needs_clarification is true, otherwise null"
}

Return ONLY the JSON object, no explanation, no markdown fences.
"""

# Recognized negation phrasings, stripped from the raw user prompt before the
# verbatim-containment safety net check runs, so negated content never gets
# force-reinjected into positive_prompt.
_NEGATION_PATTERNS = [
    re.compile(r"\binstead of\s+\w+(?:\s+\w+)?\b", re.IGNORECASE),
    re.compile(r"\bwithout\s+\w+(?:\s+\w+)?\b", re.IGNORECASE),
    re.compile(r"\bno\s+\w+(?:\s+\w+)?\b", re.IGNORECASE),
    re.compile(r"\bnot\s+\w+ing\b", re.IGNORECASE),
]


def _strip_negations(text: str) -> str:
    """Remove recognized negation phrasing from text, collapsing extra whitespace/punctuation."""
    stripped = text
    for pattern in _NEGATION_PATTERNS:
        stripped = pattern.sub("", stripped)
    # collapse leftover double spaces/commas left behind by removed phrases
    stripped = re.sub(r"\s*,\s*,", ",", stripped)
    stripped = re.sub(r"\s{2,}", " ", stripped)
    stripped = stripped.strip(" ,")
    return stripped


class LLMError(Exception):
    pass


# Native SDXL-trained resolution buckets (1024x1024-ish pixel budget) --
# real SDXL/Juggernaut/DreamShaper checkpoints were trained on these bucket
# shapes, not arbitrary width/height, so using them (vs. e.g. 512x512, which
# is an SD1.5-era resolution) noticeably improves composition/coherence.
_ORIENTATION_BUCKETS = {
    "square": (1024, 1024),
    "portrait": (896, 1152),
    "landscape": (1152, 896),
}


def _scheduler_for_checkpoint(checkpoint: str) -> str:
    # Lightning/turbo-distilled checkpoints are tuned for very few steps and
    # converge poorly with karras; sgm_uniform is the commonly recommended
    # scheduler for them. Standard (non-distilled) SDXL checkpoints benefit
    # from karras, which noticeably improves detail/coherence over "normal".
    name = checkpoint.lower()
    if "lightning" in name or "turbo" in name or "lcm" in name:
        return "sgm_uniform"
    return "karras"


def build_spec(user_prompt: str, checkpoints: list[str], loras: list[str] | None = None) -> dict:
    loras = loras or []
    system = SYSTEM_PROMPT.format(checkpoints=", ".join(checkpoints), loras=", ".join(loras) or "none installed")

    r = requests.post(
        f"{LLM_URL}/api/chat",
        json={
            "model": LLM_MODEL,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user_prompt},
            ],
            "format": "json",
            "stream": False,
            "think": False,
            "options": {"temperature": 0.7},
        },
        timeout=120,
    )
    r.raise_for_status()
    result = r.json()
    content = result.get("message", {}).get("content", "")

    try:
        spec = json.loads(content)
    except json.JSONDecodeError as e:
        raise LLMError(f"LLM returned invalid JSON: {content[:500]}") from e

    required = ["positive_prompt", "negative_prompt", "checkpoint", "orientation",
                "steps", "cfg", "sampler_name", "seed"]
    missing = [k for k in required if k not in spec]
    if missing:
        raise LLMError(f"LLM spec missing fields {missing}: {spec}")

    if spec["checkpoint"] not in checkpoints:
        spec["checkpoint"] = checkpoints[0]

    spec["width"], spec["height"] = _ORIENTATION_BUCKETS.get(
        spec.get("orientation"), _ORIENTATION_BUCKETS["square"]
    )
    spec["scheduler"] = _scheduler_for_checkpoint(spec["checkpoint"])

    spec["hires"] = bool(spec.get("hires", False))
    spec["face_fix"] = bool(spec.get("face_fix", False))

    valid_loras = []
    for entry in spec.get("loras") or []:
        if not isinstance(entry, dict) or entry.get("name") not in loras:
            continue
        try:
            strength = float(entry.get("strength", 0.8))
        except (TypeError, ValueError):
            strength = 0.8
        strength = max(0.0, min(strength, 2.0))
        valid_loras.append({"name": entry["name"], "strength": strength})
    spec["loras"] = valid_loras[:2]

    affirmative_prompt = _strip_negations(user_prompt).strip()
    if affirmative_prompt and affirmative_prompt.lower() not in spec["positive_prompt"].lower():
        spec["positive_prompt"] = f"{affirmative_prompt}, {spec['positive_prompt']}"

    if spec.get("seed", -1) in (-1, None):
        import random
        spec["seed"] = random.randint(0, 2**32 - 1)

    return spec


def assess_ambiguity(user_prompt: str) -> dict:
    """One Ollama call, format=json, flagging genuinely ambiguous/compound/conflicting
    physical descriptions that would likely cause an inconsistent render. Fails safe:
    any parse/schema/request error results in needs_clarification=False so a broken
    pre-flight never blocks generation."""
    fallback = {"needs_clarification": False, "question": None}

    try:
        r = requests.post(
            f"{LLM_URL}/api/chat",
            json={
                "model": LLM_MODEL,
                "messages": [
                    {"role": "system", "content": AMBIGUITY_SYSTEM_PROMPT},
                    {"role": "user", "content": user_prompt},
                ],
                "format": "json",
                "stream": False,
                "think": False,
                "options": {"temperature": 0.3},
            },
            timeout=30,
        )
        r.raise_for_status()
        result = r.json()
        content = result.get("message", {}).get("content", "")
        parsed = json.loads(content)
    except (requests.RequestException, ValueError, json.JSONDecodeError):
        return fallback

    if not isinstance(parsed, dict) or "needs_clarification" not in parsed:
        return fallback

    needs_clarification = bool(parsed.get("needs_clarification"))
    question = parsed.get("question") if needs_clarification else None
    if needs_clarification and not isinstance(question, str):
        # malformed: flagged as ambiguous but no usable question, fail safe
        return fallback

    return {"needs_clarification": needs_clarification, "question": question}
