"""Sorts a failed turn into what the screen shows and what the device says about it."""

# Spoken with the local voice when a turn fails before any reply, so the device is never silently stuck.
APOLOGIES = {"network": "网络好像断了，请稍后再试。", "auth": "语音服务的密钥失效了，请在管理页检查。",
             "config": "语音服务还没配置好，请在管理页检查。"}
DEFAULT_APOLOGY = "抱歉，刚才没处理好，请再说一次。"

AUTH_MARKERS = ("http 401", "http 403", "unauthorized", "invalidapikey", "invalid api",
                "accessdenied", "access denied", "forbidden")
NETWORK_MARKERS = ("urlerror", "timed out", "timeout", "connection", "network", "name resolution",
                   "temporary failure", "unreachable", "ssl", "gaierror")


def classify_error(error):
    """One of mic, speaker, config, auth, network, llm or service, judged by the error and its causes."""
    parts = []
    current = error
    while current is not None and len(parts) < 4:
        parts.append(f"{type(current).__name__} {current}")
        current = current.__cause__ or current.__context__
    text = " ".join(parts).lower()
    if "microphone" in text:
        return "mic"
    if "speaker" in text or "pipewire-pulse" in text:
        return "speaker"
    if "credential" in text:
        return "config"
    if any(marker in text for marker in AUTH_MARKERS):
        return "auth"
    if any(marker in text for marker in NETWORK_MARKERS):
        return "network"
    if "empty reply" in text:
        return "llm"
    return "service"


def apology(code):
    return APOLOGIES.get(code, DEFAULT_APOLOGY)
