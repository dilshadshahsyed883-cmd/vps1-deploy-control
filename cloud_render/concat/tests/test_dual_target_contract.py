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

    def test_desktop_and_cuda_gpu_use_same_project_schema(self):
        self.assertEqual(self.lock["upstream_tag"], self.lock["desktop"]["source_tag"])
        self.assertEqual(self.lock["upstream_commit"], self.lock["gpu"]["source_commit"])
        self.assertRegex(self.lock["upstream_commit"], r"^[0-9a-f]{40}$")
        self.assertIn('org.opencontainers.image.concat-upstream-commit="' +
                      self.lock["upstream_commit"] + '"', self.docker)
        self.assertIn('org.opencontainers.image.concat-project-schema="1"', self.docker)
        self.assertIn("gh release download " + self.lock["upstream_tag"], self.windows)

    def test_cuda_only_worker_is_nonroot_and_keeps_auth_delivery(self):
        self.assertIn('ENTRYPOINT ["python3", "-m", "cloud_render.concat.gpu.worker"]', self.docker)
        for item in ("USER 10001:10001", "ENV HOME=/worker",
                     "ENV XDG_CONFIG_HOME=/worker/.config",
                     "ENV XDG_DATA_HOME=/worker/.local/share",
                     "EXPOSE 8080", "NVIDIA_DRIVER_CAPABILITIES=compute,utility,video,graphics"):
            self.assertIn(item, self.docker)
        for item in ("COPY cloud_render/concat/gpu/worker.py",
                     "COPY cloud_render/concat/gpu/resumable.py",
                     "COPY cloud_render/concat/gpu/simple_timeline_v1.py"):
            self.assertIn(item, self.docker)
        # Never accidentally pull the retired Rust/wgpu/Vulkan renderer back.
        for item in ("concat-cli /usr/local/bin/concat-cli",
                     "mesa-vulkan-drivers", "vulkan-tools",
                     "libvulkan1", "scripts/ci/fetch.sh",
                     "cargo build --locked", "COPY cloud_render/concat/gpu/vulkan_probe.py"):
            self.assertNotIn(item, self.docker)
        worker = (CONCAT / "gpu/worker.py").read_text(encoding="utf-8")
        for item in ('"/api/diagnostics/gpu"', "RENDERER_API_TOKEN",
                     '"/health/ready"', "delivery_failed",
                     '"native_vulkan": False', "cuda-stage1"):
            self.assertIn(item, worker)
        self.assertNotIn("def _native_render", worker)
        self.assertNotIn("probe_vulkan", worker)
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
