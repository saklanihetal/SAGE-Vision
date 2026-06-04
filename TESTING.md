# SAGE-Vision — Testing & Benchmarking Procedure

This document covers the complete experimental workflow used to measure and prove the performance gains of the adaptive sensor-guided pipeline against an unoptimised baseline. Read it in full before starting — the ordering of steps matters, and skipping the thermal cool-down between phases will invalidate your temperature data.

---

## Evaluation Objectives

The experiment is designed to produce empirical evidence for four specific claims:

1. **CPU reduction** — the adaptive system uses less processor time during idle and low-complexity states.
2. **Thermal stabilisation** — the adaptive system prevents the BCM2711 SoC from climbing toward the 80°C throttling boundary.
3. **Latency improvement** — dynamic resolution scaling reduces per-frame inference time.
4. **Accuracy preservation** — the optimisations do not meaningfully degrade object detection quality compared to the unoptimised baseline.

To isolate these improvements, two runs are performed:

- **Phase A — Baseline (Control Group):** Continuous inference at fixed 640×640 resolution with no sensor input, no standby, and no resolution adaptation.
- **Phase B — Adaptive (Experimental Group):** Full system running with all three sensor gates active.

---

## Pre-Test Requirements

### Equipment checklist

Before starting either phase, confirm the following:

- [ ] RV-IoT Board is flashed with the firmware in `firmware/esp32_sensor_node/`
- [ ] PIR, LDR, and Ultrasonic sensors are wired correctly per `HARDWARE_CONNECTIONS.md`
- [ ] Both the Pi and laptop virtual environments are set up per `SETUP.md`
- [ ] `LAPTOP_SERVER_IP` in `rpi_edge/pi_edge_node.py` is set to your laptop's current IP
- [ ] `SERIAL_INTERFACE` in `rpi_edge/pi_edge_node.py` matches your ESP32's device path
- [ ] A **stopwatch** is ready (phone stopwatch is fine) for the ground-truth log
- [ ] A **physical notepad** is ready for manual object annotations
- [ ] The room is at approximately **25°C ambient temperature**

### Thermal stabilisation — mandatory before every run

The BCM2711 SoC retains heat for several minutes after a heavy workload ends. If you start a new run immediately after the previous one, the chip begins the second experiment at an elevated temperature, which makes the thermal comparison graphs misleading.

**Required procedure between runs (and before the first run):**

1. Stop all running scripts on the Pi and the laptop.
2. Leave the Pi completely powered on but idle — do **not** power-cycle it, as booting also generates heat.
3. Wait a minimum of **5 minutes**.
4. Verify the Pi's temperature has returned close to ambient before proceeding:
   ```bash
   # Run on the Pi — check every 30 seconds until it stabilises
   vcgencmd measure_temp
   ```
   The reading should be within a few degrees of your room temperature (target: ~25°C) before you start the next phase.

---

## Phase A — Baseline Run (Control Group)

### Step 1 — Disconnect the RV-IoT Board

Unplug the ESP32 RV-IoT Board from the Pi's USB ports. Only the following hardware should be connected to the Pi for this phase:

- Raspberry Pi 4B
- USB Web Camera

This ensures the baseline measurement is completely free of any sensor influence.

### Step 2 — SSH into the Pi and launch the baseline script

```bash
ssh pi@raspberrypi.local
cd SAGE-Vision/test
source ../.venv/bin/activate
python3 test_baseline_edge.py
```

You should see:
```
[BASELINE] TFLite INT8 YOLO baseline engine initialized successfully.
[INIT] Starting Unoptimized Baseline Test Run. Press Ctrl+C to terminate...
```

The script immediately begins capturing frames, running inference at 640×640 on every frame, and writing rows to `test/baseline_optimization_logs.csv`.

### Step 3 — Start the stopwatch and introduce test variables

As soon as the baseline script prints its first log line, **start your stopwatch** and begin working through the scene activity schedule below. You must also fill in the **Ground-Truth Annotation Sheet** (see the section at the end of this document) simultaneously — note exactly what physical objects are in front of the camera at each time checkpoint.

**Suggested activity schedule (minimum 3 minutes total):**

| Time elapsed | Suggested action |
|---|---|
| 0–10 s | Stand in front of the camera alone. No other objects. |
| 10–25 s | Place a water bottle in the frame. Stay in frame. |
| 25–45 s | Bring in a chair. All three objects now in frame. |
| 45–60 s | Add a laptop or bag to the scene. |
| 60–90 s | Move around and vary scene complexity freely. |
| 90–120 s | Remove all objects except yourself. Walk away. Return. |
| 120–180 s | Introduce multiple objects simultaneously. |

Keep the camera pointing at a consistent area of the room throughout. Avoid dramatically changing lighting during the baseline run.

### Step 4 — Stop and verify the log

Press `Ctrl+C` in the Pi SSH terminal. The script will print a clean shutdown message. Confirm the log was written:

```bash
ls -lh test/baseline_optimization_logs.csv
wc -l test/baseline_optimization_logs.csv
```

You should see a file with at least several hundred rows (one row per camera frame processed).

### Step 5 — Mandatory cool-down

Close all processes on the Pi and wait **at least 5 minutes** before proceeding to Phase B. Monitor temperature with `vcgencmd measure_temp` until it returns close to ambient.

---

## Phase B — Adaptive Run (Experimental Group)

### Step 1 — Reconnect all hardware

Plug the following into the Pi's USB ports:

- USB Web Camera
- RV-IoT Board (ESP32 Microcontroller Node) with all sensors attached

### Step 2 — Start the laptop logger first

On your laptop (Terminal A):

**macOS / Linux / Ubuntu:**
```bash
cd SAGE-Vision
source .venv/bin/activate
python3 host_laptop/laptop_logger.py
```

**Windows:**
```powershell
cd SAGE-Vision
.venv\Scripts\Activate.ps1
python host_laptop\laptop_logger.py
```

The laptop will print its dashboard header and begin listening silently on UDP port 8080. **The laptop logger must be running before the Pi starts transmitting.**

### Step 3 — Launch the adaptive edge node

On your Pi SSH session (Terminal B):

```bash
cd SAGE-Vision
source .venv/bin/activate
python3 rpi_edge/pi_edge_node.py
```

Confirm you see all three boot messages:
```
[SYSTEM] TFLite INT8 YOLO engine successfully initialized.
[SYSTEM] Serial Harvester thread successfully pinned to CPU Core {1}
[SYSTEM] Vision processing core engine pinned to CPU Cores {2, 3}
```

### Step 4 — Validate each adaptive gate individually

Before running the timed test, verify that all three gates are functioning correctly. These checks take about 2 minutes total.

**Gate A — PIR Standby Suppression:**
1. Step completely out of the camera's field of view and stay out.
2. Wait 5–6 seconds (the PIR cooldown window).
3. **Expected result on the laptop terminal:** mode column switches to `STANDBY (Suppressed)`, CPU usage column drops to single-digit values, latency column shows 0.0.
4. Walk back into frame.
5. **Expected result:** mode column switches back to `ACTIVE (Inference)` within one second.

**Gate B — Ultrasonic Resolution Adaptation:**
1. Confirm you are in frame (ACTIVE mode).
2. Walk very close to the camera lens — within 100–130 cm.
3. **Expected result:** the `Res` column on the laptop terminal switches from `640` to `320`.
4. Walk back to a normal distance (> 200 cm).
5. **Expected result:** `Res` column switches back to `640`.

**Gate C — LDR Low-Light CLAHE Enhancement:**
1. Place your hand or a piece of tape directly over the small LDR component on the RV-IoT Board to block all light reaching it.
2. **Expected result on the Pi terminal:** the CLAHE preprocessing path activates (no explicit visual indicator on the laptop logger, but inference continues normally rather than degrading).
3. Remove the cover.

If any gate does not respond as expected, do not proceed — investigate using the troubleshooting section of `SETUP.md` and `CODE_ISSUES_AND_FIXES.md`.

### Step 5 — Run the timed adaptive test

**Start your stopwatch** and follow the same activity schedule you used in Phase A. Fill in the Ground-Truth Annotation Sheet again for the same checkpoints.

**The activity schedule must match Phase A as closely as possible** — the comparison plots in `plot_comparison.py` are aligned by elapsed time from the start of each run, so the scenes at each checkpoint should be comparable.

| Time elapsed | Action |
|---|---|
| 0–10 s | Stand in front of the camera alone. |
| 10–25 s | Add the water bottle. |
| 25–45 s | Bring in the chair. |
| 45–60 s | Add the laptop or bag. |
| 60–90 s | Move around freely; vary complexity. |
| 90–120 s | Remove all objects except yourself. Walk away. Return. |
| 120–180 s | Introduce multiple objects simultaneously. |

### Step 6 — Stop and save logs

After at least 3 minutes, press `Ctrl+C` in **both** the Pi SSH terminal and the laptop terminal.

Adaptive log is automatically saved on the laptop at:
```
host_laptop/pi_optimization_logs.csv
```

---

## Phase C — Dataset Synchronisation and Graph Generation

### Step 1 — Copy the baseline log from the Pi to your laptop

The baseline log was saved locally on the Pi (to avoid network overhead during the test). Copy it to your laptop over SCP:

**macOS / Linux / Ubuntu:**
```bash
cd SAGE-Vision
scp pi@raspberrypi.local:~/SAGE-Vision/test/baseline_optimization_logs.csv test/
```

**Windows (PowerShell):**
```powershell
cd SAGE-Vision
scp pi@raspberrypi.local:~/SAGE-Vision/test/baseline_optimization_logs.csv test\
```

### Step 2 — Generate the evaluation dashboard

From your laptop, with the virtual environment active and from the repository root:

```bash
python3 plot_comparison.py
```

This generates `system_performance_comparison.png` in the repo root directory. The script:
- Aligns both datasets by elapsed time from their respective start points
- Filters standby frames from the latency graph to prevent zero-latency skew
- Renders four stacked panels as described below

---

## Reading the Output Dashboard

### Table 1 — Milestone Timeline Detection Comparison

A text table comparing what each system detected at the time checkpoints (10 s, 25 s, 45 s, 60 s, 75 s, 90 s elapsed). Compare this table directly against your manually written Ground-Truth Annotation Sheet.

**What to look for:**
- Both systems should detect the same objects at the same checkpoints (proving the adaptive system preserved accuracy).
- The adaptive system's confidence scores may be slightly different but should not be dramatically lower.
- Any checkpoint where one system shows `None (Standby Mode)` is expected and valid if you were out of frame at that moment.

### Graph 1 — Inference Latency Comparison

X-axis: elapsed seconds. Y-axis: per-frame inference duration in milliseconds. Standby frames (latency = 0) are filtered out of both curves.

**What to look for:** The adaptive system's line should drop noticeably whenever you were close to the camera (< 150 cm), corresponding to the 320×320 resolution gate activating. The baseline line should remain relatively flat and high throughout.

### Graph 2 — Thermal Dynamics

X-axis: elapsed seconds. Y-axis: CPU core temperature in °C. Two reference lines are drawn: the lab ambient temperature baseline (~25°C) and the Broadcom SoC throttling threshold (80°C).

**What to look for:** The baseline (red) line should climb steadily and steeply, trending toward the 80°C boundary. The adaptive (blue) line should flatten into a horizontal plateau well below throttling, demonstrating thermal equilibrium was achieved via reduced average workload.

### Graph 3 — CPU Utilisation

X-axis: elapsed seconds. Y-axis: processor utilisation percentage.

**What to look for:** The baseline should remain consistently high (often 80–100%). The adaptive system should show distinct drops during standby windows and lower average utilisation overall.

---

## Ground-Truth Annotation Sheet

You must fill this in **manually with a stopwatch during both test runs**. This is the reference used to validate the accuracy column in Table 1.

Write down the exact physical objects in front of the camera at each checkpoint. Be specific about what is present and what is not — do not guess from memory after the run.

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

Once `plot_comparison.py` has generated Table 1:

1. For each time checkpoint row in Table 1, read the Baseline Detections and Adaptive Detections columns.
2. Cross-reference each against your annotation sheet entry for the same timestamp.
3. For the adaptive system to be considered to have preserved accuracy, its detections should match the ground truth at least as well as the baseline — it is acceptable for the adaptive system to detect *more* objects (it may be more confident at 320×320 for close subjects), but it should not consistently miss objects that the baseline detected.

A simple pass/fail per checkpoint is sufficient for the write-up:

| Checkpoint | Ground Truth | Baseline Match? | Adaptive Match? |
|---|---|---|---|
| 10 s | Person | ✅ | ✅ |
| 25 s | Person, Water Bottle | ✅ | ✅ |
| 45 s | Person, Chair | ✅ | ✅ |
| 60 s | Person, Chair, Laptop | ✅ | ✅ |
