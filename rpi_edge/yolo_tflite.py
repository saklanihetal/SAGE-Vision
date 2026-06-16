"""
Lightweight YOLOv8 INT8 TFLite detector for the Raspberry Pi 4B.

This module replaces the heavyweight `ultralytics` runtime on the edge node.
It drives a fixed-input full-integer-quantized .tflite model directly through
`tflite-runtime`, performing the letterbox / quantize / invoke / dequantize /
decode / NMS pipeline that ultralytics would otherwise do internally.

A full-integer INT8 model has a FIXED input resolution baked in at export time,
so the adaptive 320/640 resolution feature is achieved by loading TWO models
(one per resolution) and selecting the matching interpreter per FSM state.
"""

import cv2
import numpy as np
from tflite_runtime.interpreter import Interpreter


class YoloTFLite:
    """Single fixed-resolution YOLOv8 INT8 TFLite detector.

    Call the instance with a BGR frame; returns (class_ids, confidences, boxes)
    as plain Python lists, where boxes are [x1, y1, x2, y2] in the ORIGINAL
    frame's pixel coordinates (letterbox padding/scale already undone).
    """

    def __init__(self, model_path, conf_thres=0.35, iou_thres=0.45):
        self.interp = Interpreter(model_path=model_path)
        self.interp.allocate_tensors()

        self.inp = self.interp.get_input_details()[0]
        self.out = self.interp.get_output_details()[0]

        self.in_h, self.in_w = int(self.inp["shape"][1]), int(self.inp["shape"][2])
        self.in_scale, self.in_zp = self.inp["quantization"]
        self.out_scale, self.out_zp = self.out["quantization"]

        self.conf_thres = conf_thres
        self.iou_thres = iou_thres

    def _letterbox(self, img):
        """Resize preserving aspect ratio and pad to the model input size."""
        h, w = img.shape[:2]
        r = min(self.in_h / h, self.in_w / w)
        nh, nw = int(round(h * r)), int(round(w * r))
        resized = cv2.resize(img, (nw, nh))
        canvas = np.full((self.in_h, self.in_w, 3), 114, dtype=np.uint8)
        canvas[:nh, :nw] = resized
        return canvas

    def __call__(self, frame_bgr):
        # Original frame size + letterbox scale, used to map boxes back later.
        # _letterbox places the resized image at the canvas top-left (pad on
        # right/bottom only), so original_coord = canvas_coord / r.
        h0, w0 = frame_bgr.shape[:2]
        r = min(self.in_h / h0, self.in_w / w0)

        # Preprocess: letterbox -> RGB -> 0..1 -> quantize to model input dtype
        img = cv2.cvtColor(self._letterbox(frame_bgr), cv2.COLOR_BGR2RGB)
        x = img.astype(np.float32) / 255.0
        if self.in_scale:  # full-integer model: map 0..1 floats into int domain
            x = x / self.in_scale + self.in_zp
        x = x.astype(self.inp["dtype"])

        self.interp.set_tensor(self.inp["index"], x[None, ...])
        self.interp.invoke()

        y = self.interp.get_tensor(self.out["index"]).astype(np.float32)
        if self.out_scale:  # dequantize INT8 output back to real values
            y = (y - self.out_zp) * self.out_scale

        # YOLOv8 detection head: (1, 84, N) -> rows 0-3 xywh, rows 4-83 class scores.
        # Some exports come transposed as (1, N, 84); normalize to (84, N).
        y = y[0]
        if y.shape[0] != 84:
            y = y.T

        boxes_xywh = y[:4].T            # (N, 4) center-x, center-y, w, h (normalized)
        scores = y[4:]                  # (80, N)
        cls = np.argmax(scores, axis=0)
        conf = np.max(scores, axis=0)

        keep = conf > self.conf_thres
        boxes_xywh, cls, conf = boxes_xywh[keep], cls[keep], conf[keep]
        if len(conf) == 0:
            return [], [], []

        # Normalized xywh (center) -> canvas pixel coords (the model-input space)
        cx = boxes_xywh[:, 0] * self.in_w
        cy = boxes_xywh[:, 1] * self.in_h
        bw = boxes_xywh[:, 2] * self.in_w
        bh = boxes_xywh[:, 3] * self.in_h
        x1 = cx - bw / 2
        y1 = cy - bh / 2
        rects = np.stack([x1, y1, bw, bh], axis=1).tolist()

        idxs = cv2.dnn.NMSBoxes(rects, conf.tolist(), self.conf_thres, self.iou_thres)
        idxs = np.array(idxs).flatten() if len(idxs) else np.array([], dtype=int)
        if len(idxs) == 0:
            return [], [], []

        # Map surviving boxes from canvas coords back to the original frame (/r)
        x2 = cx + bw / 2
        y2 = cy + bh / 2
        boxes_xyxy = np.stack([x1, y1, x2, y2], axis=1)[idxs] / r
        boxes_xyxy[:, [0, 2]] = boxes_xyxy[:, [0, 2]].clip(0, w0)
        boxes_xyxy[:, [1, 3]] = boxes_xyxy[:, [1, 3]].clip(0, h0)

        return cls[idxs].tolist(), conf[idxs].tolist(), boxes_xyxy.astype(int).tolist()
