"""Query container lifecycle tests without starting a database or a container."""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import subprocess
import sys
import types
import unittest
from pathlib import Path
from unittest import mock

from harness import query


spec = importlib.util.spec_from_file_location(
    "query_bench_db", Path(__file__).resolve().parents[1] / "systems/egglog-query/bench_db.py"
)
bench_db = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bench_db)


class QueryContainerTests(unittest.TestCase):
    def setUp(self):
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(mock.patch.dict(bench_db.os.environ, {"CONTAINER_ENGINE": ""}))
        self.which = self.stack.enter_context(mock.patch.object(bench_db.shutil, "which", return_value=None))
        self.run = self.stack.enter_context(mock.patch.object(
            bench_db.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, stdout="")
        ))
        self.sleep = self.stack.enter_context(mock.patch.object(bench_db.time, "sleep"))
        self.connection = mock.Mock()
        self.psycopg = types.SimpleNamespace(connect=mock.Mock(return_value=self.connection))
        self.stack.enter_context(mock.patch.dict(sys.modules, {"psycopg": self.psycopg}))
        self.stack.enter_context(mock.patch.object(bench_db, "tables", [], create=True))
        self.stack.enter_context(mock.patch.object(bench_db, "queries", [], create=True))

    def test_runtime_selection_and_provenance_use_same_engine(self):
        for override, podman, expected in (
            ("", None, "docker"),
            ("", "/usr/bin/podman", "podman"),
            ("/custom/docker", "/usr/bin/podman", "/custom/docker"),
        ):
            with self.subTest(override=override, podman=podman):
                with mock.patch.dict(bench_db.os.environ, {"CONTAINER_ENGINE": override}):
                    self.which.return_value = podman
                    self.run.return_value = subprocess.CompletedProcess([], 0, stdout=json.dumps([
                        {"Id": "sha256:fixture", "RepoDigests": ["umbra@sha256:fixture"]}
                    ]))
                    self.assertEqual(bench_db.container_engine(), expected)
                    identity = query._umbra_identity()
                    self.assertEqual(identity["container_engine"], expected)
                    self.assertEqual(identity["id"], "sha256:fixture")
                    self.assertEqual(identity["digests"], ["umbra@sha256:fixture"])
                    self.assertEqual(self.run.call_args.args[0], [expected, "image", "inspect", bench_db.UMBRA_IMAGE])

    def test_unavailable_image_identity_is_explicit(self):
        for result in (
            subprocess.CompletedProcess([], 1, stdout=""),
            subprocess.CompletedProcess([], 0, stdout="invalid json"),
        ):
            self.run.return_value = result
            self.assertEqual(query._umbra_identity(), {
                "image": bench_db.UMBRA_IMAGE, "container_engine": "docker", "identity": "unknown"
            })
        self.run.side_effect = FileNotFoundError("container engine unavailable")
        self.assertEqual(query._umbra_identity()["identity"], "unknown")

    def test_docker_startup_and_cleanup_use_portable_commands(self):
        bench_db.run_umbra(io.StringIO())
        self.assertEqual([call.args[0] for call in self.run.call_args_list], [
            ["docker", "rm", "-f", "umbra-bench"],
            ["docker", "run", "-d", "--name", "umbra-bench", "-p", "5432:5432", bench_db.UMBRA_IMAGE],
            ["docker", "rm", "-f", "umbra-bench"],
        ])
        self.assertTrue(all(not call.kwargs.get("shell") for call in self.run.call_args_list))
        self.connection.close.assert_called_once_with()
        self.assertEqual(self.psycopg.connect.call_args.kwargs, {"autocommit": True, "connect_timeout": 2})

    def test_container_and_connection_startup_retry(self):
        starts = []
        def process(argv, **kwargs):
            if argv[1] == "run":
                starts.append(argv)
                return subprocess.CompletedProcess(argv, 1 if len(starts) < 3 else 0)
            return subprocess.CompletedProcess(argv, 0)
        self.run.side_effect = process
        self.psycopg.connect.side_effect = [OSError("not ready"), self.connection]
        self.which.return_value = "/usr/bin/podman"
        con, cursor = bench_db.umbra_connect()
        self.assertIs(con, self.connection)
        self.assertIs(cursor, self.connection.cursor.return_value)
        self.assertEqual(len(starts), 3)
        self.assertTrue(all(argv[0] == "podman" and "--replace" not in argv for argv in starts))
        self.assertEqual(self.sleep.call_args_list, [mock.call(5), mock.call(5), mock.call(0.5)])
        self.assertEqual(self.psycopg.connect.call_count, 2)

    def test_failed_container_start_cleans_up_and_never_connects(self):
        self.run.return_value = subprocess.CompletedProcess([], 1)
        with self.assertRaisesRegex(RuntimeError, "could not start the Umbra container"):
            bench_db.umbra_connect()
        self.psycopg.connect.assert_not_called()
        calls = [call.args[0] for call in self.run.call_args_list]
        self.assertEqual(sum(argv[1] == "run" for argv in calls), 5)
        self.assertEqual(calls[-1], ["docker", "rm", "-f", "umbra-bench"])

    def test_failed_connection_cleans_up_and_reports_original_error(self):
        error = OSError("database unavailable")
        self.psycopg.connect.side_effect = error
        with self.assertRaisesRegex(RuntimeError, "after 240 attempts") as raised:
            bench_db.umbra_connect()
        self.assertIs(raised.exception.__cause__, error)
        self.assertEqual(self.psycopg.connect.call_count, 240)
        self.assertEqual(self.run.call_args.args[0], ["docker", "rm", "-f", "umbra-bench"])

    def test_failed_table_loading_closes_connection_and_removes_container(self):
        self.connection.cursor.return_value.execute.side_effect = ValueError("bad schema")
        with mock.patch.object(bench_db, "tables", ["R"]), mock.patch.object(bench_db, "schema", mock.Mock(), create=True):
            with self.assertRaisesRegex(ValueError, "bad schema"):
                bench_db.umbra_connect()
        self.connection.close.assert_called_once_with()
        self.assertEqual(self.run.call_args.args[0], ["docker", "rm", "-f", "umbra-bench"])

    def test_crash_probe_uses_selected_engine_and_reconnects(self):
        cursor = self.connection.cursor.return_value
        cursor.execute.side_effect = RuntimeError("server disconnected")
        replacement = mock.Mock()
        output = io.StringIO()
        with mock.patch.object(bench_db, "queries", [("q0", "SELECT 1", [])]), \
             mock.patch.object(bench_db, "a", types.SimpleNamespace(threads=1, timeout=60, reps=1), create=True), \
             mock.patch.object(bench_db, "Timeout", return_value=contextlib.nullcontext()), \
             mock.patch.object(bench_db, "umbra_connect", side_effect=[(self.connection, cursor), (replacement, replacement.cursor())]):
            bench_db.run_umbra(output)
        self.assertEqual(json.loads(output.getvalue())["status"], "crash")
        self.assertEqual(self.run.call_args_list[0].args[0], ["docker", "ps", "-q", "--filter", "name=umbra-bench"])
        self.connection.close.assert_called_once_with()
        replacement.close.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
