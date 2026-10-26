"""Regression: production SML must provision face detection, not just CI.

Part01 requirements already includes OpenCV, but production.runtime uses its
own package catalog. Missing OpenCV makes the person-save control inert.
"""
import unittest
from pathlib import Path

from production.runtime import CATALOG

ROOT = Path(__file__).resolve().parents[1]


class SMLFaceRuntimeContractTests(unittest.TestCase):
    def test_production_packages_include_all_face_engine_dependencies(self):
        requirements = (ROOT / "apps/self-media-library/part01/requirements.txt").read_text().splitlines()
        production = CATALOG["self-media-library"]["packages"]
        expected = {
            "opencv-python-headless==4.11.0.86",
            "numpy==2.2.6",
            "pillow==11.1.0",
        }
        self.assertTrue(expected.issubset(set(requirements)))
        self.assertTrue(expected.issubset(set(production)))

    def test_pre_activation_checks_cv2_face_recognizer_in_target_venv(self):
        code = (ROOT / "production/runtime.py").read_text()
        activation = code.index("atomic_link(current, release)", code.index("def install_app("))
        face_check = code.index("cv2.FaceDetectorYN.create", code.index("def install_app("))
        self.assertLess(face_check, activation)
        self.assertIn("cv2.FaceRecognizerSF.create", code)
        self.assertIn("dependency='opencv-face-engine'", code)


if __name__ == "__main__":
    unittest.main()
