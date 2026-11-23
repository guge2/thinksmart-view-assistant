"""Stand-ins for the device-only SDKs (DashScope, sherpa-onnx), so the voice modules import on a
development machine. Import this before any voice module."""

import sys
from unittest.mock import MagicMock

for name in ("dashscope", "sherpa_onnx"):
    try:
        __import__(name)
    except ImportError:
        sys.modules[name] = MagicMock(name=name)
