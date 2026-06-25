# SAGE-Vision — Testing & Benchmarking Procedure

This document covers the experimental workflow used to measure the adaptive sensor-guided pipeline against an unoptimised baseline. Read it in full before starting — the ordering matters, and skipping the thermal cool-down between phases will invalidate your temperature data.

Both the baseline and the adaptive node run **entirely on the Pi** and print telemetry to the Pi's own terminal. There is no laptop, no network, and no CSV writer in the loop — you capture each run's terminal output to a log file with `tee` and analyse it afterwards.

---

## Evaluation Objectives

The experiment is designed to produce empirical evidence for these claims:

1. **Energy reduction** — the adaptive system draws less average power, and fewer Joules per confirmed detection, over a realistic occupancy pattern. *(Requires the optional INA219; see `HARDWARE_CONNECTIONS.md`.)*
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
- [ ] *(For the energy claim)* INA219 wired inline on the Pi's 5 V USB-C feed, I²C enabled (`i2cdetect -y 1` shows `0x40`), and `pi-ina219` installed
- [ ] **Both nodes run with `--cloud` and `--snapshots` OFF.** The INA219 measures whole-Pi power, which includes the Wi-Fi radio and SD-card writes; `--cloud` (a Wi-Fi transmit burst every 20 s) and `--snapshots` (JPEG encode + SD write on each detection) add load only the adaptive node would carry — the baseline has neither path — biasing the energy comparison. Reserve both for live demos, never for a measured run.
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

Each script prints one telemetry line per loop to stdout. Capture it with **`tee`**, which writes the stream to a file *and* echoes it to the terminal at the same time — so you watch the run live **and** keep the full log for analysis:

```bash
python3 <script> --headless | tee ~/run_baseline.log
```

- `~/run_baseline.log` is the output file in your home directory (`~` = `/home/pi`); use a distinct name per run (e.g. `run_baseline.log`, `run_adaptive.log`).
- The node `print()`s with `flush=True`, so lines appear in both places immediately.
- `tee` captures **stdout** only (the telemetry); `Ctrl+C` ends the run and closes the file.
- Without `tee` the lines scroll past and are lost — and Phase C needs the file.

A captured line looks like:
```
[14:22:07] ACTIVE-LO | model 320 | lat   28.4ms | cpu 47.0% | temp 58.1C | pwr   5.21W | dist   95.3cm | dets: Student/Person(94.2%)
```
Fields are `|`-delimited with inline labels. `lat` shows `---` in SLEEP/STANDBY (no inference runs there); `pwr` shows `-- W` until the INA219 is wired; the **baseline log has no `dist` field** (it uses no sensors).

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

Everything you need is in the two captured logs — there is **no plotting tool or CSV pipeline to run**. You parse the fixed-format telemetry lines and compare the two runs, which were driven through the *same* activity schedule so they line up by elapsed time.

### Quick spot-checks (awk / grep)

For a fast look without writing a script:

```bash
# mean CPU% over a run
grep -o 'cpu [0-9.]*' ~/run_adaptive.log | awk '{s+=$2; n++} END{printf "mean cpu  %.1f%%\n", s/n}'
# peak temperature
grep -o 'temp [0-9.]*' ~/run_adaptive.log | awk '$2>m{m=$2} END{printf "max temp  %.1fC\n", m}'
# frames spent in each state / model
grep -oE 'BASELINE|SLEEP|STANDBY|ACTIVE-LO|ACTIVE-HI|WATCHDOG' ~/run_adaptive.log | sort | uniq -c
```

### Full per-run summary (`test/analyze_log.py`)

The repo ships a parser, [`test/analyze_log.py`](../test/analyze_log.py), that prints every objective's metric for one log — it handles both the baseline and adaptive line formats and treats `-- W` / `---` as missing. Run it once per log and compare:

```bash
python3 test/analyze_log.py ~/run_baseline.log
python3 test/analyze_log.py ~/run_adaptive.log
```

Example output:
```
file              : /home/pi/run_adaptive.log
samples / duration: 312 lines / 180 s
mean CPU %        : 41.8
max temp C        : 61.4
mean latency ms   : 33.7  (active frames only)
mean power W      : 3.95  (time-weighted)
energy J          : 711
J per detection   : 1.42  (per detection instance)
time in state:
  ACTIVE-LO :   120 s (67%)
  SLEEP     :    40 s (22%)
  ACTIVE-HI :    20 s (11%)
```

(The numbers above are illustrative — `pwr`/energy fill in only once the INA219 is wired.)

### What each number means (mapped to the objectives)

| Objective | Metric (from the script) | Expected result |
|---|---|---|
| 1. Energy | mean power (W), energy (J), J/detection | adaptive **lower** — time in SLEEP/LO instead of 640 |
| 2. CPU | mean CPU % | adaptive lower, driven by idle/LO time |
| 3. Thermal | max temp °C | baseline climbs toward 80 °C; adaptive stays lower |
| 4. Latency | mean active latency (ms) | adaptive lower whenever ACTIVE-LO (320) runs |
| — savings source | time-in-state | adaptive shows real SLEEP/STANDBY/LO time; baseline is ~100 % 640 |
| 5. Accuracy | `dets` vs ground-truth sheet | adaptive matches baseline at each checkpoint |

### Results table to fill in

| Metric | Baseline | Adaptive | Δ |
|---|---|---|---|
| Mean power (W) | | | |
| Energy (J) over run | | | |
| Mean CPU (%) | | | |
| Max temp (°C) | | | |
| Mean active latency (ms) | | | |
| % time **not** running 640 | 0 % | | |

### Honest caveats

- **Timestamps are 1-second resolution**, so duration and energy are approximate — fine over a multi-minute run, but don't quote them to the millijoule.
- Power/energy are **time-weighted** by the timestamp gaps (not a flat per-line average), because loop pace differs by state — a per-line mean would let the fast ACTIVE-LO frames dominate.
- `J per detection` counts detection *instances* across frames (a rough proxy). For *unique confirmed* objects, use the counts from the Ground-Truth Annotation Sheet instead.
- Accuracy is judged from the `dets` column against the ground-truth sheet (next section), not by the script.

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
