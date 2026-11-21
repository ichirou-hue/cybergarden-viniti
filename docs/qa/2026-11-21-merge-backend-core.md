# QA: слитый бекенд после merge `feature/backend-core` (solar на `solar_offline`)

- **Дата:** 2026-11-21.
- **Объект:** ветка `dev`, коммит `393057e` (merge `origin/feature/backend-core`) плюс незакоммиченная дельта рабочего дерева: `solar_offline.py`, `solar_api.py`, `batch.py`, `solar_version.py`, `test_solar_api.py`, `test_solar_offline.py`. Продакшен-код не менял, коммитов не делал, тесты не правил.
- **Окружение:** Linux, Python 3.12.16 из `.venv`; `pdfplumber`, `pypdf`, `reportlab` установлены, `fontTools` нет. Корпус: `_corpus/area1` - симлинк на `/home/ilya/Загрузки/Кейс5_область1_участникам/`. RAG-корпус не распакован.
- **Проверял независимо:** свои команды и скрипты в `/tmp/opencode/`, тесты как доказательство не использовал (прогнал только как дополнительный сигнал).

## Итог

Сдача CSV, скорер, batch/resume, защита эталона и негативные случаи в порядке: TOTAL F1 0.3699 (ожидание около 0.37, регрессии нет), валидатор 0 ошибок, resume берёт следующие файлы, эталон не стирается, не-PDF и PDF без текста дают 422/ValueError без частичных данных.

В веб-каталоге solar есть одна существенная недоделка merge: замечания считаются и лежат в конверте, но эндпоинт `/issues` для solar по-прежнему отдаёт пустой список, поэтому вкладка «Замечания» всегда пуста при `issues_count > 0`. Кроме неё - три дефекта интеграции поменьше (плохие `limit`/`offset` роняют соединение вместо 400, поиск по авторам в каталоге не работает для solar, `/sources` теряет `text_sources`). Ещё один известный остаток CLI (код 0 при полном провале с `--skip-errors`) и шум `fontTools` в stderr.

## Находки по серьёзности

### B1. Существенно: `/api/documents/<id>/issues` для solar всегда пуст, хотя замечания есть

- **Где:** `server.py:819-820` (`solar_documents_get`): ветка `issues` возвращает литерал `{"total": 0, "items": []}`. Конверт solar теперь содержит настоящие замечания (`solar_api._issues`, `solar_api.py:163-196`), но эндпоинт их не читает.
- **Влияние:** `static/document.js:699` строит вкладку «Замечания» только из `/issues`. Карточка при этом показывает `issues_count = 1` и в `export.json` замечание есть. Пользователь видит «Замечаний нет.» при непустом счётчике. До merge замечаний в solar-конверте не было и поведение было согласованным, то есть это регрессия интеграции, а не старый дефект.
- **Воспроизведение:**

```bash
mkdir -p /tmp/opencode/qa_repro
.venv/bin/python server.py --port 8799 --db /tmp/opencode/qa_repro/papers.sqlite3 \
    --data /tmp/opencode/qa_repro/viniti.sqlite3
curl -s -X POST -H 'Content-Type: application/pdf' -H 'X-Track: solar' \
     -H 'X-Filename: D001.pdf' --data-binary @_corpus/area1/dev/D001.pdf \
     http://127.0.0.1:8799/api/documents
# {"document_id": "solar:d001-05df334d", "issues_count": 1, ...}
curl -s 'http://127.0.0.1:8799/api/documents/solar:d001-05df334d/issues'
# {"total": 0, "items": []}
curl -s 'http://127.0.0.1:8799/api/documents/solar:d001-05df334d/export.json' \
  | .venv/bin/python -c 'import json,sys; print(json.load(sys.stdin)["issues"])'
# [{"issue_id": "I1", "record_ids": ["R029"], "check_name": "normalization", ...}]
```

- **Ожидание:** `{"total": 1, "items": [{"issue_id": "I1", "record_ids": ["R029"], ...}]}`, то есть то же, что отдаёт `/issues` для RAG (`server.py:728-736`) и что уже лежит в конверте.
- **Минимальный пример:** `solar_api._issues(tables)` даёт непустой список, `server.solar_documents_get` его игнорирует. Достаточно отдавать `envelope.get("issues", [])` с пагинацией, как в RAG-ветке.
- **Почему тесты это пропустили:** новый `test_solar_api.py` проверяет только сборку конверта (`_envelope`, `_issues`), но не HTTP-маршрут `/issues`; старый `server.solar_documents_get` остался без теста и после merge. Красный тест на этот баг я намеренно не добавлял, чтобы не сделать `unittest` красным до решения команды; рекомендую `test_solar_api`-тест через `Handler`, ожидающий `total == envelope["issues_count"]`.

### B2. Средне: неверный `limit`/`offset` на solar-эндпоинтах рвёт соединение вместо 400

- **Где:** `server.py:817` (`/results`) и `server.py:823` (`/sources`): `self._page(query)` / `self._int(...)` бросают `ValueError`, обработчика нет (в RAG-ветке он есть, `server.py:718-726`, `738-746`).
- **Фактически:** клиент получает `RemoteDisconnected`, в stderr сервера traceback `ValueError: Параметр limit должен быть целым числом.` Это противоречит контракту из `AGENTS.md`: «Ошибка в имени поля или в фильтре - 400 с перечнем допустимых».
- **Воспроизведение:** после загрузки D001 (см. B1):

```bash
curl -sv 'http://127.0.0.1:8799/api/documents/solar:d001-05df334d/results?limit=abc'
curl -sv 'http://127.0.0.1:8799/api/documents/solar:d001-05df334d/sources?limit=abc'
```

Результат во всех четырёх пробах (`limit=abc`, `limit=-5`, `offset=x`, `/sources?limit=abc`): `curl -w '%{http_code}'` печатает `000`, то есть обрыв без HTTP-ответа (проверено и на `server.py` из CLI, и на сервере, поднятом программно). `limit=100000` корректно обрезается и отдаёт 200.
- **Ожидание:** `400` с текстом про параметр, как у соседних RAG-маршрутов.
- **Атрибуция:** код `solar_documents_get` существует и до merge (`ec6c54a:server.py:487`), то есть дефект старый, но контракт API он нарушает и после merge остаётся.

### B3. Средне-низко: поиск по автору в каталоге не находит solar-документы

- **Где:** `server.py:670`: `" ".join(doc.get("authors") or [])`. У RAG `authors` - список (`rag_api.py:76,497`), у solar - строка из шаблона (`solar_api.py:181`). Строка итерируется по символам, получается `"V a j j i r a v e l ..."`.
- **Воспроизведение:** загрузить `_corpus/area1/dev/D004.pdf` с `X-Track: solar`, затем:

```bash
curl -s 'http://127.0.0.1:8799/api/documents?track=solar&q=Vajjiravel'
# {"total": 0, "items": []}
curl -s 'http://127.0.0.1:8799/api/documents?track=solar&q=Balamurugan'
# {"total": 0, "items": []}
```

Авторы в summary: `"Vajjiravel Murugesan and Balamurugan Rathinam"`; поиск по `Murugesan` находится только потому, что фамилия попала в title.
- **Ожидание:** документ находится по фамилии из `authors`. Отображение в карточке не страдает (`static/document.js:128` умеет и строку, и список).
- **Минимальный пример:** `" ".join("A B") -> "A   B"` вместо `["A B"]`. Лечится приведением к списку в `solar_api` (как `rag_api._authors`) или нормализацией в `documents_get`.

### B4. Низко: `/sources` для solar теряет все `text_sources`

- **Где:** `server.py:821-823` собирает источники только из `row["source"]` результатов. `solar_api` специально кладёт в конверт `text_sources` (`solar_api.py:108-115,195`), но эндпоинт его не смотрит.
- **Фактически на D020:** в `export.json` 915 элементов `text_sources` (kind `caption`/`text`), `/sources` отдаёт 35 (только анкоры результатов). Вкладка фрагментов (`static/document.js:769`) показывает 35 из 950.
- **Ожидание:** либо `/sources` объединяет анкоры и `text_sources`, либо `text_sources` не считается частью контракта. Сейчас поле доступно только через `export.json` и в лёгком ответе документа скрыто как heavy.
- **Воспроизведение:** загрузить `_corpus/area1/dev/D020.pdf`, сравнить `export.json.text_sources` и `/sources?limit=500`.

### B5. Низко, известно: `--skip-errors` даёт код 0 и пустые CSV, когда не разобрался ни один PDF

- **Где:** `solar_offline.py:2128-2160`: итог всегда `return 0`, а CSV пишутся в конце цикла.
- **Воспроизведение:**

```bash
mkdir -p /tmp/opencode/allbad && printf '%%PDF-1.4 broken' > /tmp/opencode/allbad/A001.pdf
cp /tmp/opencode/allbad/A001.pdf /tmp/opencode/allbad/A002.pdf
.venv/bin/python solar_offline.py --pdf-dir /tmp/opencode/allbad --out /tmp/opencode/allbad_out --skip-errors
echo $?          # 0
wc -l /tmp/opencode/allbad_out/*.csv   # по 1 строке, только заголовок
```

- **Ожидание:** ненулевой код (или явный маркер «сдача пустая»), чтобы CI/участник это заметил. В `docs/reviews/2026-11-21-solar-offline.md` пункт 3 уже числится открытым, это не регрессия merge.

### Вопросы (не баги)

- **Q1. Сообщение на не-PDF в API не по адресу.** `solar_api.ingest` (`solar_api.py:199-200`) на любой не-PDF и на превышение размера отвечает одной строкой «Нужен PDF размером до 32 МиБ.», поэтому на `b"not a pdf"` пользователь не узнаёт, что дело в формате. HTTP-код 422 корректен.
- **Q2. Два лимита размера.** `solar_api.MAX_BYTES` = 32 МиБ, `solar_facts.MAX_BYTES` = 40 МиБ, а `AGENTS.md` в «Ловушках» говорит про 20 МиБ (`library.py`/`server.py`). Для каталога действует 32; стоит либо свести к одному источнику, либо обновить `AGENTS.md`.
- **Q3. Валидатор организаторов молча пропускает проверку цитат, если в папке нет PDF.** Команда из задания `check_submission.py dev_gold /tmp/opencode/qa_solar` трактует `qa_solar` как папку PDF, не находит там `D001.pdf` и выдаёт «ошибок: 0, предупреждений: 0» без единой проверенной цитаты. Это поведение чужого инструмента, но именно поэтому команду из задания нельзя считать проверкой сдачи (см. сценарий 2).

### Предложения по улучшению

- **S1. Шум `fontTools`.** На каждый PDF в stderr сыплется порядка сотни строк `fontTools is required to fully parse the encoding...`; на 21 PDF это мегабайты лога. Результат и коды не страдают, но `batch` и CLI в CI почти нечитаемы. Лечится настройкой `logging`/`warnings` для `pdfminer` или установкой `fonttools` в `requirements.txt`.
- **S2. Метаданные публикаций в solar не проверяются ни скорером, ни валидатором.** На `dev`: у D001 пустой `year` (в эталоне 2021, DOI содержит 2021), у D002 `title` = «Tehnički vjesnik 29, 6(2022), 1861-1867 1861», `authors` = `"dm"`, у D004 `title` начинается с «Citation: ...». `score.py` для `publications` сравнивает только `publication_id`, поэтому F1 1.0000 маскирует это. Это не регрессия merge и не блокер сдачи, но карточка каталога показывает мусор как метаданные.

## Сценарии

### 1. Сдача CSV и скорер: PASS

```bash
.venv/bin/python solar_offline.py --pdf-dir _corpus/area1/dev --out /tmp/opencode/qa_solar
.venv/bin/python score.py --area solar --pred /tmp/opencode/qa_solar --ref _corpus/area1/dev_gold
```

- **Фактически:** 21 PDF, 0 пропущено, 990 строк, время 7m09s (user 7m06s). Строки: publications 21, materials 54, devices 54, layers 231, measurements 17, results 519, issues 94.
- **Скорер:**

```
table          pred  ref  matched  f1
results         519  659      196   0.3328
publications     21   21       21   1.0000
materials        54   57       13   0.2342
devices          54   89       41   0.5734
layers          231  519      127   0.3387
measurements     17   45       17   0.5484
issues           94   53       35   0.4762
TOTAL           990 1443      450   0.3699   (precision 0.4545, recall 0.3119)
```

- **Ожидание из `AGENTS.md`:** около 0.37, results 0.33, publications 1.00, devices 0.57, layers 0.34, measurements 0.55, issues 0.48, итого 0.37. Совпадает до последней цифры (0.3699), регрессии нет.
- **Детерминизм:** независимый второй прогон того же каталога (`/tmp/opencode/out_solar`, запускался параллельно) дал байт в байт те же 7 CSV (`cmp` по каждому файлу: `same`).
- **Проверено дополнительно:** 94 замечания = 74 `normalization` + 20 `contradiction`, у всех заполнен `record_ids`. В результатах 227 строк `result_origin=cited` из 519; в эталоне 57 из 659. Пустой `device_id` встречается только у cited-строк (227 из 227), как и в эталоне (57 из 57), то есть это семантика шаблона, а не потеря ссылки.

### 2. Валидатор организаторов: PASS (с оговоркой про команду из задания)

- **Команда из задания буквально:**

```bash
cd _corpus/area1 && ../../.venv/bin/python tools/check_submission.py dev_gold /tmp/opencode/qa_solar
```

  `_corpus/area1` - симлинк в `~/Загрузки`, поэтому `../../.venv/bin/python` не существует (код 127). Из реального каталога та же команда работает, но `qa_solar` трактуется как папка PDF; PDF там нет, поэтому проверка цитат пропускается и печатается `таблиц: 7, результатов: 659, ошибок: 0, предупреждений: 0`, без строки о проверенных цитатах. Это проверка эталона, а не сдачи.
- **Корректная проверка сдачи** (первый аргумент - папка CSV, второй - папка PDF):

```bash
cd /home/ilya/Загрузки/Кейс5_область1_участникам
/home/ilya/Projects/cybergarden-viniti/.venv/bin/python tools/check_submission.py \
    /tmp/opencode/qa_solar dev
```

- **Фактически:** `таблиц: 7, результатов: 519, ошибок: 0, предупреждений: 285, проверено цитат: 613, не найдено: 285`, код выхода 0.
- **Ожидание:** 0 ошибок. Выполнено. 285 предупреждений - известное ограничение канала доказательств (pdfplumber против pypdf), совпадает с `docs/reviews/2026-11-21-solar-offline.md`. Из 285: 218 `results.csv` и 67 `issues.csv` (37 + 23 + 7 по формату строки).
- **BOM:** все 7 CSV начинаются с `EF BB BF`, `check_submission.py` их читает без ошибок.

### 3. Batch, resume и dry-run: PASS

```bash
.venv/bin/python db.py --init --db /tmp/opencode/qa.sqlite3
.venv/bin/python batch.py --area solar --split dev --limit 2 --db /tmp/opencode/qa.sqlite3
.venv/bin/python batch.py --area solar --split dev --resume --limit 2 --db /tmp/opencode/qa.sqlite3
.venv/bin/python batch.py --area solar --split dev --resume --limit 2 --dry-run --db /tmp/opencode/qa.sqlite3
.venv/bin/python batch.py --area solar --split dev --dry-run
```

- **Первый запуск:** `к запуску 2, пропущено 0`; `D001 ok 12501 ms`, `D002 ok 19322 ms`, `итог: ok 2, ошибок 0`, код 0. В `jobs` у обеих записей `status=ok`, `attempts=1`, `extractor_revision=2`; в `documents` `parser_status=ok`, `parser_version=2`.
- **Второй запуск с `--resume`:** `к запуску 2, пропущено 2`; обработаны `D003` и `D004` (attempts=1), D001/D002 не тронуты. То есть повтор взял следующие файлы, а не те же.
- **`--dry-run --resume --limit 2`:** план `D005`, `D006`, `пропущено 4`. Лимит применяется после пропуска готовых.
- **`--dry-run` без `--resume`:** 21 файл в плане, код 0, база не создаётся, падений нет.
- **Ревизия:** `solar_version.PARSER_VERSION = 2`, `batch.default_revision("solar") = 2`, `jobs.extractor_revision = 2`. Совпадает.
- **Замечание:** на машине шёл второй независимый прогон того же корпуса; времена D001/D002 (12.5/19.3 с) завышены конкуренцией за CPU, на корректность не влияет.

### 4. Защита эталона: PASS

```bash
.venv/bin/python import_gold.py --area solar --db /tmp/opencode/qa_gold.sqlite3
VINITI_DATA_DB=/tmp/opencode/qa_gold.sqlite3 .venv/bin/python -c \
  'import solar_offline; solar_offline.process("solar", "D001", "_corpus/area1/dev/D001.pdf")'
```

- **Импорт:** publications 21, materials 57, devices 89, layers 519, measurements 45, results 659, issues 53, всего 1443; 21 документ `parser_status=imported`.
- **Прямой вызов `process`:** `ValueError: D001: в базе эталон (parser_status=imported); извлечение заменит его - выберите отдельную базу` через 0.01 с. Проверка до разбора, PDF не парсился. Счётчики всех 7 таблиц до и после совпадают (659/57/89/519/45/21/53), у D001 остались 47 results.
- **Через batch:** на копии базы `batch.py --area solar --split dev --limit 1 --db ...` даёт `[1/1] D001 error: ValueError: ... в базе эталон ...`, `итог: ok 0, ошибок 1`, код 1; в базе по-прежнему 659 results, 53 issues, 21 `imported`.
- **Битый PDF на месте существующей публикации:** `process` падает с `ValueError: PDF повреждён, зашифрован или не может быть разобран.`, прежние 35 results D001 остаются на месте, `documents.parser_status` остаётся `ok`. То есть неудачный разбор не стирает прошлый результат.

### 5. Веб-API solar: PASS по конверту и CSV, FAIL по `/issues` (B1)

Сервер поднимался программно (`ThreadingHTTPServer` + `Handler`, как в `test_api.py`) на случайном порту с временными `papers.sqlite3`/`viniti.sqlite3`; PDF - `_corpus/area1/dev/D001.pdf`.

```bash
curl -s -X POST -H 'Content-Type: application/pdf' -H 'X-Track: solar' \
     -H 'X-Filename: D001.pdf' --data-binary @_corpus/area1/dev/D001.pdf \
     http://127.0.0.1:PORT/api/documents
curl -s http://127.0.0.1:PORT/api/documents/solar:d001-05df334d
curl -s 'http://127.0.0.1:PORT/api/documents/solar:d001-05df334d/results'
curl -s 'http://127.0.0.1:PORT/api/documents/solar:d001-05df334d/issues'
curl -s 'http://127.0.0.1:PORT/api/documents/solar:d001-05df334d/export.csv'
curl -s http://127.0.0.1:PORT/api/documents?track=solar
```

- **Загрузка:** 201; `status=done`, `publication_id=S05DF334D3465`, `handler_revision=2`, `run.source_revision=2`. По контракту ответ 201 - это summary без «тяжёлых» полей, счётчики есть в конверте.
- **Конверт:** `counts = {"results": 35, "issues": 1}`, `pages=7`, два предупреждения парсера. `warnings`, `issues_count` и `export.json` согласованы.
- **Результаты:** 35 строк; у всех 35 заполнены `source.page` и `source.fragment`; 28 строк с непустыми `device_id` и `measurement_id`, 7 пустых - все `result_origin=cited` (в эталоне cited-строки тоже без `device_id`, см. сценарий 1). `config_id`/`dataset_id` дублируют `device_id`/`measurement_id`, как задумано.
- **CSV:** 200, `text/csv; charset=utf-8`, BOM `EF BB BF`, 36 строк, 3236 байт, ожидаемые колонки.
- **Повторная загрузка тех же байт под другим именем:** 201, тот же `document_id`, в `solar_documents` одна строка. Дубля нет.
- **`/issues`:** FAIL, пустой список при `issues_count = 1` (B1).
- **Согласованность сайта и CSV:** на синтетическом PDF с таблицей (`test_solar_offline.make_pdf`) конверт сайта даёт `results=8, issues=0`, CLI в те же секунды даёт `results=8, issues=0`, наборы `device_id` совпадают. То есть заявленный в `AGENTS.md` общий экстрактор действительно один.
- **Пагинация:** `limit=100000` обрезается, `limit=abc`/`-5`/`offset=x` рвут соединение (B2).
- **Большой файл:** `_corpus/area1/test/P042.pdf` (25.1 МиБ, 10 страниц) принят (лимит 32 МиБ), разобран за 33.6 с, `status=done`, `results=26`, `issues=3`, `scanned=false`, 500 не было.
- **Список каталога:** `/api/documents?track=solar` возвращает 1 элемент, `/api/jobs?track=solar` - `state=done`; поиск по `q` находит по title, но не по авторам (B3).
- **`/sources`:** 35 элементов для D001; для D020 35 против 950 (B4).

### 6. Негативные случаи: PASS

API (`SolarStore.ingest` через HTTP-путь `documents_post`):

| Вход | Фактически | Ожидание | Вердикт |
|---|---|---|---|
| `b"not a pdf at all"` | 422, «Нужен PDF размером до 32 МиБ.» | 422/ValueError, без 500 | PASS (сообщение спорное, Q1) |
| PDF с одной картинкой (reportlab, без текста) | 422, «Нет текстового слоя. OCR в solar-1 пока не реализован.» | 422/ValueError, понятная причина | PASS |
| `Content-Type: text/plain` | 415, «Ожидается application/pdf.» | 415 | PASS |
| пустое тело, `Content-Length: 0` | 413, «Нужен PDF размером до 32 МиБ.» | 4xx | PASS |
| `b"%PDF-1.4 broken"` | `ValueError: PDF повреждён, зашифрован или не может быть разобран.` | ValueError/422 | PASS, в `solar_documents` 0 строк |
| `b"%PDF-1.4\n"` | то же | ValueError/422 | PASS |
| PDF-каталог без страниц | `ValueError: Поддерживаются PDF от 1 до 250 страниц.` | ValueError/422 | PASS |

CLI (`solar_offline.py`):

| Вход | Фактически | Вердикт |
|---|---|---|
| не-PDF файл | код 1, `notes.txt: ошибка: Файл не является PDF.`, CSV не написаны | PASS |
| PDF без текста | код 1, `scan.pdf: ошибка: Нет текстового слоя...`, CSV не написаны | PASS |
| пустой каталог `--pdf-dir` | код 2, `error: нет входных PDF` | PASS |
| несуществующий каталог | код 2, `error: нет каталога ...` | PASS |
| смешанный каталог без `--skip-errors` | код 1, ни одного CSV (частичных данных нет) | PASS |
| тот же каталог с `--skip-errors` | код 0, CSV только из валидного файла | PASS |
| два файла с одним stem | код 1, `повторяющийся publication_id`, CSV не написаны | PASS |
| все файлы битые с `--skip-errors` | код 0, 7 CSV с одним заголовком | FAIL (B5, известно) |

## Тесты

`.venv/bin/python -m unittest -v` -> `Ran 460 tests in 21.061s`, `OK (skipped=6)`. Прогнал как дополнительный сигнал, не как доказательство; все сценарии выше проверены отдельными командами. Зелёный прогон не отменяет B1-B4: конверт покрыт `test_solar_api.py`, а HTTP-маршруты `/issues`, `/sources` и поиск каталога - нет.

## Что проверить не удалось

- **RAG-область после merge.** `_corpus/rag` не распакован (`_corpus/` содержит только `area1`), поэтому `batch --area rag`, `score --area rag` и RAG-валидатор не запускались. Изменения в `batch.py` касаются только ветки solar, но регрессию RAG на реальных данных я не подтверждал.
- **`passport.py --check`.** Требует оба комплекта участников; без RAG-корпуса команда завершается `нет комплектов участников` (код 1). К логике solar отношения не имеет.
- **`pipeline.py` и `tools/gate.py` целиком.** Задание их не требовало; прогон `pipeline --area all` невозможен без RAG-корпуса, а `pipeline --area solar` дублирует уже проверенные batch/export/validator и добавляет 4 машинные `jv_consistency` (известное расхождение 0.3693 против 0.3699 из `docs/reviews`).
- **Браузерный UI.** Проверял JSON-эндпоинты и читал `static/document.js`/`static/catalog.js`; вживую вкладки и вёрстку не открывал. Вывод про B1 опирается на код `document.js:699` и фактические ответы API.
- **PDF с паролем и OCR.** PDF с паролем не строил; OCR не реализован по дизайну, проверял только отказ на PDF без текстового слоя.
- **Полный `test/` (90 PDF) через CLI/API.** Прогнал `dev` (21 PDF) полностью и один большой файл `test/P042.pdf` через API; весь `test/` не гонял из-за времени (один прогон `dev` - около 7 минут).

## Артефакты

Скрипты проверки (не в репозитории): `/tmp/opencode/qa_api.py`, `qa_catalog.py`, `qa_search.py`, `qa_consistency.py`, `qa_cli.py`, `qa_evidence.py`, `qa_sources.py`, `qa_limits.py`, `qa_large.py`. Выходы: `/tmp/opencode/validator_sub.log` (сдача), `/tmp/opencode/unittest.log`, `/tmp/opencode/qa_solar/`.

## Повторное ревью после b637838

- **Что проверялось:** исправления B1-B4 из раздела выше, кэш по ревизии (S2) и исключённая статья D006 (S3).
- **Важно про коммит:** пока шла проверка, `dev` ушёл вперёд. Начал на `b637838`, закончил на `b5a1427` (`fix(solar): хвосты повторного ревью`, 13:23). Все вердикты ниже получены на текущем `b5a1427` (дерево чистое); B1-B4 дополнительно проходили и на `b637838`. `b5a1427` меняет ровно две вещи в этой области: `status` теперь `partial` только для нечитаемой страницы (а не для статьи без чисел) и переразбор после смены ревизии пишется в ту же карточку каталога. Обе проверены отдельно.
- **Метод:** `server.py` поднят отдельным процессом на случайном порту с временными `papers.sqlite3`/`viniti.sqlite3`; запросы через `urllib`, PDF - `_corpus/area1/dev`. Скрипт `/tmp/opencode/qa_recheck.py` (47 проверок, 0 падений), логи сервера `/tmp/opencode/qa_recheck_server.log`.

### Итог повторного ревью

B1-B4, S2 и S3 подтверждены исправленными: `/issues` отдаёт настоящие замечания, битые `limit`/`offset` дают 400, поиск по авторам работает для solar и не сломан для RAG, `/sources` отдаёт объединённый список без дублей с настоящими `kind` и фильтром, кэш учитывает ревизию и не плодит карточки, D006 помечен `done` без ложных предупреждающих блоков.

Найдена одна новая регрессия данных, внесённая `b5a1427`: правка окна `bandgap` потеряла корректное значение D012 1.62 eV на четырёх материалах. Подробности в N1 ниже.

### B1. `/issues` для solar отдаёт замечания: PASS

Команды (после загрузки `D001.pdf` с `X-Track: solar`):

```bash
curl -s 'http://127.0.0.1:PORT/api/documents/solar:d001-05df334d/issues?limit=200'
curl -s 'http://127.0.0.1:PORT/api/documents/solar:d001-05df334d/export.json'
```

- **Факт:** `{"total": 1, "items": [{"issue_id": "I1", "check_name": "normalization", "record_ids": ["R029"], "source_page": "5", ...}]}`; список побайтно совпадает с `export.json["issues"]`. `issues_count` в конверте тоже 1.
- **Ожидание:** `total=1`, `issue_id=I1`, `record_ids=["R029"]`. Выполнено.
- **Вкладка `static/document.js`:** `renderIssues` (`static/document.js:698-730`) читает `data.total`, `data.items`, `issue.issue_id`, `issue.check_name`, `issue.description`, `issue.record_ids`. Эндпоинт отдаёт ровно эти поля, поэтому вкладка получает те же данные, что и `export.json`; прежнего расхождения «счётчик 1, вкладка пустая» больше нет.
- **Код:** `server.py:824-831` вместо литерала читает `envelope.get("issues", [])` и пагинирует (`default=200`, `maximum=500`), как RAG-ветка.

### B2. Битые `limit`/`offset` дают 400: PASS

| Запрос | Факт | Ожидание | Вердикт |
|---|---|---|---|
| `/results?limit=abc` | 400, `{"error": "Параметр limit должен быть целым числом."}` | 400 | PASS |
| `/issues?offset=-1` | 400, `{"error": "Параметр offset не может быть меньше 0."}` | 400 | PASS |
| `/sources?limit=0` | 400, `{"error": "Параметр limit не может быть меньше 1."}` | 400 | PASS |
| `/results?offset=x` | 400, `{"error": "Параметр offset должен быть целым числом."}` | 400 | PASS |
| `/issues?limit=0` | 400, `{"error": "Параметр limit не может быть меньше 1."}` | 400 | PASS |
| `/results?limit=1` | 200, `total=35`, один элемент | 200 | PASS |

Обрыва соединения (`http_code 000`) больше нет ни в одном случае; в логе сервера нет traceback на эти запросы. `server.py:818-836` оборачивает `_page`/`_int` в `try/except ValueError` и отдаёт `400`, как чтение таблиц.

### B3. Поиск каталога по авторам: PASS

```bash
# после загрузки D004.pdf
curl -s 'http://127.0.0.1:PORT/api/documents?track=solar&q=Vajjiravel'
curl -s 'http://127.0.0.1:PORT/api/documents?track=solar&q=Balamurugan'
curl -s 'http://127.0.0.1:PORT/api/documents?track=rag&q=smith'
```

- **Solar:** авторы в summary `"Vajjiravel Murugesan and Balamurugan Rathinam"`; `q=Vajjiravel`, `q=Balamurugan` и `q=vajjiravel` (регистр) дают `total=1` и именно карточку D004. Прежде было `total=0`.
- **RAG не сломан:** корпуса RAG нет, поэтому в `papers.sqlite3` (там `server.py main` держит `RagStore(args.db)`) вставлена синтетическая строка с `authors: ["Alice Smith", "Bob Jones"]`. `q=smith` и `q=Bob Jones` дают `total=1`. То есть ветка для списка авторов работает.
- **Код:** `server.py:667-675` разделяет строку solar и список RAG вместо `" ".join(...)` для обоих.
- **Попутно:** `rag.sqlite3` из `AGENTS.md` не используется при запуске через CLI: `main()` создаёт `RagStore(args.db)`, то есть RAG-карточки лежат в `papers.sqlite3`. Это расхождение документа и кода, на исправление B3 не влияет, но стоит знать при проверке кэша RAG.

### B4. `/sources`: без дублей, настоящие `kind`, фильтр работает: PASS

Проверены D020, D004, плюс синтетический PDF с формулой и подписью.

| Проверка | Факт | Ожидание | Вердикт |
|---|---|---|---|
| D020, дубли по `source_id` | первые 500 из 915 уникальны; полный список через `solar_api.solar_sources` - 915 id и 915 строк | без дублей | PASS |
| D020, `total` против конверта | 915 = результат-анкоры + `text_sources` без пересечений | совпадает | PASS |
| D020, `kind` | `caption`, `text` (как в конверте) | настоящие типы | PASS |
| D020, `?kind=caption` | 6 записей, все `caption` | находит | PASS |
| D004, дубли | первые 500 уникальны; `total=1250` = анкоры + `text_sources` | без дублей | PASS |
| D004, `kind` | `caption`, `equation`, `text` | включая `caption`/`equation` | PASS |
| D004, `?kind=caption` | 7 записей, все `caption` | находит | PASS |
| синтетика `Figure 1. ...` + `PCE = f(Voc, Jsc)` | `text_sources` kinds `caption`, `equation`, `text`; `?kind=equation` - 1, `?kind=caption` - 1 | оба типа | PASS |
| `?kind=nosuch` | `{"total": 0, "items": []}` | пусто | PASS |
| `?q=16.5` | 1 запись с фрагментом | фильтр по тексту | PASS |

- **Код:** `solar_api.solar_sources` (`solar_api.py:118-150`) объединяет анкоры результатов и `text_sources`, режет по `source_id`, сохраняет `kind`, затем фильтрует по `kind` и `q`; `server.py:832-842` вызывает его.
- **Оговорка:** HTTP-ответ ограничен `limit` (максимум 500), поэтому полный список из 1250 записей D004 проверялся ещё и прямой функцией на `export.json`; там дублей нет.

### S2 (кэш по ревизии): PASS

```python
store = solar_api.SolarStore(tmp / "solar.sqlite3")
first  = store.ingest("D001.pdf", pdf)          # 11.2 s
hit    = store.ingest("D001-copy.pdf", pdf)     # 0.00 s
# затем summary.handler_revision в базе принудительно = 1
again  = store.ingest("D001-renamed.pdf", pdf)  # 11.4 s
```

- **Факт:** попадание в кэш по `sha256` занимает 0.00 с и возвращает тот же `document_id`/`updated_at`. После подмены `summary.handler_revision` на 1 повторная загрузка тех же байт переразбирает документ: 11.4 с, `handler_revision=2`, `updated_at` изменился, `results_count=35`, в `solar_documents` одна строка, `document_id` тот же (`solar:d001-05df334d`), в базе снова revision 2.
- **Ожидание:** старый кэш не отдаётся, документ переразбирается. Выполнено.
- **Важно:** карточка сохраняется и при другом имени файла. В `b637838` этой части ещё не было: `document_id` считался из имени, поэтому переразбор `D001-renamed.pdf` создал бы вторую строку; `b5a1427` берёт `document_id` из найденной по хэшу записи. Проверено, что на `b5a1427` строка одна.
- **Код:** `solar_api.py:245-268` (`cached_id`, сравнение `handler_revision` с `solar_version.PARSER_VERSION`).

### S3 (D006 вне области): PASS

```bash
# после загрузки D006.pdf с X-Track: solar
curl -s http://127.0.0.1:PORT/api/documents/solar:d006-xxxxxxxx
```

- **Факт из JSON:** `status="done"`, `publication.in_scope="false"`, `publication.exclusion_reason="Моделирование или теория без изготовленных приборов."`, `counts={"results": 0, "issues": 0}`, `scanned=false`. В `warnings` есть `«Статья вне области исследования: Моделирование или теория без изготовленных приборов..»` (две точки: причина уже кончается точкой, код добавляет ещё одну; косметика).
- **Ложные блоки `static/document.js`:** повторил условия `noticeBlock` (`document.js:184-226`) на этом summary: `excluded=true`, поэтому «Обработано частично» не показывается (оно и так требует `status="partial"`), «Подтверждённых результатов не найдено» тоже подавлено (`!excluded`). Блок `warnings` покажет причину. `scopeBadge` (`document.js:155-165`) для строки `"false"` даёт «вне области: <причина>».
- **Ожидание:** `done`, `in_scope=false`, причина в warnings, без ложных блоков. Выполнено.
- **Семантика `partial` (b5a1427):** читаемая статья без чисел теперь `done` и получает только блок «Подтверждённых результатов не найдено» (проверено на синтетическом PDF: `status=done`, `in_scope=true`, блок показывается). Реально нечитаемая страница даёт `partial`: PDF из двух страниц (текст + картинка) вернул `status="partial"` и `warnings` с `«Страница 2: текстовый слой пуст или отсутствует.»`.

### Регрессия сдачи и прежние выводы

- **Сдача не изменилась:** полный прогон на `b5a1427` - 990 строк, 0 пропущено, `score.py` TOTAL F1 **0.3699** (все таблицы как раньше), валидатор `0 ошибок, 285 предупреждений, 613 цитат, 285 не найдено`. Шесть CSV из семи побайтно совпали с прогоном до `b5a1427`.
- **Что перестало воспроизводиться:** B1, B2, B3, B4 больше не воспроизводятся (исправлены). Q1 (сообщение на не-PDF) исправлен: `ingest` теперь различает `«Файл не является PDF.»` и `«Нужен PDF размером до 32 МиБ.»`.
- **B5 (CLI, код 0 при полном провале с `--skip-errors`) воспроизводится:** два битых PDF дают `итого: 0 строк, 2 PDF пропущено`, код 0 и 7 CSV с одним заголовком. Известный остаток, не входил в правки.
- **Q2 (лимиты) остаётся, но теперь описан в `AGENTS.md`:** `library.py`/`solar_api.py` - 32 МиБ, `solar_facts.py`/`server.PARSE_MAX_BYTES` - 40 МиБ, `rag_api.py` - 20 МиБ. Сведение к одному лимиту не делалось.
- **S1 (шум `fontTools`) остаётся:** в логе полного прогона те же сотни строк на PDF.
- **S2 из прошлого раздела (метаданные) остаётся:** в новом `publications.csv` у D001 пустой `year`, у D002 `authors="dm"` и `title` = колонтитул журнала, у D004 `title` начинается с `Citation:`.
- **Тесты:** `python -m unittest` - 468 тестов OK, 6 skipped (сигнал, не доказательство).

### N1. Новая регрессия в `b5a1427`: потерян корректный bandgap D012 (средне)

- **Где:** `solar_offline._bandgap` (`solar_offline.py:1209-1223`). Правка `b5a1427` ограничила окно поиска шума сравнения текущим предложением, но ищет `BANDGAP_NOISE` по всему лиду предложения. В D012 перед `band gap` стоит несвязанное `more than 104 cm-1`, и значение отбрасывается целиком.
- **Факт:** прогон на `b5a1427` даёт 21 материал с `bandgap_value` против 25 до правки; пропали 4 материала D012 (M1-M4) со значением 1.62 eV (было `bandgap_value=1.62`, `bandgap_unit=eV`, `bandgap_method=cited`, `note="Original bandgap_value: 1.62 eV"`, стало пусто). Публикации с bandgap: было D001, D004, D012, D021, D022, стало D001, D004, D021, D022.
- **Минимальный пример:**

```bash
.venv/bin/python - <<'PY'
import solar_offline
text = ("The electrochemical deposition synthesis increased the absorption of the CZTS "
        "film to more than 104 cm-1 with a band gap between 1.62 eV and 1.51 eV.")
print(solar_offline._bandgap({"1": text}, "CZTS"))   # ('', None, '') - значения нет
PY
```

На `b637838` тот же вызов возвращал `('1.62', '1', 'band gap of 1.62 eV')`. Само предложение корректно: 1.62 eV - это band gap CZTS, `more than 104 cm-1` относится к коэффициенту поглощения.
- **Влияние:** `score.py` этого не видит (materials сравниваются по id, не по значениям), F1 остаётся 0.3699; страдает содержание сдачи и карточка материала. Регрессия относительно заявленного в `docs/reviews` «D012 1.62 восстановлен, 25/54».
- **Почему тесты пропустили:** в `test_solar_offline` есть случаи `wide bandgap` и `band-gap`, но нет предложения, где сравнение (`more than ...`) стоит в одной фразе с настоящим bandgap. Предлагаю тест на эту строку D012.
- **Ожидание:** 1.62 eV сохраняется; шум сравнения должен проверяться рядом с числом или относительно самой фразы `band gap`, а не по всему предложению.

### Что не проверялось в этот раз

- RAG-корпус по-прежнему не распакован: живой RAG-поиск проверен на синтетической записи в `papers.sqlite3`, не на реальных конвертах.
- Браузер: вкладка замечаний проверена по коду `static/document.js` и по фактическому JSON, без запуска браузера.
- OCR, пароль на PDF, полный `test/` (90 PDF) - как и в первом разделе.
- `bandgap`-правка `b5a1427` проверена на `dev` целиком только по CSV; синтетические случаи из её описания (`above the bulk bandgap of CdS`) отдельно не перепроверял за пределами минимального примера.
