"""Voice input: Telegram voice messages -> Groq Whisper -> the brain (4F).

The interface gap that mattered most for the mission: the moments you most
need executive support (driving, walking, avoiding something) are the moments
typing isn't an option. Telegram voice notes are OGG/Opus, which Whisper
accepts directly — no transcoding.
"""

from __future__ import annotations

import logging
from typing import Any

from groq import Groq

logger = logging.getLogger(__name__)

_MAX_AUDIO_BYTES = 24 * 1024 * 1024  # Groq's request cap is 25MB; stay under


class TranscriptionError(RuntimeError):
    """Voice couldn't be transcribed; the caller tells the user plainly."""


def transcribe(config: Any, audio: bytes, filename: str = "voice.ogg") -> str:
    """OGG/Opus bytes -> text via Groq Whisper. Raises TranscriptionError."""
    if not config.groq_api_key:
        raise TranscriptionError(
            "Voice input needs GROQ_API_KEY set — it's optional config, add it "
            "and restart."
        )
    if len(audio) > _MAX_AUDIO_BYTES:
        raise TranscriptionError("That voice note is too large to transcribe.")
    try:
        client = Groq(api_key=config.groq_api_key)
        result = client.audio.transcriptions.create(
            file=(filename, audio),
            model=config.groq_whisper_model,
        )
    except Exception as exc:  # Groq SDK raises many shapes; degrade to a message
        logger.exception("Groq transcription failed")
        raise TranscriptionError(
            "Couldn't transcribe that — try again or type it."
        ) from exc
    text = (result.text or "").strip()
    if not text:
        raise TranscriptionError("I couldn't hear anything in that voice note.")
    return text
