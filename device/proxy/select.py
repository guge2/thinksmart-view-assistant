#!/usr/bin/python3
"""Restore the exported policy choices through Mihomo's loopback controller."""
import http.client
import json
from pathlib import Path
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

root = Path("/data/thinksmart/proxy")
secret = json.loads((root / "controller.json").read_text(encoding="utf-8"))["secret"]
selected = json.loads((root / "selections.json").read_text(encoding="utf-8"))
opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
headers = {"Authorization": f"Bearer {secret}", "Content-Type": "application/json"}
request = urllib.request.Request("http://127.0.0.1:9090/proxies", headers=headers)
for attempt in range(40):
    try:
        with opener.open(request, timeout=5) as response:
            proxies = json.load(response)["proxies"]
        break
    except urllib.error.HTTPError:
        raise
    except (urllib.error.URLError, TimeoutError, OSError, http.client.HTTPException):
        if attempt == 39:
            raise
        time.sleep(0.25)
for group, choice in selected.items():
    if choice not in proxies.get(group, {}).get("all", []):
        continue
    request = urllib.request.Request(
        "http://127.0.0.1:9090/proxies/" + urllib.parse.quote(group, safe=""),
        data=json.dumps({"name": choice}).encode("utf-8"), headers=headers, method="PUT",
    )
    try:
        with opener.open(request, timeout=5):
            pass
    except (urllib.error.URLError, TimeoutError, OSError, http.client.HTTPException) as error:
        print(f"Skipped policy group {group}: {error}", file=sys.stderr)
print("Restored computer policy selections")
