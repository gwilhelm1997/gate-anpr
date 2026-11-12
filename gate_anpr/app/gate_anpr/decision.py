"""Multi-frame consensus and approach checks. Pure functions: no I/O."""

from collections import Counter
from dataclasses import dataclass, field

from .plates import is_valid_nz

# Detector boxes jitter a few percent between frames; beyond this the plate is moving away.
SHRINK_TOLERANCE = 0.06
# A plate that slides this far across the frame without growing is passing, not approaching.
MAX_LATERAL_SHIFT = 0.25
LATERAL_GROWTH_EXCUSE = 1.15


@dataclass(frozen=True)
class PlateRead:
    text: str  # normalised
    raw: str
    ocr_conf: float  # 0..1, weakest character
    det_conf: float
    box: tuple[int, int, int, int]  # x1, y1, x2, y2 in full-frame pixels

    @property
    def width(self) -> int:
        return self.box[2] - self.box[0]

    @property
    def area(self) -> int:
        return self.width * (self.box[3] - self.box[1])

    @property
    def centre_x(self) -> float:
        return (self.box[0] + self.box[2]) / 2


@dataclass(frozen=True)
class Frame:
    index: int
    t: float
    width: int
    height: int
    reads: list[PlateRead] = field(default_factory=list)


@dataclass(frozen=True)
class Rules:
    min_confidence: float = 0.90
    min_consistent: int = 2
    min_plate_width: int = 60
    direction: str = "towards"  # vehicles approach the gate towards (plate grows) or away from the camera


@dataclass(frozen=True)
class Verdict:
    accepted: bool
    reason: str
    plate: str | None = None
    votes: int = 0
    readings: int = 0
    confidence: float = 0.0
    growth: float | None = None
    lateral: float | None = None
    best_frame: int | None = None
    best_box: tuple[int, int, int, int] | None = None


def evaluate(frames: list[Frame], rules: Rules) -> Verdict:
    """Accept a registration only when enough frames agree and the vehicle is approaching."""
    # The largest plate in the region is the vehicle nearest the gate.
    nearest = [(f, max(f.reads, key=lambda r: r.area)) for f in frames if f.reads]
    if not nearest:
        return Verdict(False, "no_plate")

    best_guess = max(nearest, key=lambda fr: fr[1].ocr_conf)[1]
    confident = [(f, r) for f, r in nearest if r.ocr_conf >= rules.min_confidence]
    if not confident:
        return Verdict(False, "low_confidence", plate=best_guess.text or None,
                       confidence=best_guess.ocr_conf, readings=len(nearest))

    valid = [(f, r) for f, r in confident if is_valid_nz(r.text)]
    if not valid:
        return Verdict(False, "invalid_format", plate=confident[0][1].text or None,
                       readings=len(confident))

    plate, votes = Counter(r.text for _, r in valid).most_common(1)[0]
    agreeing = [(f, r) for f, r in valid if r.text == plate]
    confidence = sum(r.ocr_conf for _, r in agreeing) / votes
    best_f, best_r = max(agreeing, key=lambda fr: fr[1].area)
    base = dict(plate=plate, votes=votes, readings=len(valid), confidence=confidence,
                best_frame=best_f.index, best_box=best_r.box)

    if votes < rules.min_consistent:
        return Verdict(False, "insufficient_frames", **base)
    if votes <= len(valid) - votes:
        return Verdict(False, "inconsistent", **base)

    (_, r0), (f1, r1) = agreeing[0], agreeing[-1]
    growth = r1.width / max(r0.width, 1)
    lateral = abs(r1.centre_x - r0.centre_x) / max(f1.width, 1)
    base.update(growth=round(growth, 3), lateral=round(lateral, 3))

    if r1.width < rules.min_plate_width:
        return Verdict(False, "too_far", **base)
    # 'Approaching the gate' means growing on a towards-lane and shrinking on an away-lane.
    progress = growth if rules.direction == "towards" else 1 / max(growth, 1e-6)
    if progress < 1 - SHRINK_TOLERANCE:
        return Verdict(False, "wrong_direction", **base)
    if lateral > MAX_LATERAL_SHIFT and progress < LATERAL_GROWTH_EXCUSE:
        return Verdict(False, "passing", **base)
    return Verdict(True, "consistent", **base)
