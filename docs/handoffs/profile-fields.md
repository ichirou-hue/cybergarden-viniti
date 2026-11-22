# Состав профилей извлечения

Источник предметных полей: `GET /api/passport?area=solar|rag`. Это полные схемы экспорта, а не сокращённые примеры ручной формы.

Для каждой таблицы интерфейс должен раскрывать поля, тип, допустимые значения, единицы и ограничения, если они присутствуют в ответе. Пустое значение означает отсутствие подтверждения, а не отсутствие явления в статье.

## SOLAR: 7 таблиц, 94 полей

### publications

Ключ: `publication_id`.

| Поле | Тип | Допустимые значения |
|---|---|---|
| `publication_id` | text |  |
| `title` | text |  |
| `year` | text |  |
| `DOI` | text |  |
| `authors` | text |  |
| `in_scope` | enum | false, true |
| `exclusion_reason` | text |  |

### materials

Ключ: `material_id`.

| Поле | Тип | Допустимые значения |
|---|---|---|
| `publication_id` | text |  |
| `material_id` | text |  |
| `material_name_raw` | text |  |
| `formula_normalized` | text |  |
| `material_class` | enum | CIGS, CdTe, Sb2Se3, kesterite, other, perovskite, thin-film Si |
| `dimensionality` | enum | 2D, 2D-3D, 3D |
| `additives` | text |  |
| `bandgap_value` | number |  |
| `bandgap_unit` | text |  |
| `bandgap_method` | enum | EQE, PL, Tauc, cited |
| `note` | text |  |

### devices

Ключ: `device_id`.

| Поле | Тип | Допустимые значения |
|---|---|---|
| `publication_id` | text |  |
| `device_id` | text |  |
| `absorber_material_id` | text |  |
| `device_label` | text |  |
| `device_role` | enum | control, record, target |
| `device_type` | enum | cell, minimodule, module |
| `architecture` | enum | n-i-p, p-i-n, substrate, superstrate |
| `substrate_type` | enum | flexible, rigid |
| `active_area` | number |  |
| `area_unit` | text |  |
| `area_definition` | enum | aperture, designated illumination, total |
| `encapsulation` | text |  |
| `note` | text |  |

### layers

Ключ: `layer_id`.

| Поле | Тип | Допустимые значения |
|---|---|---|
| `publication_id` | text |  |
| `layer_id` | text |  |
| `device_id` | text |  |
| `layer_order` | number |  |
| `layer_function` | enum | ETL, HTL, absorber, back_contact, front_contact, interlayer, passivation, recombination |
| `layer_material` | text |  |
| `layer_thickness` | text |  |
| `thickness_unit` | text |  |
| `deposition_method` | text |  |
| `annealing_temperature` | text |  |
| `annealing_time` | text |  |
| `atmosphere` | text |  |
| `note` | text |  |

### measurements

Ключ: `measurement_id`.

| Поле | Тип | Допустимые значения |
|---|---|---|
| `publication_id` | text |  |
| `measurement_id` | text |  |
| `device_id` | text |  |
| `measurement_type` | enum | EQE, J-V, MPP tracking, stability |
| `spectrum` | text |  |
| `light_intensity` | text |  |
| `intensity_unit` | text |  |
| `scan_direction` | enum | average, forward, reverse |
| `scan_rate` | text |  |
| `mask_used` | enum | false, true |
| `certified` | enum | false, true |
| `certification_lab` | text |  |
| `temperature` | text |  |
| `stability_protocol` | enum | ISOS-D, ISOS-L, ISOS-T, other |
| `stress_conditions` | text |  |
| `note` | text |  |

### results

Ключ: `result_id`.

| Поле | Тип | Допустимые значения |
|---|---|---|
| `publication_id` | text |  |
| `result_id` | text |  |
| `device_id` | text |  |
| `measurement_id` | text |  |
| `material_id` | text |  |
| `metric_name` | text |  |
| `metric_value` | number |  |
| `metric_unit` | text |  |
| `aggregation` | enum | champion, mean, median |
| `spread_value` | text |  |
| `n_devices` | text |  |
| `time_point` | text |  |
| `time_unit` | text |  |
| `raw_value` | text |  |
| `raw_unit` | text |  |
| `normalized_value` | number |  |
| `normalized_unit` | text |  |
| `result_origin` | enum | cited, primary |
| `cited_publication_id` | text |  |
| `source_page` | number |  |
| `source_section` | text |  |
| `source_table` | text |  |
| `source_figure` | text |  |
| `source_fragment` | text |  |
| `extraction_status` | enum | absent, extracted, present_not_extractable |
| `duplicate_of` | text |  |
| `note` | text |  |

### issues

Ключ: `issue_id`.

| Поле | Тип | Допустимые значения |
|---|---|---|
| `publication_id` | text |  |
| `issue_id` | text |  |
| `record_ids` | text |  |
| `check_name` | text |  |
| `description` | text |  |
| `source_page` | text |  |
| `source_fragment` | text |  |

## RAG: 7 таблиц, 85 полей

### publications

Ключ: `publication_id`.

| Поле | Тип | Допустимые значения |
|---|---|---|
| `publication_id` | text |  |
| `title` | text |  |
| `year` | number |  |
| `DOI` | text |  |
| `authors` | text |  |
| `in_scope` | enum | true, false |
| `exclusion_reason` | text |  |

### configurations

Ключ: `config_id`.

| Поле | Тип | Допустимые значения |
|---|---|---|
| `publication_id` | text |  |
| `config_id` | text |  |
| `rag_variant` | text |  |
| `retrieval_method` | text |  |
| `retriever_model` | text |  |
| `embedding_model` | text |  |
| `embedding_version` | text |  |
| `chunking_method` | text |  |
| `chunk_size` | number |  |
| `chunk_overlap` | number |  |
| `chunk_unit` | text |  |
| `index_type` | text |  |
| `vector_store` | text |  |
| `top_k` | number |  |
| `reranker_model` | text |  |
| `generator_model` | text |  |
| `generator_version` | text |  |
| `prompt_strategy` | text |  |
| `context_strategy` | text |  |
| `note` | text |  |

### datasets

Ключ: `dataset_id`.

| Поле | Тип | Допустимые значения |
|---|---|---|
| `publication_id` | text |  |
| `dataset_id` | text |  |
| `dataset_name` | text |  |
| `dataset_version` | text |  |
| `language` | text |  |
| `data_split` | text |  |
| `scientific_domain` | text |  |
| `note` | text |  |

### tasks

Ключ: `task_id`.

| Поле | Тип | Допустимые значения |
|---|---|---|
| `publication_id` | text |  |
| `task_id` | text |  |
| `task_type` | text |  |
| `note` | text |  |

### experiments

Ключ: `experiment_id`.

| Поле | Тип | Допустимые значения |
|---|---|---|
| `publication_id` | text |  |
| `experiment_id` | text |  |
| `evaluation_protocol` | text |  |
| `baseline_name` | text |  |
| `hardware` | text |  |
| `latency_ms` | number |  |
| `tokens_per_query` | number |  |
| `sample_size` | number |  |
| `note` | text |  |

### results

Ключ: `result_id`.

| Поле | Тип | Допустимые значения |
|---|---|---|
| `publication_id` | text |  |
| `result_id` | text |  |
| `experiment_id` | text |  |
| `config_id` | text |  |
| `dataset_id` | text |  |
| `task_id` | text |  |
| `metric_group` | text |  |
| `metric_name` | text |  |
| `metric_variant` | text |  |
| `metric_direction` | text |  |
| `metric_k` | number |  |
| `metric_value` | number |  |
| `metric_unit` | text |  |
| `baseline_value` | number |  |
| `aggregation` | text |  |
| `sample_size` | number |  |
| `raw_value` | text |  |
| `raw_unit` | text |  |
| `normalized_value` | number |  |
| `normalized_unit` | text |  |
| `result_origin` | enum | primary, cited |
| `cited_publication_id` | text |  |
| `source_page` | number |  |
| `source_section` | text |  |
| `source_table` | text |  |
| `source_figure` | text |  |
| `source_fragment` | text |  |
| `extraction_status` | enum | extracted, present_not_extractable, absent |
| `duplicate_of` | text |  |
| `note` | text |  |

### issues

Ключ: `issue_id`.

| Поле | Тип | Допустимые значения |
|---|---|---|
| `publication_id` | text |  |
| `issue_id` | text |  |
| `record_ids` | text |  |
| `check_name` | text |  |
| `description` | text |  |
| `source_page` | number |  |
| `source_fragment` | text |  |

## Универсальный профиль

`GET /api/profiles/universal`: полный JSON и summary с типами, описаниями и ограничениями. `POST /api/parse` с `X-Profile: universal`. Не отдельная область официальной сдачи.

### studies

Объект исследования или эксперимент при конкретных условиях. Разные объекты и условия сохраняются отдельно.

| Поле | Тип | Назначение |
|---|---|---|
| `study_id` | id | Локальный идентификатор исследования. |
| `object` | text | Что исследовали: образец, система, организм, явление или набор данных. |
| `method` | text | Метод исследования, измерения или анализа, явно указанный авторами. |
| `conditions` | text | Условия, ограничения выборки и настройки конкретного исследования. |

### results

Один подтверждённый результат для одного объекта и одних условий; без смыслового объединения по совпадению чисел.

| Поле | Тип | Назначение |
|---|---|---|
| `result_id` | id | Локальный идентификатор результата. |
| `study_id` | reference | Связь с исследованием только при явном подтверждении в статье. |
| `metric` | text | Название измеряемой величины или оцениваемого показателя. |
| `value` | text | Результат дословно, включая интервал, знак сравнения или неопределённость, если они напечатаны. |
| `numeric_value` | number | Необязательное явно напечатанное скалярное значение без интервала и неопределённости; единица хранится отдельно, пересчёт не выполняется. |
| `unit` | text | Единица величины дословно из источника; пусто, если не указана. |
| `conditions` | text | Условия, при которых получен именно этот результат. |

### findings

Качественные выводы и явно указанные ограничения авторов, подтверждённые текстом статьи.

| Поле | Тип | Назначение |
|---|---|---|
| `finding_id` | id | Локальный идентификатор вывода. |
| `claim` | text | Вывод авторов дословно, без обобщения за пределы статьи. |
| `limitation` | text | Явно указанное авторами ограничение применимости вывода. |

## Настройка пользователем

Встроенные профили не перезаписываются. Для собственной схемы нужно создать копию с новым `id`, преобразовать поля в контракт `extraction_core.compile_profile` и сохранить `PUT /api/profiles/<id>`. Ручная форма `types` и исполняемый профиль `tables` имеют разные форматы: нельзя отправлять скачанный конверт ручной формы в PUT без преобразования.

Solar использует `solar_csv`; его официальный набор колонок не редактируется. Изменённая схема должна стать самостоятельным профилем с адаптером `fields`, а не изменением официального экспорта. RAG запускается предметным конвейером каталога; в Profiles API нет исполняемого встроенного `rag`. Для помощника допускается `profile_id: rag`, состав он получает из паспорта.
