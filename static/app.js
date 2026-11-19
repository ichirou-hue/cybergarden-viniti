const ui = Object.fromEntries([...document.querySelectorAll("[id]")].map(el => [el.id, el]));
const kinds = {text: "Текст", table: "Таблица", caption: "Подпись", equation: "Формула"};
const issues = {
  missing_unit: "Шкала не указана",
  out_of_range: "Вне диапазона",
  conflicting_values: "Противоречивые значения",
  recall_decreases: "Recall уменьшается при росте k",
};
const units = {ratio: "доля", unspecified: "шкала неизвестна"};
const pageSize = 25;
let paper;
let page = 0;

async function request(path, options) {
  const response = await fetch(path, options);
  const data = await response.json();
  if (!response.ok) throw new Error(data.error);
  return data;
}

function option(value, text) {
  return new Option(text, value);
}

async function work(task) {
  const controls = [...ui.upload.querySelectorAll("button"), ui.papers];
  controls.forEach(el => { el.disabled = true; });
  ui.status.className = "";
  ui.status.textContent = "Обработка…";
  try {
    ui.status.textContent = await task() || "";
  } catch (error) {
    ui.status.textContent = error.message;
    ui.status.className = "error";
  } finally {
    controls.forEach(el => { el.disabled = false; });
  }
}

async function refresh(selected) {
  const papers = await request("/api/papers");
  ui.papers.replaceChildren(...papers.map(item => option(item.id, item.title)));
  if (!papers.length) {
    return "Загрузите PDF или откройте демо.";
  }
  const id = selected || papers[0].id;
  ui.papers.value = id;
  openPaper(await request("/api/papers/" + id));
}

function openPaper(result) {
  paper = result;
  const base = "/api/papers/" + paper.id;
  ui.json.href = base + "?download";
  ui.json.download = "analysis.json";
  ui.pdf.href = base + "/pdf";
  ui.pdf.download = paper.name;
  ui.workspace.hidden = false;
  const flagged = paper.facts.filter(fact => fact.issues.length).length;
  ui.summary.textContent = [
    paper.pages + " стр.", paper.sources.length + " фрагментов",
    paper.facts.length + " метрик", flagged + " с замечаниями",
  ].join("  ·  ");
  ui.warnings.replaceChildren(...paper.warnings.map(warning => {
    const item = document.createElement("li");
    item.textContent = warning;
    return item;
  }));
  ui.view.value = paper.facts.length ? "facts" : "sources";
  ui.query.value = "";
  ui.issues.checked = false;
  setCategories();
}

function setCategories() {
  const facts = ui.view.value === "facts";
  const categories = facts
    ? [...new Set(paper.facts.map(fact => fact.metric))].sort().map(metric => [metric, metric.toUpperCase()])
    : Object.entries(kinds);
  ui.category.replaceChildren(option("", "Все"), ...categories.map(([key, text]) => option(key, text)));
  ui.issueFilter.hidden = !facts;
  page = 0;
  render();
}

function evidenceButton(label, anchor, resource, id) {
  const button = document.createElement("button");
  button.className = "secondary";
  button.textContent = label;
  button.onclick = () => {
    ui.evidenceTitle.textContent = "Страница " + anchor.page;
    ui.quote.textContent = anchor.quote;
    ui.imageError.hidden = true;
    ui.preview.hidden = false;
    ui.preview.src = "/api/papers/" + paper.id + "/" + resource + "/" + id + ".png";
    ui.evidence.showModal();
    ui.evidence.scrollTop = 0;
  };
  return button;
}

function render() {
  const facts = ui.view.value === "facts";
  const query = ui.query.value.toLocaleLowerCase().trim().split(/\s+/);
  const category = ui.category.value;
  const entries = (facts ? paper.facts : paper.sources).filter(entry => {
    if (category && category !== (facts ? entry.metric : entry.kind)) return false;
    if (facts && ui.issues.checked && !entry.issues.length) return false;
    const searchable = facts
      ? [entry.metric, entry.cutoff ? entry.metric + "@" + entry.cutoff : "", entry.value,
         entry.raw_value, entry.context, entry.evidence.quote, units[entry.unit],
         ...entry.issues.map(code => issues[code])].join(" ")
      : entry.quote;
    return query.every(word => searchable.toLocaleLowerCase().includes(word));
  });
  const columns = facts
    ? ["Метрика", "Значение", "Контекст", "Проверка", "Источник"]
    : ["Тип", "Цитата", "Источник"];
  const header = document.createElement("tr");
  for (const title of columns) {
    const cell = document.createElement("th");
    cell.scope = "col";
    cell.textContent = title;
    header.append(cell);
  }
  ui.head.replaceChildren(header);
  ui.rows.replaceChildren();
  for (const entry of entries.slice(page * pageSize, (page + 1) * pageSize)) {
    const row = ui.rows.insertRow();
    if (facts) {
      row.insertCell().textContent = entry.metric.toUpperCase() + (entry.cutoff ? "@" + entry.cutoff : "");
      row.insertCell().textContent = entry.value + " · " + units[entry.unit] + " (" + entry.raw_value + ")";
      row.insertCell().textContent = entry.context || "—";
      const check = row.insertCell();
      check.textContent = entry.issues.map(code => issues[code]).join(" · ") || "Замечаний нет";
      check.classList.toggle("flag", Boolean(entry.issues.length));
      const evidence = row.insertCell();
      evidence.append(evidenceButton("Стр. " + entry.evidence.page, entry.evidence, "facts", entry.id));
      if (entry.header) evidence.append(evidenceButton("Заголовок", entry.header, "headers", entry.id));
    } else {
      row.insertCell().textContent = kinds[entry.kind];
      row.insertCell().textContent = entry.quote;
      row.insertCell().append(evidenceButton("Стр. " + entry.page, entry, "sources", entry.id));
    }
  }
  const total = Math.max(1, Math.ceil(entries.length / pageSize));
  ui.count.textContent = entries.length ? "Найдено " + entries.length : "По этому запросу ничего не найдено.";
  ui.prev.disabled = page === 0;
  ui.next.disabled = page + 1 === total;
  ui.page.textContent = (page + 1) + " / " + total;
}

ui.upload.onsubmit = event => {
  event.preventDefault();
  const file = ui.file.files[0];
  if (!file) return;
  work(async () => {
    const result = await request("/api/papers", {
      method: "POST", body: file,
      headers: {"Content-Type": "application/pdf", "X-Filename": encodeURIComponent(file.name)},
    });
    await refresh(result.id);
    ui.file.value = "";
  });
};
ui.demo.onclick = () => work(async () => {
  const result = await request("/api/demo", {method: "POST"});
  await refresh(result.id);
});
ui.papers.onchange = () => work(async () => {
  try {
    openPaper(await request("/api/papers/" + ui.papers.value));
  } catch (error) {
    ui.papers.value = paper?.id || "";
    throw error;
  }
});
ui.view.onchange = setCategories;
for (const control of [ui.query, ui.category, ui.issues]) {
  control.oninput = () => { page = 0; render(); };
}
ui.prev.onclick = () => { page -= 1; render(); };
ui.next.onclick = () => { page += 1; render(); };
ui.close.onclick = () => ui.evidence.close();
ui.preview.onerror = () => {
  ui.preview.hidden = true;
  ui.imageError.hidden = false;
};
work(refresh);
