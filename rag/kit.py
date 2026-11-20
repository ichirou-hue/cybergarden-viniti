"""Поиск комплекта участника и загрузка схемы сдачи.

Комплект (kit) — это распакованный архив организаторов. Внутри него лежат
``dev/``, ``dev_gold/``, ``test/``, ``submission_template/`` и ``tools/``.
Путь можно задать переменной окружения ``RAG_KIT``; иначе модуль ищет
каталог с ``dev_gold/`` рядом с репозиторием.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

REPO_ROOT = Path(__file__).resolve().parent.parent
KIT_ENV = "RAG_KIT"

#: Порядок таблиц в сдаче. Совпадает с CHECKLIST и schema.json.
TABLE_ORDER: tuple[str, ...] = (
    "publications",
    "configurations",
    "datasets",
    "tasks",
    "experiments",
    "results",
    "issues",
)

_SKIP_DIRS = {
    ".git",
    ".venv",
    "venv",
    "__pycache__",
    "node_modules",
    "materials",
    ".idea",
    ".vscode",
}


class KitNotFound(RuntimeError):
    """Комплект участника не найден."""


def _walk_dirs(root: Path, depth: int) -> Iterable[Path]:
    """Обход каталогов в ширину с ограничением глубины."""
    frontier = [(root, 0)]
    while frontier:
        current, level = frontier.pop(0)
        yield current
        if level >= depth:
            continue
        try:
            children = sorted(p for p in current.iterdir() if p.is_dir())
        except OSError:
            continue
        for child in children:
            if child.name in _SKIP_DIRS or child.name.startswith("._"):
                continue
            frontier.append((child, level + 1))


def _is_kit(path: Path) -> bool:
    """Комплект именно RAG: есть dev_gold/ и схема с таблицей configurations.

    Проверка по схеме, а не по имени каталога, отличает RAG от solar:
    у solar-комплекта тоже есть dev_gold/ и submission_template/.
    """
    if not (path / "dev_gold").is_dir() or not (path / "submission_template").is_dir():
        return False
    schema_path = path / "submission_template" / "schema.json"
    if not schema_path.is_file():
        return False
    try:
        tables = json.loads(schema_path.read_text(encoding="utf-8")).get("tables", {})
    except (OSError, ValueError):
        return False
    return "configurations" in tables and "results" in tables


def find_kit_root(start: Path | None = None) -> Path:
    """Вернуть каталог комплекта, содержащий ``dev_gold/`` и ``submission_template/``."""
    env = os.environ.get(KIT_ENV)
    if env:
        candidate = Path(env).expanduser().resolve()
        if _is_kit(candidate):
            return candidate
        for child in _walk_dirs(candidate, depth=2):
            if _is_kit(child):
                return child
        raise KitNotFound(
            f"{KIT_ENV}={candidate}: не найден каталог с dev_gold/ и submission_template/"
        )

    base = (start or REPO_ROOT).resolve()
    for child in _walk_dirs(base, depth=4):
        if _is_kit(child):
            return child
    raise KitNotFound(
        "Комплект RAG не найден. Распакуйте архив организаторов рядом с репозиторием "
        f"или задайте {KIT_ENV}=<путь к RAG_ВИНИТИ_участникам_...>."
    )


@dataclass(frozen=True)
class Schema:
    """Схема сдачи из ``submission_template/schema.json``."""

    raw: dict[str, Any]

    @property
    def tables(self) -> dict[str, list[str]]:
        return self.raw["tables"]

    @property
    def keys(self) -> dict[str, str]:
        return self.raw["keys"]

    @property
    def enums(self) -> dict[str, list[str]]:
        return self.raw.get("enums", {})

    @property
    def recommended_enums(self) -> dict[str, list[str]]:
        return self.raw.get("recommended_enums", {})

    @property
    def service_fields(self) -> list[str]:
        return self.raw.get("service_fields", [])

    def columns(self, table: str) -> list[str]:
        return list(self.tables[table])

    def key(self, table: str) -> str:
        return self.keys[table]

    def enum_for(self, table: str, column: str) -> list[str] | None:
        return self.enums.get(f"{table}.{column}")


@dataclass(frozen=True)
class Kit:
    """Распакованный комплект участника."""

    root: Path

    # --- каталоги -------------------------------------------------------
    @property
    def dev(self) -> Path:
        return self.root / "dev"

    @property
    def dev_gold(self) -> Path:
        return self.root / "dev_gold"

    @property
    def test(self) -> Path:
        return self.root / "test"

    @property
    def template(self) -> Path:
        return self.root / "submission_template"

    @property
    def tools(self) -> Path:
        return self.root / "tools"

    # --- схема ----------------------------------------------------------
    def schema(self) -> Schema:
        path = self.template / "schema.json"
        return Schema(json.loads(path.read_text(encoding="utf-8")))

    # --- публикации -----------------------------------------------------
    def split_dir(self, split: str = "dev") -> Path:
        if split == "dev":
            return self.dev
        if split == "test":
            return self.test
        if split == "gold":
            return self.dev_gold
        raise ValueError(f"неизвестный split: {split}")

    def pdf(self, publication_id: str, split: str = "dev") -> Path:
        path = self.split_dir(split) / f"{publication_id}.pdf"
        if not path.is_file():
            raise FileNotFoundError(path)
        return path

    def publication_ids(self, split: str = "dev") -> list[str]:
        directory = self.split_dir(split)
        if not directory.is_dir():
            return []
        return sorted(
            p.stem for p in directory.glob("*.pdf") if not p.name.startswith("._")
        )

    def __str__(self) -> str:  # pragma: no cover - удобство отладки
        return f"Kit({self.root})"


def load_kit(start: Path | None = None) -> Kit:
    return Kit(find_kit_root(start))
