"""Import-boundary tests between Django apps and the pure scheduling engine."""

from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]
BACKEND_APPS = PROJECT_ROOT / "backend" / "apps"
ALLOWED_ENGINE_IMPORT_ROOT = BACKEND_APPS / "scheduling" / "services"


def test_only_scheduling_services_import_pure_engine_modules():
    """Non-scheduling apps must use scheduling services instead of engine DTOs."""

    offenders = []
    for path in BACKEND_APPS.rglob("*.py"):
        if "__pycache__" in path.parts:
            continue
        text = path.read_text(encoding="utf-8")
        if "scheduling_engine" not in text:
            continue
        if path.is_relative_to(ALLOWED_ENGINE_IMPORT_ROOT):
            continue
        offenders.append(str(path.relative_to(PROJECT_ROOT)))

    assert offenders == []
