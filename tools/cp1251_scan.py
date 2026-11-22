"""Найти символы, которые не печатаются в cp1251.

CI запускает python -m unittest -v без -X utf8. На Windows-раннере консоль обычно
cp1251, и любой символ вне неё в print() даёт UnicodeEncodeError: зелёный локально
тест падает в CI. Скрипт смотрит строковые литералы через ast, а не комментарии.
"""
import ast
import pathlib
import sys

SKIP = {".venv", ".venv-docling", "_refs", "_corpus", "__pycache__", ".slim", "out", "data"}


def bad_chars(text):
    found = []
    for char in text:
        try:
            char.encode("cp1251")
        except UnicodeEncodeError:
            found.append(char)
    return found


def main():
    root = pathlib.Path(__file__).resolve().parent.parent
    total = 0
    for path in sorted(root.rglob("*.py")):
        if any(part in SKIP for part in path.parts):
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError as error:
            print(f"{path.name}: не разобран ({error})")
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
                continue
            for char in bad_chars(node.value):
                total += 1
                print(f"{path.relative_to(root)}:{node.lineno} "
                      f"U+{ord(char):04X} {char!r}")
    print(f"всего символов вне cp1251 в строках: {total}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
