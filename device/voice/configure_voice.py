#!/usr/bin/python3
import json
import os
import re
import subprocess
import sys


CREDENTIAL_DIR = "/etc/thinksmart-voice"
CREDENTIAL_FILE = f"{CREDENTIAL_DIR}/aliyun.json"


def fail(message):
    print(message, file=sys.stderr)
    raise SystemExit(2)


def main():
    if os.geteuid() != 0:
        fail("must run as root")

    raw = sys.stdin.buffer.read(16385)
    if not raw or len(raw) > 16384:
        fail("invalid credential payload")
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        fail("invalid credential payload")

    api_key = str(payload.get("api_key", "")).strip()
    workspace_id = str(payload.get("workspace_id", "")).strip()
    region = str(payload.get("region", "cn-beijing")).strip()

    if not 16 <= len(api_key) <= 512 or any(char.isspace() for char in api_key):
        fail("invalid API key")
    if not re.fullmatch(r"[A-Za-z0-9-]{3,128}", workspace_id):
        fail("invalid Workspace ID")
    if region != "cn-beijing":
        fail("only cn-beijing is supported by this configuration")

    credentials = json.dumps(
        {
            "api_key": api_key,
            "workspace_id": workspace_id,
            "region": region,
        },
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")

    os.makedirs(CREDENTIAL_DIR, mode=0o755, exist_ok=True)
    temporary = f"{CREDENTIAL_FILE}.new-{os.getpid()}"
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as target:
            target.write(credentials)
            target.flush()
            os.fsync(target.fileno())
        os.replace(temporary, CREDENTIAL_FILE)
        os.chown(CREDENTIAL_FILE, 0, 0)
        os.chmod(CREDENTIAL_FILE, 0o600)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass

    subprocess.run(["systemctl", "daemon-reload"], check=True)
    subprocess.run(
        ["systemctl", "enable", "--now", "thinksmart-voice.service"],
        check=True,
    )
    print(f"阿里云百炼已配置：{region} / {workspace_id}")


if __name__ == "__main__":
    main()
