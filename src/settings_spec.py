"""Realtime session settings: defaults, coercion, and the accept payload.

Every tunable the console exposes lives here with a typed default, so the
accept payload can carry only the values that actually differ from the
service defaults.

Grouped as the console presents them:
  basic    — voices per language, delivery style, playback speed, transcription
  advanced — turn detection, sampling
"""

import context

# name -> (default, kind, group, help)
SPEC = {
    # --- identity ----------------------------------------------------------
    "bank_name_en": (
        "Commercial Bank of Qatar",
        "str",
        "basic",
        "How the agent refers to the bank when speaking English.",
    ),
    "bank_name_ar": (
        "البنك التجاري",
        "str",
        "basic",
        "How the agent refers to the bank when speaking Arabic.",
    ),
    # --- basic -------------------------------------------------------------
    "voice_en": (
        "marin",
        "str",
        "basic",
        "Voice used when the caller's preferred language is English.",
    ),
    "voice_ar": (
        "cedar",
        "str",
        "basic",
        "Voice used when the caller's preferred language is Arabic.",
    ),
    "voice_style": (
        context.DEFAULT_VOICE_STYLE,
        "str",
        "basic",
        "How the agent should sound. Folded into the system prompt as the delivery style. "
        "An Arabic style set on a customer's profile still has the final word on register.",
    ),
    "speed": (
        1.0,
        "float",
        "basic",
        "Playback speed of the agent's speech. 1.0 is normal; lower is slower and clearer.",
    ),
    "transcription_model": (
        "whisper",
        "str",
        "basic",
        "Model used to transcribe the caller. Empty disables caller transcription.",
    ),
    # --- advanced ----------------------------------------------------------
    "turn_detection_type": (
        "server_vad",
        "str",
        "advanced",
        "server_vad splits on silence, semantic_vad waits until the caller sounds finished, "
        "none means the caller is never interrupted automatically.",
    ),
    "vad_threshold": (
        0.5,
        "float",
        "advanced",
        "How loud speech must be before it counts. Raise it on a noisy line.",
    ),
    "vad_prefix_padding_ms": (
        300,
        "int",
        "advanced",
        "Audio kept from just before speech starts, so the first syllable is not clipped.",
    ),
    "vad_silence_duration_ms": (
        200,
        "int",
        "advanced",
        "Silence needed before the caller's turn is considered finished.",
    ),
    "vad_eagerness": (
        "auto",
        "str",
        "advanced",
        "Semantic VAD only. How readily the model decides the caller has finished.",
    ),
    "create_response": (
        True,
        "bool",
        "advanced",
        "Reply automatically when the caller stops speaking. Off means the agent waits.",
    ),
    "interrupt_response": (
        True,
        "bool",
        "advanced",
        "Let the caller talk over the agent and cut it off.",
    ),
    "max_output_tokens": (
        0,
        "int",
        "advanced",
        "Cap on the length of a single reply. 0 means no limit.",
    ),
}

# Ranges the service enforces, verified by probing the live endpoint. Used for
# client-side validation so a bad value is caught before a call rather than
# during one.
LIMITS = {
    "speed": (0.25, 1.5),
    "vad_threshold": (0.0, 1.0),
    "vad_prefix_padding_ms": (0, 5000),
    "vad_silence_duration_ms": (0, 5000),
    "max_output_tokens": (0, 32768),
}

# Settings where an empty string is meaningful: it switches the feature off.
# Everything else falls back to its default when blank, so an accidentally
# cleared dropdown cannot send an invalid value to the service.
BLANKABLE = {"transcription_model"}
VOICES = [
    "alloy", "ash", "ballad", "cedar", "coral",
    "echo", "marin", "sage", "shimmer", "verse",
]

TURN_DETECTION_TYPES = ["server_vad", "semantic_vad", "none"]
EAGERNESS_LEVELS = ["low", "auto", "high"]

# Returned by build_turn_detection when the key should be left out entirely,
# which is different from sending an explicit null to switch detection off.
OMIT = object()


def _coerce(value, kind, fallback):
    if value is None or value == "":
        return fallback
    try:
        if kind == "int":
            return int(float(value))
        if kind == "float":
            return float(value)
        if kind == "bool":
            if isinstance(value, bool):
                return value
            return str(value).strip().lower() in ("1", "true", "yes", "on")
        return str(value)
    except (TypeError, ValueError):
        return fallback


def resolve(stored):
    """Merges stored values over the defaults, coercing each to its type."""
    resolved = {}
    for name, (default, kind, _group, _help) in SPEC.items():
        raw = stored.get(name)
        if raw == "" and name in BLANKABLE:
            resolved[name] = ""
        else:
            resolved[name] = _coerce(raw, kind, default)

    # Clamp to the ranges the service enforces, so a stale or hand-edited value
    # cannot fail a live call.
    for name, (low, high) in LIMITS.items():
        value = resolved.get(name)
        if isinstance(value, (int, float)):
            resolved[name] = max(low, min(high, value))

    if resolved.get("turn_detection_type") not in TURN_DETECTION_TYPES:
        resolved["turn_detection_type"] = SPEC["turn_detection_type"][0]
    if resolved.get("vad_eagerness") not in EAGERNESS_LEVELS:
        resolved["vad_eagerness"] = SPEC["vad_eagerness"][0]
    if resolved.get("voice_style") not in context.VOICE_STYLES:
        resolved["voice_style"] = context.DEFAULT_VOICE_STYLE

    return resolved


def choices_for(name):
    """The allowed values for a setting, or None when it is free text."""
    if name in ("voice_en", "voice_ar"):
        return VOICES
    if name == "turn_detection_type":
        return TURN_DETECTION_TYPES
    if name == "vad_eagerness":
        return EAGERNESS_LEVELS
    if name == "voice_style":
        return list(context.VOICE_STYLES)
    return None


def choice_labels_for(name):
    """Readable labels for choices whose stored value is an id, else None."""
    if name == "voice_style":
        return {key: label for key, (label, _prompt) in context.VOICE_STYLES.items()}
    return None


def describe(values):
    """Shape the console renders: value, default, type, group and help text."""
    return [
        {
            "name": name,
            "value": values.get(name, default),
            "default": default,
            "type": kind,
            "group": group,
            "help": help_text,
            "min": LIMITS.get(name, (None, None))[0],
            "max": LIMITS.get(name, (None, None))[1],
            "choices": choices_for(name),
            "choice_labels": choice_labels_for(name),
        }
        for name, (default, kind, group, help_text) in SPEC.items()
    ]


def build_audio(values, language):
    """The `audio` block of the accept payload.

    Only values that differ from the service default are included, so the
    request stays as close as possible to the one already known to work.

    `language` is the matched customer's preferred language, or None when the
    caller's number matched no record. It picks the voice and doubles as the
    caller-transcription hint, so the hint always follows the profile rather
    than an operator setting; an unknown caller is left to auto-detection.
    """
    voice = values.get("voice_ar" if language == "ar" else "voice_en")
    output = {}
    if voice:
        output["voice"] = voice
    speed = values.get("speed", 1.0)
    if speed and abs(float(speed) - 1.0) > 1e-6:
        output["speed"] = float(speed)

    audio_input = {}
    model = (values.get("transcription_model") or "").strip()
    if model:
        transcription = {"model": model}
        if language in context.LANGUAGE_NAMES:
            transcription["language"] = language
        audio_input["transcription"] = transcription

    turn = build_turn_detection(values)
    if turn is not OMIT:
        # An explicit null is how detection is switched off; the service
        # rejects {"type": "none"}.
        audio_input["turn_detection"] = turn

    audio = {}
    if audio_input:
        audio["input"] = audio_input
    if output:
        audio["output"] = output
    return audio


def build_turn_detection(values):
    """Returns the turn_detection value, or OMIT to leave the default alone.

    Shapes verified against the live service:
      - server_vad / semantic_vad are the only accepted `type` values
      - detection is disabled by sending null, not by `{"type": "none"}`
    """
    kind = values.get("turn_detection_type", "server_vad")

    if kind == "none":
        return None

    block = {
        "type": kind,
        "create_response": bool(values.get("create_response", True)),
        "interrupt_response": bool(values.get("interrupt_response", True)),
    }

    if kind == "semantic_vad":
        eagerness = values.get("vad_eagerness", "auto")
        if eagerness and eagerness != "auto":
            block["eagerness"] = eagerness
        return block

    # server_vad: include the tuning knobs only when they differ from default.
    for key, setting in (
        ("threshold", "vad_threshold"),
        ("prefix_padding_ms", "vad_prefix_padding_ms"),
        ("silence_duration_ms", "vad_silence_duration_ms"),
    ):
        value = values.get(setting)
        if value is not None and value != SPEC[setting][0]:
            block[key] = value

    # Nothing customised at all: let the service apply its own defaults.
    if (
        set(block) == {"type", "create_response", "interrupt_response"}
        and block["create_response"]
        and block["interrupt_response"]
    ):
        return OMIT

    return block
