# Ревью: структура репозитория

Backend и RAG перенесены в `src/viniti/` и `src/rag/`; корневые фасады удалены.
В корне оставлена только CLI-точка входа `main.py`. Тесты находятся в `tests/`,
зависимости и команды объявлены в `pyproject.toml`.

## Проверки

- Полный набор: 661 тест, `OK (skipped=3)`. Для локального HTTP-тестирования
  отключён системный proxy; ключ OpenRouter не передавался.
- Editable-установка `pip install -e .` - OK.
- `python -m viniti --help`, `python -m viniti server --help`,
  `python -m viniti rag --help`, `python -m rag --help` и установленный
  `.venv/bin/viniti --help` - OK.
- `node --check static/profile.mjs` - OK.
- `git diff --check` - OK.
- `python -m viniti passport --check` требует отсутствующий локальный `_corpus/`, поэтому
  в этом окружении не выполнен.

## Находки

### 🟡 R1. Некорректное закрытие обработчика профиля

В `static/profile.mjs` обработчик `assistantAsk` открывал
`addEventListener("click", async () => {`, но закрывался как присваивание
функции. Из-за синтаксической ошибки модуль не запускался.

**Исправлено:** заменено закрытие `};` на `});`. `node --check` проходит.

## Вердикт

Исправлена синтаксическая ошибка обработчика. Проверки полного набора и новой
точки входа проходят. Коммит не создавался.
