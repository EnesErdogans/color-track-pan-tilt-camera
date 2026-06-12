from __future__ import annotations

import atexit
import ipaddress
import json
import socket
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import cv2
import numpy as np
import serial
from flask import Flask, Response, jsonify, render_template, request
from serial.tools import list_ports


CONFIG_PATH = Path(__file__).resolve().parent / "data" / "settings.json"


def load_saved_config() -> dict[str, Any]:
    try:
        return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def save_config(config: dict[str, Any]) -> None:
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = CONFIG_PATH.with_suffix(".tmp")
    temporary_path.write_text(json.dumps(config, indent=2), encoding="utf-8")
    temporary_path.replace(CONFIG_PATH)


def local_network_urls(port: int = 5000) -> list[str]:
    preferred_address: str | None = None
    try:
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        probe.connect(("1.1.1.1", 80))
        preferred_address = probe.getsockname()[0]
        probe.close()
    except OSError:
        pass

    addresses: set[str] = set()
    if preferred_address:
        addresses.add(preferred_address)
    else:
        try:
            for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
                addresses.add(info[4][0])
        except socket.gaierror:
            pass

    usable = []
    for address in addresses:
        try:
            parsed = ipaddress.ip_address(address)
        except ValueError:
            continue
        if parsed.is_loopback or parsed.is_link_local or parsed.is_unspecified:
            continue
        usable.append(f"http://{address}:{port}")
    return sorted(usable)


@dataclass
class TrackingSettings:
    lower_h: int = 0
    lower_s: int = 120
    lower_v: int = 70
    upper_h: int = 10
    upper_s: int = 255
    upper_v: int = 255
    min_area: int = 800
    dead_zone_x: int = 55
    dead_zone_y: int = 45
    gain: float = 0.025
    max_step: int = 3

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "TrackingSettings":
        defaults = cls()
        integer_fields = (
            "lower_h",
            "lower_s",
            "lower_v",
            "upper_h",
            "upper_s",
            "upper_v",
            "min_area",
            "dead_zone_x",
            "dead_zone_y",
            "max_step",
        )
        values: dict[str, Any] = {}
        for name in integer_fields:
            values[name] = int(payload.get(name, getattr(defaults, name)))
        values["gain"] = float(payload.get("gain", defaults.gain))

        values["lower_h"] = max(0, min(179, values["lower_h"]))
        values["upper_h"] = max(0, min(179, values["upper_h"]))
        for name in ("lower_s", "lower_v", "upper_s", "upper_v"):
            values[name] = max(0, min(255, values[name]))
        values["min_area"] = max(50, values["min_area"])
        values["dead_zone_x"] = max(5, values["dead_zone_x"])
        values["dead_zone_y"] = max(5, values["dead_zone_y"])
        values["gain"] = max(0.001, min(0.5, values["gain"]))
        values["max_step"] = max(1, min(20, values["max_step"]))
        return cls(**values)


class ServoController:
    def __init__(self) -> None:
        self._serial: serial.Serial | None = None
        self._port: str | None = None
        self._lock = threading.RLock()
        self.last_error: str | None = None
        self.pan_center = 90
        self.tilt_center = 90

    @property
    def connected(self) -> bool:
        return self._serial is not None and self._serial.is_open

    @property
    def port(self) -> str | None:
        return self._port

    def connect(self, port: str) -> None:
        self.disconnect()
        try:
            connection = serial.Serial(port, 115200, timeout=0.2)
            time.sleep(1.8)
            connection.reset_input_buffer()
            with self._lock:
                self._serial = connection
                self._port = port
                self.last_error = None
            try:
                self.refresh_calibration()
            except RuntimeError:
                pass
            self.center()
        except Exception as exc:
            self.last_error = str(exc)
            raise

    def disconnect(self) -> None:
        with self._lock:
            connection = self._serial
            self._serial = None
            self._port = None
        if connection is not None:
            try:
                connection.close()
            except serial.SerialException:
                pass

    def send(self, command: str) -> None:
        with self._lock:
            if not self.connected:
                raise RuntimeError("Arduino bagli degil. Once seri portu secip Baglan tusuna basin.")
            try:
                assert self._serial is not None
                self._serial.write(f"{command}\n".encode("ascii"))
                self._serial.flush()
                self.last_error = None
            except serial.SerialException as exc:
                self.last_error = str(exc)
                raise RuntimeError(f"Arduino iletisim hatasi: {exc}") from exc

    def move(self, pan: int, tilt: int) -> None:
        self.send(f"M,{int(pan)},{int(tilt)}")

    def center(self) -> None:
        self.send("C")

    def laser(self, enabled: bool) -> None:
        self.send(f"L,{1 if enabled else 0}")

    def _request(self, command: str, expected_prefix: str, timeout: float = 1.0) -> str:
        with self._lock:
            if not self.connected:
                raise RuntimeError("Arduino bagli degil. Once seri portu secip Baglan tusuna basin.")
            assert self._serial is not None
            try:
                self._serial.reset_input_buffer()
                self._serial.write(f"{command}\n".encode("ascii"))
                self._serial.flush()
                deadline = time.monotonic() + timeout
                while time.monotonic() < deadline:
                    line = self._serial.readline().decode("ascii", errors="replace").strip()
                    if line.startswith(expected_prefix):
                        self.last_error = None
                        return line
                raise RuntimeError(
                    "Arduino kalibrasyon komutuna cevap vermedi. Guncel servo_controller.ino dosyasini yukleyin."
                )
            except serial.SerialException as exc:
                self.last_error = str(exc)
                raise RuntimeError(f"Arduino iletisim hatasi: {exc}") from exc

    def refresh_calibration(self) -> dict[str, int]:
        response = self._request("Q", "CAL,", timeout=0.8)
        try:
            _, pan, tilt = response.split(",")
            self.pan_center = int(pan)
            self.tilt_center = int(tilt)
        except (ValueError, TypeError) as exc:
            raise RuntimeError(f"Arduino kalibrasyon cevabi gecersiz: {response}") from exc
        return self.calibration()

    def preview_calibration(self, pan: int, tilt: int) -> None:
        self._request(f"P,{pan},{tilt}", "OK,PREVIEW")

    def save_calibration(self, pan: int, tilt: int) -> dict[str, int]:
        self._request(f"K,{pan},{tilt}", "OK,CALIBRATION")
        self.pan_center = pan
        self.tilt_center = tilt
        return self.calibration()

    def calibration(self) -> dict[str, int]:
        return {"pan": self.pan_center, "tilt": self.tilt_center}


class CameraService:
    def __init__(self, servos: ServoController) -> None:
        saved_config = load_saved_config()
        saved_center = saved_config.get("aim_center", {})
        self.servos = servos
        self.settings = TrackingSettings()
        self.mode = "manual"
        self.camera_index: int | None = None
        self.camera_name = "Kamera secilmedi"
        self.last_error: str | None = None
        self.target_found = False
        self.target_area = 0
        self.fps = 0.0
        self.aim_center_x = max(0.0, min(1.0, float(saved_center.get("x", 0.5))))
        self.aim_center_y = max(0.0, min(1.0, float(saved_center.get("y", 0.5))))

        self._capture: cv2.VideoCapture | None = None
        self._frame: np.ndarray | None = None
        self._jpeg: bytes | None = None
        self._lock = threading.RLock()
        self._capture_io_lock = threading.Lock()
        self._running = True
        self._last_command = 0.0
        self._smoothed_target_x: float | None = None
        self._smoothed_target_y: float | None = None
        self._stable_target_frames = 0
        self._thread = threading.Thread(target=self._capture_loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._running = False
        self._thread.join(timeout=2)
        with self._capture_io_lock:
            with self._lock:
                capture = self._capture
                self._capture = None
            if capture is not None:
                capture.release()

    @staticmethod
    def _camera_backends() -> tuple[tuple[int, str], ...]:
        return (
            (cv2.CAP_DSHOW, "DirectShow"),
            (cv2.CAP_MSMF, "Media Foundation"),
            (cv2.CAP_ANY, "Otomatik"),
        )

    def _open_camera(self, index: int) -> tuple[cv2.VideoCapture, str] | None:
        for backend, backend_name in self._camera_backends():
            capture = cv2.VideoCapture(index, backend)
            if not capture.isOpened():
                capture.release()
                continue

            capture.set(cv2.CAP_PROP_FRAME_WIDTH, 640)
            capture.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)
            capture.set(cv2.CAP_PROP_BUFFERSIZE, 1)

            frame = None
            for _ in range(5):
                ok, frame = capture.read()
                if ok and frame is not None:
                    return capture, backend_name
                time.sleep(0.04)
            capture.release()
        return None

    def list_cameras(self, max_index: int = 6) -> list[dict[str, Any]]:
        cameras = []
        current = self.camera_index
        for index in range(max_index):
            if index == current and self._capture is not None:
                cameras.append(
                    {
                        "index": index,
                        "name": self.camera_name,
                        "active": True,
                    }
                )
                continue
            opened = self._open_camera(index)
            if opened is not None:
                capture, backend_name = opened
                capture.release()
                cameras.append(
                    {
                        "index": index,
                        "name": f"Kamera {index} - {backend_name}",
                        "active": False,
                    }
                )
        return cameras

    def select_camera(self, index: int) -> None:
        opened = self._open_camera(index)
        if opened is None:
            raise RuntimeError(
                f"Kamera {index} acilamadi. Kamerayi kullanan baska uygulamalari kapatin."
            )
        candidate, backend_name = opened

        with self._capture_io_lock:
            with self._lock:
                old_capture = self._capture
                self._capture = candidate
                self.camera_index = index
                self.camera_name = f"Kamera {index} - {backend_name}"
                self.last_error = None
            if old_capture is not None:
                old_capture.release()

    def set_mode(self, mode: str) -> None:
        if mode not in {"manual", "autonomous"}:
            raise ValueError("Gecersiz mod.")
        with self._lock:
            self.mode = mode

    def update_settings(self, payload: dict[str, Any]) -> None:
        with self._lock:
            self.settings = TrackingSettings.from_payload(payload)

    def set_aim_center(self, x: float, y: float, persist: bool = True) -> None:
        x = max(0.0, min(1.0, float(x)))
        y = max(0.0, min(1.0, float(y)))
        with self._lock:
            self.aim_center_x = x
            self.aim_center_y = y
        if persist:
            config = load_saved_config()
            config["aim_center"] = {"x": x, "y": y}
            save_config(config)

    def reset_aim_center(self) -> None:
        self.set_aim_center(0.5, 0.5)

    def latest_jpeg(self) -> bytes | None:
        with self._lock:
            return self._jpeg

    def status(self) -> dict[str, Any]:
        with self._lock:
            return {
                "mode": self.mode,
                "camera_index": self.camera_index,
                "camera_name": self.camera_name,
                "camera_error": self.last_error,
                "target_found": self.target_found,
                "target_area": self.target_area,
                "fps": round(self.fps, 1),
                "settings": asdict(self.settings),
                "aim_center": {
                    "x": self.aim_center_x,
                    "y": self.aim_center_y,
                },
            }

    def _capture_loop(self) -> None:
        previous = time.perf_counter()
        while self._running:
            with self._lock:
                capture = self._capture
            if capture is None:
                self._set_placeholder("Web panelinden bir kamera secin")
                time.sleep(0.1)
                continue

            with self._capture_io_lock:
                with self._lock:
                    if capture is not self._capture:
                        continue
                ok, frame = capture.read()
            if not ok or frame is None:
                with self._lock:
                    self.last_error = "Kameradan kare alinamadi."
                time.sleep(0.05)
                continue

            now = time.perf_counter()
            elapsed = now - previous
            previous = now
            if elapsed > 0:
                self.fps = self.fps * 0.85 + (1.0 / elapsed) * 0.15

            processed = self._process_frame(frame)
            ok, encoded = cv2.imencode(".jpg", processed, [cv2.IMWRITE_JPEG_QUALITY, 82])
            if ok:
                with self._lock:
                    self._frame = frame
                    self._jpeg = encoded.tobytes()

    def _set_placeholder(self, message: str) -> None:
        frame = np.zeros((480, 640, 3), dtype=np.uint8)
        cv2.putText(
            frame,
            message,
            (55, 240),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.8,
            (220, 220, 220),
            2,
            cv2.LINE_AA,
        )
        ok, encoded = cv2.imencode(".jpg", frame)
        if ok:
            with self._lock:
                self._jpeg = encoded.tobytes()

    def _process_frame(self, frame: np.ndarray) -> np.ndarray:
        output = frame.copy()
        height, width = output.shape[:2]
        with self._lock:
            settings = self.settings
            mode = self.mode
            center_x = int(round(self.aim_center_x * (width - 1)))
            center_y = int(round(self.aim_center_y * (height - 1)))

        hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)
        lower = np.array([settings.lower_h, settings.lower_s, settings.lower_v])
        upper = np.array([settings.upper_h, settings.upper_s, settings.upper_v])

        if settings.lower_h <= settings.upper_h:
            mask = cv2.inRange(hsv, lower, upper)
        else:
            lower_red = np.array([0, settings.lower_s, settings.lower_v])
            upper_red = np.array([settings.upper_h, settings.upper_s, settings.upper_v])
            lower_wrap = np.array([settings.lower_h, settings.lower_s, settings.lower_v])
            upper_wrap = np.array([179, settings.upper_s, settings.upper_v])
            mask = cv2.bitwise_or(
                cv2.inRange(hsv, lower_red, upper_red),
                cv2.inRange(hsv, lower_wrap, upper_wrap),
            )

        kernel = np.ones((5, 5), np.uint8)
        mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, kernel)
        mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, kernel)
        contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

        target = max(contours, key=cv2.contourArea) if contours else None
        area = int(cv2.contourArea(target)) if target is not None else 0
        found = target is not None and area >= settings.min_area

        if found and target is not None:
            x, y, w, h = cv2.boundingRect(target)
            raw_target_x, raw_target_y = x + w // 2, y + h // 2

            if self._smoothed_target_x is None or self._smoothed_target_y is None:
                self._smoothed_target_x = float(raw_target_x)
                self._smoothed_target_y = float(raw_target_y)
            else:
                alpha = 0.22
                self._smoothed_target_x += alpha * (raw_target_x - self._smoothed_target_x)
                self._smoothed_target_y += alpha * (raw_target_y - self._smoothed_target_y)

            target_x = int(round(self._smoothed_target_x))
            target_y = int(round(self._smoothed_target_y))
            self._stable_target_frames += 1
            cv2.rectangle(output, (x, y), (x + w, y + h), (40, 230, 80), 3)
            cv2.circle(output, (target_x, target_y), 7, (40, 40, 255), -1)
            cv2.line(output, (center_x, center_y), (target_x, target_y), (0, 220, 255), 2)

            error_x = target_x - center_x
            error_y = target_y - center_y
            if mode == "autonomous" and self._stable_target_frames >= 3:
                self._track(error_x, error_y, settings)

            cv2.putText(
                output,
                f"HEDEF alan={area}",
                (x, max(25, y - 10)),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.65,
                (40, 230, 80),
                2,
            )
        else:
            self._smoothed_target_x = None
            self._smoothed_target_y = None
            self._stable_target_frames = 0

        with self._lock:
            self.target_found = found
            self.target_area = area

        label = "OTONOM" if mode == "autonomous" else "MANUEL"
        cv2.putText(output, label, (15, 30), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (255, 255, 255), 2)
        return output

    def _track(self, error_x: int, error_y: int, settings: TrackingSettings) -> None:
        now = time.monotonic()
        if now - self._last_command < 0.16:
            return

        pan = 0
        tilt = 0
        if abs(error_x) > settings.dead_zone_x:
            effective_x = abs(error_x) - settings.dead_zone_x
            pan = (1 if error_x > 0 else -1) * max(1, int(round(effective_x * settings.gain)))
        if abs(error_y) > settings.dead_zone_y:
            effective_y = abs(error_y) - settings.dead_zone_y
            tilt = (1 if error_y > 0 else -1) * max(1, int(round(effective_y * settings.gain)))

        pan = max(-settings.max_step, min(settings.max_step, pan))
        tilt = max(-settings.max_step, min(settings.max_step, tilt))
        if pan == 0 and tilt == 0:
            return
        try:
            self.servos.move(pan, tilt)
        except RuntimeError:
            pass
        self._last_command = now


app = Flask(__name__)
app.config["SEND_FILE_MAX_AGE_DEFAULT"] = 0
servos = ServoController()
camera = CameraService(servos)


def api_error(message: str, status: int = 400):
    return jsonify({"ok": False, "error": message}), status


@app.get("/")
def index():
    return render_template("index.html")


@app.get("/video_feed")
def video_feed():
    def generate():
        while True:
            frame = camera.latest_jpeg()
            if frame is not None:
                yield b"--frame\r\nContent-Type: image/jpeg\r\n\r\n" + frame + b"\r\n"
            time.sleep(0.033)

    return Response(generate(), mimetype="multipart/x-mixed-replace; boundary=frame")


@app.get("/api/status")
def status():
    return jsonify(
        {
            "ok": True,
            **camera.status(),
            "serial_connected": servos.connected,
            "serial_port": servos.port,
            "serial_error": servos.last_error,
            "servo_calibration": servos.calibration(),
            "network_urls": local_network_urls(),
        }
    )


@app.get("/api/cameras")
def cameras():
    return jsonify({"ok": True, "cameras": camera.list_cameras()})


@app.post("/api/camera")
def select_camera():
    try:
        index_value = int(request.get_json(force=True).get("index"))
        camera.select_camera(index_value)
        return jsonify({"ok": True})
    except (TypeError, ValueError, RuntimeError) as exc:
        return api_error(str(exc))


@app.get("/api/serial/ports")
def serial_ports():
    ports = [{"device": port.device, "description": port.description} for port in list_ports.comports()]
    return jsonify({"ok": True, "ports": ports})


@app.post("/api/serial/connect")
def serial_connect():
    port = str(request.get_json(force=True).get("port", "")).strip()
    if not port:
        return api_error("Seri port secilmedi.")
    try:
        servos.connect(port)
        return jsonify({"ok": True})
    except (serial.SerialException, OSError) as exc:
        return api_error(str(exc))


@app.post("/api/serial/disconnect")
def serial_disconnect():
    servos.disconnect()
    return jsonify({"ok": True})


def calibration_values(payload: dict[str, Any]) -> tuple[int, int]:
    try:
        pan = int(payload.get("pan"))
        tilt = int(payload.get("tilt"))
    except (TypeError, ValueError) as exc:
        raise ValueError("Kalibrasyon acilari sayi olmali.") from exc
    if not 10 <= pan <= 170:
        raise ValueError("Pan merkez acisi 10 ile 170 arasinda olmali.")
    if not 20 <= tilt <= 160:
        raise ValueError("Tilt merkez acisi 20 ile 160 arasinda olmali.")
    return pan, tilt


@app.post("/api/calibration/preview")
def calibration_preview():
    try:
        pan, tilt = calibration_values(request.get_json(force=True))
        camera.set_mode("manual")
        servos.preview_calibration(pan, tilt)
        return jsonify({"ok": True})
    except ValueError as exc:
        return api_error(str(exc))
    except RuntimeError as exc:
        return api_error(str(exc), 409)


@app.post("/api/calibration/save")
def calibration_save():
    try:
        pan, tilt = calibration_values(request.get_json(force=True))
        camera.set_mode("manual")
        calibration = servos.save_calibration(pan, tilt)
        return jsonify({"ok": True, "calibration": calibration})
    except ValueError as exc:
        return api_error(str(exc))
    except RuntimeError as exc:
        return api_error(str(exc), 409)


@app.post("/api/mode")
def set_mode():
    try:
        camera.set_mode(str(request.get_json(force=True).get("mode")))
        return jsonify({"ok": True})
    except ValueError as exc:
        return api_error(str(exc))


@app.post("/api/move")
def move():
    payload = request.get_json(force=True)
    try:
        pan = max(-20, min(20, int(payload.get("pan", 0))))
        tilt = max(-20, min(20, int(payload.get("tilt", 0))))
    except (TypeError, ValueError):
        return api_error("Hareket degerleri sayi olmali.")
    try:
        camera.set_mode("manual")
        servos.move(pan, tilt)
        return jsonify({"ok": True})
    except RuntimeError as exc:
        return api_error(str(exc), 409)


@app.post("/api/center")
def center():
    try:
        camera.set_mode("manual")
        servos.center()
        return jsonify({"ok": True})
    except RuntimeError as exc:
        return api_error(str(exc), 409)


@app.post("/api/laser")
def laser():
    enabled = bool(request.get_json(force=True).get("enabled", False))
    try:
        servos.laser(enabled)
        return jsonify({"ok": True})
    except RuntimeError as exc:
        return api_error(str(exc), 409)


@app.post("/api/aim-center")
def aim_center():
    payload = request.get_json(force=True)
    if payload.get("reset"):
        camera.reset_aim_center()
        return jsonify({"ok": True, "aim_center": camera.status()["aim_center"]})

    try:
        x = float(payload.get("x"))
        y = float(payload.get("y"))
    except (TypeError, ValueError):
        return api_error("Nisangah koordinatlari gecersiz.")

    if not 0.0 <= x <= 1.0 or not 0.0 <= y <= 1.0:
        return api_error("Nisangah noktasi goruntu sinirlari icinde olmali.")

    camera.set_aim_center(x, y)
    return jsonify({"ok": True, "aim_center": camera.status()["aim_center"]})


@app.post("/api/tracking")
def tracking():
    try:
        camera.update_settings(request.get_json(force=True))
        return jsonify({"ok": True, "settings": asdict(camera.settings)})
    except (TypeError, ValueError) as exc:
        return api_error(str(exc))


def shutdown() -> None:
    camera.stop()
    servos.disconnect()


atexit.register(shutdown)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, threaded=True, debug=False)
