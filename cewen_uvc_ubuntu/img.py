#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Thermal (UVC) + IMX462 CSI dual-camera monitor and recorder."""

import argparse
import os
import subprocess
import sys
import threading
import time
from datetime import datetime

import cv2
import numpy as np

try:
    from scipy import ndimage
except ImportError:
    ndimage = None


# Thermal camera layout, must match uvc_camera.c.
THERMAL_WIDTH = 256
THERMAL_HEIGHT = 192
THERMAL_HEADER_SIZE = 4640
THERMAL_TEMP_SIZE = THERMAL_WIDTH * THERMAL_HEIGHT * 2
THERMAL_YUV_SIZE = THERMAL_WIDTH * THERMAL_HEIGHT * 2
THERMAL_FRAME_SIZE = THERMAL_HEADER_SIZE + THERMAL_TEMP_SIZE + THERMAL_YUV_SIZE
THERMAL_FPS = 25

# Image alignment values measured by the WASD calibration in readme.txt.
SHIFT_X = 92
SHIFT_Y = -3

TEMP_MIN = 0.0
TEMP_MAX = 200.0

CSI_DISPLAY_W = 640
CSI_DISPLAY_H = 360
VIDEO_FOURCC = cv2.VideoWriter_fourcc(*"mp4v")


def parse_args():
    parser = argparse.ArgumentParser(description="Thermal UVC + IMX462 CSI recorder")
    parser.add_argument("--sensor-id", type=int, default=0,
                        help="Jetson CSI sensor id (default: 0)")
    parser.add_argument("--flip", type=int, default=0,
                        help="nvvidconv flip-method (default: 0)")
    parser.add_argument("--csi-width", type=int, default=0,
                        help="force CSI width; auto tries 1920x1080 then 1280x720")
    parser.add_argument("--csi-height", type=int, default=0)
    parser.add_argument("--csi-fps", type=int, default=0)
    parser.add_argument("--v4l2-device", default="",
                        help="open a USB/V4L2 camera by path, e.g. /dev/video2")
    parser.add_argument("--record-dir", default="./recordings")
    parser.add_argument("--save-dir", default="./captured_images")
    parser.add_argument("--alarm-threshold", type=float, default=40.0,
                        help="save alarm snapshots above this temperature")
    parser.add_argument("--no-display", action="store_true",
                        help="run headless, only record")
    return parser.parse_args()


def gstreamer_pipeline(sensor_id, width, height, fps, flip):
    return (
        f"nvarguscamerasrc sensor-id={sensor_id} ! "
        f"video/x-raw(memory:NVMM), width={width}, height={height}, "
        f"format=(string)NV12, framerate=(fraction){fps}/1 ! "
        f"nvvidconv flip-method={flip} ! "
        f"video/x-raw, format=(string)BGRx ! videoconvert ! "
        f"video/x-raw, format=(string)BGR ! "
        f"appsink drop=True max-buffers=1"
    )


def probe_v4l2_camera():
    """Find a V4L2 camera that is not the thermal HikCamera."""
    try:
        result = subprocess.run(
            ["v4l2-ctl", "--list-devices"],
            capture_output=True, text=True, timeout=5,
        )
    except Exception:
        return None

    current_name = ""
    found = []
    for line in result.stdout.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if not line[:1].isspace() and stripped.endswith(":"):
            current_name = stripped[:-1]
        elif stripped.startswith("/dev/video"):
            name_lower = current_name.lower()
            if current_name and "hikcamera" not in name_lower and "teg" not in name_lower:
                found.append((current_name, stripped))

    return found[0] if found else None


def open_v4l2_device(path, args):
    try:
        cap = cv2.VideoCapture(path, cv2.CAP_V4L2)
    except Exception:
        cap = cv2.VideoCapture(path)

    if not cap.isOpened():
        cap.release()
        return None, None

    if args.csi_width:
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, args.csi_width)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, args.csi_height or 1080)
        if args.csi_fps:
            cap.set(cv2.CAP_PROP_FPS, args.csi_fps)

    ret, frame = cap.read()
    if not ret or frame is None:
        cap.release()
        return None, None

    width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)) or frame.shape[1]
    height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT)) or frame.shape[0]
    fps = int(cap.get(cv2.CAP_PROP_FPS)) or (args.csi_fps or 30)
    return cap, (width, height, fps)


def open_csi(args):
    """Try USB/V4L2 first, then CSI; IMX462 here is a USB FHD camera."""
    v4l2_candidates = []
    if args.v4l2_device:
        v4l2_candidates.append(("forced V4L2", args.v4l2_device))
    probed = probe_v4l2_camera()
    if probed:
        v4l2_candidates.append(probed)

    tried = set()
    for name, path in v4l2_candidates:
        if path in tried:
            continue
        tried.add(path)
        cap, params = open_v4l2_device(path, args)
        if cap is not None:
            print(f"[CSI] opened {name} ({path})", file=sys.stderr)
            return cap, params

    # Last-resort fallback when v4l2-ctl is not installed.
    for path in ("/dev/video2", "/dev/video3"):
        if path in tried:
            continue
        tried.add(path)
        cap, params = open_v4l2_device(path, args)
        if cap is not None:
            print(f"[CSI] opened {path}", file=sys.stderr)
            return cap, params

    # Real CSI camera setups fall back to nvarguscamerasrc.
    candidates = []
    if args.csi_width:
        width = args.csi_width
        height = args.csi_height or 1080
        fps = args.csi_fps or 30
        candidates.append((width, height, fps,
                           gstreamer_pipeline(args.sensor_id, width, height, fps, args.flip)))
    else:
        candidates.append((1920, 1080, 30,
                           gstreamer_pipeline(args.sensor_id, 1920, 1080, 30, args.flip)))
        candidates.append((1280, 720, 60,
                           gstreamer_pipeline(args.sensor_id, 1280, 720, 60, args.flip)))

    for width, height, fps, pipeline in candidates:
        cap = cv2.VideoCapture(pipeline, cv2.CAP_GSTREAMER)
        if cap.isOpened():
            ret, _ = cap.read()
            if ret:
                print(f"[CSI] opened {width}x{height}@{fps}fps "
                      f"(sensor-id={args.sensor_id})", file=sys.stderr)
                return cap, (width, height, fps)
        cap.release()

    print("[CSI] WARNING: no camera opened, thermal-only mode", file=sys.stderr)
    return None, None


def read_exact(stream, size):
    data = bytearray()
    while len(data) < size:
        chunk = stream.read(size - len(data))
        if not chunk:
            break
        data.extend(chunk)
    return bytes(data)


def looks_like_text(data):
    if not data:
        return False
    printable = sum(1 for byte in data if 32 <= byte < 127)
    return printable / len(data) > 0.8


def read_thermal_frame(stream):
    """Read one frame; tolerate old builds that mix startup text into stdout."""
    raw = read_exact(stream, THERMAL_FRAME_SIZE)
    if len(raw) != THERMAL_FRAME_SIZE:
        return None

    marker = raw.find(b"HK")
    if marker > 0 and looks_like_text(raw[:marker]):
        tail = read_exact(stream, marker)
        if len(tail) != marker:
            return None
        return raw[marker:] + tail
    return raw


def thermal_reader(process, state, stop_event):
    stream = process.stdout
    while not stop_event.is_set():
        raw = read_thermal_frame(stream)
        if raw is None:
            break
        try:
            temp_raw = np.frombuffer(
                raw, dtype="<u2", count=THERMAL_WIDTH * THERMAL_HEIGHT,
                offset=THERMAL_HEADER_SIZE,
            )
            temp_mat = (temp_raw.astype(np.float32) / 64.0) - 50.0
            temp_mat = temp_mat.reshape((THERMAL_HEIGHT, THERMAL_WIDTH))
            clipped = np.clip(temp_mat, TEMP_MIN, TEMP_MAX)
            if ndimage is not None:
                filtered = ndimage.median_filter(clipped, size=3)
            else:
                filtered = cv2.blur(clipped, (3, 3))
            max_temp = float(np.max(filtered))

            yuv = np.frombuffer(
                raw, dtype=np.uint8, count=THERMAL_YUV_SIZE,
                offset=THERMAL_HEADER_SIZE + THERMAL_TEMP_SIZE,
            )
            yuv = yuv.reshape((THERMAL_HEIGHT, THERMAL_WIDTH, 2))
            if SHIFT_Y:
                yuv = np.roll(yuv, SHIFT_Y, axis=0)
            if SHIFT_X:
                yuv = np.roll(yuv, SHIFT_X, axis=1)
            bgr = cv2.cvtColor(yuv, cv2.COLOR_YUV2BGR_YUYV)

            with state["lock"]:
                state["bgr"] = bgr
                state["max_temp"] = max_temp
                state["last_frame"] = time.time()
        except Exception as exc:
            print(f"[Thermal] frame error: {exc}", file=sys.stderr)

    print("[Thermal] reader thread exited", file=sys.stderr)


def make_thermal_display(bgr, max_temp, alarmed):
    disp = cv2.resize(
        bgr, (THERMAL_WIDTH * 2, THERMAL_HEIGHT * 2),
        interpolation=cv2.INTER_NEAREST,
    )
    color = (0, 0, 255) if alarmed else (0, 255, 0)
    cv2.putText(disp, f"MAX: {max_temp:.1f} C", (10, 28),
                cv2.FONT_HERSHEY_SIMPLEX, 0.8, color, 2)
    if alarmed:
        cv2.circle(disp, (disp.shape[1] - 30, 30), 10, (0, 0, 255), -1)
    return disp


def save_alarm_pair(thermal, csi, max_temp, save_dir, last_save):
    now = time.time()
    if now - last_save[0] < 1.0:
        return
    last_save[0] = now
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    if thermal is not None:
        path = os.path.join(save_dir, f"thermal_alarm_{max_temp:.1f}C_{ts}.jpg")
        cv2.imwrite(path, thermal)
        print(f"[ALARM] thermal: {path}", file=sys.stderr)
    if csi is not None:
        path = os.path.join(save_dir, f"csi_alarm_{max_temp:.1f}C_{ts}.jpg")
        cv2.imwrite(path, csi)
        print(f"[ALARM] csi: {path}", file=sys.stderr)


def main():
    args = parse_args()
    script_dir = os.path.dirname(os.path.abspath(__file__))
    uvc_bin = os.path.join(script_dir, "uvc_demo")

    if not os.path.exists(uvc_bin):
        print("ERROR: ./uvc_demo not found, run 'make' first.", file=sys.stderr)
        return 1

    os.makedirs(args.record_dir, exist_ok=True)
    os.makedirs(args.save_dir, exist_ok=True)

    print("[Init] Requesting sudo permission for thermal camera...", file=sys.stderr)
    auth = subprocess.run(["sudo", "-v"])
    if auth.returncode != 0:
        print("[Init] sudo authentication failed, thermal camera cannot start",
              file=sys.stderr)
        return 1

    print("[Init] Starting thermal UVC capture...", file=sys.stderr)
    try:
        process = subprocess.Popen(
            ["sudo", "-n", uvc_bin],
            stdout=subprocess.PIPE,
            stderr=sys.stderr,
        )
    except Exception as exc:
        print(f"[Init] failed to start uvc_demo: {exc}", file=sys.stderr)
        return 1

    stop_event = threading.Event()
    state = {"lock": threading.Lock(), "bgr": None, "max_temp": 0.0, "last_frame": 0.0}
    reader = threading.Thread(
        target=thermal_reader, args=(process, state, stop_event), daemon=True
    )
    reader.start()
    start_time = time.time()

    cap_csi, csi_params = open_csi(args)

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    thermal_vw = cv2.VideoWriter(
        os.path.join(args.record_dir, f"thermal_{ts}.mp4"),
        VIDEO_FOURCC, THERMAL_FPS,
        (THERMAL_WIDTH * 2, THERMAL_HEIGHT * 2),
    )
    csi_vw = None
    if cap_csi is not None:
        csi_w, csi_h, csi_fps = csi_params
        csi_vw = cv2.VideoWriter(
            os.path.join(args.record_dir, f"csi_{ts}.mp4"),
            VIDEO_FOURCC, csi_fps, (csi_w, csi_h),
        )

    if not thermal_vw.isOpened():
        print("[Init] WARNING: thermal VideoWriter failed to open", file=sys.stderr)
    if csi_vw is not None and not csi_vw.isOpened():
        print("[Init] WARNING: csi VideoWriter failed to open", file=sys.stderr)

    print(f"[Init] recording to {args.record_dir}/", file=sys.stderr)
    print("[Init] press 'q' in a window or Ctrl+C to stop", file=sys.stderr)

    last_save = [0.0]
    thermal_warned = False
    try:
        while not stop_event.is_set():
            if process.poll() is not None:
                print(f"[Thermal] uvc_demo exited with code {process.returncode}",
                      file=sys.stderr)
                break

            csi_frame = None
            if cap_csi is not None:
                ret, csi_frame = cap_csi.read()
                if not ret:
                    csi_frame = None

            with state["lock"]:
                thermal = state["bgr"].copy() if state["bgr"] is not None else None
                max_temp = state["max_temp"]

            if thermal is None and not thermal_warned and time.time() - start_time > 10:
                print("[Thermal] no frame after 10s; check uvc_demo stderr above",
                      file=sys.stderr)
                thermal_warned = True

            if thermal is not None:
                alarmed = max_temp >= args.alarm_threshold
                t_disp = make_thermal_display(thermal, max_temp, alarmed)
                thermal_vw.write(t_disp)
                if not args.no_display:
                    cv2.imshow("Thermal", t_disp)

            if csi_frame is not None and csi_vw is not None:
                csi_vw.write(csi_frame)
                if not args.no_display:
                    csi_disp = cv2.resize(csi_frame, (CSI_DISPLAY_W, CSI_DISPLAY_H))
                    cv2.imshow("CSI Visible", csi_disp)

            if thermal is not None and max_temp >= args.alarm_threshold:
                save_alarm_pair(thermal, csi_frame, max_temp, args.save_dir, last_save)

            if args.no_display:
                time.sleep(0.01)
            else:
                if cv2.waitKey(1) & 0xFF == ord("q"):
                    break
    except KeyboardInterrupt:
        pass
    finally:
        print("[Exit] shutting down...", file=sys.stderr)
        stop_event.set()
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
        reader.join(timeout=5)
        thermal_vw.release()
        if csi_vw is not None:
            csi_vw.release()
        if cap_csi is not None:
            cap_csi.release()
        if not args.no_display:
            cv2.destroyAllWindows()
        print("[Exit] done", file=sys.stderr)

    return 0


if __name__ == "__main__":
    sys.exit(main())
