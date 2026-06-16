import os
import sys
import time
import cv2
import psutil

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_RPI_EDGE_DIR = os.path.join(_SCRIPT_DIR, "..", "rpi_edge")
sys.path.insert(0, _RPI_EDGE_DIR)
from yolo_tflite import YoloTFLite

# Baseline is the unoptimised control: fixed 640x640, no sensors, no adaptive
# switching. It logs the same telemetry fields as the adaptive node (minus the
# sensor-derived distance) so an energy/latency/thermal comparison is direct.
MODEL_WEIGHTS_PATH = os.path.join(_RPI_EDGE_DIR, "yolov8n_640_int8.tflite")

# Contiguous YOLOv8 COCO 80-class index map (0-79). IDs are NOT the sparse
# 91-class COCO paper IDs — YOLO remaps them to a dense 0-79 range.
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

try:
    yolo_model = YoloTFLite(MODEL_WEIGHTS_PATH)
    print("[BASELINE] TFLite INT8 YOLO baseline engine initialized successfully.", flush=True)
except Exception as e:
    print(f"[FATAL] Model loading failure: {e}", flush=True)
    sys.exit(1)


def get_pi_hardware_metrics():
    try:
        with open("/sys/class/thermal/thermal_zone0/temp", "r") as f:
            cpu_temp = int(f.read()) / 1000.0
    except Exception:
        cpu_temp = 0.0
    return psutil.cpu_percent(interval=None), cpu_temp


def read_power_w():
    """STUB: whole-Pi power draw in watts from the INA260 (I2C).

    Returns None until the sensor is wired in. Mirrors the adaptive node so the
    baseline and adaptive energy logs share the same column.
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
    """Fan a telemetry record out to all active sinks (terminal only here)."""
    _terminal_sink(record)
    # _cloud_sink(record)   # TODO: enable once the cloud platform is chosen


def _terminal_sink(record):
    """Print one telemetry record to the terminal (baseline has no distance)."""
    lat = record["latency_ms"]
    lat_str = f"{lat:6.1f}ms" if lat is not None else "    ---  "
    pwr = record["power_w"]
    pwr_str = f"{pwr:5.2f}W" if pwr is not None else "  -- W"
    dets = record["detections"]
    det_str = ", ".join(f"{name}({conf:.1f}%)" for name, conf in dets) if dets else "none"
    print(
        f"[{record['ts']}] {record['state']:<9} | model {record['model_res']:<3} | lat {lat_str} | "
        f"cpu {record['cpu_pct']:4.1f}% | temp {record['cpu_temp_c']:4.1f}C | pwr {pwr_str} | "
        f"dets: {det_str}",
        flush=True,
    )


if __name__ == "__main__":
    print("[INIT] Starting Unoptimized Baseline Test Run. Press Ctrl+C to terminate...", flush=True)

    camera_feed = cv2.VideoCapture(0)
    camera_feed.set(cv2.CAP_PROP_FRAME_WIDTH, 640)
    camera_feed.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)

    get_pi_hardware_metrics()
    time.sleep(1.0)

    try:
        while True:
            success, raw_frame = camera_feed.read()
            if not success or raw_frame is None:
                continue

            start_inference = time.time()
            detected_classes, detected_confidences = yolo_model(raw_frame)
            inference_duration_ms = (time.time() - start_inference) * 1000.0

            detection_pairs = build_detection_pairs(detected_classes, detected_confidences)
            cpu_p, cpu_t = get_pi_hardware_metrics()

            emit_telemetry({
                "ts": time.strftime("%H:%M:%S"),
                "state": "BASELINE",
                "model_res": 640,        # fixed full resolution — the unoptimised control
                "latency_ms": inference_duration_ms,
                "cpu_pct": cpu_p,
                "cpu_temp_c": cpu_t,
                "power_w": read_power_w(),
                "detections": detection_pairs,
            })
            time.sleep(0.06)

    except KeyboardInterrupt:
        print("\n[INFO] Baseline telemetry collection run completed successfully.")
    finally:
        camera_feed.release()
