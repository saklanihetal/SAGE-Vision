import os
import sys
import time
import argparse
import threading
import urllib.request
import urllib.parse
import cv2
import numpy as np
import psutil
import pigpio
from dotenv import load_dotenv
from yolo_tflite import YoloTFLite

# =========================================================================
# FSM STATES
# =========================================================================
# The node runs a 5-state finite state machine. SLEEP/STANDBY do no inference;
# ACTIVE-LO runs the 320 model, ACTIVE-HI and WATCHDOG run the 640 model.
STATE_SLEEP     = "SLEEP"
STATE_STANDBY   = "STANDBY"
STATE_ACTIVE_LO = "ACTIVE-LO"
STATE_ACTIVE_HI = "ACTIVE-HI"
STATE_WATCHDOG  = "WATCHDOG"

# =========================================================================
# SHARED SENSOR STATE
# =========================================================================
# The GPIO harvester thread writes here; the vision/FSM thread reads. Every
# access is guarded by state_mutex. The LDR is a digital LM393 comparator
# (active-low), so light is a dark/bright boolean, not an analog value.
shared_state = {
    "pir": 0,
    "distance": 400.0,            # safe far-distance default until the first echo
    "is_dark": False,
    "last_motion_epoch": time.time(),
}
state_mutex = threading.Lock()

# =========================================================================
# DISTANCE FILTERING (ultrasonic spike rejection)
# =========================================================================
# Rolling window feeding a median filter that smooths ultrasonic jitter.
DISTANCE_WINDOW_SIZE = 5
distance_history = [400.0] * DISTANCE_WINDOW_SIZE

# Multipath spike rejection. The HC-SR04's wide (~15 deg) beam bounces off walls
# and furniture, producing single-frame jumps that no real person could make
# (e.g. 218cm -> 37cm -> 218cm in 300ms). A reading that jumps more than
# MAX_PLAUSIBLE_JUMP_CM from the current median is dropped before it reaches the
# filter. The exception is genuine fast movement: if the jump repeats for
# JUMP_CONFIRM_N readings in a row, we accept it and snap the window onto it.
MAX_PLAUSIBLE_JUMP_CM = 120.0
JUMP_CONFIRM_N = 3
consecutive_jump_count = 0

# =========================================================================
# PRESENCE FUSION + HYSTERESIS
# =========================================================================
# Absence is declared only when PIR motion, a close ultrasonic reading, AND a
# recent vision person-detection have ALL been quiet for PRESENCE_TIMEOUT_S.
# This replaces the old "PIR-expired AND far" rule, which mistook a stationary
# occupant plus one spurious sonar spike for an empty room.
PRESENCE_TIMEOUT_S = 12.0
PERSON_CLASS_ID    = 0            # YOLO/COCO dense index for "person"

# Distance hysteresis between waking and staying awake. A target must step
# inside WAKE_DISTANCE_CM to wake the node from SLEEP, but presence is kept
# alive out to the wider PRESENCE_DISTANCE_CM. The gap stops a target lingering
# near the boundary from repeatedly re-crossing the wake line.
WAKE_DISTANCE_CM     = 300.0      # closer threshold: wake from SLEEP
PRESENCE_DISTANCE_CM = 350.0      # wider threshold: keep-awake / presence vote

# Two-stage low-power wind-down. When presence goes quiet we do NOT jump straight
# from the expensive ACTIVE-HI to SLEEP. After PRESENCE_GRACE_S of quiet we drop
# to the cheap ACTIVE-LO and keep counting down; only at PRESENCE_TIMEOUT_S do we
# SLEEP. The small grace also debounces the presence boundary so a target
# hovering near it doesn't twitch HI<->LO.
PRESENCE_GRACE_S = 2.0

# HI<->LO distance band. Below CLOSE -> low-res; above FAR -> high-res; the gap
# between them is the hysteresis band that prevents churn near the threshold.
HILO_CLOSE_CM = 120.0
HILO_FAR_CM   = 160.0

# =========================================================================
# TRANSITION GATE TUNING (per edge)
# =========================================================================
# Each debounced edge requires its condition to hold continuously for HOLD_S
# AND the current state to have been occupied for at least DWELL_S before it
# commits. See TransitionGate below.
HILO_HOLD_S  = 0.75               # HI<->LO: condition must persist this long
HILO_DWELL_S = 1.5                # HI<->LO: minimum time between switches
WAKE_HOLD_S  = 0.5                # SLEEP->wake: wake signal must persist this long
WATCHDOG_RECOVER_HOLD_S = 1.5     # leaving WATCHDOG: sonar must stay healthy this long
WATCHDOG_MIN_DWELL_S    = 1.0     # minimum time to stay in WATCHDOG once entered

STANDBY_WARMUP_S = 0.2            # transient warmup hold before STANDBY picks a model

# =========================================================================
# WATCHDOG / SONAR LIVENESS
# =========================================================================
# Epoch of the last completed ultrasonic echo (the sonar heartbeat).
last_valid_reading_time = time.time()

# Failsafe: this many consecutive no-echo triggers counts as a dead sonar.
MAX_CONSECUTIVE_DROPS = 3
consecutive_dropped_readings = 0

# Set when a pigpio/daemon call fails -> forces an immediate WATCHDOG (the
# equivalent of the old "USB serial unplugged" detection).
pigpio_fault = False

# =========================================================================
# MODELS
# =========================================================================
# A full-integer INT8 model has a fixed input resolution, so adaptive switching
# uses two interpreters: 320x320 for ACTIVE-LO, 640x640 for ACTIVE-HI/WATCHDOG.
_MODEL_DIR = os.path.dirname(os.path.abspath(__file__))
MODEL_LO_PATH = os.path.join(_MODEL_DIR, "yolov8n_320_int8.tflite")
MODEL_HI_PATH = os.path.join(_MODEL_DIR, "yolov8n_640_int8.tflite")
try:
    model_lo = YoloTFLite(MODEL_LO_PATH)  # 320x320
    model_hi = YoloTFLite(MODEL_HI_PATH)  # 640x640
    print("[SYSTEM] TFLite INT8 YOLO engines (320 + 640) initialized.", flush=True)
except Exception as e:
    print(f"[FATAL] Failed to locate or parse TFLite model file: {e}", flush=True)
    sys.exit(1)


# =========================================================================
# TRANSITION GATE
# =========================================================================
class TransitionGate:
    """Debounces FSM state changes so a brief sensor blip can't flip states.

    A transition to `target` commits only when BOTH conditions hold:
      * its condition has stayed true continuously for `hold_s` seconds, and
      * the current state has been occupied for at least `dwell_s` seconds.

    Only one candidate transition is tracked at a time: requesting a different
    target, or calling clear(), resets the streak. Because the streak is timed
    rather than counted in ticks, the gate behaves identically regardless of how
    fast the FSM loop happens to be running in the current state.

    Call enter() on every committed state change to restart the dwell clock.
    """

    def __init__(self):
        self._entered_at = time.time()   # when the current state was entered
        self._candidate = None           # target currently accumulating a streak
        self._streak_start = 0.0         # when the candidate's condition first held

    def enter(self, now):
        """Record a fresh state entry: reset the dwell clock and any partial streak."""
        self._entered_at = now
        self._candidate = None
        self._streak_start = 0.0

    def request(self, target, now, hold_s, dwell_s):
        """Vote for a transition to `target` this tick; return True once confirmed."""
        if target != self._candidate:
            self._candidate = target
            self._streak_start = now
        held_long_enough = (now - self._streak_start) >= hold_s
        dwell_satisfied  = (now - self._entered_at) >= dwell_s
        return held_long_enough and dwell_satisfied

    def clear(self):
        """Abandon the current candidate when its condition stops holding."""
        self._candidate = None
        self._streak_start = 0.0


def get_pi_hardware_metrics():
    """Return (cpu_usage_pct, cpu_temp_c) from the Pi's internal telemetry."""
    try:
        with open("/sys/class/thermal/thermal_zone0/temp", "r") as f:
            cpu_temp = int(f.read()) / 1000.0
    except Exception:
        cpu_temp = 0.0
    cpu_usage = psutil.cpu_percent(interval=None)
    return cpu_usage, cpu_temp


# =========================================================================
# TELEMETRY
# =========================================================================
# One telemetry record (dict) is assembled per loop and fanned out to sinks.
# The terminal sink prints to the Pi's own console (the node runs fully offline).
# read_power_w() reads the INA219 (I2C) for whole-Pi 5V-rail power draw (watts);
# it returns None when the sensor is absent so the node still runs headless.
# _cloud_sink() is an intentional stub: a non-blocking uploader once the cloud
# platform is chosen.

# YOLOv8 COCO 80-class map (dense indices 0-79, not the sparse 91-class paper IDs).
COCO_LABELS = {
    0: "Student/Person", 1: "Bicycle", 2: "Car", 3: "Motorcycle", 4: "Airplane",
    5: "Bus", 6: "Train", 7: "Truck", 8: "Boat", 9: "Traffic Light",
    10: "Fire Hydrant", 11: "Stop Sign", 12: "Parking Meter", 13: "Bench", 14: "Bird",
    15: "Cat", 16: "Dog", 17: "Horse", 18: "Sheep", 19: "Cow",
    20: "Elephant", 21: "Bear", 22: "Zebra", 23: "Giraffe", 24: "Backpack",
    25: "Umbrella", 26: "Handbag", 27: "Tie", 28: "Suitcase", 29: "Frisbee",
    30: "Skis", 31: "Snowboard", 32: "Sports Ball", 33: "Kite", 34: "Baseball Bat",
    35: "Baseball Glove", 36: "Skateboard", 37: "Surfboard", 38: "Tennis Racket", 39: "Water Bottle",
    40: "Wine Glass", 41: "Coffee Cup/Mug", 42: "Fork", 43: "Knife", 44: "Spoon",
    45: "Bowl", 46: "Banana", 47: "Apple", 48: "Sandwich", 49: "Orange",
    50: "Broccoli", 51: "Carrot", 52: "Hot Dog", 53: "Pizza", 54: "Donut",
    55: "Cake", 56: "Chair", 57: "Couch/Sofa", 58: "Potted Plant", 59: "Bed",
    60: "Dining Table", 61: "Toilet", 62: "Lab Monitor/TV", 63: "Laptop", 64: "Computer Mouse",
    65: "Remote Control", 66: "Keyboard", 67: "Cell Phone", 68: "Microwave", 69: "Oven",
    70: "Toaster", 71: "Sink", 72: "Refrigerator", 73: "Book/Notebook", 74: "Clock",
    75: "Vase", 76: "Scissors", 77: "Teddy Bear", 78: "Hair Drier", 79: "Toothbrush",
}


# INA219 power monitor — optional. Lazily initialised on the first read so the
# node runs fine when the sensor is absent (read_power_w() returns None and the
# telemetry "pwr" column shows "-- W"). See docs/HARDWARE_CONNECTIONS.md Section 4.
INA219_SHUNT_OHMS = 0.1      # on-board shunt on the GY-219 / Adafruit INA219
INA219_MAX_AMPS = 3.2        # 320 mV gain over 0.1 Ohm; covers the Pi 4B's peak draw
_ina219 = None
_ina219_unavailable = False


def read_power_w():
    """Whole-Pi power draw in watts from the INA219 over I2C.

    The INA219 sits high-side, inline on the Pi's incoming 5 V USB-C feed (see
    docs/HARDWARE_CONNECTIONS.md, Section 4), so its power register is the total
    power delivered to the Pi. Returns watts, or None if the sensor is not
    wired / not responding so telemetry degrades gracefully to "-- W".
    """
    global _ina219, _ina219_unavailable
    if _ina219_unavailable:
        return None
    if _ina219 is None:
        try:
            from ina219 import INA219
            _ina219 = INA219(INA219_SHUNT_OHMS, INA219_MAX_AMPS)
            # 16 V range (the Pi runs at 5 V) + 320 mV gain -> up to 3.2 A at the
            # finest resolution that still spans the Pi's peak draw.
            _ina219.configure(_ina219.RANGE_16V, _ina219.GAIN_8_320MV)
        except Exception as exc:
            print(f"[PWR] INA219 unavailable ({exc}); power telemetry disabled.", flush=True)
            _ina219_unavailable = True
            return None
    try:
        return _ina219.power() / 1000.0    # pi-ina219 returns milliwatts
    except Exception:
        # e.g. a transient I2C glitch or DeviceRangeError on a current spike;
        # drop just this sample rather than killing telemetry.
        return None


def build_detection_pairs(class_ids, confidences):
    """Map raw YOLO class IDs to (label, confidence%) tuples for logging."""
    return [
        (COCO_LABELS.get(cid, f"Class_{cid}"), round(conf * 100, 1))
        for cid, conf in zip(class_ids, confidences)
    ]


# =========================================================================
# THINGSPEAK CLOUD TELEMETRY (optional — enabled with --cloud)
# =========================================================================
# Free ThingSpeak accepts one update per 15s, far slower than the per-frame
# telemetry loop, so the cloud is sampled on a timer: emit_telemetry() stashes
# the latest record (a non-blocking locked assignment) and the uploader thread
# POSTs it every CLOUD_INTERVAL_S, so the HTTP call never runs in the loop.
# The write key is read from a git-ignored .env (never hardcoded). The loader
# checks the project root first, then this script's directory, so a .env in
# either location works — copy .env.example to .env and fill it in.
_here = os.path.dirname(os.path.abspath(__file__))
for _env_path in (os.path.join(os.path.dirname(_here), ".env"),  # project root
                  os.path.join(_here, ".env")):                  # next to this script
    if os.path.isfile(_env_path):
        load_dotenv(_env_path)
        break
THINGSPEAK_WRITE_KEY = os.environ.get("THINGSPEAK_API_KEY", "")
THINGSPEAK_URL = "https://api.thingspeak.com/update"
CLOUD_INTERVAL_S = 20.0       # >= 15s free-tier floor, with margin
CLOUD_HTTP_TIMEOUT_S = 10.0   # cap a slow/hung POST so the thread never wedges
ENABLE_CLOUD = False          # overridden by the --cloud CLI flag in __main__

_latest_record = None
_latest_record_lock = threading.Lock()


def emit_telemetry(record):
    """Fan a telemetry record out to all active sinks."""
    _terminal_sink(record)
    if ENABLE_CLOUD:                      # stash latest for the uploader thread
        global _latest_record
        with _latest_record_lock:
            _latest_record = record


def cloud_uploader_worker():
    """Background thread: every CLOUD_INTERVAL_S, POST the latest telemetry
    record to ThingSpeak. None-valued metrics are omitted (ThingSpeak leaves
    that field unchanged); network failures are logged and swallowed so the
    node keeps running offline-first."""
    print(f"[CLOUD] ThingSpeak uploader started ({CLOUD_INTERVAL_S:.0f}s interval).", flush=True)
    while not shutdown_event.wait(CLOUD_INTERVAL_S):   # also wakes on shutdown
        with _latest_record_lock:
            record = _latest_record
        if record is None:
            continue

        fields = {}
        if record.get("power_w") is not None:
            fields["field1"] = f"{record['power_w']:.3f}"     # power (W)
        if record.get("latency_ms") is not None:
            fields["field2"] = f"{record['latency_ms']:.1f}"  # latency (ms)
        if record.get("cpu_temp_c") is not None:
            fields["field3"] = f"{record['cpu_temp_c']:.1f}"  # CPU temp (C)
        if record.get("distance_cm") is not None:
            fields["field4"] = f"{record['distance_cm']:.1f}" # distance (cm)
        if not fields:
            continue

        url = f"{THINGSPEAK_URL}?api_key={THINGSPEAK_WRITE_KEY}&{urllib.parse.urlencode(fields)}"
        try:
            with urllib.request.urlopen(url, timeout=CLOUD_HTTP_TIMEOUT_S) as resp:
                if resp.read().decode().strip() == "0":   # "0" = rejected (rate limit / bad key)
                    print("[CLOUD] update rejected (rate limit / invalid key?).", flush=True)
        except Exception as exc:
            print(f"[CLOUD] upload failed ({exc}); will retry next interval.", flush=True)


def _terminal_sink(record):
    """Print one telemetry record to the Pi terminal."""
    res = record["model_res"]
    res_str = str(res) if res else "---"
    lat = record["latency_ms"]
    lat_str = f"{lat:6.1f}ms" if lat is not None else "    ---  "
    pwr = record["power_w"]
    pwr_str = f"{pwr:5.2f}W" if pwr is not None else "  -- W"
    dets = record["detections"]
    det_str = ", ".join(f"{name}({conf:.1f}%)" for name, conf in dets) if dets else "none"
    print(
        f"[{record['ts']}] {record['state']:<9} | model {res_str:<3} | lat {lat_str} | "
        f"cpu {record['cpu_pct']:4.1f}% | temp {record['cpu_temp_c']:4.1f}C | pwr {pwr_str} | "
        f"dist {record['distance_cm']:6.1f}cm | dets: {det_str}",
        flush=True,
    )


# =========================================================================
# ON-PI DEMO GUI (optional — disabled with --headless for remote deployment)
# =========================================================================
ENABLE_GUI = True          # overridden by the --headless CLI flag in __main__
shutdown_event = threading.Event()

WINDOW_NAME = "SAGE-Vision"
HEADER_H = 72              # solid HUD bar stacked ABOVE the unobstructed video
BOX_COLOR = (255, 0, 0)    # pure blue (BGR) detection boxes
STATE_COLORS = {           # HUD state-name colour coding
    STATE_SLEEP:     (170, 170, 170),
    STATE_STANDBY:   (170, 170, 170),
    STATE_ACTIVE_LO: (0, 200, 0),
    STATE_ACTIVE_HI: (0, 200, 0),
    STATE_WATCHDOG:  (0, 0, 255),
}
_fullscreen = False


def _text(img, s, org, color, scale=0.5, thick=1, outline=False):
    """Draw text, optionally with a black outline for legibility on any scene."""
    if outline:
        cv2.putText(img, s, org, cv2.FONT_HERSHEY_SIMPLEX, scale, (0, 0, 0), thick + 2, cv2.LINE_AA)
    cv2.putText(img, s, org, cv2.FONT_HERSHEY_SIMPLEX, scale, color, thick, cv2.LINE_AA)


def _compose_gui(video, record, boxes, detection_pairs):
    """Draw blue boxes on the video and stack a solid HUD header above it."""
    for (x1, y1, x2, y2), (name, conf) in zip(boxes, detection_pairs):
        cv2.rectangle(video, (x1, y1), (x2, y2), BOX_COLOR, 2)
        _text(video, f"{name} {conf:.0f}%", (x1, max(y1 - 6, 12)), BOX_COLOR, 0.5, 1, outline=True)

    header = np.full((HEADER_H, video.shape[1], 3), (30, 30, 30), dtype=np.uint8)
    state = record["state"]
    _text(header, f"SAGE-Vision   {state}", (8, 20), STATE_COLORS.get(state, (255, 255, 255)), 0.6, 1)

    res = record["model_res"]; res_str = str(res) if res else "---"
    lat = record["latency_ms"]; lat_str = f"{lat:.0f} ms" if lat is not None else "---"
    fps = record.get("fps"); fps_str = f"{fps:.0f} FPS" if fps else "-- FPS"
    _text(header, f"Model {res_str} | Objects: {len(detection_pairs)} | {lat_str} | {fps_str}",
          (8, 43), (255, 255, 255), 0.5, 1)

    pwr = record["power_w"]; pwr_str = f"{pwr:.2f} W" if pwr is not None else "-- W"
    dist = record.get("distance_cm")
    dist_str = f"{dist:.0f} cm" if dist is not None else "n/a"
    _text(header, f"cpu {record['cpu_pct']:.0f}% | {record['cpu_temp_c']:.0f}C | {pwr_str} | dist {dist_str}",
          (8, 64), (255, 255, 255), 0.5, 1)

    return np.vstack([header, video])


def _idle_frame():
    """Black placeholder shown during SLEEP/STANDBY (no inference)."""
    f = np.zeros((480, 640, 3), dtype=np.uint8)
    _text(f, "SYSTEM IDLE", (205, 250), (170, 170, 170), 1.0, 2, outline=True)
    return f


def show_gui(video, record, boxes, detection_pairs):
    """Render one GUI frame and pump keys. Returns 'quit' if the user pressed q."""
    global _fullscreen
    cv2.imshow(WINDOW_NAME, _compose_gui(video, record, boxes, detection_pairs))
    key = cv2.waitKey(1) & 0xFF
    if key == ord('q'):
        return "quit"
    if key == ord('f'):
        _fullscreen = not _fullscreen
        cv2.setWindowProperty(WINDOW_NAME, cv2.WND_PROP_FULLSCREEN,
                              cv2.WINDOW_FULLSCREEN if _fullscreen else cv2.WINDOW_NORMAL)
    return None


# =========================================================================
# THREAD 1: GPIO SENSOR HARVESTER (pinned to CPU core 1)
# =========================================================================
# Hardware GPIO pin map (BCM numbering). The HC-SR04 ECHO line is 5V and MUST
# pass through a resistor voltage divider down to 3.3V before reaching the Pi.
PIR_PIN  = 17   # PIR digital motion output
LDR_PIN  = 27   # LM393 light comparator digital output (active-low: LOW = dark)
TRIG_PIN = 23   # HC-SR04 trigger output
ECHO_PIN = 24   # HC-SR04 echo input (via 5V -> 3.3V voltage divider)

# Ultrasonic timing.
TRIGGER_INTERVAL_S = 0.060   # ~16.6 Hz sonar trigger pace
ECHO_TIMEOUT_S     = 0.058   # TUNABLE: no echo within this window => dead sonar.
                             # Must exceed your HC-SR04's no-object pulse length
                             # (~38 ms datasheet; some clones stretch longer).

# Echo pulse state shared between the pigpio edge callback and the harvester loop.
# pigpio fires the callback from its own real-time thread (the equivalent of the
# ESP32 IRAM ISR), so accesses are guarded by echo_lock.
echo_lock = threading.Lock()
_echo_start_tick = 0
_echo_pulse_ready = False
_echo_duration_us = 0


def _echo_edge_callback(gpio, level, tick):
    """pigpio EITHER_EDGE callback: timestamp the HC-SR04 echo pulse width.

    Rising edge stamps the start tick; falling edge computes the pulse duration.
    pigpio.tickDiff handles the 32-bit microsecond counter wraparound.
    """
    global _echo_start_tick, _echo_pulse_ready, _echo_duration_us
    with echo_lock:
        if level == 1:            # rising edge: pulse start
            _echo_start_tick = tick
        elif level == 0:          # falling edge: pulse end
            if _echo_start_tick != 0:
                _echo_duration_us = pigpio.tickDiff(_echo_start_tick, tick)
                _echo_start_tick = 0
                _echo_pulse_ready = True


def gpio_harvester_worker():
    # Pin to CPU core 1 to isolate sensor sampling from inference jitter.
    os.sched_setaffinity(0, {1})
    print(f"[SYSTEM] GPIO sensor harvester pinned to CPU core {os.sched_getaffinity(0)}", flush=True)

    global last_valid_reading_time, distance_history, consecutive_dropped_readings
    global pigpio_fault, _echo_pulse_ready, _echo_duration_us, consecutive_jump_count

    pi = pigpio.pi()  # connect to the local pigpiod daemon
    if not pi.connected:
        print("[FATAL] Cannot connect to pigpiod. Start it with 'sudo pigpiod'.", flush=True)
        sys.exit(1)

    pi.set_mode(TRIG_PIN, pigpio.OUTPUT)
    pi.set_mode(ECHO_PIN, pigpio.INPUT)
    pi.set_mode(PIR_PIN, pigpio.INPUT)
    pi.set_mode(LDR_PIN, pigpio.INPUT)
    pi.write(TRIG_PIN, 0)
    pi.callback(ECHO_PIN, pigpio.EITHER_EDGE, _echo_edge_callback)

    last_trigger = 0.0
    awaiting_echo = False
    trigger_sent_at = 0.0

    while True:
        try:
            now = time.monotonic()

            # 1. Fire a non-blocking 10us trigger pulse on the fixed cadence.
            if now - last_trigger >= TRIGGER_INTERVAL_S:
                last_trigger = now
                pi.gpio_trigger(TRIG_PIN, 10, 1)
                awaiting_echo = True
                trigger_sent_at = now

            # 2. Consume a completed echo transaction, if one is ready.
            with echo_lock:
                pulse_ready = _echo_pulse_ready
                duration_us = _echo_duration_us
                _echo_pulse_ready = False

            if pulse_ready:
                awaiting_echo = False
                dist_cm = (duration_us * 0.0343) / 2.0
                # Clamp out-of-range readings to a sentinel.
                if dist_cm > 500.0 or dist_cm <= 0:
                    dist_cm = -1.0

                # A completed echo (even out-of-range) means the sonar is alive,
                # so it counts as a heartbeat: refresh the watchdog and clear drops.
                with state_mutex:
                    if dist_cm > 0:
                        prev_median = float(np.median(distance_history))
                        if abs(dist_cm - prev_median) > MAX_PLAUSIBLE_JUMP_CM:
                            # Suspected multipath spike: only believe it once it
                            # repeats JUMP_CONFIRM_N times (= real fast movement).
                            consecutive_jump_count += 1
                            if consecutive_jump_count >= JUMP_CONFIRM_N:
                                distance_history = [dist_cm] * DISTANCE_WINDOW_SIZE
                                consecutive_jump_count = 0
                            # else: drop it; do NOT pollute the median window
                        else:
                            consecutive_jump_count = 0
                            distance_history.pop(0)
                            distance_history.append(dist_cm)
                        shared_state["distance"] = float(np.median(distance_history))
                    consecutive_dropped_readings = 0
                last_valid_reading_time = time.time()

            # 3. Sonar liveness: no echo edges at all within the window => a drop.
            elif awaiting_echo and (now - trigger_sent_at) > ECHO_TIMEOUT_S:
                awaiting_echo = False
                with state_mutex:
                    consecutive_dropped_readings += 1

            # 4. Poll the level-read sensors (PIR motion, LM393 light). These are
            #    NOT health-monitorable: a dead level pin reads a constant value
            #    indistinguishable from a genuinely quiet/lit room.
            pir_val = pi.read(PIR_PIN)
            is_dark = (pi.read(LDR_PIN) == 0)   # active-low: LOW output = dark
            with state_mutex:
                shared_state["pir"] = pir_val
                shared_state["is_dark"] = is_dark
                if pir_val == 1:
                    shared_state["last_motion_epoch"] = time.time()

            pigpio_fault = False
            time.sleep(0.01)   # light yield; pigpiod captures echo edges independently

        except Exception as loop_fault:
            # A pigpio call failed (e.g. pigpiod died) -> flag immediate failsafe.
            pigpio_fault = True
            with state_mutex:
                consecutive_dropped_readings += 1
            print(f"[SENSOR ALERT] GPIO read fault or pigpiod connection lost: {loop_fault}", flush=True)
            time.sleep(0.2)


# =========================================================================
# THREAD 2: 5-STATE ADAPTIVE INFERENCE ENGINE (pinned to CPU cores 2 & 3)
# =========================================================================
def adaptive_vision_streamer():
    # Pin to CPU cores 2 & 3 so frame math runs parallel to sensor sampling.
    os.sched_setaffinity(0, {2, 3})
    print(f"[SYSTEM] Vision engine pinned to CPU cores {os.sched_getaffinity(0)}", flush=True)

    camera_feed = cv2.VideoCapture(0)
    camera_feed.set(cv2.CAP_PROP_FRAME_WIDTH, 640)
    camera_feed.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)

    if ENABLE_GUI:
        cv2.namedWindow(WINDOW_NAME, cv2.WINDOW_NORMAL)
        cv2.resizeWindow(WINDOW_NAME, 640, HEADER_H + 480)

    # FSM state. The single TransitionGate debounces every state change; it is
    # re-armed with gate.enter() at each commit so dwell is measured per state.
    current_state = STATE_SLEEP
    gate = TransitionGate()
    standby_start = 0.0
    clahe_active = False
    prev_loop_t = time.time()     # for live FPS estimation

    # Presence-fusion trackers. last_presence_epoch is refreshed by any presence
    # signal; last_person_epoch is the vision vote, refreshed when YOLO sees a person.
    last_presence_epoch = time.time()
    last_person_epoch = 0.0

    print("[INIT] Launching 5-state FSM kernel loop...", flush=True)

    while True:
        now = time.time()
        fps = 1.0 / (now - prev_loop_t) if now > prev_loop_t else 0.0
        prev_loop_t = now

        # -----------------------------------------------------------------
        # STEP A: WATCHDOG FAILSAFE (entry is immediate; recovery is gated)
        # -----------------------------------------------------------------
        # The sonar is the only health-monitorable sensor. It is unhealthy if the
        # daemon faulted, no valid echo has arrived for 2s, or consecutive echo
        # timeouts hit the drop threshold.
        time_since_last_valid_read = now - last_valid_reading_time
        with state_mutex:
            drops_count = consecutive_dropped_readings
        sonar_unhealthy = (pigpio_fault
                           or time_since_last_valid_read > 2.0
                           or drops_count >= MAX_CONSECUTIVE_DROPS)

        if current_state != STATE_WATCHDOG and sonar_unhealthy:
            # Enter immediately from any state: safety beats responsiveness here.
            print(f"[WATCHDOG CRITICAL] Sensor feed compromised "
                  f"(pigpio_fault={pigpio_fault}, elapsed {time_since_last_valid_read:.1f}s, "
                  f"drops {drops_count}). Entering failsafe.", flush=True)
            current_state = STATE_WATCHDOG
            gate.enter(now)
        elif current_state == STATE_WATCHDOG:
            # Recovery is sticky: leave only after the sonar has stayed healthy
            # for WATCHDOG_RECOVER_HOLD_S AND we've held WATCHDOG for the min
            # dwell. This stops a marginal sonar from flapping WATCHDOG<->ACTIVE.
            if sonar_unhealthy:
                gate.clear()
            elif gate.request(STATE_STANDBY, now, WATCHDOG_RECOVER_HOLD_S, WATCHDOG_MIN_DWELL_S):
                print("[WATCHDOG RESOLVED] Heartbeat restored. Returning to STANDBY.", flush=True)
                current_state = STATE_STANDBY
                standby_start = now
                gate.enter(now)

        # -----------------------------------------------------------------
        # STEP B: READ SENSOR STATE + COMPUTE PRESENCE
        # -----------------------------------------------------------------
        with state_mutex:
            pir = shared_state["pir"]
            distance = shared_state["distance"]
            is_dark = shared_state["is_dark"]
            last_motion = shared_state["last_motion_epoch"]

        # Presence stays alive if ANY signal fires: PIR motion, a close sonar
        # reading, or a recent vision person-detection. quiet_for then drives the
        # two-stage wind-down (grace -> cheap LO, timeout -> SLEEP).
        last_presence_epoch = max(last_presence_epoch, last_motion, last_person_epoch)
        if distance < PRESENCE_DISTANCE_CM:
            last_presence_epoch = now
        quiet_for    = now - last_presence_epoch
        winding_down = quiet_for > PRESENCE_GRACE_S      # quiet -> cheap LO wind-down
        absent       = quiet_for > PRESENCE_TIMEOUT_S    # quiet long enough -> SLEEP

        # -----------------------------------------------------------------
        # STEP C: FSM TRANSITIONS (every debounced edge goes through the gate)
        # -----------------------------------------------------------------
        if current_state == STATE_SLEEP:
            # Wake on sustained motion or a target stepping inside WAKE_DISTANCE_CM.
            # The gate's hold requirement filters single stray PIR pulses / blips.
            wake_signal = (pir == 1) or (distance < WAKE_DISTANCE_CM)
            if wake_signal and gate.request(STATE_STANDBY, now, WAKE_HOLD_S, dwell_s=0.0):
                current_state = STATE_STANDBY
                standby_start = now
                gate.enter(now)
                print("[FSM] SLEEP -> STANDBY. Wake confirmed.", flush=True)
            elif not wake_signal:
                gate.clear()

        elif current_state == STATE_STANDBY:
            # Brief warmup, then pick the model from the current distance.
            if now - standby_start >= STANDBY_WARMUP_S:
                current_state = STATE_ACTIVE_LO if distance < HILO_CLOSE_CM else STATE_ACTIVE_HI
                gate.enter(now)
                print(f"[FSM] STANDBY -> {current_state} (distance {distance:.1f}cm).", flush=True)

        elif current_state == STATE_ACTIVE_LO:
            if absent:
                # Vacancy confirmed by the 12s presence timeout (already debounced).
                current_state = STATE_SLEEP
                gate.enter(now)
                print("[FSM] ACTIVE-LO -> SLEEP. Vacancy confirmed.", flush=True)
            elif winding_down:
                # Presence quiet: hold cheap LO as the wind-down; do not escalate.
                gate.clear()
            elif distance > HILO_FAR_CM:
                # Present + far -> high-res, debounced by the gate.
                if gate.request(STATE_ACTIVE_HI, now, HILO_HOLD_S, HILO_DWELL_S):
                    current_state = STATE_ACTIVE_HI
                    gate.enter(now)
                    print("[FSM] ACTIVE-LO -> ACTIVE-HI. Target moved past far threshold.", flush=True)
            else:
                gate.clear()   # crossing condition lapsed -> drop partial confirmation

        elif current_state == STATE_ACTIVE_HI:
            if winding_down:
                # Presence quiet: drop to cheap LO for the wind-down countdown
                # instead of burning HI inference right up to SLEEP.
                current_state = STATE_ACTIVE_LO
                gate.enter(now)
                print("[FSM] ACTIVE-HI -> ACTIVE-LO. Presence quiet; low-power wind-down.", flush=True)
            elif distance < HILO_CLOSE_CM:
                # Present + close -> low-res, debounced by the gate.
                if gate.request(STATE_ACTIVE_LO, now, HILO_HOLD_S, HILO_DWELL_S):
                    current_state = STATE_ACTIVE_LO
                    gate.enter(now)
                    print("[FSM] ACTIVE-HI -> ACTIVE-LO. Target moved inside close threshold.", flush=True)
            else:
                gate.clear()   # crossing condition lapsed -> drop partial confirmation

        # -----------------------------------------------------------------
        # STEP D: LOW-LIGHT PREPROCESSING GATE
        # -----------------------------------------------------------------
        # The LM393 comparator gives a clean digital dark/bright signal; its
        # onboard pot threshold and built-in hysteresis replace any software
        # dead-band. WATCHDOG assumes worst-case low light.
        clahe_active = True if current_state == STATE_WATCHDOG else is_dark

        # -----------------------------------------------------------------
        # STEP E: EXECUTE THE CURRENT STATE
        # -----------------------------------------------------------------
        # SLEEP and STANDBY do no inference; emit an idle record and pace down.
        if current_state in (STATE_SLEEP, STATE_STANDBY):
            cpu_pct, cpu_c = get_pi_hardware_metrics()
            idle_record = {
                "ts": time.strftime("%H:%M:%S"),
                "state": current_state,
                "model_res": None,
                "latency_ms": None,
                "cpu_pct": cpu_pct,
                "cpu_temp_c": cpu_c,
                "power_w": read_power_w(),
                "distance_cm": distance,
                "fps": fps,
                "detections": [],
            }
            emit_telemetry(idle_record)
            if ENABLE_GUI and show_gui(_idle_frame(), idle_record, [], []) == "quit":
                break
            # SLEEP paces at 0.5s: slow enough to save power, fast enough for the
            # gate's wake hold to confirm a genuine signal within ~1s. STANDBY
            # spins fast to clear the brief warmup.
            time.sleep(0.5 if current_state == STATE_SLEEP else 0.05)
            continue

        # Inference states: ACTIVE-LO, ACTIVE-HI, WATCHDOG.
        success, raw_frame = camera_feed.read()
        if not success or raw_frame is None:
            continue

        # Optional low-light enhancement (CLAHE on the luma channel).
        if clahe_active:
            yuv = cv2.cvtColor(raw_frame, cv2.COLOR_BGR2YUV)
            clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
            yuv[:, :, 0] = clahe.apply(yuv[:, :, 0])
            processed_frame = cv2.cvtColor(yuv, cv2.COLOR_YUV2BGR)
        else:
            processed_frame = raw_frame

        # Select the model and loop pacing for the current state.
        if current_state == STATE_ACTIVE_LO:
            detector, img_inference_size, loop_pacing_rate = model_lo, 320, 0.01   # close: highly responsive
        elif current_state == STATE_ACTIVE_HI:
            detector, img_inference_size, loop_pacing_rate = model_hi, 640, 0.40   # far: slow to save resources
        else:  # STATE_WATCHDOG
            detector, img_inference_size, loop_pacing_rate = model_hi, 640, 0.10   # failsafe fallback pace

        start_inference = time.time()
        detected_classes, detected_confidences, detected_boxes = detector(processed_frame)
        inference_duration_ms = (time.time() - start_inference) * 1000.0

        # Vision presence vote: a detected person keeps the node awake even with
        # no PIR motion (a motionless-but-visible occupant).
        if PERSON_CLASS_ID in detected_classes:
            last_person_epoch = time.time()

        detection_pairs = build_detection_pairs(detected_classes, detected_confidences)
        cpu_pct, cpu_c = get_pi_hardware_metrics()

        active_record = {
            "ts": time.strftime("%H:%M:%S"),
            "state": current_state,
            "model_res": img_inference_size,
            "latency_ms": inference_duration_ms,
            "cpu_pct": cpu_pct,
            "cpu_temp_c": cpu_c,
            "power_w": read_power_w(),
            "distance_cm": distance,
            "fps": fps,
            "detections": detection_pairs,
        }
        emit_telemetry(active_record)

        if ENABLE_GUI and show_gui(processed_frame, active_record, detected_boxes, detection_pairs) == "quit":
            break

        time.sleep(loop_pacing_rate)

    # GUI quit ('q') -> tidy up and signal the main thread to exit.
    camera_feed.release()
    if ENABLE_GUI:
        cv2.destroyAllWindows()
    shutdown_event.set()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="SAGE-Vision adaptive edge inference node")
    parser.add_argument("--headless", action="store_true",
                        help="run without the on-Pi GUI window (remote / no-monitor deployment)")
    parser.add_argument("--cloud", action="store_true",
                        help="stream telemetry (power, latency, CPU temp, distance) to ThingSpeak every 20s")
    args = parser.parse_args()
    ENABLE_GUI = not args.headless
    ENABLE_CLOUD = args.cloud
    if ENABLE_CLOUD and not THINGSPEAK_WRITE_KEY:
        print("[CLOUD] --cloud set but THINGSPEAK_API_KEY is empty (check rpi_edge/.env); "
              "cloud upload disabled.", flush=True)
        ENABLE_CLOUD = False

    print(f"[INIT] Launching SAGE-Vision edge node "
          f"(GUI {'enabled' if ENABLE_GUI else 'disabled'}, "
          f"cloud {'enabled' if ENABLE_CLOUD else 'disabled'})...", flush=True)
    harvester_thread = threading.Thread(target=gpio_harvester_worker, daemon=True)
    vision_thread = threading.Thread(target=adaptive_vision_streamer, daemon=True)

    harvester_thread.start()
    vision_thread.start()
    if ENABLE_CLOUD:
        threading.Thread(target=cloud_uploader_worker, daemon=True).start()

    try:
        shutdown_event.wait()
    except KeyboardInterrupt:
        pass
    print("\n[SHUTDOWN] SAGE-Vision edge node stopped.", flush=True)
