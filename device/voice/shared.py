"""The display's system module (ui/system.py) is the device library shared by voice, display and
gateway: settings, weather, volume, the wake phrase and atomic file writes. It is installed with the
display; in a repository checkout (tests) it is imported from ../ui instead."""

import os
import sys

_INSTALLED = "/usr/local/lib/thinksmart-ui"
_CHECKOUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "ui")
# Appended, so the display's modules (app.py among them) never shadow the voice service's own.
sys.path.append(_INSTALLED if os.path.isdir(_INSTALLED) else _CHECKOUT)

import system  # noqa: E402

__all__ = ["system"]
