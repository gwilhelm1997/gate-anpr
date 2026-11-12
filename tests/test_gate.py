import pytest

from gate_anpr.gate import GateController

RELAY = "switch.sonoff_100219c178_2"


def relay_gate(ha, **kw):
    ha.set(RELAY, "off", friendly_name="Outside gate control OPEN")
    return GateController(ha, RELAY, confirm_timeout=2, poll_interval=0.01, **kw)


async def test_open_relay_pulse_is_confirmed(fake_ha):
    gate = relay_gate(fake_ha)
    assert await gate.open(cooldown=60) == ("opened", "OPEN relay pulsed and released")
    assert fake_ha.calls == [("switch", "turn_on", RELAY)]


async def test_second_open_within_cooldown_is_blocked(fake_ha):
    gate = relay_gate(fake_ha)
    await gate.open(cooldown=60)
    result, _ = await gate.open(cooldown=60)
    assert result == "cooldown" and len(fake_ha.calls) == 1


async def test_relay_stuck_on_fails_closed(fake_ha):
    gate = relay_gate(fake_ha)
    fake_ha.set(RELAY, "on", friendly_name="Outside gate control OPEN")
    assert (await gate.open(60))[0] == "gate_unavailable" and not fake_ha.calls


@pytest.mark.parametrize("state", ["unavailable", "unknown"])
async def test_unavailable_relay_fails_closed(fake_ha, state):
    gate = relay_gate(fake_ha)
    fake_ha.set(RELAY, state, friendly_name="Outside gate control OPEN")
    assert (await gate.open(60))[0] == "gate_unavailable" and not fake_ha.calls


async def test_toggle_input_is_refused(fake_ha):
    fake_ha.set("switch.sonoff_100219c178_4", "off", friendly_name="Outside Gate toggle")
    gate = GateController(fake_ha, "switch.sonoff_100219c178_4")
    assert (await gate.open(60))[0] == "unsupported_gate" and not fake_ha.calls


async def test_home_assistant_unreachable_fails_closed(fake_ha):
    gate = relay_gate(fake_ha)
    fake_ha.fail_states = True
    assert (await gate.open(60))[0] == "gate_unavailable" and not fake_ha.calls


async def test_failed_command_is_not_retried(fake_ha):
    gate = relay_gate(fake_ha)
    fake_ha.fail_calls = True
    assert (await gate.open(60))[0] == "command_failed"
    fake_ha.fail_calls = False
    assert (await gate.open(60))[0] == "cooldown" and not fake_ha.calls


async def test_relay_without_inching_is_reported(fake_ha):
    gate = relay_gate(fake_ha)
    fake_ha.release_after = 10
    result, detail = await gate.open(60)
    assert result == "opened" and "inching" in detail


async def test_position_sensor_open_means_no_command(fake_ha):
    gate = relay_gate(fake_ha, position_entity="binary_sensor.gate_open")
    fake_ha.set("binary_sensor.gate_open", "on")
    assert (await gate.open(60))[0] == "already_open" and not fake_ha.calls


async def test_position_sensor_unknown_fails_closed(fake_ha):
    gate = relay_gate(fake_ha, position_entity="binary_sensor.gate_open")
    fake_ha.set("binary_sensor.gate_open", "unavailable")
    assert (await gate.open(60))[0] == "gate_not_closed" and not fake_ha.calls


async def test_cover_opens_only_from_closed(fake_ha):
    fake_ha.set("cover.gate", "closed", supported_features=3)
    gate = GateController(fake_ha, "cover.gate", confirm_timeout=1, poll_interval=0.01)
    assert (await gate.open(60))[0] == "opened"
    assert fake_ha.calls == [("cover", "open_cover", "cover.gate")]


@pytest.mark.parametrize("state,result", [("open", "already_open"), ("closing", "gate_not_closed"),
                                          ("unknown", "gate_not_closed")])
async def test_cover_not_closed_is_never_commanded(fake_ha, state, result):
    fake_ha.set("cover.gate", state, supported_features=3)
    assert (await GateController(fake_ha, "cover.gate").open(60))[0] == result and not fake_ha.calls


async def test_other_domains_refused(fake_ha):
    fake_ha.set("button.gate", "unknown")
    assert (await GateController(fake_ha, "button.gate").open(60))[0] == "unsupported_gate"
