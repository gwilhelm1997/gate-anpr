# Changelog

## 2.0.0

- Multiple gates: add any number of gates (lanes) in the Gate ANPR panel, each with its own camera,
  vehicle sensor, gate OPEN control, optional position sensor, direction (vehicles approach towards or
  away from the camera), detection region, rules, Recognition and Automatic opening switches.
- Per-gate access mode: "Approved vehicles only" or "Allow all vehicles" (still needs a consistent,
  approaching plate read; every opening is logged).
- Approved registrations apply to all gates, or only to the gates you tick.
- One Home Assistant device and dashboard view per gate. Gates sharing a gate controller share its cooldown.
- The existing single-gate setup is migrated to the first gate, "Outside gate". Its entity IDs change to
  `..._gate_anpr_outside_gate_...`. The dashboard is rebuilt unless you edited it.
- Reason `departing` is now `wrong_direction`.

## 0.1.1

- "+ Approve" button on every plate in the access history: add passing vehicles to the approved list in one click.

## 0.1.0

- First release: FastALPR recognition in an isolated low-priority process, multi-frame consensus,
  approach check, detection region, exact NZ registration matching, explicit OPEN command with
  cooldown and confirmation, MQTT entities, auto-created dashboard, Ingress panel, log-only default.
