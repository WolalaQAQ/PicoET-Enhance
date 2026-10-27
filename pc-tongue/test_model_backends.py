"""Provider routing and startup fallback without requiring a particular GPU."""
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch
import warnings
from packaging.requirements import Requirement

import numpy as np

from model import TongueModel


class FakeOrt:
    ExecutionMode = SimpleNamespace(ORT_SEQUENTIAL="sequential")
    GraphOptimizationLevel = SimpleNamespace(ORT_DISABLE_ALL="disabled")

    def __init__(self, available, failures=None, silent=None):
        self.available = available
        self.failures = failures or {}
        self.silent = silent or set()
        self.calls = []
        self.preloaded = 0
        self.cuda_allocations = 0
        self.OrtValue = SimpleNamespace(ortvalue_from_numpy=self.allocate,
                                       ortvalue_from_shape_and_type=lambda *args: self.allocate(None))

    def allocate(self, *args):
        self.cuda_allocations += 1
        return SimpleNamespace(update_inplace=lambda value: None)

    def get_available_providers(self):
        return self.available

    def preload_dlls(self):
        self.preloaded += 1

    def SessionOptions(self):
        return SimpleNamespace(enable_mem_pattern=True)

    def InferenceSession(self, artifact, options, providers):
        provider, config = providers[0]
        key = "graph" if config.get("enable_cuda_graph") else provider
        row = dict(provider=provider, config=config, options=options, providers=providers, runs=0, disabled=False)
        self.calls.append(row)
        if self.failures.get(key) == "create":
            raise RuntimeError(key+" create failed")

        def run(*args):
            row["runs"] += 1
            if self.failures.get(key) == "run":
                raise RuntimeError(key+" run failed")
            return [-np.ones((1, 1, 56, 56), np.float32)]

        binding = SimpleNamespace(bind_ortvalue_input=lambda *args: None,
                                  bind_ortvalue_output=lambda *args: None,
                                  synchronize_inputs=lambda: None, synchronize_outputs=lambda: None,
                                  copy_outputs_to_cpu=lambda: run())
        active = ["CPUExecutionProvider"] if key in self.silent else [provider, "CPUExecutionProvider"]
        return SimpleNamespace(get_providers=lambda: list(dict.fromkeys(active)), run=run,
                               disable_fallback=lambda: row.update(disabled=True),
                               io_binding=lambda: binding, run_with_iobinding=run)


class BackendTests(unittest.TestCase):
    def setUp(self):
        work = Path(__file__).resolve().parent/'tests-work'
        work.mkdir(exist_ok=True)
        self.tmp = tempfile.TemporaryDirectory(dir=work)
        self.addCleanup(self.tmp.cleanup)
        self.checkpoint = Path(self.tmp.name)/"model.pt"
        self.checkpoint.with_suffix(".onnx").write_bytes(b"fake model for fake session")

    def create(self, ort, device="auto"):
        with patch.dict("sys.modules", {"onnxruntime": ort, "openvino": SimpleNamespace()}), warnings.catch_warnings(record=True):
            warnings.simplefilter("always")
            return TongueModel(str(self.checkpoint), device)

    def test_auto_uses_ort_cuda_without_pytorch_cuda_detection(self):
        ort = FakeOrt(["CPUExecutionProvider", "DmlExecutionProvider", "CUDAExecutionProvider"])
        with patch("model.torch.cuda.is_available", side_effect=AssertionError("must query ORT instead")):
            model = self.create(ort)
        self.assertEqual((model.engine, model.device), ("onnxgraph", "cuda"))
        self.assertEqual(ort.calls[0]["config"]["use_tf32"], "0")
        self.assertEqual(ort.calls[0]["runs"], 3)
        self.assertTrue(ort.calls[0]["disabled"])
        self.assertEqual(model.fallbacks, [])

    def test_graph_failure_tries_ordinary_cuda_before_other_gpus(self):
        ort = FakeOrt(["CUDAExecutionProvider", "DmlExecutionProvider", "CPUExecutionProvider"], {"graph": "run"})
        model = self.create(ort)
        self.assertEqual((model.engine, model.device), ("onnx", "cuda"))
        self.assertEqual(len(model.fallbacks), 1)
        self.assertEqual(model.fallbacks[0]["engine"], "onnxgraph")
        self.assertEqual(len(ort.calls), 2)

    def test_no_cuda_uses_directml_with_required_session_options(self):
        ort = FakeOrt(["DmlExecutionProvider", "CPUExecutionProvider"])
        model = self.create(ort)
        self.assertEqual(model.device, "dml")
        self.assertFalse(ort.calls[0]["options"].enable_mem_pattern)
        self.assertEqual(ort.calls[0]["options"].execution_mode, "sequential")
        self.assertEqual(ort.calls[0]["config"], {"device_id": "0"})
        self.assertEqual(ort.calls[0]["providers"][-1], "CPUExecutionProvider")
        self.assertEqual(ort.cuda_allocations, 0)
        self.assertEqual(ort.preloaded, 0)

    def test_openvino_targets_intel_gpu_and_keeps_fp32(self):
        ort = FakeOrt(["OpenVINOExecutionProvider", "DmlExecutionProvider", "CPUExecutionProvider"])
        model = self.create(ort)
        self.assertEqual(model.device, "openvino")
        self.assertEqual(ort.calls[0]["config"]["device_type"], "GPU")
        config = json.loads(ort.calls[0]["config"]["load_config"])
        self.assertEqual(config["GPU"]["INFERENCE_PRECISION_HINT"], "f32")
        self.assertEqual(ort.calls[0]["options"].graph_optimization_level, "disabled")

    def test_unusable_intel_gpu_continues_to_directml(self):
        ort = FakeOrt(["OpenVINOExecutionProvider", "DmlExecutionProvider", "CPUExecutionProvider"],
                      {"OpenVINOExecutionProvider": "create"})
        model = self.create(ort)
        self.assertEqual(model.device, "dml")
        self.assertEqual(model.fallbacks[0]["device"], "openvino")

    def test_failed_cuda_continues_to_cross_vendor_gpu(self):
        ort = FakeOrt(["CUDAExecutionProvider", "DmlExecutionProvider", "CPUExecutionProvider"],
                      {"graph": "create", "CUDAExecutionProvider": "create"})
        model = self.create(ort)
        self.assertEqual(model.provider, "DmlExecutionProvider")
        self.assertEqual(len(model.fallbacks), 2)

    def test_first_directml_execution_failure_falls_back_to_cpu(self):
        ort = FakeOrt(["DmlExecutionProvider", "CPUExecutionProvider"], {"DmlExecutionProvider": "run"})
        model = self.create(ort)
        self.assertEqual(model.device, "cpu")
        self.assertEqual(model.fallbacks[0]["error"], "DmlExecutionProvider run failed")
        self.assertEqual(model.providers, ["CPUExecutionProvider"])

    def test_cpu_only_install_runs_without_gpu_operations(self):
        ort = FakeOrt(["CPUExecutionProvider"])
        model = self.create(ort)
        self.assertEqual(model.device, "cpu")
        self.assertEqual(ort.cuda_allocations, 0)
        self.assertEqual(ort.preloaded, 0)
        self.assertEqual(model.fallbacks, [])

    def test_silent_ort_constructor_fallback_is_not_misreported_as_gpu(self):
        ort = FakeOrt(["DmlExecutionProvider", "CPUExecutionProvider"], silent={"DmlExecutionProvider"})
        model = self.create(ort)
        self.assertEqual(model.device, "cpu")
        self.assertIn("did not initialize", model.fallbacks[0]["error"])

    def test_forced_backends_never_switch_to_cpu(self):
        for target, provider in [("cuda", "CUDAExecutionProvider"), ("dml", "DmlExecutionProvider"),
                                 ("openvino", "OpenVINOExecutionProvider")]:
            with self.subTest(target=target):
                ort = FakeOrt([provider, "CPUExecutionProvider"], {"graph": "create", provider: "create"})
                with self.assertRaisesRegex(RuntimeError, "create failed"):
                    self.create(ort, target)
                self.assertEqual(len(ort.calls), 1)

    def test_forced_uninstalled_backend_reports_missing_package(self):
        with self.assertRaisesRegex(RuntimeError, "DmlExecutionProvider is not installed"):
            self.create(FakeOrt(["CPUExecutionProvider"]), "dml")

    def test_explicit_cpu_does_not_probe_gpus(self):
        ort = FakeOrt(["CUDAExecutionProvider", "DmlExecutionProvider", "CPUExecutionProvider"])
        self.assertEqual(self.create(ort, "cpu").device, "cpu")
        self.assertEqual([call["provider"] for call in ort.calls], ["CPUExecutionProvider"])

    def test_all_failed_backends_report_the_attempt_chain(self):
        ort = FakeOrt(["DmlExecutionProvider", "CPUExecutionProvider"],
                      {"DmlExecutionProvider": "create", "CPUExecutionProvider": "run"})
        with self.assertRaisesRegex(RuntimeError, "All ONNX Runtime backends failed:.*dml.*cpu"):
            self.create(ort)

    def test_missing_model_is_not_hidden_by_provider_fallback(self):
        self.checkpoint.with_suffix(".onnx").unlink()
        ort = FakeOrt(["CPUExecutionProvider"])
        with self.assertRaisesRegex(FileNotFoundError, "ONNX model does not exist"):
            self.create(ort)
        self.assertEqual(ort.calls, [])

    def test_invalid_target_and_invalid_graph_combination(self):
        for target in ["unknown", "onnxgraph:cpu", "torch:dml"]:
            with self.subTest(target=target), self.assertRaises(ValueError):
                self.create(FakeOrt(["CPUExecutionProvider"]), target)

    def test_non_cuda_provider_uses_same_prediction_and_reports_selection(self):
        for target, provider in [("dml", "DmlExecutionProvider"), ("openvino", "OpenVINOExecutionProvider")]:
            with self.subTest(target=target):
                model = self.create(FakeOrt([provider, "CPUExecutionProvider"]))
                result = model.predict(np.zeros((400, 400), np.uint8))
                self.assertEqual(result["mask"].shape, (56, 56))
                self.assertFalse(result["present"])
                self.assertEqual(result["device"], target)
                description = model.describe()
                self.assertEqual(description["provider"], provider)
                self.assertEqual(description["requested_device"], "auto")
                json.dumps(description, allow_nan=False)

    def test_install_profiles_do_not_mix_conflicting_ort_wheels(self):
        work = Path(__file__).resolve().parent
        for name, expected in [('requirements.txt', 'onnxruntime-directml'),
                               ('requirements-cuda.txt', 'onnxruntime-gpu'),
                               ('requirements-openvino.txt', 'onnxruntime-openvino'),
                               ('requirements-cpu.txt', 'onnxruntime')]:
            with self.subTest(profile=name):
                lines = (work/name).read_text().splitlines()+(work/'requirements-base.txt').read_text().splitlines()
                requirements = [Requirement(line) for line in lines if line and not line.startswith('-r')]
                packages = [row.name for row in requirements
                            if row.name.startswith('onnxruntime') and
                            (row.marker is None or row.marker.evaluate({'sys_platform': 'win32'}))]
                self.assertEqual(packages, [expected])


if __name__ == "__main__":
    unittest.main()
