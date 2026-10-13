"""Regression for the sandbox execution watchdog, its retry, output-limit
error relay and the retryable operation record (no compute).

A submitted cell that never reached the Jupyter kernel used to hang the call
for the full one-hour sandbox lifetime, and output-limit failures reached the
model only as "ValueError at line N".
"""
import ast
import builtins
import contextlib
import importlib.util
from pathlib import Path
import re
import sys
import tempfile
import threading
import time
import types
import unittest
from typing import Any, Callable

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# Importing server.py touches the live artifact store; extract only what is tested.
tree = ast.parse((ROOT / "sandbox-analysis-mcp/server.py").read_text())
WANTED = {
    "EXECUTION_STARTED_MARKER", "SandboxExecutionTimeout", "ExecutionWatchdog", "execute_opensandbox_input",
    "OUTPUT_CONTRACT_ERRORS", "python_error_hint", "python_error_text",
}


def wanted(node: ast.stmt) -> bool:
    if isinstance(node, (ast.FunctionDef, ast.ClassDef)):
        return node.name in WANTED
    return isinstance(node, ast.Assign) and any(isinstance(target, ast.Name) and target.id in WANTED for target in node.targets)


namespace: dict = {"Any": Any, "Callable": Callable, "threading": threading, "time": time, "contextlib": contextlib, "re": re, "builtins": builtins}
exec(compile(ast.Module(body=[node for node in tree.body if wanted(node)], type_ignores=[]), "execution-guards-source", "exec"), namespace)
ExecutionWatchdog = namespace["ExecutionWatchdog"]
SandboxExecutionTimeout = namespace["SandboxExecutionTimeout"]


def load_artifact_store() -> types.ModuleType:
    spec = importlib.util.spec_from_file_location("artifact_store", ROOT / "artifact_store.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class WatchdogTests(unittest.TestCase):
    def test_code_that_never_starts_is_stopped_as_not_started(self):
        stops = []
        watchdog = ExecutionWatchdog(lambda: stops.append(1), 0.2, 30)
        time.sleep(1.2)
        with self.assertRaises(SandboxExecutionTimeout) as raised:
            watchdog.finish()
        self.assertFalse(raised.exception.started)
        self.assertEqual(stops, [1])

    def test_started_code_runs_until_the_execution_budget(self):
        stops = []
        watchdog = ExecutionWatchdog(lambda: stops.append(1), 0.2, 1.0)
        watchdog.observe_stdout(types.SimpleNamespace(text=namespace["EXECUTION_STARTED_MARKER"] + "\n"))
        time.sleep(0.7)
        self.assertEqual(stops, [])
        time.sleep(1.0)
        with self.assertRaises(SandboxExecutionTimeout) as raised:
            watchdog.finish()
        self.assertTrue(raised.exception.started)
        self.assertEqual(stops, [1])

    def test_finished_code_is_never_stopped(self):
        stops = []
        watchdog = ExecutionWatchdog(lambda: stops.append(1), 0.2, 1.0)
        watchdog.observe_stdout(types.SimpleNamespace(text=namespace["EXECUTION_STARTED_MARKER"]))
        watchdog.finish()
        time.sleep(0.7)
        self.assertEqual(stops, [])


class RetryTests(unittest.TestCase):
    def run_with(self, outcomes):
        calls = []

        def once(*_args, **_kwargs):
            calls.append(1)
            outcome = outcomes[len(calls) - 1]
            if isinstance(outcome, Exception):
                raise outcome
            return outcome

        namespace["_execute_opensandbox_once"] = once
        return calls

    def test_not_started_is_retried_once(self):
        calls = self.run_with([SandboxExecutionTimeout(False), {"ok": True}])
        self.assertEqual(namespace["execute_opensandbox_input"]("/tmp/in", b"x", "src"), {"ok": True})
        self.assertEqual(len(calls), 2)

    def test_second_not_started_is_reported(self):
        calls = self.run_with([SandboxExecutionTimeout(False), SandboxExecutionTimeout(False)])
        with self.assertRaises(SandboxExecutionTimeout):
            namespace["execute_opensandbox_input"]("/tmp/in", b"x", "src")
        self.assertEqual(len(calls), 2)

    def test_started_timeout_is_not_retried(self):
        calls = self.run_with([SandboxExecutionTimeout(True)])
        with self.assertRaises(SandboxExecutionTimeout):
            namespace["execute_opensandbox_input"]("/tmp/in", b"x", "src")
        self.assertEqual(len(calls), 1)


class ErrorRelayTests(unittest.TestCase):
    def hint(self, value, name="ValueError"):
        return namespace["python_error_hint"]({"name": name, "value": value, "traceback": "<generated-analysis>, line 2"}, "a\nb\n")

    def test_limit_message_is_relayed(self):
        message = "derived frame must contain 1..200 fields and at most 50000 rows"
        hint = self.hint(message)
        self.assertEqual(hint["output_contract"], message)
        self.assertEqual(namespace["python_error_text"](hint), f"Python ValueError at generated code lines [2]: {message}")
        self.assertEqual(self.hint("derived frame exceeds 16777216 bytes")["output_contract"], "derived frame exceeds 16777216 bytes")

    def test_other_values_stay_private(self):
        for value, name in (("could not convert string to float: '42.7 secret'", "ValueError"),
                            ("derived frame must contain 1..200 fields and at most 5000 rows; row 3 = 9", "ValueError"),
                            ("derived frame contains unsupported values", "TypeError")):
            hint = self.hint(value, name)
            self.assertNotIn("output_contract", hint)
            self.assertNotIn(value, namespace["python_error_text"](hint))


class OperationRecordTests(unittest.TestCase):
    def test_terminated_operation_can_run_again(self):
        store_module = load_artifact_store()
        with tempfile.TemporaryDirectory() as root:
            store = store_module.ArtifactStore(Path(root))
            context = {"org_id": "1", "user_id": "2", "session_id": "s"}
            runs = []

            def terminated():
                runs.append(1)
                return {"ok": False, "evidence": {"effect_outcome": "terminated"}}

            store.run_once(context, "execute_python_preprocessing", {"code": "x"}, terminated)
            store.run_once(context, "execute_python_preprocessing", {"code": "x"}, terminated)
            self.assertEqual(len(runs), 2)
            self.assertEqual(store.operation_statuses(context), [])

            def failed():
                runs.append(1)
                return {"ok": False, "evidence": {}}

            store.run_once(context, "execute_python_preprocessing", {"code": "y"}, failed)
            store.run_once(context, "execute_python_preprocessing", {"code": "y"}, failed)
            self.assertEqual(len(runs), 3)


if __name__ == "__main__":
    unittest.main()
