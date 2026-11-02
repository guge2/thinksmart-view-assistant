#!/usr/bin/python3
"""Read chat credentials from stdin; never print the key."""
import json
import os
from pathlib import Path
import subprocess
import sys
import urllib.error
import urllib.request


def main():
    if os.geteuid() != 0:
        raise SystemExit("must run as root")
    payload = json.loads(sys.stdin.buffer.readline(16385))
    key = str(payload.get("api_key", "")).strip()
    if not key.startswith("sk-or-") or any(c.isspace() for c in key):
        raise SystemExit("invalid OpenRouter API key")
    request = urllib.request.Request("https://openrouter.ai/api/v1/key", headers={"Authorization": "Bearer " + key})
    try:
        with urllib.request.urlopen(request, timeout=20):
            pass
    except urllib.error.HTTPError as error:
        raise SystemExit(f"OpenRouter authentication failed (HTTP {error.code}); credential not changed") from None
    config = {
        "provider": "openrouter",
        "provider_order": ["google-vertex/global"],
        "base_url": "https://openrouter.ai/api/v1",
        "model": payload.get("model", "google/gemini-3.8-flash"),
        "api_key": key,
        "max_tokens": 4096,
        "context_window": 1048576,
    }
    root = Path("/etc/thinksmart-voice")
    root.mkdir(mode=0o755, exist_ok=True)
    path = root / "chat.json"
    temporary = root / "chat.json.new"
    with os.fdopen(os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w", encoding="utf-8") as out:
        json.dump(config, out, ensure_ascii=False)
    os.chmod(temporary, 0o600)
    os.chown(temporary, 0, 0)
    os.replace(temporary, path)
    dropin = Path("/etc/systemd/system/thinksmart-voice.service.d")
    dropin.mkdir(parents=True, exist_ok=True)
    (dropin / "chat.conf").write_text(
        "[Unit]\nWants=mihomo.service\nAfter=mihomo.service\n\n"
        "[Service]\nLoadCredential=chat.json:/etc/thinksmart-voice/chat.json\n",
        encoding="utf-8",
    )
    subprocess.run(["systemctl", "daemon-reload"], check=True)
    print("Chat credential stored for " + config["model"])


if __name__ == "__main__":
    main()
