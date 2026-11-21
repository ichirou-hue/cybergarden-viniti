"""Профили извлечения для ручного этапа: встроенные и пользовательские JSON."""

import json
import os
from pathlib import Path
import re
from tempfile import NamedTemporaryFile

from extraction_core import compile_profile


ROOT = Path(__file__).resolve().parent
BUILTINS = ("solar", "custom-example")
NAME = re.compile(r"[A-Za-z][A-Za-z0-9_]{0,63}")
MAX_BYTES = 1024 * 1024


def directory(path=None):
    """Каталог пользовательских профилей; в тестах и на сервере задаётся явно."""
    if path is not None:
        return Path(path)
    return Path(os.environ.get("VINITI_PROFILES_DIR") or ROOT / "data" / "profiles")


def path_for(name, *, root=None):
    if not isinstance(name, str):
        raise ValueError("Некорректный id профиля.")
    if name in BUILTINS:
        return ROOT / "profiles" / f"{name}.json"
    if not NAME.fullmatch(name):
        raise ValueError("Некорректный id профиля.")
    return directory(root) / f"{name}.json"


def _read(path):
    try:
        data = path.read_bytes()
    except FileNotFoundError:
        raise KeyError(path.stem) from None
    except OSError:
        raise ValueError("Не удалось прочитать файл профиля.") from None
    if len(data) > MAX_BYTES:
        raise ValueError("Профиль больше 1 МиБ.")
    try:
        return json.loads(data)
    except (UnicodeError, json.JSONDecodeError):
        raise ValueError("Профиль должен быть корректным JSON в UTF-8.") from None


def load(name, *, root=None):
    """Вернуть (профиль, источник, путь). КлючError - профиль не найден."""
    path = path_for(name, root=root)
    profile = compile_profile(_read(path))
    source = "builtin" if name in BUILTINS else "user"
    if source == "user" and profile["id"] != name:
        raise ValueError("id профиля не совпадает с именем файла.")
    return profile, source, path


def summary(profile, *, source, name=None):
    tables = []
    if profile.get("adapter") == "solar_csv":
        tables = _solar_tables()
    else:
        for table, specification in profile.get("tables", {}).items():
            fields = []
            for field, settings in specification["fields"].items():
                item = {
                    "name": field,
                    "type": settings["type"],
                    "description": settings["description"],
                    "required": bool(settings.get("required")),
                }
                for key in ("unit", "reference"):
                    if settings.get(key):
                        item[key] = settings[key]
                fields.append(item)
            tables.append({
                "name": table,
                "key": specification["key"],
                "description": specification.get("description", ""),
                "fields": fields,
            })
    return {
        "id": name or profile["id"],
        "version": profile["version"],
        "description": profile.get("description", ""),
        "adapter": profile.get("adapter", "fields"),
        "source": source,
        "tables": tables,
    }


def _solar_tables():
    from db import load_passport
    from solar_schema import TABLES

    try:
        schema = load_passport()["areas"]["solar"]["tables"]
    except (KeyError, OSError, ValueError):
        schema = {}
    tables = []
    for table, columns in TABLES.items():
        known = schema.get(table, {}).get("fields", {})
        tables.append({
            "name": table,
            "key": "publication_id" if table == "publications" else f"{table[:-1]}_id",
            "description": "",
            "fields": [
                {
                    "name": column,
                    "type": known.get(column, {}).get("type", "text"),
                    "description": "",
                    "required": False,
                }
                for column in columns
            ],
        })
    return tables


def list_profiles(*, root=None, include_users=True):
    """Список без содержимого полей; битый пользовательский файл виден как ошибка."""
    items = []
    for name in BUILTINS:
        try:
            profile, source, _ = load(name, root=root)
        except (KeyError, ValueError) as error:
            items.append({"id": name, "source": "builtin", "error": str(error)})
        else:
            items.append(summary(profile, source=source, name=name))
    if not include_users:
        return items
    folder = directory(root)
    if folder.is_dir():
        for path in sorted(folder.glob("*.json")):
            name = path.stem
            if not NAME.fullmatch(name):
                continue
            try:
                profile, source, _ = load(name, root=root)
            except (KeyError, ValueError) as error:
                items.append({"id": name, "source": "user", "error": str(error)})
            else:
                items.append(summary(profile, source=source, name=name))
    return items


def save(name, profile, *, root=None):
    if name in BUILTINS:
        raise ValueError("Встроенный профиль нельзя перезаписать; сохраните копию под другим id.")
    if not isinstance(name, str) or not NAME.fullmatch(name):
        raise ValueError("Некорректный id профиля.")
    if not isinstance(profile, dict) or profile.get("id") != name:
        raise ValueError("id профиля должен совпадать с именем файла.")
    profile = compile_profile(profile)
    folder = directory(root)
    try:
        folder.mkdir(parents=True, exist_ok=True)
    except OSError:
        raise ValueError("Не удалось создать каталог профилей.") from None
    path = folder / f"{name}.json"
    with NamedTemporaryFile("w", encoding="utf-8", dir=folder, prefix=".profile-", delete=False) as stream:
        temporary = Path(stream.name)
        json.dump(profile, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    try:
        os.replace(temporary, path)
    except OSError:
        temporary.unlink(missing_ok=True)
        raise ValueError("Не удалось сохранить профиль.") from None
    return summary(profile, source="user", name=name)


def delete(name, *, root=None):
    if name in BUILTINS:
        raise ValueError("Встроенный профиль удалить нельзя.")
    path = path_for(name, root=root)
    try:
        path.unlink()
    except FileNotFoundError:
        raise KeyError(name) from None
    except OSError:
        raise ValueError("Не удалось удалить профиль.") from None
