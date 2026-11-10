"""Run unittest suites without importing optional pytest-only phone modules.

The separate Python 3.12 paired job runs the phone pytest suites. Legacy
Python compatibility jobs retain every unittest module and its original loader.
"""

import ast
import sys
import unittest
from pathlib import Path


def modules(root):
    for path in sorted((root / "tests").glob("test_*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        has_class = any(isinstance(node, ast.ClassDef) for node in tree.body)
        custom_loader = any(
            isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "load_tests"
            for node in tree.body
        )
        # Pure function suites may import pytest fixtures indirectly. They have
        # no unittest cases and belong to the complete paired pytest job.
        if not has_class and not custom_loader:
            continue
        yield path.stem


def main():
    root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(root))
    sys.path.insert(0, str(root / "tests"))
    loader = unittest.TestLoader()
    suite = unittest.TestSuite(loader.loadTestsFromName(name) for name in modules(root))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    raise SystemExit(0 if result.wasSuccessful() else 1)


if __name__ == "__main__":
    main()
