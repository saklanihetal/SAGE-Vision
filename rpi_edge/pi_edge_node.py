import os
import sys
import time
import argparse
import threading
import cv2
import numpy as np
import psutil
import pigpio
from yolo_tflite import YoloTFLite

# =========================================================================
# SECTION 1: GLOBAL STATE ENGINE CONFIGURATIONS & FINITE STATE MACHINE TOKENS
# =========================================================================

# Explicit Finite State Machine tokens representing the 5 core operational states
STATE_SLEEP     = "SLEEP"
STATE_STANDBY   = "STANDBY"
STATE_ACTIVE_LO = "ACTIVE-LO"
STATE_ACTIVE_HI = "ACTIVE-HI"
STATE_WATCHDOG  = "WATCHDOG"

# Initialize thread-safe shared state cache dictionary. The LDR is now a digital
# LM393 comparator (active-low), so light is a boolean dark/bright flag rather
# than an analog value.
shared_state = {
    "pir": 0,
    "distance": 400.0,  # Bounded initialized safe default far distance value
    "is_dark": False,
    "last_motion_epoch": time.time()
}
state_mutex = threading.Lock()

# Rolling buffer array to compute a true median filter for the ultrasonic metrics
DISTANCE_WINDOW_SIZE = 5
distance_history = [400.0] * DISTANCE_WINDOW_SIZE

# Multipath spike rejection (Flap-A mitigation). The HC-SR04's wide (~15 deg)
# beam bounces off walls and furniture, producing single-frame distance jumps
# that are physically impossible for a moving person (e.g. 218cm -> 37cm -> 218cm
# in 300ms). Any reading that jumps more than MAX_PLAUSIBLE_JUMP_CM from the
# current median is treated as a spike and DROPPED, so it never pollutes the
# filter. The exception is genuine fast movement: if the jump persists for
# JUMP_CONFIRM_N consecutive readings, we accept it and snap the window to it.
MAX_PLAUSIBLE_JUMP_CM = 120.0
JUMP_CONFIRM_N = 3
consecutive_jump_count = 0

# State-transition debounce (Flap-A mitigation). A single distance reading
# crossing the 120/160 cm hysteresis band must NOT flip the active model. A
# HI<->LO switch is committed only when the crossing condition holds for
# TRANSITION_CONFIRM_N consecutive FSM ticks AND the current state has been held
# for at least MIN_DWELL_S. Together these clamp the rapid ACTIVE-HI/ACTIVE-LO
# churn seen when a stationary target sits near a threshold. These gate ONLY the
# HI<->LO switch; SLEEP/STANDBY/WATCHDOG transitions are unaffected.
MIN_DWELL_S = 1.5
TRANSITION_CONFIRM_N = 3

# Presence fusion (Flap-B mitigation). Absence is declared only when PIR motion,
# a close ultrasonic reading, AND a recent vision person-detection have ALL been
# quiet for PRESENCE_TIMEOUT_S. This replaces the old "PIR-expired AND far"
# rule, which mistook a stationary occupant (PIR sees only motion) plus a single
# spurious sonar spike for an empty room, causing SLEEP<->ACTIVE cycling.
PRESENCE_TIMEOUT_S   = 12.0
PRESENCE_DISTANCE_CM = 350.0
PERSON_CLASS_ID      = 0       # YOLO/COCO dense index for "person"
# Low-power wind-down (Flap-B energy reclaim). When presence signals go quiet we
# do NOT jump straight to SLEEP from ACTIVE-HI (the most expensive state). After
# PRESENCE_GRACE_S of quiet we drop to cheap ACTIVE-LO (320 model) and count down;
# only after PRESENCE_TIMEOUT_S total do we SLEEP. The wind-down reuses ACTIVE-LO
# rather than a separate state. Set PRESENCE_GRACE_S to 0 to drop to LO the
# instant signals lapse; the small default debounces the PRESENCE_DISTANCE_CM
# boundary so a person hovering near it doesn't twitch HI<->LO.
PRESENCE_GRACE_S     = 2.0

# Heartbeat line tracking the exact epoch of the last completed ultrasonic echo
last_valid_reading_time = time.time()

# Failsafe limit: if 3 consecutive sonar triggers return no echo, trigger WATCHDOG
MAX_CONSECUTIVE_DROPS = 3
consecutive_dropped_readings = 0

# Initialize local quantized TFLite engines. A full-integer INT8 model has a
# fixed input resolution, so adaptive switching uses two interpreters: one at
# 320x320 for ACTIVE-LO and one at 640x640 for ACTIVE-HI / WATCHDOG. [cite: 174]
_MODEL_DIR = os.path.dirname(os.path.abspath(__file__))
MODEL_LO_PATH = os.path.join(_MODEL_DIR, "yolov8n_320_int8.tflite")
MODEL_HI_PATH = os.path.join(_MODEL_DIR, "yolov8n_640_int8.tflite")
try:
    model_lo = YoloTFLite(MODEL_LO_PATH)  # 320x320
    model_hi = YoloTFLite(MODEL_HI_PATH)  # 640x640
    print("[SYSTEM] TFLite INT8 YOLO engines (320 + 640) successfully initialized.", flush=True)
except Exception as e:
    print(f"[FATAL] Failed to locate or parse TFLite model file: {e}", flush=True)
    sys.exit(1)


def get_pi_hardware_metrics():
    """Reads internal Pi telemetry sensors for project evaluation data."""
    try:
        with open("/sys/class/thermal/thermal_zone0/temp", "r") as f:
            temp_raw = int(f.read())
        cpu_temp = temp_raw / 1000.0
    except Exception:
        cpu_temp = 0.0
        
    cpu_usage = psutil.cpu_percent(interval=None)
    return cpu_usage, cpu_temp


# =========================================================================
# TELEMETRY RECORD & SINKS
# =========================================================================
# Telemetry is assembled into one record (dict) per loop and fanned out to a
# set of sinks. The terminal sink prints to the Pi's own console (the system
# runs fully offline). The power and cloud sinks are intentional stubs to be
# filled in later:
#   - read_power_w(): INA260 over I2C — whole-Pi 5V rail power draw (watts)
#   - _cloud_sink():  non-blocking uploader once the cloud platform is chosen

# Contiguous YOLOv8 COCO 80-class index map (0-79), used to label detections in
# the terminal/telemetry output. IDs are the dense YOLO indices, not the sparse
# 91-class COCO paper IDs.
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


def read_power_w():
    """STUB: whole-Pi power draw in watts from the INA260 (I2C).

    Returns None until the sensor is wired in. To enable, read the INA260 here
    (e.g. adafruit_ina260) and return its .power value converted to watts.
    """
    return None


def build_detection_pairs(class_ids, confidences):
    """Map raw YOLO class IDs to (label, confidence%) tuples for logging."""
    pairs = []
    for cid, conf in zip(class_ids, confidences):
        label = COCO_LABELS.get(cid, f"Class_{cid}")
        pairs.append((label, round(conf * 100, 1)))
    return pairs


def emit_telemetry(record):
    """Fan a telemetry record out to all active sinks."""
    _terminal_sink(record)
    # _cloud_sink(record)   # TODO: enable once the cloud platform is chosen


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
    # Blue detection boxes + plain blue labels (black-outlined for legibility)
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
# THREAD 1: ASYNCHRONOUS GPIO SENSOR HARVESTER (PINNED TO CPU CORE 1)
# =========================================================================
# Hardware GPIO pin map (BCM numbering). The HC-SR04 ECHO line is 5V and MUST
# pass through a resistor voltage divider down to 3.3V before reaching the Pi.
PIR_PIN  = 17   # PIR digital motion output
LDR_PIN  = 27   # LM393 light comparator digital output (active-low: LOW = dark)
TRIG_PIN = 23   # HC-SR04 trigger output
ECHO_PIN = 24   # HC-SR04 echo input (via 5V -> 3.3V voltage divider)

# Ultrasonic timing constants mirroring the original 16.6 Hz firmware cadence
TRIGGER_INTERVAL_S = 0.060   # ~16.6 Hz sonar trigger pace
ECHO_TIMEOUT_S     = 0.058   # TUNABLE: no echo within this window => dead sonar.
                             # Must exceed your HC-SR04's no-object pulse length
                             # (~38 ms datasheet; some clones stretch longer).

# Echo pulse state shared between the pigpio edge callback and the harvester loop.
# pigpio fires the callback from its own real-time thread (the equivalent of the
# ESP32 IRAM ISR), so reads/writes are guarded by echo_lock (the critical section).
echo_lock = threading.Lock()
_echo_start_tick = 0
_echo_pulse_ready = False
_echo_duration_us = 0

# Set when a pigpio/daemon call fails -> forces an immediate WATCHDOG (the
# equivalent of the old "USB serial unplugged" detection).
pigpio_fault = False


def _echo_edge_callback(gpio, level, tick):
    """pigpio EITHER_EDGE callback: timestamps the HC-SR04 echo pulse width.

    Mirrors the firmware ISR: rising edge stamps the start tick, falling edge
    computes the pulse duration. pigpio.tickDiff handles the 32-bit microsecond
    counter wraparound correctly.
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
    # Pin this thread to CPU Core 1 to isolate sensor sampling from inference jitter
    os.sched_setaffinity(0, {1})
    print(f"[SYSTEM] GPIO Sensor Harvester pinned to CPU Core {os.sched_getaffinity(0)}", flush=True)

    global last_valid_reading_time, distance_history, consecutive_dropped_readings
    global pigpio_fault, _echo_pulse_ready, _echo_duration_us, consecutive_jump_count

    pi = pigpio.pi()  # connects to the local pigpiod daemon
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

            # 1. Non-blocking periodic 10us trigger pulse (mirrors firmware cadence)
            if now - last_trigger >= TRIGGER_INTERVAL_S:
                last_trigger = now
                pi.gpio_trigger(TRIG_PIN, 10, 1)   # 10 microsecond HIGH pulse
                awaiting_echo = True
                trigger_sent_at = now

            # 2. Consume a completed echo transaction if one is ready
            with echo_lock:
                pulse_ready = _echo_pulse_ready
                duration_us = _echo_duration_us
                _echo_pulse_ready = False

            if pulse_ready:
                awaiting_echo = False
                dist_cm = (duration_us * 0.0343) / 2.0
                # Outlier rejection + out-of-range sentinel (identical to firmware)
                if dist_cm > 500.0 or dist_cm <= 0:
                    dist_cm = -1.0
                # A COMPLETED echo (even out-of-range) is a valid heartbeat: the
                # sonar is alive, so refresh the watchdog timer and clear drops.
                with state_mutex:
                    if dist_cm > 0:
                        prev_median = float(np.median(distance_history))
                        if abs(dist_cm - prev_median) > MAX_PLAUSIBLE_JUMP_CM:
                            # Suspected multipath spike. Only believe it once it
                            # repeats JUMP_CONFIRM_N times (= real fast movement).
                            consecutive_jump_count += 1
                            if consecutive_jump_count >= JUMP_CONFIRM_N:
                                distance_history = [dist_cm] * DISTANCE_WINDOW_SIZE
                                consecutive_jump_count = 0
                            # else: drop this reading; do NOT pollute the median
                        else:
                            consecutive_jump_count = 0
                            distance_history.pop(0)
                            distance_history.append(dist_cm)
                        # True median removes residual jitter from the accepted window
                        shared_state["distance"] = float(np.median(distance_history))
                    consecutive_dropped_readings = 0
                last_valid_reading_time = time.time()

            # 3. Sonar liveness: NO echo edges at all within the window => dead sensor
            elif awaiting_echo and (now - trigger_sent_at) > ECHO_TIMEOUT_S:
                awaiting_echo = False
                with state_mutex:
                    consecutive_dropped_readings += 1

            # 4. Poll the level-read sensors (PIR motion, LM393 light). These are
            #    not health-monitorable: a dead level pin reads a constant value
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
            # A pigpio call failed (e.g. pigpiod died) -> flag immediate failsafe
            pigpio_fault = True
            with state_mutex:
                consecutive_dropped_readings += 1
            print(f"[SENSOR ALERT] GPIO read fault or pigpiod connection lost: {loop_fault}", flush=True)
            time.sleep(0.2)


# =========================================================================
# THREAD 2: 5-STATE ADAPTIVE INFERENCE ENGINE (PINNED TO CPU CORES 2 & 3)
# =========================================================================
def adaptive_vision_streamer():
    # Pin the vision thread to CPU Cores 2 and 3 to maximize parallel frame math handling
    os.sched_setaffinity(0, {2, 3})
    print(f"[SYSTEM] Vision processing core engine pinned to CPU Cores {os.sched_getaffinity(0)}", flush=True)

    camera_feed = cv2.VideoCapture(0)
    camera_feed.set(cv2.CAP_PROP_FRAME_WIDTH, 640)
    camera_feed.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)

    if ENABLE_GUI:
        cv2.namedWindow(WINDOW_NAME, cv2.WINDOW_NORMAL)
        cv2.resizeWindow(WINDOW_NAME, 640, HEADER_H + 480)

    # Internal FSM state tracking variables
    current_state = STATE_SLEEP
    standby_ticks_start = 0.0
    clahe_active = False
    prev_loop_t = time.time()   # for live FPS estimation

    # Flap-A debounce trackers for the HI<->LO switch (see MIN_DWELL_S above).
    state_entered_at = time.time()
    last_state_seen = current_state
    hilo_candidate = None
    hilo_candidate_count = 0

    # Flap-B presence-fusion trackers (see PRESENCE_TIMEOUT_S above). The epoch is
    # refreshed by any presence signal; last_person_epoch is the vision vote.
    last_presence_epoch = time.time()
    last_person_epoch = 0.0

    def _hilo_confirmed(target):
        """Return True only when `target` has been requested for
        TRANSITION_CONFIRM_N consecutive ticks AND the current state has been
        held >= MIN_DWELL_S. Counts up across ticks; reset via _hilo_reset()."""
        nonlocal hilo_candidate, hilo_candidate_count
        if target != hilo_candidate:
            hilo_candidate, hilo_candidate_count = target, 1
        else:
            hilo_candidate_count += 1
        dwell_ok = (loop_t - state_entered_at) >= MIN_DWELL_S
        return dwell_ok and hilo_candidate_count >= TRANSITION_CONFIRM_N

    def _hilo_reset():
        """Clear a partial confirmation when the crossing condition lapses, so
        only CONSECUTIVE qualifying ticks ever accumulate."""
        nonlocal hilo_candidate, hilo_candidate_count
        hilo_candidate, hilo_candidate_count = None, 0

    print("[INIT] Launching 5-State Finite State Machine Kernel Loop...", flush=True)

    while True:
        # Live loop-rate (effective display FPS, includes pacing/sleep)
        loop_t = time.time()
        fps = 1.0 / (loop_t - prev_loop_t) if loop_t > prev_loop_t else 0.0
        prev_loop_t = loop_t

        # Stamp when the FSM last changed state, so MIN_DWELL_S can be measured.
        # Also clears any partial HI<->LO confirmation carried from the old state.
        if current_state != last_state_seen:
            state_entered_at = loop_t
            last_state_seen = current_state
            hilo_candidate, hilo_candidate_count = None, 0

        # -----------------------------------------------------------------
        # STEP A: WATCHDOG FAILSAFE OVERRIDE RULE EVALUATION (ENHANCED)
        # -----------------------------------------------------------------
        time_since_last_valid_read = time.time() - last_valid_reading_time

        with state_mutex:
            drops_count = consecutive_dropped_readings

        # Failsafe triggers on: lost pigpiod connection, 2s with no valid echo,
        # OR consecutive sonar echo timeouts hitting the drop threshold.
        if pigpio_fault or time_since_last_valid_read > 2.0 or drops_count >= MAX_CONSECUTIVE_DROPS:
            if current_state != STATE_WATCHDOG:
                print(f"[WATCHDOG CRITICAL] Sensor feed compromised! "
                      f"(pigpio_fault={pigpio_fault}, elapsed: {time_since_last_valid_read:.1f}s, "
                      f"consecutive drops: {drops_count}). Entering Failsafe Mode.", flush=True)
                current_state = STATE_WATCHDOG
        elif current_state == STATE_WATCHDOG:
            print("[WATCHDOG RESOLVED] Heartbeat restored. Returning to STANDBY.", flush=True)
            current_state = STATE_STANDBY
            standby_ticks_start = time.time()

        # -----------------------------------------------------------------
        # STEP B: EXTRACT MUTEX-LOCKED TELEMETRY & PRE-CALCULATE CONDITIONS
        # -----------------------------------------------------------------
        with state_mutex:
            pir = shared_state["pir"]
            distance = shared_state["distance"]
            is_dark = shared_state["is_dark"]
            last_motion = shared_state["last_motion_epoch"]
            
        # Presence fusion (Flap-B): keep presence alive if ANY signal fires.
        # quiet_for then drives the two-stage wind-down: grace -> cheap ACTIVE-LO,
        # timeout -> SLEEP.
        now = time.time()
        last_presence_epoch = max(last_presence_epoch, last_motion)   # PIR motion
        if distance < PRESENCE_DISTANCE_CM:
            last_presence_epoch = now                                 # something is close
        if last_person_epoch > last_presence_epoch:
            last_presence_epoch = last_person_epoch                   # vision saw a person
        quiet_for    = now - last_presence_epoch
        winding_down = quiet_for > PRESENCE_GRACE_S      # quiet -> cheap LO wind-down
        absent       = quiet_for > PRESENCE_TIMEOUT_S    # quiet long enough -> SLEEP

        # -----------------------------------------------------------------
        # STEP C: FSM STATE TRANSITION HANDLING MATRIX
        # -----------------------------------------------------------------
        if current_state == STATE_SLEEP:
            # Wake condition: any new PIR movement OR target steps inside the 350cm boundary
            if pir == 1 or distance < 350.0:
                current_state = STATE_STANDBY
                standby_ticks_start = time.time()  # Start the 200 ms warmup timer hook
                print(f"[FSM TRANSITION] SLEEP -> STANDBY. Initiating warmup tick.", flush=True)

        elif current_state == STATE_STANDBY:
            # Hold transient state for exactly one 200 ms execution window tick
            if (time.time() - standby_ticks_start) >= 0.200:
                if distance < 120.0:
                    current_state = STATE_ACTIVE_LO
                elif distance >= 160.0:
                    current_state = STATE_ACTIVE_HI
                else:
                    current_state = STATE_ACTIVE_HI  # Default to high-res mode inside overlap zone
                print(f"[FSM TRANSITION] STANDBY -> {current_state} based on target distance: {distance:.1f}cm", flush=True)

        elif current_state == STATE_ACTIVE_LO:
            if absent:
                current_state = STATE_SLEEP
                print("[FSM TRANSITION] ACTIVE-LO -> SLEEP. Target vacancy confirmed.", flush=True)
            elif winding_down:
                # Presence signals quiet: hold cheap LO as the low-power wind-down
                # and do NOT escalate to HI while counting down to SLEEP.
                _hilo_reset()
            elif distance > 160.0:  # present + far -> high-res; debounced (A1+A2)
                if _hilo_confirmed(STATE_ACTIVE_HI):
                    current_state = STATE_ACTIVE_HI
                    print(f"[FSM TRANSITION] ACTIVE-LO -> ACTIVE-HI. Target exited close-zone threshold (> 160cm).", flush=True)
            else:  # crossing condition lapsed -> drop any partial confirmation
                _hilo_reset()

        elif current_state == STATE_ACTIVE_HI:
            if winding_down:
                # Presence signals quiet: drop to cheap LO for the wind-down
                # countdown instead of burning HI inference right up to SLEEP.
                current_state = STATE_ACTIVE_LO
                print("[FSM TRANSITION] ACTIVE-HI -> ACTIVE-LO. Presence quiet; low-power wind-down before sleep.", flush=True)
            elif distance < 120.0:  # present + close -> low-res; debounced (A1+A2)
                if _hilo_confirmed(STATE_ACTIVE_LO):
                    current_state = STATE_ACTIVE_LO
                    print(f"[FSM TRANSITION] ACTIVE-HI -> ACTIVE-LO. Target entered close-zone threshold (< 120cm).", flush=True)
            else:  # crossing condition lapsed -> drop any partial confirmation
                _hilo_reset()

        # -----------------------------------------------------------------
        # STEP D: ORTHOGONAL LDR PREPROCESSING GATE EVALUATION
        # -----------------------------------------------------------------
        # The LM393 comparator supplies a clean digital dark/bright signal; its
        # onboard potentiometer threshold and built-in comparator hysteresis now
        # replace the former software dead-band entirely.
        if current_state == STATE_WATCHDOG:
            clahe_active = True  # Watchdog assumes worst-case low light automatically
        else:
            clahe_active = is_dark

        # -----------------------------------------------------------------
        # STEP E: OUTPUT PROFILE EXECUTION BASED ON CURRENT DRIVING STATE
        # -----------------------------------------------------------------
        # Handle non-inference states (SLEEP & STANDBY) to maximize power savings
        if current_state in [STATE_SLEEP, STATE_STANDBY]:
            cpu_pct, cpu_c = get_pi_hardware_metrics()
            idle_record = {
                "ts": time.strftime("%H:%M:%S"),
                "state": current_state,
                "model_res": None,        # no inference in this state
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

            # Pacing adjustment: deep sleep if sleeping, rapid loop if warming up
            time.sleep(1.0 if current_state == STATE_SLEEP else 0.05)
            continue

        # Inference Processing States (ACTIVE-LO, ACTIVE-HI, WATCHDOG)
        success, raw_frame = camera_feed.read()
        if not success or raw_frame is None:
            continue

        # Apply orthogonal image preprocessing layers if activated 
        if clahe_active:
            yuv_buffer = cv2.cvtColor(raw_frame, cv2.COLOR_BGR2YUV)
            clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8,8))
            yuv_buffer[:, :, 0] = clahe.apply(yuv_buffer[:, :, 0])
            processed_frame = cv2.cvtColor(yuv_buffer, cv2.COLOR_YUV2BGR)
        else:
            processed_frame = raw_frame

        # Resolve resolution model, size constraints and pacing rates [cite: 358, 360]
        if current_state == STATE_ACTIVE_LO:
            detector = model_lo
            img_inference_size = 320
            loop_pacing_rate = 0.01  # Target close: run highly responsive loop pacing
        elif current_state == STATE_ACTIVE_HI:
            detector = model_hi
            img_inference_size = 640
            loop_pacing_rate = 0.40  # Target far: slow down pacing to preserve resources
        else:  # STATE_WATCHDOG
            detector = model_hi
            img_inference_size = 640
            loop_pacing_rate = 0.10  # Failsafe fallback loop pacing rate

        # Run model inference and track math execution performance times
        start_inference = time.time()
        detected_classes, detected_confidences, detected_boxes = detector(processed_frame)
        inference_duration_ms = (time.time() - start_inference) * 1000.0

        # Flap-B vision vote: a detected person refreshes presence directly, so a
        # motionless-but-visible occupant keeps the node awake even with no PIR.
        if PERSON_CLASS_ID in detected_classes:
            last_person_epoch = time.time()

        detection_pairs = build_detection_pairs(detected_classes, detected_confidences)
        cpu_pct, cpu_c = get_pi_hardware_metrics()

        active_record = {
            "ts": time.strftime("%H:%M:%S"),
            "state": current_state,
            "model_res": img_inference_size,   # 320 (ACTIVE-LO) or 640 (ACTIVE-HI / WATCHDOG)
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

    # GUI quit ('q') -> tidy up and signal the main thread to exit
    camera_feed.release()
    if ENABLE_GUI:
        cv2.destroyAllWindows()
    shutdown_event.set()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="SAGE-Vision adaptive edge inference node")
    parser.add_argument("--headless", action="store_true",
                        help="run without the on-Pi GUI window (remote / no-monitor deployment)")
    args = parser.parse_args()
    ENABLE_GUI = not args.headless

    print(f"[INIT] Launching 5-State Adaptive Video Edge Node Framework "
          f"(GUI {'enabled' if ENABLE_GUI else 'disabled'})...", flush=True)
    harvester_thread = threading.Thread(target=gpio_harvester_worker, daemon=True)
    vision_thread = threading.Thread(target=adaptive_vision_streamer, daemon=True)

    harvester_thread.start()
    vision_thread.start()

    try:
        shutdown_event.wait()
    except KeyboardInterrupt:
        pass
    print("\n[SHUTDOWN] SAGE-Vision edge node stopped.", flush=True)