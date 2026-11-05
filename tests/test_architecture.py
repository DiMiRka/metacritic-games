"""Направления зависимостей между пакетами app: web → домены → core

Новый импорт против этих правил — повод сначала подумать, где должен жить код
"""

import ast
from pathlib import Path

import pytest

APP = Path(__file__).parents[1] / "app"

ALLOWED = {
    "core": set(),
    "scraper": {"core"},
    "catalog": {"core"},
    "auth": {"core"},
    "enrichment": {"core", "catalog", "scraper"},
    "crawler": {"core", "catalog", "scraper", "enrichment", "auth"},
    "monitoring": {"core", "catalog", "crawler", "auth"},
    "web": {"core", "catalog", "crawler", "monitoring", "auth"},
    "worker": {"core", "crawler", "enrichment", "scraper"},
    "cli": {"core", "auth", "worker"},
    "__main__": {"cli"},
}


def package_of(path: Path) -> str:
    return path.relative_to(APP).with_suffix("").parts[0]


def imported_packages(path: Path) -> set[str]:
    found = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.ImportFrom) and node.module:
            names = [node.module]
        elif isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
        else:
            continue
        found |= {name.split(".")[1] for name in names if name.startswith("app.")}
    return found


SOURCES = sorted(p for p in APP.rglob("*.py") if p.name != "__init__.py")


@pytest.mark.parametrize("path", SOURCES, ids=lambda p: str(p.relative_to(APP)))
def test_imports_follow_layers(path: Path) -> None:
    package = package_of(path)
    assert package in ALLOWED, f"новый пакет {package}: добавьте его в ALLOWED"
    forbidden = imported_packages(path) - ALLOWED[package] - {package}
    assert not forbidden, f"{package} не должен зависеть от {sorted(forbidden)}"


def test_nothing_imports_web() -> None:
    offenders = [
        str(p.relative_to(APP))
        for p in SOURCES
        if package_of(p) != "web" and "web" in imported_packages(p)
    ]
    assert offenders == []
