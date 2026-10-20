"""Result selection must preserve evidence and refuse incompatible comparisons."""
import contextlib
import io
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from harness import artifacts, cli, config, results, runner


class ResultTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.addCleanup(patch.stopall)
        patch.object(config, "RUNS", self.root / "runs").start()
        patch.object(config, "RESULTS", self.root / "results").start()
        patch.object(config, "EXPERIMENTS", self.root / "experiments").start()
        patch.object(artifacts, "_provenance", return_value={"harness_commit": "test"}).start()
        self.cfg = {"description": "test", "benchmarks": ["suite/a"], "variants": [
            {"label": "base", "system": "egglog", "ref": "base"},
            {"label": "new", "system": "egglog", "ref": "new"}],
            "threads": [1], "reps": 2, "baseline": "base", "speedup_labels": {"new": "+ optimization"}}
        self.exp = config.experiment_from_dict("trial", self.cfg)

    def bundle(self, *, bench="a", environment="env", labels=("base", "new"), state="complete", timing=1.0, tag=None):
        cfg = {**self.cfg, "benchmarks": [f"suite/{bench}"],
               "variants": [v for v in self.cfg["variants"] if v["label"] in labels]}
        if cfg["baseline"] not in labels:
            cfg["baseline"] = None
        rd, m = artifacts.create_run("trial", "matrix", cfg, {"hostname": "server"}, tag=tag)
        m["environment_id"] = environment
        m["cases"] = [{"suite": "suite", "benchmark": bench, "variant": v, "threads": 1, "reps": 2} for v in labels]
        rows = [{"experiment": "trial", "run_id": m["run_id"], "suite": "suite", "benchmark": bench,
                 "variant": label, "threads": 1, "rep": rep, "status": "ok", "wall_s": timing + rep,
                 "environment_id": environment, "machine_id": "server", "build_id": label,
                 "input_id": bench, "system": "egglog", "commit": label, "flags": [], "patches": []}
                for label in labels for rep in range(2)]
        (rd / "measurements.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
        (rd / "logs").mkdir()
        (rd / "logs" / "stdout").write_text("temporary raw output")
        if state == "running":
            artifacts.write_manifest(rd, m)
        else:
            artifacts.finish_run(rd, m, state)
        return rd, m, rows

    def test_default_is_one_latest_complete_run(self):
        first, _, _ = self.bundle()
        newer, _, _ = self.bundle(timing=5)
        self.bundle(state="interrupted")
        self.bundle(state="running")
        rows = results.load_rows(self.exp)
        self.assertEqual({r["run_id"] for r in rows}, {newer.name})
        with self.assertRaisesRegex(SystemExit, "overlapping"):
            results.load_rows(self.exp, "all")
        self.assertEqual({r["run_id"] for r in results.load_rows(self.exp, first.name)}, {first.name})

    def test_frozen_selection_survives_source_deletion_and_later_runs(self):
        source, m, original = self.bundle()
        # There is deliberately no recipe on this machine: imported bundles are self-contained.
        directory = results.select_runs("trial", [source.name], "paper-v1")
        self.assertFalse((directory / "runs" / source.name / "logs").exists())
        shutil.rmtree(source)
        self.bundle(timing=100)
        selected = results.load_rows(self.exp, selection="paper-v1")
        self.assertEqual(selected, original)
        manifest, _ = results.read_selection("trial", "paper-v1")
        self.assertEqual(manifest["config"]["speedup_labels"], {"new": "+ optimization"})
        with contextlib.redirect_stdout(io.StringIO()) as output, self.assertRaises(SystemExit) as exit_:
            cli.main(["summary", "trial", "--selection", "paper-v1"])
        self.assertEqual(exit_.exception.code, 0)
        self.assertIn("1.500", output.getvalue())
        with self.assertRaisesRegex(SystemExit, "already exists"):
            results.select_runs("trial", [source.name], "paper-v1")

    def test_selection_tampering_is_detected(self):
        rd, _, _ = self.bundle()
        directory = results.select_runs("trial", [rd.name], "paper")
        with (directory / "measurements.jsonl").open("a") as out:
            out.write("{}\n")
        with self.assertRaisesRegex(SystemExit, "modified content"):
            results.load_rows(self.exp, selection="paper")

    def test_disjoint_runs_merge_but_environment_and_code_must_match(self):
        first, _, _ = self.bundle()
        second, m, rows = self.bundle(bench="b")
        directory = results.select_runs("trial", [first.name, second.name], "full")
        self.assertEqual(len(results.load_rows(self.exp, selection="full")), 8)
        self.assertTrue(directory.is_dir())
        other, _, _ = self.bundle(bench="c", environment="other-machine")
        with self.assertRaisesRegex(SystemExit, "incompatible machines"):
            results.select_runs("trial", [first.name, other.name], "bad-host")
        for row in rows:
            row["build_id"] = "different-code"
        (second / "measurements.jsonl").write_text("".join(json.dumps(r)+"\n" for r in rows))
        artifacts.seal_run(second, m)
        with self.assertRaisesRegex(SystemExit, "different code"):
            results.select_runs("trial", [first.name, second.name], "bad-code")

    def test_overlap_duplicate_and_changed_inputs_are_rejected(self):
        first, _, rows = self.bundle()
        second, _, _ = self.bundle()
        with self.assertRaisesRegex(SystemExit, "overlapping"):
            results.select_runs("trial", [first.name, second.name], "overlap")
        with self.assertRaisesRegex(SystemExit, "duplicate measurement"):
            results.summarize(rows + [rows[0]])
        rows[2]["input_id"] = "changed-input"
        with self.assertRaisesRegex(SystemExit, "different benchmark inputs"):
            results.summarize(rows)

    def test_complete_flag_does_not_hide_missing_repetitions(self):
        rd, m, rows = self.bundle()
        rows.pop()
        (rd / "measurements.jsonl").write_text("".join(json.dumps(r)+"\n" for r in rows))
        artifacts.seal_run(rd, m)
        with self.assertRaisesRegex(SystemExit, "incomplete repetitions"):
            results.select_runs("trial", [rd.name], "incomplete")
        rows[-1]["status"] = "timeout"
        (rd / "measurements.jsonl").write_text("".join(json.dumps(r)+"\n" for r in rows))
        artifacts.seal_run(rd, m)
        results.select_runs("trial", [rd.name], "with-timeout")
        self.assertIn("timeout", [r["status"] for r in results.load_rows(self.exp, selection="with-timeout")])

    def test_legacy_requires_explicit_provenance_acknowledgment(self):
        config.EXPERIMENTS.mkdir()
        (config.EXPERIMENTS / "trial.toml").write_text('benchmarks=["suite/a"]\n[[variants]]\nlabel="base"\nsystem="egglog"\nref="old"\n')
        self.exp.results_dir.mkdir(parents=True)
        row = {"experiment": "trial", "run_id": "legacy", "suite": "suite", "benchmark": "a",
               "variant": "base", "threads": 1, "rep": 0, "status": "ok", "wall_s": 1,
               "host": {"hostname": "old-server"}, "commit": "old"}
        (self.exp.results_dir / "legacy.jsonl").write_text(json.dumps(row)+"\n")
        self.assertEqual(results.load_rows(self.exp, "legacy"), [row])
        with self.assertRaisesRegex(SystemExit, "legacy completion is unknown"):
            results.load_rows(self.exp)
        with self.assertRaisesRegex(SystemExit, "allow-legacy"):
            results.select_runs("trial", ["legacy"], "legacy-paper")
        results.select_runs("trial", ["legacy"], "legacy-paper", allow_legacy=True)
        selection, _ = results.read_selection("trial", "legacy-paper")
        self.assertEqual(selection["provenance"], "legacy-incomplete")

    def test_scope_metadata_defaults_aliases_and_conflicts(self):
        self.assertEqual(results.timing_scope({}), "process")
        self.assertEqual(results.timing_scope({"report": {"timing_scope": "analysis"}}), "analysis")
        self.assertEqual(results.timing_scope({"timing_scope": "egglog-analysis",
                                             "report": {"timing_scope": "analysis"}}), "analysis")
        with self.assertRaisesRegex(SystemExit, "conflicting timing_scope"):
            results.timing_scope({"timing_scope": "process", "report": {"timing_scope": "analysis"}})
        with self.assertRaisesRegex(SystemExit, "unsupported timing_scope"):
            results.timing_scope({"timing_scope": "unknown"})

    def test_wall_time_scope_cannot_change_between_variants_or_repetitions(self):
        _, _, rows = self.bundle()
        for row in rows:
            row["report"] = {"run_s": 0.5}
            if row["variant"] == "new":
                row["timing_scope"] = "analysis"
        with self.assertRaisesRegex(SystemExit, "mixed wall-time measurement scopes"):
            results.summarize(rows)
        self.assertEqual(len(results.summarize(rows, "report.run_s")), 2)
        rows = rows[:2]
        rows[1]["timing_scope"] = "analysis"
        with self.assertRaisesRegex(SystemExit, "mixed wall-time measurement scopes"):
            results.summarize(rows)

    def test_failed_attempt_scope_does_not_relabel_success_or_hide_failure(self):
        _, _, rows = self.bundle()
        for row in rows:
            row["timing_scope"] = "analysis"
        rows[1].update(status="timeout", timing_scope="process", wall_s=600)
        summary = results.summarize(rows)[("suite", "a", "base", 1)]
        self.assertEqual(summary["status"], "timeout")
        self.assertEqual(summary["n"], 1)
        self.assertEqual(summary["median"], rows[0]["wall_s"])
        self.assertEqual(summary["timing_scope"], "analysis")

    def test_changed_variant_env_is_rejected_even_with_same_build_id(self):
        _, _, rows = self.bundle(labels=("base",))
        rows[1]["env"] = {"EGGLOG_PARALLEL_FREE_JOIN_FORK_DEPTH": "0"}
        for modern in (True, False):
            with self.subTest(modern=modern):
                selected = [dict(row) for row in rows]
                if not modern:
                    for row in selected:
                        for field in ("environment_id", "machine_id", "build_id", "input_id"):
                            row.pop(field, None)
                with self.assertRaisesRegex(SystemExit, "runtime environment overrides"):
                    results.summarize(selected)
        rows[1]["env"] = {}
        self.assertEqual(len(results.summarize(rows)), 1)

    @staticmethod
    def group(label="base", bench="a"):
        return {"suite": "suite", "benchmark": bench, "variant": label, "threads": 1}

    @staticmethod
    def replace_rows(rd, manifest, rows):
        (rd / "measurements.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
        artifacts.seal_run(rd, manifest)

    def test_explicit_groups_preserve_all_repetitions_and_entire_sources(self):
        first, _, first_rows = self.bundle()
        second, _, second_rows = self.bundle(timing=8)
        groups = {first.name: [self.group("base")], second.name: [self.group("new")]}
        directory = results.select_runs("trial", [first.name, second.name], "chosen", groups=groups)
        rows = results.load_rows(self.exp, selection="chosen")
        self.assertEqual(rows, first_rows[:2] + second_rows[2:])
        manifest, _ = results.read_selection("trial", "chosen")
        self.assertEqual(manifest["source_groups"], groups)
        self.assertEqual(manifest["provenance"], "captured")
        for source in (first, second):
            archived = directory / "runs" / source.name / "measurements.jsonl"
            self.assertEqual(archived.read_bytes(), (source / "measurements.jsonl").read_bytes())
            self.assertIn(archived.relative_to(directory).as_posix(), manifest["checksums"])
        self.assertEqual(len(manifest["coverage"]), 2)

    def test_explicit_groups_reject_incomplete_unknown_duplicate_and_rep_filters(self):
        rd, _, _ = self.bundle()
        valid = self.group()
        invalid = [
            ({}, "every exact"),
            ({rd.name: [valid], "unknown": [valid]}, "every exact"),
            ({rd.name: []}, "nonempty"),
            ({rd.name: [self.group(bench="missing")]}, "unknown selected groups"),
            ({rd.name: [valid, valid]}, "duplicate selected group"),
            ({rd.name: [{**valid, "rep": 0}]}, "exactly suite"),
            ({rd.name: [{**valid, "threads": True}]}, "invalid group"),
        ]
        for groups, message in invalid:
            with self.subTest(groups=groups), self.assertRaisesRegex(SystemExit, message):
                results.select_runs("trial", [rd.name], "bad-groups", groups=groups)
        self.assertFalse((config.RESULTS / "trial" / "bad-groups").exists())

    def test_group_filter_cannot_hide_incomplete_source_or_remaining_overlap(self):
        first, manifest, rows = self.bundle()
        second, _, _ = self.bundle()
        with self.assertRaisesRegex(SystemExit, "overlapping"):
            results.select_runs("trial", [first.name, second.name], "overlap-groups",
                                groups={first.name: [self.group()], second.name: [self.group()]})
        # Drop a repetition from an excluded group: the whole source must still validate.
        self.replace_rows(first, manifest, rows[:-1])
        with self.assertRaisesRegex(SystemExit, "incomplete repetitions"):
            results.select_runs("trial", [first.name], "incomplete-source",
                                groups={first.name: [self.group("base")]})

    def test_group_selection_keeps_terminal_failure(self):
        rd, manifest, rows = self.bundle()
        rows[1]["status"] = "timeout"
        self.replace_rows(rd, manifest, rows)
        results.select_runs("trial", [rd.name], "failed-group", groups={rd.name: [self.group()]})
        selected = results.load_rows(self.exp, selection="failed-group")
        self.assertEqual([r["status"] for r in selected], ["ok", "timeout"])
        self.assertEqual(results.summarize(selected)[("suite", "a", "base", 1)]["status"], "timeout")

    def test_legacy_group_selection_preserves_raw_sources_and_provenance(self):
        config.EXPERIMENTS.mkdir()
        (config.EXPERIMENTS / "trial.toml").write_text(
            'benchmarks=["suite/a"]\n[[variants]]\nlabel="base"\nsystem="egglog"\nref="old"\n'
            '[[variants]]\nlabel="new"\nsystem="egglog"\nref="old"\n')
        self.exp.results_dir.mkdir(parents=True)
        sources = {}
        for run in ("older", "newer"):
            rows = [{"experiment": "trial", "run_id": run, "suite": "suite", "benchmark": "a",
                     "variant": label, "threads": 1, "rep": rep, "status": "ok", "wall_s": 1 + rep,
                     "host": {"hostname": "old-server"}, "commit": "old"}
                    for label in ("base", "new") for rep in range(2)]
            path = self.exp.results_dir / f"{run}.jsonl"
            path.write_text("".join(json.dumps(r) + "\n" for r in rows))
            sources[run] = path
        groups = {"older": [self.group("base")], "newer": [self.group("new")]}
        with self.assertRaisesRegex(SystemExit, "allow-legacy"):
            results.select_runs("trial", list(sources), "legacy-groups", groups=groups)
        directory = results.select_runs("trial", list(sources), "legacy-groups", allow_legacy=True, groups=groups)
        manifest, _ = results.read_selection("trial", "legacy-groups")
        self.assertEqual(manifest["provenance"], "legacy-incomplete")
        self.assertEqual(manifest["source_groups"], groups)
        for run, path in sources.items():
            self.assertEqual((directory / "runs" / run / "measurements.jsonl").read_bytes(), path.read_bytes())
            self.assertFalse((directory / "runs" / run / "run.json").exists())
        self.assertEqual(len(results.load_rows(self.exp, selection="legacy-groups")), 4)

    def test_legacy_selection_freezes_recorded_env_not_current_recipe(self):
        config.EXPERIMENTS.mkdir()
        (config.EXPERIMENTS / "trial.toml").write_text(
            'benchmarks=["suite/a"]\n[[variants]]\nlabel="base"\nsystem="egglog"\nref="old"\n'
            'env={MODE="today"}\n')
        self.exp.results_dir.mkdir(parents=True)
        row = {"experiment": "trial", "run_id": "legacy", "suite": "suite", "benchmark": "a",
               "variant": "base", "threads": 1, "rep": 0, "status": "ok", "wall_s": 1,
               "host": {"hostname": "old-server"}, "commit": "old"}
        path = self.exp.results_dir / "legacy.jsonl"
        for suffix, recorded_env in (("missing", None), ("recorded", {"MODE": "then"})):
            sample = dict(row)
            if recorded_env is not None:
                sample["env"] = recorded_env
            path.write_text(json.dumps(sample) + "\n")
            name = f"env-{suffix}"
            results.select_runs("trial", ["legacy"], name, allow_legacy=True)
            manifest, _ = results.read_selection("trial", name)
            self.assertEqual(manifest["config"]["variants"][0]["env"], recorded_env or {})

    def test_selection_freezes_each_retained_benchmark_scope(self):
        first, _, _ = self.bundle(bench="a")
        second, manifest, rows = self.bundle(bench="b")
        for row in rows:
            row["timing_scope"] = "analysis"
        manifest["config"]["benchmark_timing"] = {"suite/b": "analysis"}
        self.replace_rows(second, manifest, rows)
        results.select_runs("trial", [first.name, second.name], "timed")
        selected, _ = results.read_selection("trial", "timed")
        self.assertEqual(selected["config"]["benchmark_timing"], {"suite/a": "process", "suite/b": "analysis"})

    def test_mixed_scope_wall_time_selection_is_rejected_before_writing(self):
        rd, manifest, rows = self.bundle()
        for row in rows:
            if row["variant"] == "new":
                row["timing_scope"] = "analysis"
        self.replace_rows(rd, manifest, rows)
        with self.assertRaisesRegex(SystemExit, "mixed wall-time measurement scopes"):
            results.select_runs("trial", [rd.name], "bad-scopes")
        self.assertFalse((config.RESULTS / "trial" / "bad-scopes").exists())
        results.select_runs("trial", [rd.name], "diagnostic-scopes", metric="report.run_s")

    def test_raw_reports_use_captured_config_without_a_recipe(self):
        rd, _, original = self.bundle()
        exp, rows = results.report_data("trial", rd.name)
        self.assertEqual(rows, original)
        self.assertEqual(exp.baseline, "base")
        self.assertEqual(exp.speedup_labels, self.cfg["speedup_labels"])
        with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(SystemExit) as exited:
            cli.main(["summary", "trial", "--run", rd.name])
        self.assertEqual(exited.exception.code, 0)

    def test_terminal_failure_is_not_reported_as_a_successful_median(self):
        _, _, rows = self.bundle()
        rows[1]["status"] = "timeout"
        summary = results.summarize(rows)
        self.assertEqual(summary[("suite", "a", "base", 1)]["status"], "timeout")
        self.assertEqual(summary[("suite", "a", "base", 1)]["n"], 1)

    def test_port_changes_across_thread_counts_are_incompatible(self):
        _, _, rows = self.bundle()
        rows = [dict(rows[0], rep=0, threads=1, port_id="old"),
                dict(rows[0], rep=0, threads=8, port_id="new")]
        with self.assertRaisesRegex(SystemExit, "different port"):
            results.summarize(rows)

    def test_tag_ambiguity_and_cli_source_exclusivity(self):
        self.bundle(tag="test")
        self.bundle(tag="test")
        with self.assertRaisesRegex(SystemExit, "ambiguous"):
            results.load_rows(self.exp, "test")
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as exit_:
            cli.main(["summary", "trial", "--run", "latest", "--selection", "paper"])
        self.assertEqual(exit_.exception.code, 2)


class WorkflowIntegrationTests(unittest.TestCase):
    def test_run_transfer_select_and_plot_with_real_adapter_processes(self):
        with tempfile.TemporaryDirectory() as temporary, contextlib.ExitStack() as stack:
            root = Path(temporary)
            for name in ("ROOT", "RUNS", "RESULTS", "EXPERIMENTS", "BENCHMARKS", "SYSTEMS"):
                destination = root if name == "ROOT" else root / name.lower()
                stack.enter_context(patch.object(config, name, destination))
            stack.enter_context(patch.object(runner, "ROOT", root))
            stack.enter_context(patch.object(artifacts, "_provenance", return_value={"harness_commit": "fixture"}))
            (root / "benchmarks" / "suite").mkdir(parents=True)
            (root / "benchmarks" / "suite" / "a.egg").write_text("(relation R (i64))\n")
            adapter = root / "systems" / "fixture"
            adapter.mkdir(parents=True)
            (adapter / "build").write_text("""#!/usr/bin/env python3
import json, pathlib, sys
out = pathlib.Path(__file__).resolve().parents[2] / 'build' / sys.argv[1]
out.mkdir(parents=True, exist_ok=True)
(out / 'build.json').write_text(json.dumps({'commit':sys.argv[1], 'system':'fixture'}))
print(out)
""")
            (adapter / "run").write_text("""#!/usr/bin/env python3
import json, pathlib, sys
out = pathlib.Path(sys.argv[5])
wall = 0.02 if pathlib.Path(sys.argv[1]).name == 'base' else 0.01
(out / 'report.json').write_text(json.dumps({'wall_s':wall, 'sizes':{'R':2}}))
(out / 'stdout').write_text('fixture completed')
""")
            for script in (adapter / "build", adapter / "run"):
                script.chmod(0o755)
            (root / "scratch").mkdir()
            recipe = root / "scratch" / "trial.toml"
            recipe.write_text('benchmarks=["suite/a"]\nreps=2\ntimeout=5\nmemory_gb=0\nbaseline="base"\n'
                              '[[variants]]\nlabel="base"\nsystem="fixture"\nref="base"\n'
                              '[[variants]]\nlabel="new"\nsystem="fixture"\nref="new"\n')

            def command(*argv):
                with contextlib.redirect_stdout(io.StringIO()) as output, self.assertRaises(SystemExit) as exited:
                    cli.main(list(argv))
                self.assertEqual(exited.exception.code, 0, output.getvalue())
                return output.getvalue()

            command("run", str(recipe), "--machine", "fixture-server")
            bundle = artifacts.list_runs("trial")[0]
            manifest = artifacts.verify_run(bundle)
            self.assertEqual(manifest["state"], "complete")
            self.assertEqual(len(results.report_data("trial")[1]), 4)
            self.assertFalse(config.RESULTS.exists())
            archive = root / "run.tar.gz"
            command("export", bundle.name, "--out", str(archive))
            shutil.rmtree(bundle)
            command("import", str(archive))
            recipe.unlink()
            command("select", "trial", "--runs", bundle.name, "--as", "paper-v1")
            command("check", "trial", "--selection", "paper-v1")
            summary = command("summary", "trial", "--selection", "paper-v1")
            self.assertIn("2.00x", summary)
            try:
                import matplotlib
            except ImportError:
                self.skipTest("matplotlib unavailable; run with the project environment for figure smoke test")
            command("plot", "trial", "--selection", "paper-v1", "--out", str(root / "figures"))
            self.assertTrue((root / "figures" / "trial-1t-speedup.pdf").exists()
                            or list((root / "figures").glob("*speedup.pdf")))
            self.assertTrue((root / "figures" / "trial.csv").is_file())


if __name__ == "__main__":
    unittest.main()
