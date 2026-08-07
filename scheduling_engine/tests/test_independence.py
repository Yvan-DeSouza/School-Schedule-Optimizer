import ast
from pathlib import Path


def test_engine_source_has_no_django_or_backend_imports():
    package_root = Path(__file__).resolve().parents[1]
    for source_file in package_root.rglob("*.py"):
        if "tests" in source_file.parts:
            continue
        tree = ast.parse(source_file.read_text(encoding="utf-8"), filename=str(source_file))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                modules = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                modules = [node.module or ""]
            else:
                continue
            assert all(not (module == "django" or module.startswith("django.") or module == "backend" or module.startswith("backend.")) for module in modules)
