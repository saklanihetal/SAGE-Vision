# SAGE-Vision — Testing & Benchmarking Procedure

This document covers the experimental workflow used to measure the adaptive sensor-guided pipeline against an unoptimised baseline. Read it in full before starting — the ordering matters, and skipping the thermal cool-down between phases will invalidate your temperature data.

Both the baseline and the adaptive node run **entirely on the Pi** and print telemetry to the Pi's own terminal. There is no laptop, no network, and no CSV writer in the loop — you capture each run's terminal output to a log file with `tee` and analyse it afterwards.

---

## Evaluation Objectives

The experiment is designed to produce empirical evidence for these claims:

1. **Energy reduction** — the adaptive system draws less average power, and fewer Joules per confirmed detection, over a realistic occupancy pattern. *(Requires the optional INA260; see `HARDWARE_CONNECTIONS.md`.)*
2. **CPU reduction** — the adaptive system uses less processor time during idle and low-complexity states.
3. **Thermal stabilisation** — the adaptive system keeps the BCM2711 SoC away from the 80°C throttling boundary.
4. **Latency improvement** — dynamic resolution scaling reduces per-frame inference time when subjects are close.
5. **Accuracy preservation** — the optimisations do not meaningfully degrade detection quality versus the baseline.

Two runs are compared:

- **Phase A — Baseline (Control):** continuous inference at fixed 640×640, no sensors, no standby, no adaptation (`test/test_baseline_edge.py`).
- **Phase B — Adaptive (Experimental):** full system with all three sensor gates active (`rpi_edge/pi_edge_node.py`).

> **Fairness rule:** run **both** phases the same way. Use `--headless` for both benchmark runs so the GUI's display cost is not counted in one but not the other. Reserve the GUI for demos, not for the numbers you publish.

---

## Pre-Test Requirements

### Equipment checklist

- [ ] Raspberry Pi 4B with the sensors wired per `HARDWARE_CONNECTIONS.md` (PIR→GPIO 17, LM393→GPIO 27, HC-SR04 TRIG/ECHO→GPIO 23/24)
- [ ] `pigpiod` running (`sudo systemctl enable --now pigpiod`)
- [ ] USB web camera connected
- [ ] Both `.tflite` models present in `rpi_edge/` (`yolov8n_320_int8.tflite`, `yolov8n_640_int8.tflite`)
- [ ] *(For the energy claim)* INA260 wired inline on the Pi's 5 V rail, I²C enabled, and `read_power_w()` implemented in `pi_edge_node.py`
- [ ] A **stopwatch** ready for the ground-truth log
- [ ] A **physical notepad** for manual object annotations
- [ ] Room at approximately **25°C ambient**

### Thermal stabilisation — mandatory before every run

The BCM2711 retains heat for several minutes after a heavy workload. Starting a new run on a hot chip makes the thermal comparison misleading.

**Between runs (and before the first run):**

1. Stop all running scripts on the Pi.
2. Leave the Pi powered on but idle — do **not** power-cycle it (booting also generates heat).
3. Wait at least **5 minutes**.
4. Verify the temperature has returned close to ambient before proceeding:
   ```bash
   vcgencmd measure_temp     # repeat every ~30 s until it stabilises near room temp
   ```

---

## Capturing a run's telemetry

Each script prints one telemetry line per loop. Pipe that to a log file with `tee` so you can both watch it live and keep it for analysis:

```bash
python3 <script> --headless | tee ~/run_baseline.log
```

A captured line looks like:
```
[14:22:07] ACTIVE-LO | model 320 | lat   28.4ms | cpu 47.0% | temp 58.1C | pwr  -- W | dist   95.3cm | dets: Student/Person(94.2%)
```
The fields (`state`, `model`, `lat`, `cpu`, `temp`, `pwr`, `dist`, `dets`) are fixed-position and easy to parse later with `awk`/`grep` or a short Python script. `pwr` shows `-- W` until the INA260 is wired.

---

## Phase A — Baseline Run (Control)

### Step 1 — Confirm only the camera matters

The baseline uses no sensors, so their state is irrelevant. Only the Pi + USB camera affect this run.

### Step 2 — Launch the baseline script (capture to a log)

```bash
cd SAGE-Vision
source .venv/bin/activate
python3 test/test_baseline_edge.py --headless | tee ~/run_baseline.log
```

You should see:
```
[BASELINE] TFLite INT8 YOLO baseline engine initialized successfully.
[INIT] Starting Unoptimized Baseline Test Run (GUI disabled). Press 'q' or Ctrl+C to terminate...
```
It then runs inference at 640×640 on every frame and prints a `BASELINE` telemetry line per frame.

### Step 3 — Start the stopwatch and run the activity schedule

As the first telemetry line prints, **start your stopwatch** and work through the schedule below, filling in the **Ground-Truth Annotation Sheet** (end of this document) at each checkpoint.

| Time elapsed | Suggested action |
|---|---|
| 0–10 s | Stand in front of the camera alone. No other objects. |
| 10–25 s | Place a water bottle in the frame. Stay in frame. |
| 25–45 s | Bring in a chair. All three objects in frame. |
| 45–60 s | Add a laptop or bag to the scene. |
| 60–90 s | Move around and vary scene complexity freely. |
| 90–120 s | Remove all objects except yourself. Walk away. Return. |
| 120–180 s | Introduce multiple objects simultaneously. |

Keep the camera aimed at a consistent area, and avoid dramatic lighting changes during the baseline.

### Step 4 — Stop and verify the log

Press `Ctrl+C`. Confirm the log captured rows:
```bash
wc -l ~/run_baseline.log
```

### Step 5 — Mandatory cool-down

Wait at least **5 minutes**, monitoring `vcgencmd measure_temp` until near ambient, before Phase B.

---

## Phase B — Adaptive Run (Experimental)

### Step 1 — Confirm sensors and daemon

Sensors wired per `HARDWARE_CONNECTIONS.md`, `pigpiod` running. Quick check:
```bash
python3 -c "import pigpio; p=pigpio.pi(); print('pigpio:', p.connected)"
```

### Step 2 — Validate each adaptive gate (≈2 minutes)

Run the node (you can use the GUI here for validation, but switch to `--headless` for the measured run in Step 3) and watch the telemetry lines:

```bash
python3 rpi_edge/pi_edge_node.py
```

**Gate A — PIR standby suppression:**
1. Step out of frame and stay out for 5–6 s.
2. **Expected:** `state` switches to `SLEEP` (via `STANDBY`); `cpu` drops; lines show `model ---`, `dets: none`. GUI shows "SYSTEM IDLE".
3. Walk back in → returns to an `ACTIVE-*` state within ~1 s.

**Gate B — Ultrasonic resolution adaptation:**
1. In frame, walk to within ~100–120 cm of the camera.
2. **Expected:** `state` becomes `ACTIVE-LO` and `model` shows `320`.
3. Step back beyond ~160 cm → `ACTIVE-HI`, `model 640` (note the 120–160 cm hysteresis band).

**Gate C — LM393 low-light CLAHE:**
1. Cover the LM393 light sensor (or dim the room past its threshold).
2. **Expected:** CLAHE engages before inference (no explicit column, but with the GUI on you'll see the contrast-enhanced frame).
3. Uncover it.

If a gate misbehaves, stop and use the troubleshooting section of `SETUP.md`.

### Step 3 — Run the timed adaptive test (capture to a log)

```bash
python3 rpi_edge/pi_edge_node.py --headless | tee ~/run_adaptive.log
```

**Start your stopwatch** and follow the **same activity schedule as Phase A**, filling in the Ground-Truth Annotation Sheet for the same checkpoints. Matching the schedule is what makes the two logs comparable by elapsed time.

### Step 4 — Stop

Press `Ctrl+C` (or `q` if you left the GUI on). Confirm `~/run_adaptive.log` captured rows.

---

## Phase C — Analysis

There is no automated plotting script in the repo (the old `plot_comparison.py` / CSV pipeline was removed). Analyse the two captured logs directly — a short Python/`awk` parser over the fixed-position fields is enough. Suggested comparisons, mapped to the objectives:

- **Energy (obj. 1):** average `pwr` over each run, and Joules per detection = (mean watts × run seconds) / (number of confirmed detections). Compare baseline vs adaptive over the same schedule. *(Needs INA260.)*
- **CPU (obj. 2):** mean/percentiles of the `cpu` column.
- **Thermal (obj. 3):** plot `temp` vs elapsed time; the baseline should climb toward 80°C while the adaptive curve flattens lower.
- **Latency (obj. 4):** distribution of `lat` per `state`; the adaptive `ACTIVE-LO` (320) frames should be markedly faster than baseline 640.
- **% time-in-state (adaptive only):** count lines per `state` — shows where the savings come from (time spent in SLEEP/STANDBY vs ACTIVE).
- **Accuracy (obj. 5):** cross-reference the `dets` column at each checkpoint against your Ground-Truth Annotation Sheet (see below).

> Tip: filter out idle lines for the latency comparison (`grep -E 'ACTIVE|BASELINE'`), since SLEEP/STANDBY lines have no latency.

---

## Ground-Truth Annotation Sheet

Fill this in **manually with a stopwatch during both runs**. It is the reference for the accuracy comparison.

Write the exact physical objects in front of the camera at each checkpoint — be specific about what is and isn't present; don't reconstruct from memory afterward.

```
============================================================
  SAGE-Vision — Ground-Truth Annotation Sheet
============================================================
  Run Type:  [ ] Baseline   [ ] Adaptive
  Date/Time: ______________________
  Room ambient temperature: _______ °C
============================================================

  At 10 seconds elapsed:
  Objects present: ________________________________________

  At 25 seconds elapsed:
  Objects present: ________________________________________

  At 45 seconds elapsed:
  Objects present: ________________________________________

  At 60 seconds elapsed:
  Objects present: ________________________________________

  At 75 seconds elapsed:
  Objects present: ________________________________________

  At 90 seconds elapsed:
  Objects present: ________________________________________

  Notes / anomalies:
  ________________________________________________________
  ________________________________________________________
============================================================
```

**Example completed entry:**
```
  At 25 seconds elapsed:
  Objects present: Student/Person (standing), Water Bottle (on desk, ~1 m from camera)
```

### How to use this sheet for accuracy validation

1. For each checkpoint, find the telemetry line nearest that elapsed time in each log and read its `dets` field.
2. Cross-reference against your annotation for the same timestamp.
3. For the adaptive system to count as preserving accuracy, its detections should match the ground truth at least as well as the baseline — it may detect *more* (320×320 can be more confident on close subjects), but it should not consistently miss objects the baseline caught.

A simple pass/fail per checkpoint is sufficient for the write-up:

| Checkpoint | Ground Truth | Baseline Match? | Adaptive Match? |
|---|---|---|---|
| 10 s | Person | ✅ | ✅ |
| 25 s | Person, Water Bottle | ✅ | ✅ |
| 45 s | Person, Chair | ✅ | ✅ |
| 60 s | Person, Chair, Laptop | ✅ | ✅ |
