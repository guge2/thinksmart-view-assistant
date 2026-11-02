#!/usr/bin/python3
"""Store the difficult-task model credential supplied on stdin."""
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
    config = {
        "provider": "openai", "api": "openai-responses",
        "base_url": "https://api.openai.com/v1", "model": model,
        "api_key": key, "reasoning_effort": "high",
        "max_tokens": 16384, "context_window": 1050000,
    }
    root = Path("/etc/thinksmart-voice")
    root.mkdir(mode=0o755, exist_ok=True)
    temporary = root / "expert.json.new"
    with os.fdopen(os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w", encoding="utf-8") as out:
        json.dump(config, out)
    os.chmod(temporary, 0o600)
    os.chown(temporary, 0, 0)
    os.replace(temporary, root / "expert.json")
    dropin = Path("/etc/systemd/system/thinksmart-voice.service.d")
    dropin.mkdir(parents=True, exist_ok=True)
    (dropin / "expert.conf").write_text(
        "[Unit]\nWants=mihomo.service\nAfter=mihomo.service\n\n"
        "[Service]\nLoadCredential=expert.json:/etc/thinksmart-voice/expert.json\n",
        encoding="utf-8",
    )
    subprocess.run(["systemctl", "daemon-reload"], check=True)
    print("Difficult-task credential stored for " + model + " (high)")


if __name__ == "__main__":
    main()
