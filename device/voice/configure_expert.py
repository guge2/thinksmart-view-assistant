#!/usr/bin/python3
"""Store the difficult-task model credential supplied on stdin."""
import json
import os
from pathlib import Path
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
    if not key.startswith("sk-") or any(c.isspace() for c in key):
        raise SystemExit("invalid OpenAI API key")
    model = "gpt-6.1-sol"
    request = urllib.request.Request(
        "https://api.openai.com/v1/models/" + model,
        headers={"Authorization": "Bearer " + key},
    )
    try:
        with urllib.request.urlopen(request, timeout=30):
            pass
    except urllib.error.HTTPError as error:
        raise SystemExit(f"OpenAI model access failed (HTTP {error.code}); credential not changed") from None
    except (urllib.error.URLError, TimeoutError, OSError) as error:
        raise SystemExit(f"OpenAI model access request failed ({type(error).__name__}); credential not changed") from None
    config = {
        "provider": "openai", "api": "openai-responses",
        "base_url": "https://api.openai.com/v1", "model": model,
        "api_key": key, "reasoning_effort": "high",
        "max_tokens": 16384, "context_window": 1050000,
    }
    root = Path("/etc/thinksmart-voice")
    root.mkdir(mode=0o755, exist_ok=True)
    atomic_write(root / "expert.json", json.dumps(config, ensure_ascii=False), 0o600)
    dropin = Path("/etc/systemd/system/thinksmart-voice.service.d")
    dropin.mkdir(parents=True, exist_ok=True)
    atomic_write(
        dropin / "expert.conf",
        "[Unit]\nWants=mihomo.service\nAfter=mihomo.service\n\n"
        "[Service]\nLoadCredential=expert.json:/etc/thinksmart-voice/expert.json\n",
        0o644,
    )
    subprocess.run(["systemctl", "daemon-reload"], check=True)
    print("Difficult-task credential stored for " + model + " (high)")


if __name__ == "__main__":
    main()
