import unittest
from unittest.mock import patch

import cv2
import numpy as np

import app


class CameraAppTests(unittest.TestCase):
    def setUp(self):
        self.client = app.app.test_client()

    def test_tracking_settings_clamp_values(self):
        settings = app.TrackingSettings.from_payload(
            {
                "lower_h": -10,
                "upper_h": 999,
                "gain": 10,
                "max_step": 0,
                "min_area": 1,
            }
        )
        self.assertEqual(settings.lower_h, 0)
        self.assertEqual(settings.upper_h, 179)
        self.assertEqual(settings.gain, 0.5)
        self.assertEqual(settings.max_step, 1)
        self.assertEqual(settings.min_area, 50)

    def test_index_and_status_routes(self):
        index_response = self.client.get("/")
        status_response = self.client.get("/api/status")
        self.assertEqual(index_response.status_code, 200)
        self.assertEqual(status_response.status_code, 200)
        self.assertTrue(status_response.get_json()["ok"])

    def test_mode_validation(self):
        response = self.client.post("/api/mode", json={"mode": "invalid"})
        self.assertEqual(response.status_code, 400)

    def test_manual_move_requires_serial_connection(self):
        app.servos.disconnect()
        response = self.client.post("/api/move", json={"pan": 4, "tilt": 0})
        self.assertEqual(response.status_code, 409)
        self.assertFalse(response.get_json()["ok"])

    def test_custom_aim_center(self):
        app.camera.set_aim_center(0.25, 0.75, persist=False)
        center = app.camera.status()["aim_center"]
        self.assertEqual(center, {"x": 0.25, "y": 0.75})
        app.camera.set_aim_center(0.5, 0.5, persist=False)

    def test_aim_center_rejects_out_of_bounds_point(self):
        response = self.client.post("/api/aim-center", json={"x": 1.2, "y": 0.5})
        self.assertEqual(response.status_code, 400)

    def test_calibration_rejects_out_of_range_values(self):
        response = self.client.post(
            "/api/calibration/preview",
            json={"pan": 180, "tilt": 90},
        )
        self.assertEqual(response.status_code, 400)

    def test_calibration_save_returns_saved_values(self):
        with patch.object(
            app.servos,
            "save_calibration",
            return_value={"pan": 88, "tilt": 94},
        ):
            response = self.client.post(
                "/api/calibration/save",
                json={"pan": 88, "tilt": 94},
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.get_json()["calibration"],
            {"pan": 88, "tilt": 94},
        )

    def test_status_contains_network_and_calibration_data(self):
        response = self.client.get("/api/status")
        payload = response.get_json()
        self.assertIn("network_urls", payload)
        self.assertIn("servo_calibration", payload)

    def test_red_target_is_detected(self):
        app.camera.update_settings(
            {
                "lower_h": 170,
                "upper_h": 10,
                "lower_s": 120,
                "upper_s": 255,
                "lower_v": 70,
                "upper_v": 255,
                "min_area": 500,
            }
        )
        frame = np.zeros((480, 640, 3), dtype=np.uint8)
        cv2.rectangle(frame, (240, 160), (400, 320), (0, 0, 255), -1)
        processed = app.camera._process_frame(frame)
        self.assertEqual(processed.shape, frame.shape)
        self.assertTrue(app.camera.target_found)
        self.assertGreater(app.camera.target_area, 500)


if __name__ == "__main__":
    unittest.main()
