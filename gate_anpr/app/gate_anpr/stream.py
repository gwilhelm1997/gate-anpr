"""On-demand RTSP reader: connects when a vehicle is detected, disconnects after a quiet period.

No traffic or decoding while no vehicle is present. During a session every frame is decoded, starting
at the first keyframe (P-frames need every earlier frame, so earlier ones would be smeared).
"""

import logging
import re
import threading
import time

import av

_LOG = logging.getLogger(__name__)
_CREDENTIALS = re.compile(r"//[^@/]+@")
LINGER = 30.0  # stay connected this long after a session, for the next vehicle


def redact(url: str) -> str:
    return _CREDENTIALS.sub("//***@", url or "")


class StreamReader:
    def __init__(self, url: str, name: str, linger: float = LINGER):
        self.url = url
        self.name = name
        self.linger = linger
        self._cond = threading.Condition()
        self._frame = None  # (monotonic time, seq, BGR ndarray)
        self._seq = 0
        self._active = 0
        self._released = 0.0
        self._wake = threading.Event()
        self._stop = threading.Event()
        self.connected = False
        self.last_error: str | None = None
        self._thread = threading.Thread(target=self._run, name=f"stream-{name}", daemon=True)
        self._thread.start()

    def info(self) -> dict:
        state = "streaming" if self.connected else ("connecting" if self._active else "idle")
        return {"state": state, "connected": self.connected, "error": self.last_error, "source": redact(self.url)}

    def activate(self) -> int:
        """Start (or keep) streaming. Returns the sequence number to read after."""
        with self._cond:
            self._active += 1
            seq = self._seq
        self._wake.set()
        return seq

    def deactivate(self) -> None:
        with self._cond:
            self._active = max(0, self._active - 1)
            self._released = time.monotonic()

    def latest(self, after_seq: int):
        """Non-blocking: the newest frame decoded after `after_seq`, or None."""
        with self._cond:
            if self._frame and self._frame[1] > after_seq:
                return self._frame
        return None

    def next_frame(self, after_seq: int, timeout: float):
        """Blocking: the newest frame decoded after `after_seq`, or None on timeout."""
        deadline = time.monotonic() + timeout
        with self._cond:
            while not self._stop.is_set():
                if self._frame and self._frame[1] > after_seq:
                    return self._frame
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                self._cond.wait(remaining)
        return None

    def close(self) -> None:
        self._stop.set()
        self._wake.set()
        with self._cond:
            self._cond.notify_all()

    def _wanted(self) -> bool:
        with self._cond:
            return self._active > 0 or time.monotonic() - self._released < self.linger

    def _run(self) -> None:
        while not self._stop.is_set():
            self._wake.wait()
            self._wake.clear()
            if self._stop.is_set():
                return
            while self._wanted() and not self._stop.is_set():
                try:
                    self._stream()
                except Exception as err:  # noqa: BLE001 - sessions fall back to snapshots
                    self.last_error = redact(str(err))
                    _LOG.warning("%s: video stream error (%s)", self.name, self.last_error)
                    self._stop.wait(2)
                finally:
                    self.connected = False

    def _stream(self) -> None:
        with av.open(self.url, options={"rtsp_transport": "tcp", "stimeout": "5000000", "timeout": "5000000"},
                     timeout=10) as container:
            video = container.streams.video[0]
            video.thread_type = "AUTO"
            video.codec_context.skip_frame = "NONKEY"
            self.connected, self.last_error = True, None
            _LOG.info("%s: video stream connected", self.name)
            started = False
            for packet in container.demux(video):
                if self._stop.is_set() or not self._wanted():
                    _LOG.info("%s: video stream disconnected (idle)", self.name)
                    return
                if not started and packet.is_keyframe:
                    started = True
                    video.codec_context.skip_frame = "DEFAULT"
                for frame in packet.decode():
                    image = frame.to_ndarray(format="bgr24")
                    with self._cond:
                        self._seq += 1
                        self._frame = (time.monotonic(), self._seq, image)
                        self._cond.notify_all()
