from gate_anpr.decision import Frame, PlateRead, Rules, evaluate
from gate_anpr.plates import is_valid_nz, normalise

RULES = Rules(min_confidence=0.9, min_consistent=2, min_plate_width=60)


def read(text, width=120, conf=0.97, cx=1900, cy=1500):
    return PlateRead(text, text, conf, 0.8, (cx - width // 2, cy - width // 8, cx + width // 2, cy + width // 8))


def frames(*reads_per_frame):
    return [Frame(i, i * 0.6, 3840, 2160, list(r)) for i, r in enumerate(reads_per_frame)]


def test_normalise_never_substitutes_characters():
    assert normalise(" abc-123 ") == "ABC123"
    assert normalise("O0I1") == "O0I1"
    assert normalise("KDX·472") == "KDX472"


def test_nz_format():
    assert is_valid_nz("ABC123") and is_valid_nz("HELLO") and is_valid_nz("A")
    assert not is_valid_nz("ABC1234") and not is_valid_nz("") and not is_valid_nz("AB 12")


def test_approaching_consistent_plate_is_accepted():
    v = evaluate(frames([read("ABC123", 100)], [read("ABC123", 115)]), RULES)
    assert v.accepted and v.plate == "ABC123" and v.votes == 2 and v.growth > 1


def test_stationary_vehicle_at_gate_is_accepted():
    assert evaluate(frames([read("ABC123", 150)], [read("ABC123", 149)]), RULES).accepted


def test_single_frame_is_never_enough():
    v = evaluate(frames([read("ABC123")]), RULES)
    assert not v.accepted and v.reason == "insufficient_frames"


def test_disagreeing_frames_are_rejected():
    v = evaluate(frames([read("ABC123")], [read("ABC128")], [read("ABC123")], [read("ABC128")]), RULES)
    assert not v.accepted and v.reason == "inconsistent"


def test_majority_with_minimum_agreement_is_accepted():
    v = evaluate(frames([read("ABC123")], [read("ABC128")], [read("ABC123", 125)]), RULES)
    assert v.accepted and v.plate == "ABC123" and v.votes == 2


def test_low_confidence_reads_do_not_count():
    v = evaluate(frames([read("ABC123", conf=0.85)], [read("ABC123", conf=0.89)]), RULES)
    assert not v.accepted and v.reason == "low_confidence"


def test_invalid_nz_format_rejected():
    v = evaluate(frames([read("ABC1234")], [read("ABC1234")]), RULES)
    assert not v.accepted and v.reason == "invalid_format"


def test_departing_vehicle_rejected():
    v = evaluate(frames([read("ABC123", 160)], [read("ABC123", 130)]), RULES)
    assert not v.accepted and v.reason == "wrong_direction"


def test_away_lane_accepts_shrinking_and_rejects_growing_plates():
    away = Rules(min_confidence=0.9, min_consistent=2, min_plate_width=60, direction="away")
    assert evaluate(frames([read("ABC123", 160)], [read("ABC123", 130)]), away).accepted
    assert evaluate(frames([read("ABC123", 100)], [read("ABC123", 130)]), away).reason == "wrong_direction"


def test_passing_vehicle_rejected():
    v = evaluate(frames([read("ABC123", 100, cx=800)], [read("ABC123", 102, cx=2200)]), RULES)
    assert not v.accepted and v.reason == "passing"


def test_distant_vehicle_rejected():
    v = evaluate(frames([read("ABC123", 40)], [read("ABC123", 44)]), RULES)
    assert not v.accepted and v.reason == "too_far"


def test_nearest_plate_wins_over_parked_car_behind():
    parked = read("ZZZ999", 50, cx=500)
    v = evaluate(frames([parked, read("ABC123", 120)], [parked, read("ABC123", 130)]), RULES)
    assert v.accepted and v.plate == "ABC123"


def test_no_plates():
    assert evaluate(frames([], []), RULES).reason == "no_plate"


def test_single_frame_lane_accepts_one_confident_read():
    one = Rules(min_confidence=0.9, min_consistent=1, min_plate_width=40)
    frames = [Frame(0, 0, 3840, 2160, [PlateRead("GWL417", "GWL417", 0.99, 0.9, (100, 100, 150, 112))]),
              Frame(1, 0, 3840, 2160, [])]
    v = evaluate(frames, one)
    assert v.accepted and v.plate == "GWL417"
