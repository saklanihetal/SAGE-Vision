import os
import sys
import time
import csv
import cv2
import psutil
from datetime import datetime
from ultralytics import YOLO

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
MODEL_WEIGHTS_PATH = os.path.join(_SCRIPT_DIR, "..", "rpi_edge", "yolov8n_full_integer_quant.tflite")

# Comprehensive COCO dataset index mapping optimized for CS Lab environments
COCO_LABELS = {
    0: "Student/Person",
    1: "Bicycle",
    2: "Car",
    3: "Motorcycle",
    4: "Airplane",
    5: "Bus",
    6: "Train",
    7: "Truck",
    9: "Traffic Light",
    11: "Fire Hydrant",
    13: "Stop Sign",
    14: "Parking Meter",
    15: "Bench",
    16: "Bird",
    17: "Cat",
    18: "Dog",
    19: "Horse",
    20: "Sheep",
    21: "Cow",
    22: "Elephant",
    23: "Bear",
    24: "Backpack",
    25: "Umbrella",
    26: "Handbag",
    27: "Tie",
    28: "Suitcase",
    32: "Sports Ball",
    39: "Water Bottle",
    41: "Coffee Cup/Mug",
    42: "Fork",
    43: "Knife",
    44: "Spoon",
    45: "Bowl",
    46: "Banana",
    47: "Apple",
    48: "Sandwich",
    56: "Chair",
    57: "Couch/Sofa",
    58: "Potted Plant",
    59: "Bed",
    60: "Dining Table",
    62: "Lab Monitor/TV",
    63: "Laptop",
    64: "Computer Mouse",
    65: "Remote Control",
    66: "Keyboard",
    67: "Cell Phone",
    68: "Microwave",
    69: "Oven",
    71: "Sink",
    72: "Refrigerator",
    73: "Book/Notebook",
    74: "Clock",
    75: "Vase",
    76: "Scissors",
    77: "Teddy Bear",
    78: "Hair Drier",
    79: "Toothbrush",
    255: "None"
}
# Initialize clean CSV file with the exact matching schema
csv_headers = ["Timestamp", "System Mode", "CPU Usage %", "CPU Temp C", "Sensor Distance cm", "Inference Image Res", "Latency ms", "Objects Tracked", "Confidence Scores"]
with open(LOG_FILE_NAME, mode="w", newline="") as f:
    writer = csv.writer(f)
    writer.writerow(csv_headers)

try:
    yolo_model = YOLO(MODEL_WEIGHTS_PATH, task="detect")
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
    
    # Warm up hardware diagnostics metrics
    get_pi_hardware_metrics()
    time.sleep(1.0)
    
    try:
        while True:
            success, raw_frame = camera_feed.read()
            if not success or raw_frame is None:
                continue
                
            # Baseline Constraint: Always execute model checks at max resolution (640) with no filtering
            start_inference = time.time()
            inference_results = yolo_model(raw_frame, imgsz=640, verbose=False)
            inference_duration_ms = (time.time() - start_inference) * 1000.0
            
            detected_objects = []
            detected_confs = []
            
            for result in inference_results:
                if result.boxes is not None and len(result.boxes) > 0:
                    classes = result.boxes.cls.cpu().int().tolist()
                    confidences = result.boxes.conf.cpu().float().tolist()
                    
                    for cid, conf in zip(classes, confidences):
                        label = COCO_LABELS.get(cid, f"Class_{cid}")
                        detected_objects.append(label)
                        detected_confs.append(f"{round(conf * 100, 1)}")
                        
            objects_str = ", ".join(detected_objects) if detected_objects else "None"
            confs_str = ", ".join(detected_confs) if detected_confs else "0"
            
            cpu_p, cpu_t = get_pi_hardware_metrics()
            timestamp_full = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            
            # Record telemetry rows natively into local CSV spreadsheet file
            with open(LOG_FILE_NAME, mode="a", newline="") as f:
                writer = csv.writer(f)
                writer.writerow([
                    timestamp_full, "BASELINE (Unoptimized)", cpu_p, cpu_t, 
                    0.0, 640, round(inference_duration_ms, 2), objects_str, confs_str
                ])
                
            print(f"Recorded baseline frame - CPU: {cpu_p}% | Temp: {cpu_t}°C | Latency: {inference_duration_ms:.1f}ms", flush=True)
            
            # Match baseline camera frame capture delay pacing ceiling (~15 FPS tracking constraint)
            time.sleep(0.06)
            
    except KeyboardInterrupt:
        print("\n[INFO] Baseline telemetry collection run completed successfully.")
    finally:
        camera_feed.release()