"""Export the computer's effective Clash Verge configuration for ThinkSmart."""
import argparse
import copy
import json
import os
from pathlib import Path
import secrets
import shutil

import yaml


def write_private(path, text):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as target:
        target.write(text)


def prepare(source, output):
    source = source.resolve()
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

    rules = config.setdefault("rules", [])
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
    for key in (
        "port", "socks-port", "redir-port", "tproxy-port", "listeners",
        "external-controller-tls", "external-controller-pipe", "external-controller-unix",
        "external-controller-cors", "external-ui", "external-ui-url", "external-ui-name",
    ):
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
    output_resolved = output.resolve()
    for provider in (config.get("rule-providers") or {}).values():
        relative = Path(provider["path"])
        if relative.is_absolute() or ".." in relative.parts or relative.drive:
            raise ValueError("Rule cache must have a relative path")
        src_file = (source / relative).resolve()
        target = (output_resolved / relative).resolve()
        if not src_file.is_relative_to(source) or not target.is_relative_to(output_resolved):
            raise ValueError("Rule cache escapes source or output directory")
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src_file, target)
    for name in ("Country.mmdb", "ASN.mmdb", "geoip.dat", "geosite.dat"):
        if (source / name).is_file():
            shutil.copy2(source / name, output / name)
    for name, enabled in (("config.yaml", True), ("bootstrap.yaml", False)):
        exported = copy.deepcopy(config)
        exported["tun"]["enable"] = enabled
        write_private(output / name, yaml.safe_dump(exported, allow_unicode=True, sort_keys=False))
    (output / "selections.json").write_text(json.dumps(selected, ensure_ascii=False), encoding="utf-8")
    # All node credentials and subscription URLs stay in ignored work files.
    write_private(output / "controller.json", json.dumps({"secret": config["secret"]}))
    print(f"Exported {len(config.get('proxies') or [])} nodes, {len(config.get('rule-providers') or {})} rule providers; no credentials printed")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, default=None)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.source is None:
        appdata = os.environ.get("APPDATA")
        if not appdata:
            parser.error("--source is required when APPDATA is not set")
        args.source = Path(appdata) / "io.github.clash-verge-rev.clash-verge-rev"
    prepare(args.source, args.output)
