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
        pytest_import = any(
            (isinstance(node, ast.Import) and any(alias.name == "pytest" for alias in node.names))
            or (isinstance(node, ast.ImportFrom) and node.module == "pytest")
            for node in tree.body
        )
        unittest_class = any(
            isinstance(node, ast.ClassDef)
            and any("TestCase" in ast.unparse(base) for base in node.bases)
            for node in tree.body
        )
        if pytest_import and not unittest_class:
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
