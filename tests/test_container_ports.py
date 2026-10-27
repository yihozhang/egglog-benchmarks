"""Exercise benchmark names against the container ports' argument contracts."""

import importlib.util
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("container_run", ROOT / "systems/_container/run.py")
adapter = importlib.util.module_from_spec(spec)
spec.loader.exec_module(adapter)


class ContainerPortTests(unittest.TestCase):
    def test_math_12_uses_iteration_argument(self):
        for system in ("hegg", "metatheory", "ego", "foresight"):
            with self.subTest(system=system):
                self.assertEqual(
                    adapter.resolve_port(ROOT / "benchmarks/math-microbenchmark" / system,
                                         "math-microbenchmark-12"),
                    ("math-microbenchmark", ["--iters", "12"]),
                )

    def test_default_math_uses_port_default(self):
        for system in ("hegg", "metatheory", "ego", "foresight", "ggs"):
            with self.subTest(system=system):
                self.assertEqual(
                    adapter.resolve_port(ROOT / "benchmarks/math-microbenchmark" / system,
                                         "math-microbenchmark"),
                    ("math-microbenchmark", []),
                )

    def test_ggs_exact_math_wrapper_takes_precedence(self):
        self.assertEqual(
            adapter.resolve_port(ROOT / "benchmarks/math-microbenchmark/ggs",
                                 "math-microbenchmark-12"),
            ("math-microbenchmark-12", []),
        )

    def test_instance_sizes_keep_instance_argument(self):
        with tempfile.TemporaryDirectory() as directory:
            ports = Path(directory)
            (ports / "horner.scala").touch()
            (ports / "matmul-assoc.scala").touch()
            for name, expected in (("horner-10", ("horner", ["--instance", "10"])),
                                   *((f"matmul-assoc-{size}",
                                      ("matmul-assoc", ["--instance", str(size)]))
                                     for size in (320, 480, 640))):
                with self.subTest(name=name):
                    self.assertEqual(adapter.resolve_port(ports, name), expected)

    def test_missing_port_is_unsupported(self):
        with tempfile.TemporaryDirectory() as directory:
            self.assertEqual(adapter.resolve_port(Path(directory), "math-microbenchmark-12"),
                             (None, []))


if __name__ == "__main__":
    unittest.main()
