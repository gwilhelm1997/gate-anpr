"""Multi-frame consensus and passing-vehicle checks. Pure functions: no I/O."""

from collections import Counter
from dataclasses import dataclass, field

from .plates import is_valid_nz

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
    chars: tuple[float, ...] = ()  # confidence of each character of `text` (empty if unknown)

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
    single_read_confidence: float = 0.99


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
    """Accept a registration only when enough frames agree and the vehicle is not just passing."""
    # The largest plate in the region is the vehicle nearest the gate.
    nearest = [(f, max(f.reads, key=lambda r: r.area)) for f in frames if f.reads]
    if not nearest:
        return Verdict(False, "no_plate")

    best_guess = max(nearest, key=lambda fr: fr[1].ocr_conf)[1]
    confident = [(f, r) for f, r in nearest if r.ocr_conf >= rules.min_confidence]
    if not confident:
        return _character_vote(nearest, rules) or Verdict(
            False, "low_confidence", plate=best_guess.text or None, confidence=best_guess.ocr_conf,
            readings=len(nearest))

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
        # A near-certain read with no rival is enough: the plate often leaves the region after one frame.
        if votes == len(valid) and confidence >= rules.single_read_confidence:
            return _checked(agreeing, Verdict(True, "single_confident", **base), rules)
        return _character_vote(nearest, rules) or Verdict(False, "insufficient_frames", **base)
    if votes <= len(valid) - votes:
        return _character_vote(nearest, rules) or Verdict(False, "inconsistent", **base)
    return _checked(agreeing, Verdict(True, "consistent", **base), rules)


def _checked(agreeing, verdict: Verdict, rules: Rules) -> Verdict:
    """Size and passing checks on the frames that support an accepted plate."""
    base = {k: v for k, v in verdict.__dict__.items() if k not in ("accepted", "reason")}
    (_, r0), (f1, r1) = agreeing[0], agreeing[-1]
    growth = r1.width / max(r0.width, 1)
    lateral = abs(r1.centre_x - r0.centre_x) / max(f1.width, 1)
    base.update(growth=round(growth, 3), lateral=round(lateral, 3))

    if r1.width < rules.min_plate_width:
        return Verdict(False, "too_far", **base)
    # Direction is not checked: a plate may grow or shrink. One that only slides sideways is passing.
    size_change = max(growth, 1 / max(growth, 1e-6))
    if lateral > MAX_LATERAL_SHIFT and size_change < LATERAL_GROWTH_EXCUSE:
        return Verdict(False, "passing", **base)
    return Verdict(True, verdict.reason, **base)


def _character_vote(nearest, rules: Rules) -> Verdict | None:
    """Combine frames that each misread or doubted a different character (NBR594, NBR591, VBR594).

    Every character must be read with at least the minimum confidence in at least `min_consistent`
    frames, and more often than any other confident character at that position, so this is as strict
    per character as the whole-plate rule is per plate. Returns None when it cannot decide."""
    reads = [(f, r) for f, r in nearest if r.text and len(r.chars) == len(r.text)]
    if not reads:
        return None
    length, _ = Counter(len(r.text) for _, r in reads).most_common(1)[0]
    reads = [(f, r) for f, r in reads if len(r.text) == length]
    plate, weakest = [], 1.0
    for i in range(length):
        sure = Counter(r.text[i] for _, r in reads if r.chars[i] >= rules.min_confidence)
        ranked = sure.most_common(2)
        if not ranked or ranked[0][1] < rules.min_consistent or (len(ranked) > 1 and ranked[1][1] >= ranked[0][1]):
            return None
        char = ranked[0][0]
        plate.append(char)
        weakest = min(weakest, max(r.chars[i] for _, r in reads if r.text[i] == char))
    text = "".join(plate)
    if not is_valid_nz(text):
        return None
    # Frames that read this plate apart from at most one character support it.
    agreeing = [(f, r) for f, r in reads if sum(a != b for a, b in zip(r.text, text)) <= 1]
    best_f, best_r = max(agreeing, key=lambda fr: fr[1].area)
    verdict = Verdict(True, "character_vote", plate=text, votes=len(agreeing), readings=len(reads),
                      confidence=weakest, best_frame=best_f.index, best_box=best_r.box)
    return _checked(agreeing, verdict, rules)
