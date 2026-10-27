#!/usr/bin/env python3
"""Diagnostic only: extend the quiet allowance for mttkrp's batch-4 schedule.

This is deliberately separate from the frozen timing matrix. It checks whether
the early stop survives 2,000 additional quiet iterations rather than treating
the small final e-graph as an ordinary time-to-target result.
"""
from pathlib import Path
import hashlib
import json
import os
import resource
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "systems/egglog"))
import interactive

os.chdir(ROOT)
os.sched_setaffinity(0, list(range(8)) + list(range(32, 40)))
resource.setrlimit(resource.RLIMIT_AS, (32 * 1024**3, 32 * 1024**3))
source = ROOT / "runs/20261025T102242418162Z-19596e296768-paper-final/logs/54975abfe1c9bca3f2a1/r0/storel-schedule.egg"
output = ROOT / "logs/paper/storel-stall-diagnostic"
output.mkdir(parents=True, exist_ok=True)
interactive.STALL_STEPS = 2000
command = [str(ROOT / "build/egglog-storel/c1e12aef4907/egglog"),
           "--no-decomp", "-j", "1", "--mode", "interactive"]
(output / "cmd").write_text(" ".join(command) + "\n")
rc, report = interactive.run_node_limit(
    command, source, output, {**os.environ, "RUST_LOG": "error"}, 120,
)
report.update(exit_code=rc, quiet_allowance=2000,
              schedule_sha256=hashlib.sha256(source.read_bytes()).hexdigest(),
              purpose="Diagnostic only; excluded from timing figures")
(output / "report.json").write_text(json.dumps(report, indent=2) + "\n")
print(json.dumps(report, indent=2))
sys.exit(rc)
