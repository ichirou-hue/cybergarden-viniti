export default {
  track: "rag",
  label: "RAG",
  note: "Метрики поиска и ответов",
  searchHint: "Метрика, значение, набор, цитата",
  filters: [
    {key: "metric", label: "Метрика", facet: "metrics", allLabel: "Все метрики"},
    {key: "k", label: "k", facet: "ks", allLabel: "Любой k"},
    {key: "dataset", label: "Набор", facet: "datasets", allLabel: "Все наборы"},
    {key: "config", label: "Конфигурация", facet: "configs", allLabel: "Все конфигурации"},
    {key: "status", label: "Статус", facet: "statuses", allLabel: "Все статусы"},
  ],
  columns: [
    {key: "metric", label: "Метрика"},
    {key: "value", label: "Значение"},
    {key: "config", label: "Конфигурация"},
    {key: "dataset", label: "Набор / задача"},
    {key: "group", label: "Группа"},
    {key: "status", label: "Статус"},
    {key: "check", label: "Проверка"},
    {key: "source", label: "Источник"},
  ],
  entities: [
    {
      key: "configurations", label: "Конфигурации",
      columns: [
        {key: "config_id", label: "Код"},
        {key: "label", label: "Название"},
        {key: "detail", label: "Описание"},
      ],
    },
    {
      key: "datasets", label: "Наборы данных",
      columns: [
        {key: "dataset_id", label: "Код"},
        {key: "label", label: "Название"},
        {key: "detail", label: "Описание"},
      ],
    },
    {
      key: "tasks", label: "Задачи",
      columns: [
        {key: "task_id", label: "Код"},
        {key: "label", label: "Название"},
        {key: "detail", label: "Описание"},
      ],
    },
    {
      key: "experiments", label: "Эксперименты",
      columns: [
        {key: "experiment_id", label: "Код"},
        {key: "label", label: "Название"},
      ],
    },
  ],
  entityFilters: {config_id: "config", dataset_id: "dataset"},
  kinds: {text: "Текст", table: "Таблица", caption: "Подпись", equation: "Формула"},
  units: {
    fraction: "доля",
    "%": "%",
    queries: "запросов",
    files: "файлов",
    "USD/1k queries": "USD / 1k запросов",
  },
  issueLabels: {
    metric_scale_assumed: "Шкала принята по заголовку",
    recall_decreases: "Recall падает при росте k",
    conflicting_values: "Противоречивые значения",
    missing_unit: "Единица не указана",
    out_of_range: "Значение вне диапазона",
  },
  fieldLabels: {
    config_id: "Код",
    dataset_id: "Код",
    task_id: "Код",
    experiment_id: "Код",
    label: "Название",
    detail: "Описание",
  },
  emptyHint: "В этой статье не найдено извлечённых результатов.",
};