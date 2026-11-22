# Локальный запуск проекта

Папка `docs` содержит документацию и комплекты данных, но не исходники приложения. Код клонируется отдельно из [GitHub](https://github.com/ichirou-hue/cybergarden-viniti). Для приватного репозитория заранее запросите доступ у владельца.

## Требования

Нужен Python 3.12 с `pip` и `venv`. Команды ниже соответствуют README проекта и `requirements.txt`.

## Linux и macOS

```bash
git clone https://github.com/ichirou-hue/cybergarden-viniti.git
cd cybergarden-viniti
python3.12 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m viniti server
```

Откройте http://127.0.0.1:8765. Сервер привязывается к localhost. Остановить его можно сочетанием Ctrl+C.

Проверка тестов из каталога репозитория:

```bash
.venv/bin/python -m unittest -v
```

## Windows (PowerShell)

Из каталога клонированного репозитория:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m viniti server
```

Откройте http://127.0.0.1:8765. Для тестов выполните:

```powershell
.\.venv\Scripts\python.exe -m unittest -v
```

База данных по умолчанию создаётся в `data/papers.sqlite3`. Параметры `--port` и `--db` позволяют задать другой порт и путь к базе. После установки зависимостей приложение работает офлайн.

## Комплекты данных

Архивы в `docs/materials/` — отдельные входные материалы, а не исходники проекта. Распакуйте Solar и RAG в разные каталоги: имена документов D/P могут повторяться между областями, и смешивание затруднит идентификацию публикаций. Для команд и ссылок на сами архивы см. [начальную страницу](НАЧАТЬ-ЗДЕСЬ.md).
