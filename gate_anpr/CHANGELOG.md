# Changelog

## 2.2.1

- Dashboard: "All gates" overview tab with every gate's live camera, switches and last result.

## 2.2.0

- Live video recognition: give a gate its camera's RTSP stream and frames come straight from the
  video (no ~1.2 s snapshot per frame). While idle only keyframes are decoded, so CPU stays low; on a
  vehicle trigger full decoding starts at the next keyframe. Falls back to snapshots automatically
  if the stream is down. The stream address (with its password) is never shown again or logged.

## 2.1.0

- Faster decisions: camera snapshots are fetched in parallel and overlap with recognition instead of
  waiting for each other (new option "Parallel snapshots", default 2; frame interval now 0.4 s).

## 2.0.3

- Gate editor: built-in entity search list (works in the Home Assistant app and on phones). Every typed word is matched against name and entity ID; likely entities are listed first.

## 2.0.2

- Gate OPEN control search no longer offers CLOSE, STOP or toggle inputs, or Gate ANPR's own switches.

## 2.0.1

- Gate editor: entity fields are searchable (type any part of the name or entity ID).

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
