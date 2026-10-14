"""Fixed, bounded macOS voice catalog; no speech text or arbitrary commands."""

from __future__ import annotations

import re
import subprocess
import sys
import time
from threading import RLock


class InstalledVoiceCatalog:
    def __init__(self) -> None:
        self._lock = RLock()
        self._voices: dict[str, str] = {}
        self._catalog_at = float("-inf")
        self.failed = False

    def voice(self, language: str) -> str | None:
        if sys.platform != "darwin":
            return None
        with self._lock:
            if time.monotonic() - self._catalog_at > (5 if self.failed else 300):
                self._voices = {}
                self.failed = False
                try:
                    result = subprocess.run(
                        ["/usr/bin/say", "-v", "?"],
                        stdin=subprocess.DEVNULL,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.DEVNULL,
                        timeout=10,
                        check=True,
                    )
                    if len(result.stdout) > 262_144:
                        raise ValueError("voice catalog too large")
                    for line in result.stdout.decode("utf-8").splitlines()[:1024]:
                        header = line.partition("#")[0].strip().rsplit(None, 1)
                        if len(header) != 2:
                            continue
                        name, locale = header
                        if (
                            not name
                            or name.startswith("-")
                            or len(name) > 128
                            or any(ord(c) < 32 for c in name)
                            or not re.fullmatch(r"[a-zA-Z0-9_-]{1,32}", locale)
                        ):
                            continue
                        base = locale.replace("-", "_").split("_")[0]
                        base = {"nb": "no", "jv": "jw", "fil": "tl", "iw": "he"}.get(
                            base, base
                        )
                        if locale.replace("-", "_") == "zh_HK":
                            base = "yue"
                        if (
                            base not in self._voices
                            or name == "Samantha"
                            or "(" in self._voices[base]
                            and "(" not in name
                        ):
                            self._voices[base] = name
                    # Native English uses Samantha, never a substitute voice.
                    if self._voices.get("en") != "Samantha":
                        self._voices.pop("en", None)
                except (OSError, subprocess.SubprocessError, UnicodeError, ValueError):
                    self.failed = True
                self._catalog_at = time.monotonic()
            return self._voices.get(language)
