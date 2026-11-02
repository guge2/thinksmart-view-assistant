"""Export the computer's effective Clash Verge configuration for ThinkSmart."""
import argparse
import copy
import json
import os
from pathlib import Path
import secrets
import shutil

import yaml


def prepare(source, output):
    output.mkdir(parents=True, exist_ok=True)
    config = yaml.safe_load((source / "clash-verge.yaml").read_text(encoding="utf-8"))
    profiles = yaml.safe_load((source / "profiles.yaml").read_text(encoding="utf-8"))
    profile = next(item for item in profiles["items"] if item["uid"] == profiles["current"])
    selected = {item["name"]: item["now"] for item in profile.get("selected", [])}
    groups = config.get("proxy-groups", [])
    ai_group = next(group["name"] for group in groups if "AI" in group["name"])
    # Selections in Verge's profile are separate from its generated YAML.
    # Explicit groups keep their selection at startup; filtered groups are
    # restored through Mihomo's local API after it has expanded their nodes.
    for group in groups:
        entries = group.get("proxies", [])
        choice = selected.get(group["name"])
        if choice in entries:
            group["proxies"] = [choice] + [name for name in entries if name != choice]

    rules = config["rules"]
    openrouter = f"DOMAIN-SUFFIX,openrouter.ai,{ai_group}"
    if openrouter not in rules:
        index = next((i for i, rule in enumerate(rules) if rule.startswith("RULE-SET,Custom_AI,")), 0)
        rules.insert(index, openrouter)
    config.update({
        "mode": "rule",
        "mixed-port": 7890,
        "allow-lan": False,
        "bind-address": "127.0.0.1",
        "external-controller": "127.0.0.1:9090",
        "secret": secrets.token_urlsafe(32),
        "log-level": "info",
        "profile": {"store-selected": True, "store-fake-ip": True},
    })
    for key in ("external-controller-pipe", "external-controller-unix", "external-controller-cors",
                "external-ui", "external-ui-url", "external-ui-name"):
        config.pop(key, None)
    config.setdefault("dns", {})["listen"] = "127.0.0.1:7874"
    config["tun"] = {
        "enable": True,
        "device": "thinksmart-tun",
        "stack": "mixed",
        "auto-route": True,
        "auto-redirect": False,
        "auto-detect-interface": True,
        "strict-route": False,
        "dns-hijack": ["any:53", "tcp://any:53"],
        "route-exclude-address": ["10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16",
                                  "100.64.0.0/10", "fc00::/7", "fe80::/10"],
    }
    for provider in (config.get("rule-providers") or {}).values():
        relative = Path(provider["path"])
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError("Rule cache must have a relative path")
        target = output / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source / relative, target)
    for name in ("Country.mmdb", "ASN.mmdb", "geoip.dat", "geosite.dat"):
        if (source / name).is_file():
            shutil.copy2(source / name, output / name)
    for name, enabled in (("config.yaml", True), ("bootstrap.yaml", False)):
        exported = copy.deepcopy(config)
        exported["tun"]["enable"] = enabled
        (output / name).write_text(yaml.safe_dump(exported, allow_unicode=True, sort_keys=False), encoding="utf-8")
    (output / "selections.json").write_text(json.dumps(selected, ensure_ascii=False), encoding="utf-8")
    # All node credentials and subscription URLs stay in ignored work files.
    (output / "controller.json").write_text(json.dumps({"secret": config["secret"]}), encoding="utf-8")
    print(f"Exported {len(config['proxies'])} nodes, {len(config['rule-providers'])} rule providers; no credentials printed")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, default=Path(os.environ["APPDATA"]) / "io.github.clash-verge-rev.clash-verge-rev")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    prepare(args.source, args.output)
