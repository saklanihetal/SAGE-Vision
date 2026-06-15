import os
import sys
import time
import cv2
import psutil
from datetime import datetime

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_RPI_EDGE_DIR = os.path.join(_SCRIPT_DIR, "..", "rpi_edge")
sys.path.insert(0, _RPI_EDGE_DIR)
from yolo_tflite import YoloTFLite

# Baseline is the unoptimised control: fixed 640x640, no adaptive switching.
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
    255: "None"
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

            detected_pairs = []
            for cid, conf in zip(detected_classes, detected_confidences):
                label = COCO_LABELS.get(cid, f"Class_{cid}")
                detected_pairs.append((label, round(conf * 100, 1)))
            
            # Construct standard 4-tuple structure array
            tuples_list = []
            for i in range(4):
                if i < len(detected_pairs):
                    tuples_list.append(detected_pairs[i])
                else:
                    tuples_list.append(("None", 0.0))
            
            cpu_p, cpu_t = get_pi_hardware_metrics()
            current_time = datetime.now().strftime("%H:%M:%S")
            
            print(f"{current_time:<12} | BASELINE (Unoptimized) | CPU: {cpu_p:.1f}% | Temp: {cpu_t:.1f}°C | Latency: {inference_duration_ms:.1f}ms | Detections: {tuples_list}", flush=True)
            time.sleep(0.06)
            
    except KeyboardInterrupt:
        print("\n[INFO] Baseline telemetry collection run completed successfully.")
    finally:
        camera_feed.release()