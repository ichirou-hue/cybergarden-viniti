# Ревью: перенос параллельной работы на новую раскладку (чат, OpenAI-совместимый провайдер)

- Дерево: `/home/ilya/Projects/cybergarden-viniti`, незакоммиченные правки поверх `origin/dev` (`7c4f7a5`).
- Предмет: delta рабочего дерева (`git diff origin/dev`) плюс untracked `static/chat-api.js`,
  `static/chat.js`, `static/chat-doc.js`, `static/chat-screen.js`, `static/markdown.js`.
- Роль: reviewer. Код не менял, тесты и UI-харнессы запускал.

## Контекст правки

HEAD уже содержит рефакторинг в `src/`-раскладку, серверный чат (`src/viniti/chat.py`,
маршруты `/api/chat`), `obsidian`, `pdf_quality`, universal-профиль. В рабочем дереве лежит
параллельная работа, перенесённая на эту раскладку: фронтенд научного чата, поддержка любого
OpenAI-совместимого провайдера в `llm_provider` и его потребителях (`profile_assistant`,
`solar_extract`, `vision_source`, `extract_document`, `server`), правки `profile.mjs`,
обновлённые тесты и селекторы `tools/uitest`.

Отдельно проверял именно риск переноса: потерянные функции upstream, битые импорты, обращения
к несуществующим полям и маршрутам, контракт `chat-api.js` <-> `chat.py`/`server.py`.

## Как проверял

- `git diff origin/dev` по всем 27 файлам, untracked `static/chat*.js` и `static/markdown.js`
  прочитаны целиком; `src/viniti/chat.py` и chat-маршруты `server.py` прочитаны как контракт.
- `.venv/bin/python -m unittest -q`: 967 тестов, OK, 4 skipped (совпадает с ожиданием).
- `node --check` для всех `static/*.js` и `static/*.mjs`: без ошибок.
- `tools/mock_api.py --port 8799` + `cd tools/uitest && node harness.mjs`: 41/41.
- `node tools/uitest/profile-assistant.mjs` (jsdom): проходит.
- Дополнительно одноразовый jsdom-прогон `#/chat` против мока (не коммитил): экран строится,
  список документов, composer, демо-ответ и кликабельная цитата работают в режимах
  `Одна статья` и `Вся библиотека`.
- Сверка `llm_provider.complete_env` с реальным поведением проверена запуском с подменённым
  транспортом; ключи и `.env` в отчёт не переносятся.

## Вердикт

**Нужны правки.** Потерянных при переносе функций и битых импортов не нашёл: `profile.mjs`
сохранил все функции upstream (`validateTypes` встроен в `readProfile`), `deadline` в `awaitJob`
совпадает с upstream построчно, все импорты новых `chat*.js` резолвятся. Блокер один: выбор
endpoint в `complete_env` уводит ключ OpenRouter на `OPENAI_BASE_URL` (в `.env.example` он
прописан), из-за чего ломается рабочий путь solar-extract и ключ уходит чужому хосту.
Остальное - контрактные расхождения фронтенда чата и незакрытые тестовые пробелы.

## 🔴 Блокирует

### B1. `src/viniti/llm_provider.py:556-570` - `complete_env` отправляет ключ OpenRouter на `OPENAI_BASE_URL`

`complete_env` всегда передаёт `base_url=config["base_url"]`, а `config_from_env`
(`llm_provider.py:228-250`) берёт `OPENAI_BASE_URL`, если он задан, независимо от того, что
ключ пришёл из `OPENROUTER_API_KEY` (`source == "openrouter"`). `legacy_endpoint`
(`llm_provider.py:394-420`) при явном `base_url` не смотрит на `source`.

Проверено запуском: при `OPENROUTER_API_KEY` + `OPENAI_BASE_URL=https://api.openai.com/v1`
запрос уходит на `https://api.openai.com/v1/chat/completions`. До правки
`solar_extract`/`extract_document`/`vision_source` вызывали `complete(api_key=..., model=...)`,
и `legacy_endpoint` при явных учётных данных всегда выбирал OpenRouter `ENDPOINT`. То есть это
регрессия, а не только унаследованная особенность `chat()`.

В `.env.example:16` значение `OPENAI_BASE_URL=https://api.openai.com/v1` прописано по умолчанию.
Сценарий «скопировал `.env.example`, заполнил только `OPENROUTER_*`» (он же описан в README как
валидный fallback) теперь отправляет ключ OpenRouter в OpenAI: 401 и ключ у чужого провайдера.
Затронуты `solar-extract`, `extract-document`, помощник профиля и vision.

Что сделать: строить `base_url` в `config_from_env`/`complete_env` по источнику ключа
(`OPENAI_BASE_URL` только при `source == "openai"`, иначе `OPENROUTER_BASE_URL`/`ENDPOINT`),
либо оставить `OPENAI_BASE_URL` пустым в `.env.example`. Добавить тест на пару
«ключ OpenRouter + заданный OPENAI_BASE_URL»: сейчас `test_complete_env_follows_configured_base_url`
проверяет только случай `OPENAI_API_KEY`.

## 🟡 Стоит исправить

### 1. `src/viniti/llm_provider.py:525, 310-311` - `OPENAI_JSON_MODE` действует только на `chat()`

Флаг прокинут в `config_from_env` и читается только в `chat()` (`llm_provider.py:310`).
`complete()` по-прежнему жёстко ставит `"response_format": {"type": "json_object"}`
(`llm_provider.py:525`), а через него идут `vision_source`, `solar_extract`, `extract_document`,
`profile_assistant`. При этом `.env.example:10-11` ставит комментарий про `response_format`
сразу под vision-настройками, то есть обещает ровно тот путь, который флаг не покрывает.
Проверено: при `OPENAI_JSON_MODE=off` `complete_env` всё равно отправляет `response_format`.

Что сделать: прокинуть `json_mode` в `complete()` (параметр или чтение конфига) и покрыть
тестом хотя бы `complete_env` с vision-сообщением; либо перенести комментарий, если флаг
задуман только для чата.

### 2. `static/chat-api.js:200`, `static/chat.js:18` - клик по цитате standalone-разбора (`parse:`) ведёт на 404

`backendCitation` обнуляет `source_id` у `parse:`, но `citationFrom` (`chat-api.js:116`) уже
положил сам объект источника в `citation.source`, поэтому `openCitation` (`chat.js:18`) идёт
в ветку `openEvidence`, а не в `pdf_url`.

`openEvidence` строит `preview.src = /api/documents/<parse:...>/anchors/<id>.png` и
`pdfLink.href = /api/documents/<parse:...>/pdf#page=`, а маршрут `/api/documents/*` для
`parse:` отвечает 404 (`server.py:1765-1775`: `document_track` видит `rag`, владельца нет).
Комментарий в `chat-api.js:198-199` («ссылку на PDF отдаёт сервер в `pdf_url`») не выполняется.

Путь достижим: `chat_documents` в режиме `library` берёт `workspace_items()`, а тот добавляет
завершённые standalone-разборы как `parse:<id>` (`server.py:1401-1402`), `load_owned_envelope`
их разрешает (`server.py:1354-1366`). Ответ библиотеки может содержать цитату `parse:`, и её
кнопка откроет диалог с битым превью и битым fallback-PDF.

Что сделать: при `document_id.startsWith("parse:")` очищать и `citation.source`, чтобы
сработала ветка `pdf_url` (или в `openCitation` сначала проверять `pdf_url`). Заодно добавить
`parse:`-цитату в jsdom-проверку.

### 3. `static/chat-api.js:103-118, 205-252` - `provenance` теряется, распознанные блоки выглядят как проверенный текст статьи

`chat._validate_citations` отдаёт `provenance` (`pdf_text`/`recognized`) именно для того, чтобы
потребитель различал буквальную цитату PDF и текст модели (`chat.py:278-292`, тест
`test_recognized_source_is_available_with_explicit_provenance`). `citationFrom` поле не
переносит, `normalizeBackend` тоже, поэтому `messageNode`/`citationList` показывают
`recognized`-цитату так же, как проверенную, с номером страницы.

Именно для universal-разбора (`parse:` через Docling) и для solar-карточек с
`recognized_sources` это единственный сигнал о том, что цитата не буквальная. По правилам
`AGENTS.md` vision-текст нельзя подавать как подтверждённый факт без пометки.

Что сделать: перенести `provenance` в объект цитаты и показать бейдж «распознано моделью,
сверьте с PDF» для `recognized`.

### 4. `static/chat-screen.js:18-28, 61-69` и `server.py:1615-1649` - история сервера не восстанавливается в UI

Сервер хранит диалог по `(user, mode, context_key)` и отдаёт его `GET /api/chat`
(`chat_get`), но фронтенд этот маршрут не вызывает: история живёт только в module-level
`Map` в `chat-screen.js`/`chat-doc.js`. После перезагрузки страницы чат выглядит пустым, а
модель продолжает получать прошлые ходы из базы (`chat_post` -> `store.history` -> `_history_for_model`).
Пользователь видит «новый» диалог, но ответ учитывает прежние вопросы.

Дополнительно `chat-doc.js:14` и `chat-screen.js` используют один и тот же серверный ключ
`doc:<id>` для одного документа, поэтому вопросы, заданные на странице статьи, попадают в
историю модели для экрана «Одна статья» и наоборот, хотя локально это разные сессии.

Что сделать: либо грузить историю через `GET /api/chat` при монтировании и рендерить её
(маршрут уже есть, включая `context_label`), либо не хранить серверную историю для этого UI.
Сейчас это скрытое состояние, о котором интерфейс не сообщает.

### 5. `static/chat-screen.js:410-417` - «Очистить» не сбрасывает историю, если выбрано больше 12 статей

`send()` режет выборку до 12 (`chat-api.js:320`) и отправляет `mode=selection` с 12 id, а
`reset()` отправляет `documentIds: [...state.selected]` целиком. Сервер отвечает 400
(`server.py:1427-1428`), ошибка глотается `.catch(() => {})`, серверная история остаётся.
UI при этом позволяет отметить больше 12 статей и предупреждает только в `note` после ответа.

Что сделать: в `reset()` резать тот же набор, что уходил в `askLibrary`, или ограничить выбор
12 статьями в интерфейсе.

### 6. `static/chat-screen.js:229-231`, `static/chat.js:110-118, 151`, `src/viniti/chat.py:233-242` - «общий ответ модели» обещан, но недостижим

Пустой экран библиотеки обещает: «Если вопрос не о библиотеке, общий ответ модели будет
помечен отдельно». Бэкенд это умеет (`mode=general`, `general_knowledge`, отдельный
system-промпт), в `chat.js` есть `modelBlock`/`model_note`, но ни один вызов не отправляет
`mode=general`, а `normalizeBackend` никогда не заполняет `model_note`. В результате при
вопросе вне библиотеки пользователь получит `not_found`, а не помеченный общий ответ.

Что сделать: либо реализовать отправку `mode=general` (например, после `not_found` по кнопке),
либо убрать обещание из текста и мёртвые ветки, чтобы не обещать несуществующее поведение.

### 7. `src/viniti/llm_provider.py:77, 81, 439, 467` - лимит изображения недостижим из-за общего лимита

`MAX_IMAGE_URL_CHARS = 4 МБ`, но общий `MAX_CONTENT_CHARS = 2 000 000` считается по сумме
текста и `data:`-URL. То есть изображение больше ~1,5 МБ (в base64) отклоняется раньше, чем
сработает собственный лимит, с сообщением «Запрос к модели превышает допустимый размер» -
без указания на картинку. Vision как раз нужен страницам без текстового слоя, то есть сканам,
где PNG тяжелее. На `_corpus/area1/dev` страницы дают 1,1-1,4 млн символов base64, то есть
запас небольшой.

Что сделать: считать бюджет изображений отдельно от текстового (или поднять общий лимит для
`complete` с картинками) и назвать причину в ошибке.

### 8. Тестовые пробелы вокруг новой функциональности

- Нет ни одной проверки `static/chat*.js` в `tools/uitest` (`harness.mjs` покрывает каталог,
  карточку, паспорт). Находки 2-6 выше ловятся одним jsdom-сценарием чата.
- `profile.mjs` прибавил ~900 строк (библиотека профилей, `saveProfile`/`copyProfile`/
  `deleteProfile`, `/api/profiles`), проверок на это нет; `profile-assistant.mjs` дергает
  только карточки помощника.
- Нет теста на выбор vision-модели по `OPENAI_VISION_MODEL`/`OPENROUTER_VISION_MODEL` и на
  серверный 503 в `parse_post` с новыми именами переменных (`server.py:807-812`).
- `test_multimodal_content_parts_are_accepted` проверяет крошечную картинку; граничное
  поведение лимита (находка 7) не покрыто.

Что сделать: добавить `tools/uitest/chat.mjs` (mock 404 -> демо-бейдж, клик по цитате,
`#/chat?scope=library`) и пару unit-тестов на профили и vision-конфиг.

## ⚪ Мелочи и вкусовое

- `src/viniti/solar_extract.py:14-15`: `ENDPOINT` и `NoRedirect` импортированы, но больше не
  используются (встречаются в файле по одному разу, только в импорте).
- `src/viniti/profile_assistant.py:276-283`: `config_from_env()` вызывается до `complete_fn`,
  поэтому даже тестовый шов с подменённой моделью требует настроенного окружения. Достаточно
  проверять конфиг только когда `complete_fn is complete_env`.
- `static/chat-api.js:271-301, 316-366`: параметры `history` и `signal` в `askArticle`/
  `askLibrary`/`documentsForLibrary` не используются, `historyOf()` в `chat-screen.js`/`chat-doc.js`
  считается и выбрасывается. Либо задействовать (связано с находкой 4), либо убрать.
- `static/chat-screen.js:406-417`: в режиме `single` без выбранной статьи `reset()` шлёт
  `DELETE /api/chat?mode=document` без `document_id`, сервер отвечает 400, ошибка глотается.
  Мелочь, но в консоли сети будет регулярный 400.
- `static/chat-screen.js:96-98, 130-138`: если один трек упал, а второй пуст, показывается
  «Библиотека пуста» и сообщение об ошибке трека теряется.
- `static/chat-screen.js:349-364`: отмена запроса и последующий «Повторить» оставляют на
  сервере и первый (невидимый) ответ, и повторный вопрос: в истории модели будет два `user`
  подряд. Это следствие abort, но стоит знать.
- `static/profile.mjs:1185-1310`: карточки не читают серверные `duplicate`, `requires_copy` и
  `audit`: дубликат определяется заново по форме, встроенный профиль блокируется локально,
  «Проверить дубли» делает клиентскую проверку и не зовёт `/api/profile/assist` с `mode=audit`.
  Поведение корректное, но серверный аудит для UI остаётся мёртвым кодом.
- `static/chat-api.js:135-148`: `sourceCache` живёт до перезагрузки вкладки; после повторной
  загрузки PDF с новым `PARSER_VERSION` чат в текущей вкладке может видеть старые `/sources`.
- `tests/test_obsidian.py:44-49`: архив теперь не закрывается (для `BytesIO` это только GC,
  но `names()` можно переписать на `with` и возвращать данные).
- `static/solar.html` всё ещё в репозитории и грузит `/profile.mjs`, хотя маршрут
  `/solar.html` перекрыт на `manual-profile.html` (`server.py:384`). Файл мёртвый; если его
  когда-нибудь отдадут напрямую, `profile.mjs` упадёт на отсутствующих id (`profilePanel`,
  `profileId` и т.д.). Лучше удалить, как и обещано в `docs/reviews/2026-11-21-manual-profile-restore.md`.

## Проверено, претензий нет

- Перенос `profile.mjs` не потерял функции upstream: `sessionReady`, `resumeJob`,
  `restoreEntry`, `rememberedJob`, `handoffFailed`, `releasePending`, `stashProfilePDF`,
  `peekProfilePDF`, `pagehide` на месте; `validateTypes` встроен в `readProfile`.
  `awaitJob` с `deadline = Date.now() + 35 * 60 * 1000` совпадает с upstream построчно;
  `JOB_TTL_MS` согласован с серверным лимитом разбора.
- Все `byId(...)` в `profile.mjs` (39 id) есть в `manual-profile.html`; в `chat*.js`,
  `document.js`, `passport.js`, `ui.js` недостающих id в `index.html` нет.
- Новые `chat*.js` импортируют только существующие модули (`api.js`, `dom.js`, `ui.js`,
  `evidence.js`); динамические импорты `chat-doc.js` и `markdown.js` резолвятся, экспорты
  совпадают (`renderDocumentChat`, `passportMarkdown`, `markdownFilename`).
- Контракт цитат: `citations[{ref, quote, page, document_id, source_id, pdf_url, anchor_url}]`
  совпадает; `contradictions` корректно переводятся из `{topic, refs, explanation}` в
  `{metric, note, citations}` и режутся по числу известных refs; `not_found`/`unverified`
  отображаются отдельными состояниями, а не как доказанный ответ.
- Лимит 12 статей соблюдён и на сервере (`chat.MAX_DOCUMENTS`), и в `askLibrary`.
- CSRF и аутентификация: `postJSON`/`deleteJSON` шлют `X-CSRF-Token`, сервер требует
  `require_user()` для POST/DELETE и проверяет владельца документа (`load_owned_envelope`)
  с одинаковым 404 для чужих; `GET /api/chat` логинится без CSRF.
- Демо-фолбэк в `chat-api.js` не выдумывает источники: цитаты берутся из
  `/api/documents/<id>/sources`, `visual_unverified`/`requires_review` отфильтрованы, каждый
  демо-ответ несёт бейдж «демо» и оговорку. 404 «Маршрут не найден» отделён от 404 документа.
- `profile_assistant`: замена `resolve_config` на `config_from_env` корректна, `mode=audit`
  возвращает аудит без ключа и модели (проверено), ответ использует `action`/`duplicate`/
  `explanation`/`example`, `requires_copy` приходит от сервера, клиент дополнительно блокирует
  встроенный профиль.
- `llm_provider`: ключ не попадает в ошибки и argv, редиректы запрещены, размеры ответа и
  запроса ограничены, `complete` сохранил сигнатуру (новый только необязательный `base_url`),
  `_content_chars` отклоняет неизвестные части до сети; тесты на это есть.
- Правка `tests/test_llm_provider.py` `max_tokens 1200 -> 1400`: ассерт был устаревшим ещё в
  `origin/dev` (код уже отправлял 1400), это не сокрытие дефекта, а синхронизация ожидания.
- `tests/test_obsidian.py`: закрытие архива внутри helper действительно ломало чтение;
  новые проверки экранирования (`(?<!\\)<img`, `\\<img`) корректны для `_md`, которая
  экранирует `<`/`>`/`[`/`]`. `tests/test_pdf_quality.py`: строка в `pdf_quality.py` действительно
  «научной достоверности», прежнее ожидание было ошибочным.
- Остальные правки `tools/uitest` (3 ссылки в шапке, 6 вкладок, `.assistant-card`) совпадают с
  текущей разметкой; `harness.mjs` 41/41.

## Осталось непроверенным

- Браузерные сценарии `tools/uitest/passport.mjs`, `manual-profile.mjs`, `e2e.mjs`, `auth.mjs`
  (нужен Chromium, по условию не запускал). Селекторы сверил статически.
- Живой провайдер: реальная модель, реальный `/api/profile/assist`, `/api/chat` с настоящим
  ключом и vision по сканированной странице не запускались.
- Поведение Docling-источников на реальном документе: вывод о `provenance` сделан по коду и
  тесту `test_chat.py`, реальный ответ `/api/chat` для `parse:`-документа не смотрел.
- Вёрстка чата на 390 px и Tab-порядок в новых экранах: только чтение CSS.
- Гонка двух вкладок при сохранении/удалении профиля и `copyProfile` против параллельного
  создания того же id.
- Серверный 503 для `parser=vision` с новыми именами переменных: логика прочитана, запрос в
  поднятый сервер не отправлял.

## Статус после правок (2026-11-22)

Блокер и содержательные замечания закрыты в рабочем дереве, повторная проверка ревьюером
подтвердила отсутствие блокеров:

- B1: `config_from_env` выбирает базу по источнику ключа; OpenRouter-ключ больше не следует
  `OPENAI_BASE_URL`. Добавлены тест и `OPENROUTER_BASE_URL` в `.env.example`/whitelist `.env`.
- `complete()` уважает `OPENAI_JSON_MODE`; добавлен тест на `off`.
- `parse:`-цитаты открывают серверный `pdf_url`; `provenance=recognized` показывается бейджем
  «распознано» с отдельным `aria-label` и корректной сеткой.
- История диалога гидратируется через `GET /api/chat` в обоих экранах; abort при уходе
  обрабатывается, режим `multi` с одной статьёй совпадает с `send()`, `clearChat` режет
  selection до 12 id в каталожном порядке.
- Бюджеты текста и изображений в `complete()` разделены; добавлены тесты.
- Убраны обещания режима `general`, который фронтенд не вызывает.

Проверка после правок и повторного слияния с `origin/dev` (88c2121): `python -m unittest -v` -
971 OK, 4 skipped; `harness.mjs` - 41/41; `node --check` без ошибок. Остались только пункты из
«Осталось непроверенным»: живой провайдер, браузерная вёрстка и реальный Docling-документ.
