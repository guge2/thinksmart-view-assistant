#!/usr/bin/python3
"""Read chat credentials from stdin; never print the key."""
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request


def atomic_write(path, content, mode):
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        os.fchown(fd, 0, 0)
        os.fchmod(fd, mode)
        with os.fdopen(fd, "w", encoding="utf-8") as out:
            fd = None
            out.write(content)
            out.flush()
            os.fsync(out.fileno())
        os.replace(temporary, path)
    finally:
        if fd is not None:
            os.close(fd)
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def main():
    if os.geteuid() != 0:
        raise SystemExit("must run as root")
    raw = sys.stdin.buffer.read(16385)
    if not raw or len(raw) > 16384:
        raise SystemExit("invalid credential payload")
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise SystemExit("invalid credential payload") from None
    if not isinstance(payload, dict):
        raise SystemExit("invalid credential payload")
    key = str(payload.get("api_key", "")).strip()
    if not key.startswith("sk-or-") or any(c.isspace() for c in key):
        raise SystemExit("invalid OpenRouter API key")
    model = str(payload.get("model") or "google/gemini-3.8-flash").strip()
    if not re.fullmatch(r"[A-Za-z0-9._:/-]{1,128}", model):
        raise SystemExit("invalid model name")
    request = urllib.request.Request("https://openrouter.ai/api/v1/key", headers={"Authorization": "Bearer " + key})
    try:
        with urllib.request.urlopen(request, timeout=20):
            pass
    except urllib.error.HTTPError as error:
        raise SystemExit(f"OpenRouter authentication failed (HTTP {error.code}); credential not changed") from None
    except (urllib.error.URLError, TimeoutError, OSError) as error:
        raise SystemExit(f"OpenRouter authentication request failed ({type(error).__name__}); credential not changed") from None
    config = {
        "provider": "openrouter",
        "provider_order": ["google-vertex/global"],
        "base_url": "https://openrouter.ai/api/v1",
        "model": model,
        "api_key": key,
        "max_tokens": 4096,
        "context_window": 1048576,
    }
    root = Path("/etc/thinksmart-voice")
    root.mkdir(mode=0o755, exist_ok=True)
    atomic_write(root / "chat.json", json.dumps(config, ensure_ascii=False), 0o600)
    dropin = Path("/etc/systemd/system/thinksmart-voice.service.d")
    dropin.mkdir(parents=True, exist_ok=True)
    atomic_write(
        dropin / "chat.conf",
        "[Unit]\nWants=mihomo.service\nAfter=mihomo.service\n\n"
        "[Service]\nLoadCredential=chat.json:/etc/thinksmart-voice/chat.json\n",
        0o644,
    )
    subprocess.run(["systemctl", "daemon-reload"], check=True)
    print("Chat credential stored for " + config["model"])


if __name__ == "__main__":
    main()
