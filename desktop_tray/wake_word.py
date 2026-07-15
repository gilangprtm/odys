#!/usr/bin/env python3
"""Wake word detection untuk "Sira" — offline Vosk keyphrase.

- Download small model otomatis (~40MB) kalau belum ada
- Listen via sounddevice, trigger callback when "sira" detected
- Thread-safe start/stop
"""
from __future__ import annotations

import json
import logging
import os
import queue
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Callable

import sounddevice as sd

log = logging.getLogger(__name__)

MODEL_URL = "https://alphacephei.com/vosk/models/vosk-model-small-en-us-0.15.zip"
MODEL_DIR_NAME = "vosk-model-small-en-us-0.15"
MODEL_CACHE = Path(tempfile.gettempdir()) / "odys-vosk"

# Indonesian small model (better for "Sira" detection)
MODEL_URL_ID = "https://alphacephei.com/vosk/models/vosk-model-small-id-0.4.zip"
MODEL_DIR_NAME_ID = "vosk-model-small-id-0.4"


def _ensure_model() -> Path | None:
    """Download Vosk model if missing. Returns model path or None."""
    # Try ID model first (better for "Sira"), fallback to EN
    for model_dir, url in [(MODEL_DIR_NAME_ID, MODEL_URL_ID), (MODEL_DIR_NAME, MODEL_URL)]:
        model_path = MODEL_CACHE / model_dir
        if model_path.is_dir():
            return model_path

    # Download
    print("  ⬇️  Download Vosk model (~40MB)...")
    import urllib.request
    import zipfile

    MODEL_CACHE.mkdir(parents=True, exist_ok=True)
    zip_path = MODEL_CACHE / "model.zip"

    try:
        urllib.request.urlretrieve(MODEL_URL_ID, zip_path)
        with zipfile.ZipFile(zip_path, "r") as zf:
            zf.extractall(MODEL_CACHE)
        zip_path.unlink()
        p = MODEL_CACHE / MODEL_DIR_NAME_ID
        if p.is_dir():
            return p
    except Exception as e:
        log.warning(f"Gagal download ID model: {e}")

    # Try EN
    try:
        urllib.request.urlretrieve(MODEL_URL, zip_path)
        with zipfile.ZipFile(zip_path, "r") as zf:
            zf.extractall(MODEL_CACHE)
        zip_path.unlink()
        p = MODEL_CACHE / MODEL_DIR_NAME
        if p.is_dir():
            return p
    except Exception as e:
        log.warning(f"Gagal download EN model: {e}")

    if zip_path.exists():
        zip_path.unlink()
    return None


class WakeWordDetector:
    """Listen for "Sira" in microphone audio via Vosk keyphrase mode.

    Usage:
        def on_wake():
            print("Sira detected!")

        d = WakeWordDetector(on_wake)
        d.start()
        ...
        d.stop()
    """

    def __init__(
        self,
        on_detected: Callable[[], None],
        keyphrase: str = "sira",
        model_path: str | Path | None = None,
    ):
        self.on_detected = on_detected
        self.keyphrase = keyphrase.lower().strip()
        self._model_path = Path(model_path) if model_path else _ensure_model()
        self._queue: queue.Queue[bytes | None] = queue.Queue()
        self._thread: threading.Thread | None = None
        self._running = threading.Event()

    @property
    def available(self) -> bool:
        return self._model_path is not None and self._model_path.is_dir()

    def start(self):
        if self._running.is_set():
            return
        if not self.available:
            log.warning("Vosk model not available — wake word disabled")
            return

        self._running.set()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        log.info("Wake word listener started (keyphrase=%s)", self.keyphrase)

    def stop(self):
        self._running.clear()
        self._queue.put_nowait(None)  # unblock audio callback

    def _run(self):
        try:
            from vosk import KaldiRecognizer, Model as VoskModel
        except ImportError:
            log.error("vosk not installed — pip install vosk")
            return

        model = VoskModel(str(self._model_path))
        rec = KaldiRecognizer(model, 16000)
        rec.SetWords(False)

        # Set keyphrase for wake-word-only mode
        rec.SetKeyphrase(self.keyphrase)

        def audio_callback(indata, frames, time_info, status):
            if status:
                log.debug(f"Audio status: {status}")
            if self._running.is_set():
                self._queue.put(bytes(indata))

        try:
            stream = sd.RawInputStream(
                samplerate=16000,
                blocksize=8000,
                device=None,
                dtype="int16",
                channels=1,
                callback=audio_callback,
            )
            with stream:
                while self._running.is_set():
                    data = self._queue.get()
                    if data is None:
                        break
                    if rec.AcceptWaveform(data):
                        result = json.loads(rec.Result())
                        text = result.get("text", "").strip().lower()
                        if self.keyphrase in text:
                            log.info("Wake word detected: %s", text)
                            try:
                                self.on_detected()
                            except Exception as e:
                                log.error("Wake callback error: %s", e)
        except Exception as e:
            log.error("Wake word stream error: %s", e)
