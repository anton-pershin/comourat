"""Static checks for the comourat library boundary (spec 22, AC6)."""

import pathlib
import re

import pytest

COMOURAT_ROOT = pathlib.Path(__file__).resolve().parents[1] / "comourat"

IMPORT_RE = re.compile(r"^\s*(?:from|import)\s+mourat[\.\s]", re.MULTILINE)


def comourat_python_files():
    return sorted(COMOURAT_ROOT.rglob("*.py"))


def test_no_module_under_comourat_imports_mourat():
    violations = [
        str(path)
        for path in comourat_python_files()
        if IMPORT_RE.search(path.read_text(encoding="utf-8"))
    ]
    assert violations == []


def test_comourat_modules_importable():
    import comourat.clients
    import comourat.collectors
    import comourat.enrichment
    import comourat.influence
    import comourat.models
    import comourat.resolution
    import comourat.verification


def test_config_via_constructor_no_env_lookups():
    """comourat reads no environment variables for credentials (AC6)."""
    env_re = re.compile(r"os\.(?:environ|getenv)", re.MULTILINE)
    violations = [
        str(path)
        for path in comourat_python_files()
        if env_re.search(path.read_text(encoding="utf-8"))
    ]
    assert violations == []
