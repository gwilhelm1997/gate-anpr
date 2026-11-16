"""RTSP reader for fast recognition.

On demand (default): connects when a vehicle is detected and disconnects after a quiet period.
Persistent: stays connected but only keeps the packets since the last keyframe, undecoded, so idling
costs almost no CPU. When a session starts, that short group of pictures is decoded at once, giving a
current frame immediately instead of after a reconnect and a wait for the next keyframe.
Frames are handed out undecoded-to-RGB; `to_image` converts only the ones that are actually read.
"""

import logging
import re
import threading
import time

import av

_LOG = logging.getLogger(__name__)
_CREDENTIALS = re.compile(r"//[^@/]+@")
LINGER = 30.0  # stay connected this long after a session, for the next vehicle
MAX_GOP = 600  # packets kept while idle; a camera keyframe interval is far shorter


def to_image(frame):
    """BGR ndarray for a stream frame (anything else, e.g. an ndarray or JPEG, is returned as is)."""
    return frame.to_ndarray(format="bgr24") if isinstance(frame, av.VideoFrame) else frame


def redact(url: str) -> str:
    return _CREDENTIALS.sub("//***@", url or "")


class StreamReader:
    def __init__(self, url: str, name: str, linger: float = LINGER, persistent: bool = False):
        self.url = url
        self.name = name
        self.linger = linger
        self.persistent = persistent
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
        if persistent:
            self._wake.set()

    def info(self) -> dict:
        if self.connected:
            state = "streaming" if self._decoding() else "standby"
        else:
            state = "connecting" if self._active or self.persistent else "idle"
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

    def _decoding(self) -> bool:
        with self._cond:
            return self._active > 0 or time.monotonic() - self._released < self.linger

    def _wanted(self) -> bool:
        return self.persistent or self._decoding()

    def _publish(self, frame) -> None:
        with self._cond:
            self._seq += 1
            self._frame = (time.monotonic(), self._seq, frame)
            self._cond.notify_all()

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
            self.connected, self.last_error = True, None
            _LOG.info("%s: video stream connected", self.name)
            gop: list = []  # packets from the last keyframe on, not yet decoded
            live = False
            for packet in container.demux(video):
                if self._stop.is_set() or not self._wanted():
                    _LOG.info("%s: video stream disconnected (idle)", self.name)
                    return
                if packet.dts is None:
                    continue  # flush packet
                if packet.is_keyframe:
                    gop = [packet]
                elif gop:
                    gop.append(packet)
                    if len(gop) > MAX_GOP:
                        gop = []  # never start mid-group: wait for the next keyframe
                if not self._decoding():
                    live = False
                    continue
                if not live:
                    if not gop:
                        continue  # P-frames need their keyframe; wait for one
                    pending, live = list(gop), True
                else:
                    pending = [packet]
                newest = None
                for i, pkt in enumerate(pending):  # catching up: hand out the keyframe at once, then the newest
                    for frame in pkt.decode():
                        newest = frame
                        if i == 0 and len(pending) > 1:
                            self._publish(frame)
                if newest is not None:
                    self._publish(newest)
