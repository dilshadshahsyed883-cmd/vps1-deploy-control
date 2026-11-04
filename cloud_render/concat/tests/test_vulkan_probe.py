"""Safe GPU diagnostics: distinguish CUDA visibility from Vulkan renderer."""
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from cloud_render.concat.gpu import vulkan_probe


class GpuProbeTests(unittest.TestCase):
    def test_nvidia_vulkan_and_compute_visible_but_render_unverified(self):
        def fake(command, **kwargs):
            if command[0] == "vulkaninfo":
                return SimpleNamespace(returncode=0, stdout="""GPU0:
    deviceName = NVIDIA GeForce RTX 3060 Ti
    deviceType = PHYSICAL_DEVICE_TYPE_DISCRETE_GPU
    driverName = NVIDIA
GPU1:
    deviceName = llvmpipe (LLVM 20.1.2, 256 bits)
    deviceType = PHYSICAL_DEVICE_TYPE_CPU
    driverName = llvmpipe
""")
            return SimpleNamespace(returncode=0, stdout="NVIDIA GeForce RTX 3060 Ti\n")
        with patch.object(vulkan_probe.subprocess, "run", side_effect=fake):
            response = vulkan_probe.probe()
        self.assertTrue(response["nvidia_vulkan_visible"])
        self.assertTrue(response["nvidia_compute_visible"])
        self.assertTrue(response["software_vulkan_visible"])
        self.assertEqual(response["renderer_hint"], "nvidia_vulkan_detected_render_unverified")
        self.assertNotIn("RENDER_OUTPUT_TOKEN", json.dumps(response))

    def test_software_only_not_mislabeled_nvidia(self):
        def fake(command, **kwargs):
            if command[0] == "vulkaninfo":
                return SimpleNamespace(returncode=0, stdout="""GPU0:
    deviceName = llvmpipe (LLVM 20.1.2, 256 bits)
    deviceType = PHYSICAL_DEVICE_TYPE_CPU
    driverName = llvmpipe
""")
            # CUDA/NVML available does NOT imply NVIDIA Vulkan is available.
            return SimpleNamespace(returncode=0, stdout="NVIDIA GeForce RTX 3060 Ti\n")
        with patch.object(vulkan_probe.subprocess, "run", side_effect=fake):
            response = vulkan_probe.probe()
        self.assertFalse(response["nvidia_vulkan_visible"])
        self.assertTrue(response["nvidia_compute_visible"])
        self.assertEqual(response["renderer_hint"], "software_only_render_unverified")

    def test_no_device_and_missing_tools(self):
        with patch.object(vulkan_probe.subprocess, "run", side_effect=FileNotFoundError):
            response = vulkan_probe.probe()
        self.assertIsNone(response["vulkaninfo_exit_code"])
        self.assertFalse(response["nvidia_vulkan_visible"])
        self.assertEqual(response["renderer_hint"], "no_vulkan_adapter_detected")

    def test_diagnostic_requires_bearer(self):
        import http.client
        import sys
        import threading
        from cloud_render.concat.gpu.worker import Handler, RenderWorker, SaladDualStackHTTPServer
        with tempfile.TemporaryDirectory() as folder:
            token = "s" * 48
            worker = RenderWorker(Path(folder), binary=Path(sys.executable), token=token)
            server = SaladDualStackHTTPServer(("::", 0), Handler)
            server.worker = worker
            t = threading.Thread(target=server.serve_forever, daemon=True)
            t.start()
            try:
                conn = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=10)
                conn.request("GET", "/api/diagnostics/gpu")
                response = conn.getresponse()
                self.assertEqual(response.status, 401)
                response.read()
                conn.close()

                conn = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=10)
                with patch("cloud_render.concat.gpu.worker.probe_vulkan",
                           return_value={"renderer_hint": "software_only_render_unverified"}):
                    conn.request("GET", "/api/diagnostics/gpu",
                                 headers={"Authorization": "Bearer " + token})
                    response = conn.getresponse()
                    self.assertEqual(response.status, 200)
                    self.assertEqual(
                        json.loads(response.read())["renderer_hint"],
                        "software_only_render_unverified"
                    )
                conn.close()
            finally:
                server.shutdown()
                server.server_close()
                t.join(timeout=5)
                worker.pool.shutdown(wait=True)


if __name__ == "__main__":
    unittest.main()
