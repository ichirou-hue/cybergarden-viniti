# QA: `solar_offline` как основной экстрактор solar и исправления N-01/N-02

- **Дата:** 2026-11-21, повторная проверка после перевода solar на `solar_offline.process`.
- **Объект:** рабочее дерево `/home/ilya/Projects/cybergarden-viniti`; `solar_offline.py`, `batch.py`, `pipeline.py`, `solar_facts.py`, `test_solar_offline.py`, `test_batch.py`. Продакшен-код не менял, коммитов не делал.
- **Окружение:** Linux, Python 3.12.16 из `.venv`, `pypdf 6.21.4`, `pdfplumber 0.11.9`, `reportlab 4.4.9`; `fontTools` отсутствует. Корпус: `/home/ilya/Загрузки/Кейс5_область1_участникам/`; `_corpus/area1` - симлинк на него.
- **Источник истины по колонкам:** `submission_template/*.csv` и `tools/check_submission.py`.
- **Запуск:** везде `python -X utf8`; пути команд - от корня репозитория, если не указано иное.

## Итог

N-01 и N-02 исправлены и проверены вручную и тестами. `batch.py` по умолчанию ведёт solar через `solar_offline.process`: пишет все семь таблиц и конверт `documents`/`blocks`, jobs получают `extractor_revision = 2`. Импорт эталона защищён: `solar_offline.process` отказывается стирать `parser_status='imported'`, batch записывает job error, 659 results остаются на месте. `pipeline.py` полностью проходит solar/dev, экспорт и гейт дают `ошибок: 0`. CLI пишет BOM, `scan_direction` без reverse/forward остаётся пустым. Полный `unittest` - 408 тестов OK (6 skip).

Подтверждённых новых багов не нашёл. Есть два расхождения, которые требуют решения, а не однозначной правки:

- N-03: `export_csv` из **pipeline-базы** даёт TOTAL F1 0.3693, а не 0.3699; причина - 4 дополнительных `jv_consistency` issue, которые добавляет стадия `issues.generate`. Из batch-базы (`data/solar_offline.sqlite3`, 94 issues) получается ровно 0.3699, как у CLI.
- N-04: повтор `batch --limit 2 --resume` показывает `пропущено 2`, но из-за `--limit` обрабатывает следующие D003/D004. Если ожидалась полная идемпотентность повторного запуска, это не так; если `--limit` - лимит новой работы, поведение корректно.

## Статусы

| Пункт | Статус | Проверка |
|---|---|---|
| B-01. `--json` и `result.json` | **fixed** | `--help` обещает `<name>.json`; фактически `<stem>.json`, справка и факт совпадают. Поведение оставлено осознанно. |
| B-02. `--skip-errors` и `OSError` | **fixed** | PermissionError, отсутствующий файл и битый PDF пропускаются с кодом 0; `PdfminerException` из `build` заворачивается в `ValueError`. |
| B-03. `.PDF` в `--pdf-dir` | **fixed** | Каталог только с `ONLY.PDF` обработан (30 строк); смешанный `D001.pdf` + `D002.PDF` дал 104 строки. |
| B-04. Дубли `publication_id` | **fixed** | Без `--skip-errors` дубль даёт код 1 и сообщение, CSV не пишутся; с флагом первый обработан, второй пропущен, валидатор: `ошибок: 0`. |
| B-05. Сообщение для не-PDF | **fixed** | Разделены `Файл не является PDF.` и `Нужен PDF размером до 40 МиБ.`; CLI печатает `файл: ошибка: ...`, код 1 без traceback. |
| N-01. Битый первый файл с тем же stem | **fixed** | `seen.add` перенесён после успешного разбора. Вручную: битый `a/SAME.pdf` + валидный `b/SAME.pdf` с `--skip-errors` дают 30 строк, 1 пропуск, код 0; в `publications.csv` 2 строки. Тест `test_broken_first_file_does_not_block_a_valid_namesake` проходит. |
| N-02. Несуществующий `--pdf-dir` | **fixed** | Вручную: `--pdf-dir /tmp/opencode/qa3/nope` даёт код 2 и `error: нет каталога ...`; файл вместо каталога - то же. Тест `test_missing_directory_is_a_clean_error` проходит. |
| N-03. Score из pipeline-базы | **open / вопрос** | Нужно подтвердить, относится ли ожидание 0.3699 к pipeline-базе или к batch-базе. На batch-базе 0.3699, на pipeline-базе 0.3693 из-за 4 дополнительных issues. |
| N-04. `--limit` + `--resume` | **open / вопрос** | Повтор с `--limit 2 --resume` пропускает готовые 2, но берёт следующие 2. Если это лимит новой работы, всё верно; если ожидался no-op, поведение не совпадает. |

## Проверенные сценарии

| № | Сценарий | Команда | Ожидалось | Фактически | Вердикт |
|---|---|---|---|---|---|
| 1 | Batch, 2 публикации | `db.py --init --db /tmp/opencode/qa3/batchcheck/solar.sqlite3`; `batch.py --area solar --split dev --db ... --limit 2` | 2 ok; все 7 таблиц; documents/blocks; jobs revision 2 | `ok 2, ошибок 0`; publications 2, materials 7, devices 7, layers 35, measurements 3, results 49, issues 1; documents 2, blocks 7+7; jobs `extractor_revision=2`; `documents.parser_version=2`, `parser_status=ok` | ✅ |
| 1a | Повтор с `--resume` | та же команда + `--resume` | Пропускает готовые | `к запуску 2, пропущено 2`; D001/D002 пропущены, D003/D004 обработаны (N-04) | ⚠️ вопрос N-04 |
| 2 | Эталон не перезаписывается | `import_gold.py --area solar --db /tmp/opencode/qa3/gold/solar.sqlite3`; `batch.py ... --limit 1` | 659 results; batch error «эталон»; данные не тронуты | import: `results=659`, 21 imported document; batch: `[1/1] D001 error: ValueError: ... в базе эталон ...`, код 1; после: results 659, D001 rows 1/6/6/30/4/47/5 | ✅ |
| 3 | Pipeline, весь solar/dev | `pipeline.py --area solar --split dev --db /tmp/opencode/qa3/pipeline/solar.sqlite3 --out /tmp/opencode/qa3/pipeline/out` | 21 ok; экспорт; гейт `ошибок: 0` | 21 ok, 0 ошибок; export publications 21, materials 54, devices 54, layers 231, measurements 17, results 519, issues 98; gate `clean but needs review`, errors 0, review 288; exit 0 | ✅ |
| 4 | `export_csv` + `score`, batch DB | `export_csv.py --area solar --db data/solar_offline.sqlite3 --out /tmp/opencode/qa3/export-batch`; `score.py --area solar --pred ... --ref .../dev_gold` | TOTAL F1 0.3699 | 0.3699; issues 94 | ✅ |
| 4a | `export_csv` + `score`, pipeline DB | то же с `/tmp/opencode/qa3/pipeline/solar.sqlite3` | 0.3699, как у CLI | 0.3693; issues 98 (на 4 больше) | ⚠️ N-03 |
| 5 | CLI BOM и повтор | `solar_offline.py D001.pdf --out /tmp/opencode/qa3/cli --json`; повтор | BOM EF BB BF, повтор перезаписывает | Во всех 7 CSV первые 3 байта `EF BB BF`; 7 CSV + `D001.json`; SHA-256 всех файлов при повторе совпали | ✅ |
| 6 | `scan_direction` | синтетический PDF с таблицей без reverse/forward, затем с reverse и с forward | Пусто без слова; reverse/forward при наличии | `none -> ''`, `reverse -> 'reverse'`, `forward -> 'forward'` | ✅ |
| 7 | Идемпотентность process | два `batch --limit 1` на одной свежей базе без `--resume` | Счётчики не растут | publications 1, materials 5, devices 5, layers 25, measurements 2, results 35, issues 1; documents 1, blocks 7; jobs attempts 2 | ✅ |
| 8 | Fallback старого филлера | `batch.py --area solar --limit 1 --processor extract:process --extractor-revision 1` | Старый путь, revision 1 | job `extractor_revision=1`, document `parser_version=1`, results 28, остальные шесть таблиц пусты | ✅ |
| 9 | Полный unittest | `.venv/bin/python -X utf8 -m unittest` | Все тесты OK | `Ran 408 tests in 19.628s`, `OK (skipped=6)` | ✅ |
| 10 | CSV-колонки | сравнение с `submission_template/*.csv` | Заголовки и число колонок как в шаблоне | Все 7 файлов: заголовки совпадают, колонки 7/11/13/13/16/27/7, `None`/`nan`/`NaN` нет | ✅ |
| 11 | D006 в pipeline DB | проверка `documents`/таблиц D006 | `in_scope=false`, причина, пустые предметные таблицы | `parser_status=ok`, `parser_version=2`, 10 blocks; publications 1, остальные 0; причина «Моделирование или теория без изготовленных приборов.» | ✅ |
| 12 | BOM в `export_csv` | `export_csv.py` на batch DB | BOM | Все 7 CSV начинаются с `EF BB BF` | ✅ |

## Наблюдения

- **Предупреждения валидатора.** Pipeline-гейт: `ошибок: 0`, `review 288`; предупреждения о цитатах не структурные. Одиночный D001: `ошибок: 0`, 3 предупреждения.
- **`pipeline` добавляет issues.** На batch-базе 94 issues, после `issues.generate` - 98 (4 `jv_consistency`). Валидатор это принимает, score меняется с 0.3699 на 0.3693.
- **`--resume` и `--limit`.** На полной базе `--resume --limit 2 --dry-run` даёт `к запуску 0, пропущено 21`; на базе с первыми двумя - `к запуску 2, пропущено 2`. Это ожидаемо для лимита новой работы, но не для «повтор ничего не делает».
- **BOM.** CLI и `export_csv` теперь пишут BOM; `check_submission.py` открывает `utf-8-sig` и ошибок не даёт.
- **Шум `fontTools` в stderr** сохраняется (около 100 строк на один PDF); результаты и коды не страдают.
- **Метаданные.** D006 title склеен без пробелов, у D001/D002 пустой year; на гейт не влияет.

## Что не проверено

- RAG-пайплайн после переключения solar не прогонялся на реальном корпусе; диспетчеризация покрыта `test_batch.py`, RAG остаётся на `extract.process`.
- Полный `test/` (90 PDF) не прогонялся; проверены `dev` (21 PDF), batch/process, pipeline, CLI и полный unittest.
- Windows-специфичное поведение (BOM в Excel, кодировка консоли) не запускалось.
- OCR и научная полнота извлечения не оценивались.
