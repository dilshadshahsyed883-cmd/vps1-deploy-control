"""Block Concat desktop/GPU source-version drift and unsafe releases."""
import json
import re
import unittest
from pathlib import Path

CONCAT = Path(__file__).resolve().parents[1]
ROOT = CONCAT.parents[1]


class DualTargetContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.lock = json.loads((CONCAT / "source.lock.json").read_text(encoding="utf-8"))
        cls.docker = (CONCAT / "gpu/Dockerfile").read_text(encoding="utf-8")
        cls.windows = (ROOT / ".github/workflows/vps1-cloud-render-concat-package.yml").read_text(encoding="utf-8")
        cls.dual = (ROOT / ".github/workflows/vps1-cloud-render-dual-validation.yml").read_text(encoding="utf-8")
        cls.image = (ROOT / ".github/workflows/vps1-cloud-render-gpu-image.yml").read_text(encoding="utf-8")

    def test_desktop_and_gpu_pinned_to_same_version(self):
        self.assertEqual(self.lock["upstream_tag"], self.lock["desktop"]["source_tag"])
        self.assertEqual(self.lock["upstream_commit"], self.lock["gpu"]["source_commit"])
        self.assertRegex(self.lock["upstream_commit"], r"^[0-9a-f]{40}$")
        self.assertIn("ARG CONCAT_COMMIT=" + self.lock["upstream_commit"], self.docker)
        self.assertIn('test "$(git rev-parse HEAD)" = "$CONCAT_COMMIT"', self.docker)
        self.assertIn("gh release download " + self.lock["upstream_tag"], self.windows)

    def test_gpu_worker_is_nonroot_authenticated_and_candidate_only(self):
        self.assertIn('ENTRYPOINT ["python3", "-m", "cloud_render.concat.gpu.worker"]', self.docker)
        self.assertIn("USER 10001:10001", self.docker)
        self.assertIn("ENV HOME=/worker", self.docker)
        self.assertIn("ENV XDG_CONFIG_HOME=/worker/.config", self.docker)
        self.assertIn("ENV XDG_DATA_HOME=/worker/.local/share", self.docker)
        self.assertIn("EXPOSE 8080", self.docker)
        self.assertIn("scripts/ci/fetch.sh sherpa-onnx-linux-x64-static-lib", self.docker)
        self.assertNotIn("scripts/ci/fetch.sh sherpa-onnx-linux-x64 sherpa.tar.bz2", self.docker)
        self.assertIn("COPY cloud_render/concat/gpu/worker.py", self.docker)
        self.assertIn("COPY cloud_render/concat/gpu/resumable.py /opt/app/cloud_render/concat/gpu/resumable.py", self.docker)
        self.assertIn("COPY cloud_render/concat/gpu/vulkan_probe.py /opt/app/cloud_render/concat/gpu/vulkan_probe.py", self.docker)
        self.assertIn("mesa-vulkan-drivers vulkan-tools", self.docker)
        self.assertIn("NVIDIA_DRIVER_CAPABILITIES=compute,utility,video,graphics", self.docker)
        worker = (CONCAT / "gpu/worker.py").read_text(encoding="utf-8")
        self.assertIn('"/api/diagnostics/gpu"', worker)
        self.assertIn("RENDERER_API_TOKEN", worker)
        self.assertIn('"/health/ready"', worker)
        self.assertIn('"export.run"', worker)
        self.assertIn("delivery_failed", worker)
        self.assertIn("candidate-", self.image)
        self.assertIn("packages: write", self.image)
        self.assertNotIn("kubectl", self.image)
        self.assertNotIn("salad deploy", self.image)
        self.assertIn("github.event_name == 'workflow_dispatch'", self.dual)
        self.assertNotIn("docker push", self.dual)
        self.assertNotIn("kubectl", self.dual)

    def test_project_format_contract(self):
        from cloud_render.concat.project_bundle import PROJECT_NAMES
        self.assertIn(self.lock["project_schema"]["file"], PROJECT_NAMES)
        self.assertEqual(self.lock["project_schema"]["bundle_version"], 1)

    def test_custom_work_requires_both_source_builds(self):
        policy = self.lock["release_rule"].lower()
        self.assertIn("both windows desktop editor and linux gpu worker", policy)
        self.assertIn("same source commit", policy)
        self.assertIn("render parity", policy)


if __name__ == "__main__":
    unittest.main()
