import time

import av
import numpy as np

from gate_anpr.decision import PlateRead
from gate_anpr.store import Lane
from gate_anpr.stream import StreamReader, redact
from test_service import lane_data, make  # noqa: F401, F811 - pytest fixture reuse


def clip(path, frames=60, gop=10):
    out = av.open(str(path), "w")
    s = out.add_stream("libx264", rate=25)
    s.width, s.height, s.pix_fmt = 320, 240, "yuv420p"
    s.options = {"g": str(gop), "keyint_min": str(gop), "sc_threshold": "0"}
    for i in range(frames):
        frame = av.VideoFrame.from_ndarray(np.full((240, 320, 3), (i * 4) % 255, np.uint8), format="bgr24")
        for p in s.encode(frame):
            out.mux(p)
    for p in s.encode():
        out.mux(p)
    out.close()


def test_on_demand_reader_connects_only_when_active_and_starts_at_a_keyframe(tmp_path):
    path = tmp_path / "gate.mp4"
    clip(path, frames=60, gop=10)
    reader = StreamReader(str(path), "test", linger=0.5)
    try:
        time.sleep(0.5)
        assert reader.info()["state"] == "idle" and reader.latest(0) is None  # nothing fetched while idle
        seq = reader.activate()
        item = reader.next_frame(seq, 10)
        assert item is not None and item[2].shape == (240, 320, 3)
    finally:
        reader.deactivate()
        reader.close()


def test_stream_password_is_never_exposed():
    url = "rtsp://admin:s3cret@192.168.1.50:554/Preview_01_main"
    assert redact(url) == "rtsp://***@192.168.1.50:554/Preview_01_main"
    lane = Lane.from_dict({**lane_data(), "stream_url": url})
    assert "s3cret" not in str(lane.public()) and lane.public()["stream_configured"] is True


class FakeReader:
    """Delivers its frames one by one once activated; `frames=None` means the stream never connects."""

    def __init__(self, frames):
        self.frames, self.active, self.url = frames, 0, "rtsp://x"
        self.connected = frames is not None

    def info(self):
        return {"connected": self.connected}

    def activate(self):
        self.active += 1
        return 0

    def deactivate(self):
        self.active -= 1

    def latest(self, after):
        if not self.frames or after >= len(self.frames):
            return None
        return (time.monotonic(), after + 1, self.frames[after])

    def close(self):
        pass


def plate(text, width):
    return PlateRead(text, text, 0.97, 0.9, (1800, 1400, 1800 + width, 1400 + width // 4))


async def test_session_uses_live_video_instead_of_snapshots(make, fake_ha):  # noqa: F811
    svc = make([], auto_open=True)
    far, near = np.zeros((4, 4, 3), np.uint8), np.ones((4, 4, 3), np.uint8)
    svc.engine.recognise = lambda img, region, t={0: [plate("ABC123", 100)], 1: [plate("ABC123", 120)]}: \
        _reads(t[int(img[0, 0, 0])])
    svc._streams[1] = reader = FakeReader([far, near])
    event = await svc.run_session(1, test=False)
    assert event["result"] == "granted" and event["detail"]["frame_source"] == "video stream"
    assert reader.active == 0


async def test_unhealthy_stream_falls_back_to_snapshots(make, fake_ha):  # noqa: F811
    svc = make([b"far", b"near"], auto_open=True)
    svc._streams[1] = FakeReader(None)
    event = await svc.run_session(1, test=False)
    assert event["result"] == "granted" and event["detail"]["frame_source"] == "snapshots"


async def _reads(reads):
    return 3840, 2160, reads, 40
