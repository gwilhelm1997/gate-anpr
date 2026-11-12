# Gate ANPR

Reads number plates of vehicles approaching the gate, compares them with your approved list and
sends the gate an explicit **OPEN** command. Everything runs on your Home Assistant machine: no cloud,
no Frigate, no subscription, no continuous recording.

## Gates

Each **gate** (lane) is one camera watching one gate controller. Add, edit or delete gates in the
**Gate ANPR** panel (sidebar) with **+ Add gate**. Every field is a dropdown of your own entities:

| Setting | Meaning |
|---|---|
| Recognition camera | High-resolution snapshot camera, e.g. Reolink **Snapshots Clear** (enable it first) |
| Live view camera | Camera shown live on the dashboard (optional) |
| Vehicle detection sensor | Reolink vehicle sensor that starts recognition |
| Gate OPEN control | Relay on the controller's OPEN input, or a cover. Toggle channels are not offered |
| Gate position sensor | Optional. When set, the gate is only commanded while it reports closed |
| Access | **Approved vehicles only**, or **Allow all vehicles** (e.g. a way out) |
| Vehicles approach the gate | **Towards the camera** (plate grows) or **Away from the camera** (plate shrinks) |
| Rules | Confidence, matching frames, cooldown, minimum plate width and detection region |

Every gate has its own **Recognition** and **Automatic opening** switches, its own Home Assistant
device ("Gate ANPR *name*") and its own dashboard view. New gates start with automatic opening off.
Gates that drive the same gate controller share its cooldown, so two cameras can never double-open it.

**Allow all vehicles** still requires a consistent, approaching plate read in the region, so every
opening is logged with a registration and an image. Use it where any vehicle may pass, such as an exit.

Approved registrations apply to **all gates** unless you tick specific gates next to the plate.

## Parked vehicles and exclusion zones

A vehicle that stays in view must never open the gate, and must not hide one that arrives. Per gate:

- **Exclusion zones**: rectangles, in percent of the frame, where plates are always ignored. Write
  them as `left,top,right,bottom`, several separated by `;`, e.g. `0,0,30,40; 70,0,100,30`. Use them
  for parking spots, a neighbour's driveway or the road. "Show camera + region" draws them in red.
- **Treat as parked after** (default 2 minutes): a plate seen at the same spot and size in a detection
  at least that long ago is ignored. A car that pulls up and waits is unaffected (it was moving), and a
  parked car that drives off is read again because its position changes.
- The parked memory is kept in RAM: after an app restart a parked car is recognised as parked again
  from its next sighting plus the waiting time. Use an exclusion zone for spots that are always parked in.

Ignored plates and the reason are listed in each event's details in the access history.

## Faster recognition with the video stream

By default each frame is a 4K snapshot, which your camera takes about 1.2 s to deliver. Give a gate
its camera's **video stream** (Edit gate → Video stream) and frames come straight from the live video:

```
rtsp://USER:PASSWORD@CAMERA-IP:554/Preview_01_main
```

- Enable RTSP on the camera first: Reolink app → camera → Settings → Network → Advanced →
  Server settings → **RTSP** on. Use a camera user; a dedicated read-only user is best.
- While no vehicle is present only keyframes are decoded (one every couple of seconds), so the CPU
  load is small. On a vehicle trigger, full-rate decoding starts at the next keyframe.
- For the quickest start, set the camera's **I-frame interval** to 1× frame rate (Reolink app →
  Display/Stream settings → Clear → I-frame interval). That halves the wait for the next keyframe.
- If the stream drops, the gate automatically uses snapshots and reconnects in the background.
  The gate card shows which source is in use.
- The address, including its password, is stored only in the app's private data. It's never shown
  again in the panel and never logged.

## How it decides

1. The Reolink **vehicle detection** sensor turns on.
2. The app grabs up to 6 high-resolution snapshots over about 8 seconds, through Home Assistant's
   own camera integration.
3. FastALPR (ONNX Runtime) finds plates inside the **detection region** and reads them. It runs in a
   separate low-priority process with a limited number of CPU threads.
4. A registration is accepted only when all of these hold:
   - the same reading appears in at least **Matching frames required** frames (default 2), and in
     more frames than every other reading combined;
   - every character is read with at least **Minimum OCR confidence** (default 90%);
   - it is a valid New Zealand registration (1–6 letters or digits);
   - the vehicle is **approaching the gate**: the plate is wide enough (**Minimum plate width**), its
     size changes in the gate's direction (growing for "towards the camera", shrinking for "away") and
     it is not sliding across the frame (a passing vehicle).
5. The reading must match an enabled approved registration **exactly**. Spaces and dashes are ignored.
   Characters are never substituted (no 0/O or 1/I guessing) and nothing similar is accepted.
6. Only then, and only with that gate's **Automatic opening** on, the app sends OPEN.
   On an **Allow all vehicles** gate, step 5 is skipped.

## Gate safety

- **OPEN only.** The gate entity must be the relay wired to the gate controller's dedicated OPEN input
  (here `switch.sonoff_100219c178_2`, "Outside gate control OPEN"), or a `cover` entity. Switches named
  like a toggle are refused. OPEN on a gate that is already open leaves it open.
- **Fail closed.** No command is sent when any of these is true: the relay is unavailable or not idle;
  Home Assistant or MQTT (your dashboard's off switch) is disconnected; the camera or recognition
  fails; the readings disagree; or an optional position sensor is not reporting closed.
- **No repeats.** One recognition runs at a time. After any OPEN command, further commands wait for
  the **Opening cooldown** (default 60 s). Failed commands are never retried automatically.
- **Confirmed.** After OPEN, the app watches the relay pulse and release (inching). The result is
  recorded as `opened`, `open_unconfirmed` or `command_failed`.
- Your existing gate controls, safety beams, manual operation and automations are not touched.
  The app only adds one more `switch.turn_on` of the OPEN relay, the same action your
  "Inside gate open" automation uses for the other gate.

Your gate has no position sensor, so the app cannot see whether the gate is open. Fitting a reed switch
and setting **Gate position sensor** adds that check.

## Installation

Requirements: Home Assistant OS (amd64 or aarch64), the Reolink integration, the **Mosquitto broker**
app and the **MQTT** integration (both already installed here).

1. In Home Assistant, enable the entity **Gate Cam Snapshots Clear** (`camera.gate_cam_snapshots_clear`):
   Settings → Devices & services → Reolink → Gate Cam → entities → Snapshots Clear → Enable.
   This gives 3840×2160 frames. The live "Fluent" view is only 640×360 and too small for plates.
2. Copy the `gate_anpr` folder into the `addons` share. Either run `tools\deploy.ps1` from this
   repository, or copy it by hand over Samba to `\\homeassistant\addons\gate_anpr`.
3. Settings → Apps → Install app → ⋮ → **Check for updates**. **Gate ANPR** appears under
   **Local apps**. Install it. The first build downloads the dependencies and models and takes a few minutes.
4. The **Configuration** tab holds recognition engine options. Its "First gate" fields are used once,
   to create the first gate; after that, gates are managed in the Gate ANPR panel.
5. Start the app and turn on **Watchdog**. On first start it creates the **Gate ANPR** dashboard in the
   sidebar, and the **Gate ANPR** panel for managing registrations.

**Automatic opening starts OFF** and stays off until you switch it on.

## Commissioning (do this before enabling automatic opening)

1. Open the Gate ANPR panel → **Show camera + region**. Set the region (dashboard → Recognition
   settings) so it covers the approach lane in front of the gate and excludes the road. A tighter
   region gives better plate detection.
2. Add your registrations under **Approved registrations**.
3. For a few days, drive in and out, in daylight and at night. Every detection is logged with its
   image, the readings from every frame, confidence, plate growth (`growth` > 1 means approaching)
   and the decision. Approved vehicles show as `approved_monitor`: "would have opened".
4. Tune if needed:
   - Misses at night → clean the lens, trim foliage near the camera (IR reflections), or lower
     **Minimum OCR confidence** slightly (never below about 85%).
   - Plates found too late → lower **Minimum plate width**, or choose a larger detector model.
5. When the log shows consistent, correct results, turn on **Automatic opening**.

## Dashboard

The app creates the dashboard on first start with standard cards only:

- live gate camera;
- Automatic opening and Recognition switches, the gate relay and a **Test recognition** button
  (a test never operates the gate);
- last registration, result, reason, confidence and vehicle;
- last plate crop and the four most recent detection images;
- access history (granted and rejected), plus a logbook of results;
- recognition settings: confidence, matching frames, cooldown, plate width and region;
- diagnostics: engine, self-test time, CPU, recognition time, link status and the last verdict.

It has one view per gate and is rebuilt when gates are added, renamed or removed, unless you have
edited it yourself (then it is left alone). For a fresh copy, delete the dashboard and restart the app.
Turn off **Create dashboard** to stop all of this.

## Automations

Every decision fires the event `gate_anpr_access` with `lane`, `lane_id`, `plate`, `result`, `reason`,
`granted`, `confidence`, `label` and `test`, for example for notifications:

```yaml
triggers:
  - trigger: event
    event_type: gate_anpr_access
    event_data: {result: rejected}
actions:
  - action: notify.all_devices
    data:
      message: "Gate: {{ trigger.event.data.plate }} not opened ({{ trigger.event.data.reason }})"
```

## Results and reasons

| result | meaning |
|---|---|
| `granted` | approved, OPEN sent and confirmed (`opened`), or the gate was already open |
| `approved_monitor` | approved, but automatic opening is off (log-only mode) |
| `approved_test` | approved during a test recognition |
| `approved_not_opened` | approved, but a safety check blocked the command (see the reason) |
| `rejected` | `not_approved`, `low_confidence`, `inconsistent`, `insufficient_frames`, `invalid_format`, `wrong_direction`, `passing`, `too_far`, `no_plate` |
| `error` | camera, Home Assistant or recognition failure; nothing was operated |

## Data and privacy

Settings, approved registrations, history and images live in the app's private `/data` volume.
They are included in Home Assistant backups and reachable only through the authenticated panel.
History older than **History retention** (default 30 days) is deleted automatically.

## Updating

Copy the new `gate_anpr` folder (`tools\deploy.ps1`), then Settings → Apps → Gate ANPR →
**Update** (or **Rebuild** if the version did not change). Settings, plates and history are kept.
