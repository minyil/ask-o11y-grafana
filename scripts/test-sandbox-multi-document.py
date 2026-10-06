"""Regression for the multi-document preprocessing wrapper (no compute).

The deployed sandbox image's capture.run_document accepts one document, so the
host wrapper exposes the extra uploads by wrapping capture.execute. This runs
the generated wrapper against the real capture.py to prove the namespace
actually receives `documents`.
"""
import ast
import importlib.util
from pathlib import Path
import sys
import types
import unittest

ROOT = Path(__file__).resolve().parents[1]

# Importing server.py touches the live artifact store; extract only the pure
# wrapper functions, as test-sandbox-inline-json.py does.
tree = ast.parse((ROOT / "sandbox-analysis-mcp/server.py").read_text())
WANTED = {"document_input_path", "wrapped_document_code"}
nodes: list[ast.stmt] = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name in WANTED]
namespace: dict = {"Any": object}
exec(compile(ast.Module(body=nodes, type_ignores=[]), "multi-document-source", "exec"), namespace)
document_input_path = namespace["document_input_path"]
wrapped_document_code = namespace["wrapped_document_code"]


def load_capture() -> types.ModuleType:
    spec = importlib.util.spec_from_file_location("capture", ROOT / "sandbox-analysis-mcp/capture.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class MultiDocumentWrapperTests(unittest.TestCase):
    def test_single_document_wrapper_is_unchanged(self):
        expected = "from capture import run_document\nrun_document('x = 1', '/tmp/input-document.csv', 'csv', 7)"
        self.assertEqual(wrapped_document_code("x = 1", "csv", 7), expected)
        self.assertEqual(wrapped_document_code("x = 1", "csv", 7, [{"path": "/tmp/input-document.csv"}]), expected)

    def test_paths_do_not_collide(self):
        paths = [document_input_path(index, "csv") for index in range(5)]
        self.assertEqual(len(set(paths)), 5)
        self.assertEqual(paths[0], "/tmp/input-document.csv")

    def test_wrapper_injects_documents_into_real_capture(self):
        capture = load_capture()
        seen = {}

        def original_execute(code, seed, ns, *rest):
            seen.update(code=code, seed=seed, namespace=dict(ns), rest_len=len(rest))

        capture.execute = original_execute
        capture.runtime_modules = lambda: (None, None, None, None)
        documents = [
            {"path": "/tmp/input-document.csv", "input_format": "csv", "filename": "a.csv", "sheet": None},
            {"path": "/tmp/input-document-1.xlsx", "input_format": "xlsx", "filename": "b.xlsx", "sheet": "Data"},
        ]
        source = wrapped_document_code("print(documents)", "csv", 42, documents)
        previous = sys.modules.get("capture")
        sys.modules["capture"] = capture
        try:
            exec(compile(source, "wrapper", "exec"), {})
        finally:
            if previous is None:
                sys.modules.pop("capture", None)
            else:
                sys.modules["capture"] = previous
        self.assertEqual(seen["code"], "print(documents)")
        self.assertEqual(seen["seed"], 42)
        self.assertEqual(seen["namespace"]["document_path"], "/tmp/input-document.csv")
        self.assertEqual(seen["namespace"]["input_format"], "csv")
        self.assertEqual(seen["namespace"]["documents"], documents)
        self.assertEqual(seen["rest_len"], 5)


if __name__ == "__main__":
    unittest.main()
