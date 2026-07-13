"""STT/TTS providers.

Cloud (xAI / OpenAI / Google / MiniMax) is the product default (ADR-004).
Optional local full-STT (faster-whisper / Moonshine stretch) lives in
``local_stt`` behind ``provider = "faster_whisper"`` — never for ambient wake.
"""
