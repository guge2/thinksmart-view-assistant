"""The ASR/TTS websocket shared across turns, so a turn skips the TLS and websocket handshake."""

import logging
import threading
import time

from session import TurnCancelled

# A shared socket idle longer than this is replaced at the next wake, in case it died silently.
LINK_IDLE_SECONDS = 60


class SpeechLink:
    def __init__(self, client):
        self.client = client
        self.link = None
        self.used = 0.0
        self.lock = threading.Lock()

    def get(self, refresh=False):
        with self.lock:
            link = self.link
            if link is None or link.closed or (refresh and time.monotonic() - self.used > LINK_IDLE_SECONDS):
                self.link = self.client.ws_connection(module="audio", timeout=8)
                if link is not None:
                    threading.Thread(target=link.close, name="link-close", daemon=True).start()
            self.used = time.monotonic()
            return self.link

    def drop(self):
        with self.lock:
            link, self.link = self.link, None
        if link is not None:
            threading.Thread(target=link.close, name="link-close", daemon=True).start()

    def prepare(self):
        """Opens or refreshes the socket in the background while the wake cue plays."""
        try:
            self.get(refresh=True)
        except Exception as error:
            logging.warning("语音连接预建失败：%s", error)

    def start(self, factory):
        """Starts a recognition or synthesis task on the shared socket, else on a socket of its own.
        factory(link) builds the task; link None means a socket of its own."""
        try:
            return factory(self.get()).start()
        except TurnCancelled:
            raise
        except Exception as error:
            logging.warning("共享语音连接不可用，改用独立连接：%s", error)
            self.drop()
        return factory(None).start()
