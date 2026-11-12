import time

import av
import numpy as np

from gate_anpr.decision import PlateRead
from gate_anpr.store import Lane
from gate_anpr.stream import StreamReader, redact
from test_service import FakeEngine, lane_data, make  # noqa: F401 - fixture reuse


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


def count_frames(path, active):
    reader = StreamReader.__new__(StreamReader)  # drive the decode loop by hand, without its thread
    reader.url, reader.name = str(path), "test"
    reader._active, decoded = (1 if active else 0), 0
    with av.open(str(path)) as c:
        video = c.streams.video[0]
        full = False
        for packet in c.demux(video):
            if not reader._active:
                full = False
            elif packet.is_keyframe:
                full = True
            video.codec_context.skip_frame = "DEFAULT" if full else "NONKEY"
            decoded += len(packet.decode())
    return decoded


def test_idle_decodes_keyframes_only_and_active_decodes_everything(tmp_path):
    path = tmp_path / "gate.mp4"
    clip(path)
    assert count_frames(path, active=False) == 6  # 60 frames, a keyframe every 10
    assert count_frames(path, active=True) == 60


def test_reader_thread_delivers_frames_and_reconnects_at_end(tmp_path):
    path = tmp_path / "gate.mp4"
    clip(path)
    reader = StreamReader(str(path), "test")
    try:
        item = reader.next_frame(0, 10)
        assert item is not None and item[2].shape == (240, 320, 3)
        assert reader.info()["source"] == str(path)
    finally:
        reader.close()


def test_stream_password_is_never_exposed():
    url = "rtsp://admin:s3cret@192.168.1.50:554/Preview_01_main"
    assert redact(url) == "rtsp://***@192.168.1.50:554/Preview_01_main"
    lane = Lane.from_dict({**lane_data(), "stream_url": url})
    assert "s3cret" not in str(lane.public()) and lane.public()["stream_configured"] is True


class FakeReader:
    def __init__(self, frames, healthy=True):
        self.frames, self.ok, self.active = frames, healthy, 0
        self.url = "rtsp://x"

    def healthy(self):
        return self.ok

    def info(self):
        return {"connected": self.ok}

    def activate(self):
        self.active += 1

    def deactivate(self):
        self.active -= 1

    def next_frame(self, after, timeout):
        if after < 0:
            return (time.monotonic(), 0, None)
        if after >= len(self.frames):
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
    assert reader.active == 0 and fake_ha.frames == []  # no snapshot requested


async def test_unhealthy_stream_falls_back_to_snapshots(make, fake_ha):  # noqa: F811
    svc = make([b"far", b"near"], auto_open=True)
    svc._streams[1] = FakeReader([], healthy=False)
    event = await svc.run_session(1, test=False)
    assert event["result"] == "granted" and event["detail"]["frame_source"] == "snapshots"


async def _reads(reads):
    return 3840, 2160, reads, 40
