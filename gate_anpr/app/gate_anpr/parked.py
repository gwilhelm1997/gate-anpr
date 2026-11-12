"""Ignore plates in exclusion zones, and plates of vehicles parked in view."""

import time

# Same spot = centre within this fraction of the frame, plate width within this ratio.
SAME_SPOT = 0.03
SAME_SIZE = 0.15
MEMORY = 6 * 3600  # sightings older than this are forgotten


class ParkedFilter:
    def __init__(self, clock=time.time):
        self._clock = clock
        self._seen: list[tuple[float, str, float, float, float]] = []  # time, plate, cx, cy, width (fractions)

    @staticmethod
    def _norm(read, width, height):
        x1, y1, x2, y2 = read.box
        return (x1 + x2) / 2 / width, (y1 + y2) / 2 / height, (x2 - x1) / width

    def keep(self, reads, width, height, exclusions, parked_minutes, session_start):
        """The reads that are neither excluded nor parked. Returns (kept, ignored_reasons)."""
        kept, ignored = [], []
        for read in reads:
            cx, cy, w = self._norm(read, width, height)
            if any(l <= cx <= r and t <= cy <= b for l, t, r, b in exclusions):
                ignored.append(f"{read.text}: exclusion zone")
                continue
            if parked_minutes and self._parked(read.text, cx, cy, w, session_start - parked_minutes * 60):
                ignored.append(f"{read.text}: parked")
                continue
            kept.append(read)
        return kept, ignored

    def _parked(self, plate, cx, cy, w, before):
        return any(t <= before and p == plate and abs(x - cx) <= SAME_SPOT and abs(y - cy) <= SAME_SPOT
                   and abs(sw - w) <= SAME_SIZE * max(sw, w) for t, p, x, y, sw in self._seen)

    def remember(self, frames, at=None):
        """Record where each plate was seen in a finished session."""
        now = self._clock() if at is None else at
        self._seen = [s for s in self._seen if now - s[0] < MEMORY]
        for frame in frames:
            for read in frame.reads:
                if read.text:
                    self._seen.append((now, read.text, *self._norm(read, frame.width, frame.height)))
        self._seen = self._seen[-2000:]
