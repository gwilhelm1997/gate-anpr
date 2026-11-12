"""Always-connected RTSP reader: keyframes only while idle (cheap), every frame during a session.

Frames are ready the moment a vehicle is detected, so recognition never waits ~1.2 s for a 4K snapshot.
"""

import logging
import re
import threading
import time

import av

_LOG = logging.getLogger(__name__)
_CREDENTIALS = re.compile(r"//[^@/]+@")


def redact(url: str) -> str:
    return _CREDENTIALS.sub("//***@", url or "")


class StreamReader:
    def __init__(self, url: str, name: str):
        self.url = url
        self.name = name
        self._cond = threading.Condition()
        self._frame = None  # (monotonic time, seq, BGR ndarray)
        self._seq = 0
        self._active = 0
        self._stop = threading.Event()
        self.connected = False
        self.last_error: str | None = None
        self._thread = threading.Thread(target=self._run, name=f"stream-{name}", daemon=True)
        self._thread.start()

    def info(self) -> dict:
        age = time.monotonic() - self._frame[0] if self._frame else None
        return {"connected": self.connected, "frame_age_s": round(age, 1) if age is not None else None,
                "error": self.last_error, "source": redact(self.url)}

    def healthy(self) -> bool:
        # Idle decoding still yields a keyframe every few seconds; older means the stream is stuck.
        return self.connected and self._frame is not None and time.monotonic() - self._frame[0] < 10

    def activate(self) -> None:
        with self._cond:
            self._active += 1

    def deactivate(self) -> None:
        with self._cond:
            self._active = max(0, self._active - 1)

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
        with self._cond:
            self._cond.notify_all()

    def _run(self) -> None:
        delay = 2
        while not self._stop.is_set():
            try:
                with av.open(self.url, options={"rtsp_transport": "tcp", "stimeout": "5000000",
                                                "timeout": "5000000"}, timeout=10) as container:
                    video = container.streams.video[0]
                    video.thread_type = "AUTO"
                    self.connected, self.last_error, delay = True, None, 2
                    _LOG.info("%s: video stream connected (%s)", self.name, redact(self.url))
                    full = False
                    for packet in container.demux(video):
                        if self._stop.is_set():
                            return
                        # Start full decoding only at a keyframe: P-frames need every earlier frame,
                        # so switching mid-group would give smeared images until the next keyframe.
                        if not self._active:
                            full = False
                        elif packet.is_keyframe:
                            full = True
                        wanted = "DEFAULT" if full else "NONKEY"
                        if video.codec_context.skip_frame != wanted:
                            video.codec_context.skip_frame = wanted
                        for frame in packet.decode():
                            image = frame.to_ndarray(format="bgr24")
                            with self._cond:
                                self._seq += 1
                                self._frame = (time.monotonic(), self._seq, image)
                                self._cond.notify_all()
            except Exception as err:  # noqa: BLE001 - reconnect forever; sessions fall back to snapshots
                self.last_error = redact(str(err))
                _LOG.warning("%s: video stream error (%s); reconnecting in %ss", self.name, self.last_error, delay)
            finally:
                self.connected = False
            self._stop.wait(delay)
            delay = min(delay * 2, 60)
