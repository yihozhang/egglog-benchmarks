"""Protocol and result-boundary checks for loading-excluded pointer analysis."""

import importlib.util
import json
import os
import signal
import sys
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
spec = importlib.util.spec_from_file_location("egglog_interactive", ROOT / "systems/egglog/interactive.py")
interactive = importlib.util.module_from_spec(spec)
spec.loader.exec_module(interactive)
from harness.config import load_suite
from harness.results import summarize


class InteractiveTimingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.program = self.root / "program.egg"
        self.program.write_text("(relation input (i64))\n" + interactive.START + "\n(run 1)\n" + interactive.END + "\n(print-size input)\n")
        self.engine = self.root / "engine.py"

    def run_engine(self, body, timeout=5, protocol="command-done"):
        self.engine.write_text(body)
        if protocol == "legacy-done":
            self.program.write_text(self.program.read_text().replace("(run 1)", interactive.LEGACY_SCHEDULE))
        return interactive.run_interactive(
            [sys.executable, str(self.engine)], self.program, self.root, dict(os.environ), timeout, protocol,
        )

    def test_excludes_setup_and_final_reporting(self):
        rc, report = self.run_engine('''import sys, time
for number, line in enumerate(sys.stdin):
    assert line.startswith('(include "'), line
    time.sleep([0.10, 0.03, 0.10][number])
    if number == 2:
        print("((input 12))")
    print("(done)", flush=True)
''')
        self.assertEqual(rc, 0)
        self.assertEqual(report["timing_scope"], "analysis")
        self.assertEqual(report["wall_s"], report["analysis_wall_s"])
        self.assertGreaterEqual(report["analysis_wall_s"], 0.025)
        self.assertGreaterEqual(report["setup_wall_s"], 0.095)
        self.assertGreater(report["process_wall_s"] - report["wall_s"], 0.19)
        self.assertEqual((self.root / "analysis.egg").read_text(), "(run 1)\n")
        self.assertIn("(print-stats)", (self.root / "finish.egg").read_text())
        self.assertIn("((input 12))", (self.root / "stdout").read_text())

    def test_interactive_error_is_failure_even_when_engine_exits_zero(self):
        rc, report = self.run_engine('''import sys
sys.stdin.readline()
print("(error)", flush=True)
''')
        self.assertNotEqual(rc, 0)
        self.assertIn("(error)", report["protocol_error"])
        self.assertNotIn("analysis_wall_s", report)

    def test_missing_done_is_failure(self):
        rc, report = self.run_engine('''import sys
sys.stdin.readline()
print("output without a completion acknowledgment", flush=True)
''')
        self.assertNotEqual(rc, 0)
        self.assertIn("before the expected (done)", report["protocol_error"])

    def test_missing_done_times_out(self):
        rc, report = self.run_engine('''import sys, time
sys.stdin.readline()
time.sleep(10)
''', timeout=0.15)
        self.assertEqual(rc, 124)
        self.assertEqual(report["status_hint"], "timeout")
        self.assertLess(report["process_wall_s"], 2)

    def test_engine_sigkill_is_reported_as_oom(self):
        rc, report = self.run_engine('''import os, signal, sys
sys.stdin.readline()
os.kill(os.getpid(), signal.SIGKILL)
''')
        self.assertNotEqual(rc, 0)
        self.assertEqual(report["signal"], signal.SIGKILL)
        self.assertEqual(report["status_hint"], "oom")

    def test_completion_must_be_a_whole_line(self):
        rc, report = self.run_engine('''import sys
sys.stdin.readline()
print("some output containing (done)", flush=True)
''')
        self.assertNotEqual(rc, 0)
        self.assertIn("protocol_error", report)

    def test_requires_unique_ordered_nonempty_markers(self):
        for text in ("(run 1)", interactive.END + "\n" + interactive.START,
                     interactive.START + "\n" + interactive.END,
                     interactive.START + "\n" + interactive.START + "\n(run 1)\n" + interactive.END):
            with self.subTest(program=text), self.assertRaises(ValueError):
                interactive.split_program(text)


    def test_legacy_wraps_only_untimed_phases(self):
        rc, report = self.run_engine('''import sys, time
for number, line in enumerate(sys.stdin):
    assert line.startswith('(include "'), line
    time.sleep([0.10, 0.03, 0.10][number])
    print("(done)", flush=True)
''', protocol="legacy-done")
        self.assertEqual(rc, 0)
        self.assertEqual(report["interactive_protocol"], "legacy-done")
        self.assertEqual(report["timing_method"], "interactive-command")
        self.assertGreaterEqual(report["analysis_wall_s"], 0.025)
        self.assertGreaterEqual(report["setup_wall_s"], 0.095)
        self.assertGreater(report["process_wall_s"] - report["analysis_wall_s"], 0.19)
        for name in ("setup.egg", "finish.egg"):
            phase = (self.root / name).read_text()
            self.assertTrue(phase.startswith("(set-option interactive_mode 0)\n"))
            self.assertTrue(phase.endswith("(set-option interactive_mode 1)\n"))
        self.assertEqual((self.root / "analysis.egg").read_text(), interactive.LEGACY_SCHEDULE + "\n")
        transcript = (self.root / "stdin.egg").read_text()
        self.assertEqual(len(transcript.splitlines()), 3)
        self.assertNotIn("extract", transcript)

    def test_legacy_parse_error_is_detected_even_with_inherited_logging_off(self):
        with patch.dict(os.environ, {"RUST_LOG": "off", "RUST_LOG_STYLE": "always"}):
            rc, report = self.run_engine('''import os, sys
assert os.environ["RUST_LOG"] == "error"
assert os.environ["RUST_LOG_STYLE"] == "never"
for number, line in enumerate(sys.stdin):
    if number == 0:
        print("(done)", flush=True)
    elif number == 1:
        print("[ERROR] parse failure", file=sys.stderr, flush=True)
    else:
        print("UNEXPECTED_NEXT_PHASE", flush=True)
''', protocol="legacy-done")
        self.assertEqual(rc, 1)
        self.assertIn("legacy egglog reported an error", report["protocol_error"])
        self.assertNotIn("analysis_wall_s", report)
        self.assertNotIn("status_hint", report)
        self.assertLess(report["process_wall_s"], 2)
        self.assertIn("[ERROR] parse failure", (self.root / "stderr").read_text())
        self.assertNotIn("UNEXPECTED_NEXT_PHASE", (self.root / "stdout").read_text())

    def test_legacy_split_error_output_is_reassembled(self):
        rc, report = self.run_engine('''import sys, time
sys.stdin.readline()
print("[ERR", end="", file=sys.stderr, flush=True)
time.sleep(0.005)
print("OR] type error", file=sys.stderr, flush=True)
for line in sys.stdin:
    pass
''', protocol="legacy-done")
        self.assertEqual(rc, 1)
        self.assertIn("legacy egglog reported an error", report["protocol_error"])
        self.assertIn("[ERROR] type error", (self.root / "stderr").read_text())

    def test_legacy_split_done_output_is_reassembled(self):
        rc, report = self.run_engine('''import sys, time
for line in sys.stdin:
    print("(do", end="", flush=True)
    time.sleep(0.005)
    print("ne)", flush=True)
''', protocol="legacy-done")
        self.assertEqual(rc, 0)
        self.assertGreater(report["analysis_wall_s"], 0)

    def test_legacy_missing_done_is_not_success(self):
        rc, report = self.run_engine('''import sys
sys.stdin.readline()
print("some output containing (done)", flush=True)
''', protocol="legacy-done")
        self.assertNotEqual(rc, 0)
        self.assertIn("expected (done)", report["protocol_error"])
        self.assertNotIn("analysis_wall_s", report)

    def test_legacy_requires_the_single_supported_schedule(self):
        invalid = self.program.read_text().replace("(run 1)", interactive.LEGACY_SCHEDULE + "\n" + interactive.LEGACY_SCHEDULE)
        self.program.write_text(invalid)
        with self.assertRaisesRegex(ValueError, "legacy analysis timing requires exactly"):
            interactive.run_interactive([sys.executable], self.program, self.root, dict(os.environ), 5, "legacy-done")

    def test_legacy_missing_response_times_out(self):
        rc, report = self.run_engine('''import sys, time
sys.stdin.readline()
time.sleep(10)
''', timeout=0.15, protocol="legacy-done")
        self.assertEqual(rc, 124)
        self.assertEqual(report["status_hint"], "timeout")
        self.assertLess(report["process_wall_s"], 2)


class MeasurementScopeTests(unittest.TestCase):
    def test_only_pointer_suite_opts_in(self):
        self.assertEqual({b.egglog_timing for b in load_suite("pointer-analysis")}, {"analysis"})
        self.assertEqual({b.egglog_timing for b in load_suite("math-microbenchmark")}, {"process"})

    @staticmethod
    def row(run, threads, scope=None):
        row = {"run_id": run, "suite": "pointer-analysis", "benchmark": "tiny", "variant": "egglog",
               "threads": threads, "rep": 0, "status": "ok", "wall_s": 1.0,
               "host": {"hostname": "test"}, "harness_commit": "test", "report": {"run_s": 0.5}}
        if scope is not None:
            row["timing_scope"] = scope
        return row

    def test_historical_process_rows_still_work(self):
        result = summarize([self.row("old", 1), self.row("old", 8)])
        self.assertEqual({s["timing_scope"] for s in result.values()}, {"process"})

    def test_rejects_disjoint_runs_that_mix_scopes(self):
        rows = [self.row("001-old", 1), self.row("002-new", 8, "analysis")]
        with self.assertRaisesRegex(SystemExit, "[Mm]ixed.*[Ss]cope|timing.*scope"):
            summarize(rows)

    def test_analysis_rerun_requires_explicit_selection(self):
        old = [self.row("001-old", 1), self.row("001-old", 8)]
        new = [self.row("002-new", 1, "analysis"), self.row("002-new", 8, "analysis")]
        with self.assertRaises(SystemExit):
            summarize(old + new)
        result = summarize(new)
        self.assertEqual({s["timing_scope"] for s in result.values()}, {"analysis"})


if __name__ == "__main__":
    unittest.main()
