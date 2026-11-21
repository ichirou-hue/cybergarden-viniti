# ADR-0003: Отдельный OpenRouter vision-проход по Docling crops

- Статус: Предложен
- Дата: 2026-11-21

## Контекст

Даниил запросил извлечение данных графиков через API вместо обязательной загрузки локальной 4B-модели. «Доставать всё» означает пытаться обработать все доступные изображения и явно учитывать пропуски, а не гарантировать восстановление всех чисел.

**Реализовано:** `docling_source.py` сохраняет figures с `id/image/caption/page/bbox`, crops и предупреждениями; координаты приведены к TOPLEFT. `extract_document.py --charts` включает исключительно локальный chart2csv. `extraction_core.py` переносит `parser_output` в `result.json` и `parser.json`, атомарно публикует комплект без перезаписи; визуальные кандидаты не становятся solar-записями. Источники могут быть пропущены адаптером с предупреждением; предел экспорта — 32 МиБ. Подробности — в `docs/2026-11-20-docling-adapter-guide.md`.

`solar_extract.complete` уже сериализует произвольные JSON messages, поэтому пригоден для multimodal content без нового транспорта. Есть фиксированный HTTPS endpoint, `NoRedirect`, timeout, 0–3 повтора, лимит ответа **8 МиБ**, проверка `finish_reason == stop`, JSON parsing. Это не проверка схемы chart-ответа и не подтверждение поддержки vision конкретной моделью. `response_format=json_object` не обеспечивает JSON Schema.

**Предложено ниже, ещё не реализовано:** vision stage, его CLI, контракт и fault policy. Ключ/vision-модель не настроены; живой API-прогон и точность не проверены. UI, backend и семь solar CSV вне изменения. ADR дополняет изоляцию extraction из ADR-0002, не заменяет его.

## Варианты

### A. Оставить как есть: native `--charts`

Нет стоимости изменений и регрессий; offline после загрузки весов. Но существенные память/загрузка, качество не измерено, запрос на API не закрыт.

### B. Отдельный CLI по сохранённому bundle — выбран

Минимальная новая стадия с подменяемым клиентом, без повторного OCR при API retry. Небольшая стоимость реализации, легко тестировать и удалить без изменения parser/solar. API даёт задержки, расходы и передачу изображений провайдеру; качество не гарантировано.

### C. Встроить vision непосредственно в Docling conversion

Один запуск удобнее, но связывает длительный OCR с сетевыми сбоями, усложняет повтор и публикацию результата. Больше регрессионный риск; общего framework, очереди и нового UI для этого не нужно.

## Решение

### Граница и запуск

Рекомендуемый будущий интерфейс: `extract_charts.py BUNDLE -o NEW_DIR`. Вход — существующий Docling bundle (`result.json`, `parser.json`, `assets/figures/`); оригинал только читается. Стадия не импортирует Docling для конверсии и не запускает OCR. `--charts` и текущие режимы сохраняют прежний смысл, автоматического API fallback нет.

Обязательны тот же `OPENROUTER_API_KEY` и отдельный **`OPENROUTER_VISION_MODEL`**, без default и без наследования текстового `OPENROUTER_MODEL`. Явный запуск означает передачу crops и подписей внешнему API; это предупреждение должно быть в help. По одному multimodal-запросу на каждый figure: системная инструкция, caption как недоверенные данные, PNG как `image_url` с data URL. Переиспользовать `complete`, внедрять клиент в тестах.

### Рекомендуемый контракт `vision-charts-1`

Новый каталог содержит `vision.json` и отдельный `chart_candidates.csv`, **не** `results.csv` и не gold. JSON — основной, CSV — проекция точек с идентификаторами figure/panel/series и ссылкой на JSON.

| Уровень | Поля и смысл |
|---|---|
| Run | `schema_version`, `document` (publication_id, PDF sha256), `input_bundle` (путь, хеши result/parser), `model`, `prompt_revision`, `status`, `coverage`, `limitations`, `figures` |
| Figure | `figure_id`, `source`, `status`, `kind: chart/non_chart/unknown`, `panels`, `limitations`, `error` |
| Source | Точная копия локальных `image/caption/page/bbox/bbox_scope`, `coord_origin=TOPLEFT`, SHA-256 crop; bbox относится ко всему figure, не к точке |
| Panel | `panel_id`, `label_raw`, `axes`, `series`, `limitations` |
| Axis | `axis_id`, `role: x/y/other`, `label_raw`, `unit_raw`, `scale: linear/log/unknown`, `ticks_raw` |
| Series | `series_id`, `label_raw`, `x_axis_id`, `y_axis_id`, `points`, `limitations` |
| Point | `point_id`, `x_raw`, `y_raw`, `x_reading`, `y_reading` (`printed/estimated/unknown`), `uncertainty`, `review_status=unverified` |

Неизвестные значения — JSON `null`, enum неопределённости — `unknown`; CSV null — пустая ячейка. Числа и единицы сохраняются строками, без нормализации и достраивания кривой. `printed` — чтение явной подписи значения, не вывод по делениям; визуальная оценка координаты — `estimated`. `uncertainty` различает напечатанные error bars (`x_raw/y_raw`, тип, если известен) и неопределённость визуального чтения (`reading_note`); отсутствие оценки не означает нулевую погрешность.

Модель возвращает только содержимое анализа; источник и технические ID привязывает приложение. Caption/page/bbox нельзя переписывать модельным ответом. Неизвестные шкалы, легенды, перекрытые серии, отсутствующие подписи и неразрешимые панели перечисляются в limitations. Все кандидаты, включая printed, остаются `unverified`; проверка JSON не повышает этот статус.

Технические проверки схемы, типов, конечных чисел, ссылок осей/серий и бюджета — в коде. Пользователь не пишет код/имена функций правил в profile: содержательная проверка согласованности — LLM-проверка внутри системы с замечаниями и источниками, не доказательство истинности и не автокоррекция raw. Новый vision stage не расширяет profile DSL и не меняет существующие solar validators.

### Бюджеты и fault policy

- До API проверить bundle, согласованность `parser.json` с `result.parser_output`, безопасные пути внутри bundle, декодирование PNG, размеры/число пикселей, число figures и суммарный размер запросов **с base64**. Бюджеты input bytes, figures, output tokens, timeout и retries конечны и явно отражаются в run. Превышение общего бюджета — отказ до сети, не первые N изображений.
- Все figures получают запись; локального фильтра «не похоже на график» нет. `coverage` содержит total, attempted, succeeded, failed, skipped и причины, исходные предупреждения парсера; `completeness=not_measured` всегда. Ноль figures означает `no_figures`, не «в PDF нет графиков».
- Повреждённый/отсутствующий crop: `status=failed`, `error.code=invalid_crop`; его не отправлять, остальные допустимо обработать. Невалидный bundle целиком — остановка до API.
- Сетевые повторы только существующей политикой `complete`: 429/500/502/503/504 и сетевые ошибки, максимум `1 + retries` попыток. Не повторять 400/401/403, redirect, неправильную схему, усечённый или слишком большой ответ; не подменять модель/native-режимом. 401/403 прекращают run, оставшиеся figures получают `skipped` с причиной.
- Невалидный JSON/schema или `finish_reason != stop` отклоняет **весь ответ данного figure**, частичные точки не принимаются. После прочих локальных/API ошибок допустимо продолжить остальные figures; повтор запуска читает тот же bundle, никогда не повторяет OCR.
- Новый результат публикуется атомарно без перезаписи. `status=completed` и exit 0 допустимы только при отсутствии failed/skipped; `partial/failed` дают ненулевой exit и явную диагностику. Успешные кандидаты можно сохранить в partial-комплекте с этим статусом также в CSV; ошибка не маскируется пустым успешным CSV. `unreadable` или неполный анализ отражаются в limitations даже при технически успешном ответе.
- Исходный parser bundle не изменяется при любом исходе. Логи/ошибки не содержат ключ, request body, base64 или provider response body. Ошибка публикации — ненулевой exit без видимого неполного каталога.

## Последствия

Повтор vision отделён от дорогого OCR, native-путь обратимо сохранён. Цена — отдельный артефакт, API-расходы и ручная сверка. Мелкие подписи, логарифмические шкалы и смешанные панели особенно рискованны; охват всех crops не равен охвату всех рисунков PDF или всех точек непрерывной кривой. Автоперенос в solar gold/готовые метрики запрещён.

План developer:

1. Добавить небольшой reader/validator bundle, builder multimodal messages и валидатор chart-контракта с injected client.
2. Добавить отдельный CLI и атомарный sidecar export; сохранить неизменность старого parser и `--charts`.
3. Покрыть fake-client тестами budgets/all-figures, invalid crop/path, multi-panel/log/unknown, response limit/truncation, API retries/auth failure, partial exit и no-overwrite.
4. После настройки модели/ключа и разрешения передачи выполнить ограниченную ручную проверку dev-crops, включая printed/estimated и ошибки; записать ограничения и затраты. До неё не заявлять качество или полноту.
