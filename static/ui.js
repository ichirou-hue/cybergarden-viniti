import { el } from "./dom.js";

const DOC_STATUS = {
  done: ["обработано", "ok"],
  partial: ["частично", "warn"],
  failed: ["ошибка", "bad"],
  queued: ["в очереди", "idle"],
  running: ["в работе", "warn"],
};

const EXTRACT_STATUS = {
  extracted: ["извлечено", "ok"],
  present_not_extractable: ["не извлекается", "warn"],
  absent: ["нет в статье", "idle"],
};

export function docStatus(value) {
  const [label, tone] = DOC_STATUS[value] || [value || "—", "idle"];
  return el("span", {class: `badge ${tone}`, text: label});
}

export function extractStatus(value) {
  const [label, tone] = EXTRACT_STATUS[value] || [value || "—", "idle"];
  return el("span", {class: `badge ${tone}`, text: label});
}

export function seconds(ms) {
  if (!ms) return "—";
  const value = Number(ms);
  if (!Number.isFinite(value)) return "—";
  if (value >= 60000) {
    const total = Math.round(value / 1000);
    const minutes = Math.floor(total / 60);
    const rest = total % 60;
    return `${minutes} мин${rest ? ` ${rest} с` : ""}`;
  }
  return `${(value / 1000).toFixed(1).replace(".", ",")} с`;
}

export function number(value) {
  if (value === null || value === undefined || value === "") return "—";
  const numeric = Number(value);
  if (!Number.isFinite(numeric)) return String(value);
  return numeric.toLocaleString("ru-RU", {maximumFractionDigits: 4});
}

export function plural(count, one, few, many) {
  const mod10 = count % 10;
  const mod100 = count % 100;
  if (mod10 === 1 && mod100 !== 11) return one;
  if (mod10 >= 2 && mod10 <= 4 && (mod100 < 12 || mod100 > 14)) return few;
  return many;
}

export function issueLabel(descriptor, name) {
  return (descriptor.issueLabels && descriptor.issueLabels[name]) || name;
}

export function fieldLabel(descriptor, key) {
  if (descriptor.fieldLabels && descriptor.fieldLabels[key]) return descriptor.fieldLabels[key];
  const words = String(key).replace(/_/g, " ");
  return words.charAt(0).toUpperCase() + words.slice(1);
}

export function unitLabel(descriptor, unit) {
  if (!unit) return "";
  return (descriptor.units && descriptor.units[unit]) || unit;
}