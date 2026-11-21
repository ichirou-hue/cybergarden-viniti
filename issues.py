"""Машинные проверки согласованности: строки таблицы ``issues``.

Задача проверок - не извлечь новое, а найти в уже извлечённом то, что не сходится
само с собой: пересчитанная величина не равна напечатанной, единица измерения
противоречит величине, у результата нет условий измерения. Каждая такая находка
сама является проверяемым результатом: она ссылается на ``record_id``, из которых
получена, и на цитату, по которой её можно перепроверить.

Поэтому у каждой строки есть ``source_page`` и ``source_fragment``, скопированные
у исходного результата, а не сочинённые: :mod:`verify` должен принимать их без
исключений.

Нумерация: наши находки получают ``issue_id`` с префиксом ``C`` (computed), эталон
организаторов нумеруется ``I...``. Перегенерация удаляет только ``C*``, поэтому
импортированные вручную находки не пропадают.

Запуск:
    python issues.py --area solar              # записать находки в базу
    python issues.py --area solar --dry-run    # только показать
"""
import argparse
import collections
import pathlib
import sys
from decimal import Decimal, InvalidOperation

import db as storage

# PCE = Jsc * Voc * FF. Напечатанные в статье значения округлены, поэтому точного
# равенства не бывает: 15.28 mA/cm2 * 1.197 V * 0.71 = 12.986 против 12.89 в статье
# - это округление, а не ошибка. Находкой считаем расхождение, которое больше и
# абсолютного порога, и относительного: иначе мелкие КПД дают ложные срабатывания
# от одного округления в третьем знаке.
JV_ABSOLUTE = Decimal("0.15")
JV_RELATIVE = Decimal("0.01")

# Допуск пересчёта единиц: FF 0.71 и 71.0 - одно и то же число.
UNIT_SCALE = (Decimal(1), Decimal(100), Decimal("0.01"), Decimal(1000), Decimal("0.001"))

J_V_METRICS = ("Jsc", "Voc", "FF", "PCE")


def number(text):
    """Decimal для напечатанного числа, или None."""
    if text is None:
        return None
    cleaned = str(text).strip().replace(" ", "").replace("\u00a0", "").rstrip("%")
    for dash in ("\u2212", "\u2013", "\u2014"):
        cleaned = cleaned.replace(dash, "-")
    cleaned = cleaned.replace(",", ".")
    if not cleaned:
        return None
    try:
        return Decimal(cleaned)
    except (InvalidOperation, ValueError):
        return None


def same_scale(left, right):
    """True когда два числа равны с точностью до пересчёта единиц."""
    if left is None or right is None:
        return True
    return any(left == right * factor for factor in UNIT_SCALE)


def fraction(value, unit):
    """Значение как доля: FF 71.0 % -> 0.71, FF 0.71 -> 0.71."""
    if value is None:
        return None
    if unit and "%" in unit and value > Decimal("1.5"):
        return value / 100
    return value


def volts(value, unit):
    """Значение в вольтах: 960 mV -> 0.96."""
    if value is None:
        return None
    if unit and unit.strip().lower() in ("mv", "мв") and value > Decimal("10"):
        return value / 1000
    return value


# ------------------------------------------------------------------ находки

def issue(publication, index, records, check, description):
    """Одна строка ``issues`` из группы исходных строк."""
    primary = records[0]
    return {
        "publication_id": publication,
        "issue_id": f"C{index:02d}",
        "record_ids": ";".join(record["result_id"] for record in records),
        "check_name": check,
        "description": description,
        "source_page": primary.get("source_page") or "",
        "source_fragment": primary.get("source_fragment") or "",
    }


def jv_consistency(publication, rows):
    """PCE против Jsc * Voc * FF.

    Группируем по прибору, измерению, агрегату и моменту времени: только внутри
    такой группы четыре величины описывают один и тот же замер. Сравнивать Jsc
    одного прибора с PCE другого - классическая ошибка склейки, и именно её
    проверка ловит.
    """
    groups = collections.defaultdict(dict)
    for row in rows:
        if row["metric_name"] not in J_V_METRICS:
            continue
        key = (row["device_id"], row["measurement_id"], row["aggregation"],
               row["time_point"])
        groups[key][row["metric_name"]] = row

    found = []
    for key, group in groups.items():
        if len(group) < len(J_V_METRICS):
            continue
        jsc = number(group["Jsc"]["metric_value"])
        voc = volts(number(group["Voc"]["metric_value"]), group["Voc"]["metric_unit"])
        ff = fraction(number(group["FF"]["metric_value"]), group["FF"]["metric_unit"])
        pce = number(group["PCE"]["metric_value"])
        if None in (jsc, voc, ff, pce) or not pce:
            continue
        computed = jsc * voc * ff
        difference = abs(computed - pce)
        if difference <= JV_ABSOLUTE or difference / abs(pce) <= JV_RELATIVE:
            continue
        records = [group[name] for name in J_V_METRICS]
        found.append(issue(
            publication, 0, records, "jv_consistency",
            f"КПД {pce} % не сходится с Jsc {group['Jsc']['metric_value']} "
            f"{group['Jsc']['metric_unit']}, Voc {group['Voc']['metric_value']} "
            f"{group['Voc']['metric_unit']} и FF {group['FF']['metric_value']}: "
            f"пересчёт даёт {computed:.3f} %, расхождение {difference:.3f} п.п. "
            f"Либо округление исходных чисел, либо Jsc, Voc, FF и PCE относятся "
            f"к разным замерам."))
    return found


def unit_inconsistency(publication, rows):
    """Единица измерения противоречит величине.

    FF - доля от единицы. Если она записана как ``71.0`` с единицей ``fraction``
    или как ``0.71`` с единицей ``%``, то пересчёт молча сдвинет число в сто раз.
    """
    found = []
    for row in rows:
        value = number(row["metric_value"])
        if value is None:
            continue
        unit = (row["metric_unit"] or "").strip().lower()
        problem = ""
        if row["metric_name"] == "FF":
            if unit in ("fraction", "доля") and value > Decimal("1.5"):
                problem = f"FF {row['metric_value']} записан как доля, но больше 1"
            elif unit in ("%", "percent") and value <= Decimal("1.5"):
                problem = f"FF {row['metric_value']} записан в процентах, но меньше 1.5"
        elif row["metric_name"] == "Voc":
            if unit in ("v", "в") and value > Decimal("10"):
                problem = f"Voc {row['metric_value']} записан в вольтах, но больше 10"
            elif unit in ("mv", "мв") and value < Decimal("10"):
                problem = f"Voc {row['metric_value']} записан в милливольтах, но меньше 10"
        # КПД ниже 1.5 % не проверяется: у полупрозрачных и двусторонних приборов
        # он действительно бывает 0.2 %, и правило «процент меньше полутора -
        # значит доля» дало бы 25 ложных находок на настоящих данных эталона.
        if problem:
            found.append(issue(
                publication, 0, [row], "unit_inconsistency",
                f"{problem}: пересчёт в другую единицу изменит число в сто раз, "
                f"сравнение с другими статьями станет неверным."))
    return found


def missing_conditions(publication, rows):
    """У собственного результата нет условий измерения.

    Без ``measurement_id`` неизвестно, при какой освещённости и каком ходе
    развёртки получено число, поэтому сравнивать его с чужими нельзя.

    Результаты из литературы (``result_origin = cited``) не проверяются: их
    условия описаны в цитируемой работе, а не в этой, и требовать их здесь -
    значит выдать 57 ложных находок на 58 строк.
    """
    found = []
    for row in rows:
        if (row["measurement_id"] or "").strip():
            continue
        if (row["result_origin"] or "").strip().lower() == "cited":
            continue
        found.append(issue(
            publication, 0, [row], "missing_conditions",
            f"Для {row['metric_name']} = {row['metric_value']} "
            f"{row['metric_unit']} не указаны условия измерения: неизвестны "
            f"спектр, освещённость и направление развёртки, поэтому величину "
            f"нельзя сравнивать с другими приборами и статьями."))
    return found


def voc_deficit(publication, rows, materials):
    """Voc_deficit против ширины зоны: Eg/q - Voc.

    Для перовскитов и тонких плёнок Voc_deficit - это разница между шириной зоны
    в вольтах и достигнутым Voc. Если пересчёт не сходится, значит либо Voc, либо
    bandgap относятся к другому материалу.
    """
    bandgap = {}
    for material in materials:
        value = number(material["bandgap_value"])
        unit = (material["bandgap_unit"] or "").lower()
        if value is None:
            continue
        if unit in ("ev", "эв"):
            bandgap[material["material_id"]] = value
        elif unit in ("nm", "нм") and value:
            bandgap[material["material_id"]] = Decimal("1240") / value
    if not bandgap:
        return []

    devices = {}
    found = []
    by_device = collections.defaultdict(dict)
    for row in rows:
        if row["metric_name"] in ("Voc", "Voc_deficit"):
            by_device[row["device_id"]][row["metric_name"]] = row

    for device, group in by_device.items():
        if len(group) < 2:
            continue
        voc = volts(number(group["Voc"]["metric_value"]), group["Voc"]["metric_unit"])
        deficit = number(group["Voc_deficit"]["metric_value"])
        if voc is None or deficit is None:
            continue
        reference = bandgap.get(_material_of(device, rows))
        if reference is None:
            continue
        computed = reference - voc
        if abs(computed - deficit) <= Decimal("0.05"):
            continue
        found.append(issue(
            publication, 0, [group["Voc"], group["Voc_deficit"]],
            "voc_deficit_consistency",
            f"Voc_deficit {group['Voc_deficit']['metric_value']} не сходится с "
            f"шириной зоны {reference} эВ и Voc {group['Voc']['metric_value']} "
            f"{group['Voc']['metric_unit']}: пересчёт даёт {computed:.3f} эВ."))
    return found


def _material_of(device_id, rows):
    for row in rows:
        if row["device_id"] == device_id and row["material_id"]:
            return row["material_id"]
    return None


def normalized_mismatch(publication, rows):
    """normalized_value против metric_value.

    Нормализация - это пересчёт единиц, а не новое измерение: если числа
    разошлись больше чем на пересчёт, одно из них записано неверно.
    """
    found = []
    for row in rows:
        value = number(row["metric_value"])
        normalized = number(row["normalized_value"])
        if value is None or normalized is None:
            continue
        if same_scale(value, normalized):
            continue
        found.append(issue(
            publication, 0, [row], "normalization_mismatch",
            f"normalized_value {row['normalized_value']} "
            f"{row['normalized_unit']} не соответствует metric_value "
            f"{row['metric_value']} {row['metric_unit']}: пересчёт единиц не "
            f"объясняет расхождение."))
    return found


# ------------------------------------------------------------------ запись

SOLAR_CHECKS = {
    "jv_consistency": jv_consistency,
    "unit_inconsistency": unit_inconsistency,
    "missing_conditions": missing_conditions,
}
RAG_CHECKS = {
    "normalization_mismatch": normalized_mismatch,
    "unit_inconsistency": unit_inconsistency,
}


def generate(area, connection):
    """Все находки области, сгруппированные по публикации и пронумерованные."""
    rows = [dict(row) for row in connection.execute(
        f"SELECT * FROM {storage.quote(storage.table_name(area, 'results'))}")]
    by_publication = collections.defaultdict(list)
    for row in rows:
        by_publication[row["publication_id"]].append(row)

    materials = []
    if area == "solar":
        materials = [dict(row) for row in connection.execute(
            f"SELECT * FROM {storage.quote(storage.table_name(area, 'materials'))}")]

    checks = SOLAR_CHECKS if area == "solar" else RAG_CHECKS
    result = []
    for publication in sorted(by_publication):
        found = []
        for check in checks.values():
            found.extend(check(publication, by_publication[publication]))
        if area == "solar":
            found.extend(voc_deficit(publication, by_publication[publication], materials))
        # Нумерация по порядку находок, чтобы C01.. были стабильны между запусками.
        for index, row in enumerate(found, 1):
            row["issue_id"] = f"C{index:02d}"
        result.extend(found)
    return result


def write(area, connection, rows):
    """Заменить вычисленные находки области; эталонные (I*) не трогаются."""
    name = storage.quote(storage.table_name(area, "issues"))
    passport = storage.load_passport()
    columns = passport["areas"][area]["tables"]["issues"]["columns"]
    with connection:
        connection.execute(f"DELETE FROM {name} WHERE issue_id GLOB 'C*'")
        connection.executemany(
            f"INSERT OR REPLACE INTO {name} "
            f"({', '.join(storage.quote(column) for column in ['area'] + columns)}) "
            f"VALUES ({', '.join('?' for _ in range(len(columns) + 1))})",
            [[area] + [row.get(column) or "" for column in columns] for row in rows])
    return len(rows)


def verify_rows(area, rows, split="dev"):
    """Проверить находки по настоящим PDF.

    Находка обязана быть проверяемой так же, как результат: страница и цитата в
    ней скопированы у исходного результата, поэтому ``verify.locate_citation``
    должен их принять. Числа у находки нет - она не измерение.
    """
    import batch
    import pdf_source
    import verify

    by_publication = collections.defaultdict(list)
    for row in rows:
        by_publication[row["publication_id"]].append(row)

    directory = batch.split_dir(area, split)
    checked = accepted = 0
    problems = []
    for publication in sorted(by_publication):
        path = directory / f"{publication}.pdf"
        if not path.exists():
            problems.append((publication, "", f"нет файла {path}"))
            continue
        document = pdf_source.extract(path.read_bytes())
        sources = document.get("sources") or []
        texts = document.get("page_texts") or {}
        for row in by_publication[publication]:
            checked += 1
            ok, reason, _ = verify.locate_citation(row, sources, texts)
            if ok:
                accepted += 1
            else:
                problems.append((publication, row["issue_id"], reason))
    return checked, accepted, problems


def main(argv):
    parser = argparse.ArgumentParser(description="Проверки согласованности -> issues")
    parser.add_argument("--area", choices=("solar", "rag"), required=True)
    parser.add_argument("--db", type=pathlib.Path, default=storage.DEFAULT_DB)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--verify", action="store_true",
                        help="проверить каждую находку по PDF из корпуса")
    parser.add_argument("--split", default="dev", help="каталог корпуса для --verify")
    args = parser.parse_args(argv)

    connection = storage.connect(args.db)
    try:
        rows = generate(args.area, connection)
        if args.dry_run:
            for row in rows:
                print(f"{row['publication_id']} {row['issue_id']} {row['check_name']}: "
                      f"{row['description']}")
            print(f"\nвсего находок: {len(rows)}")
            if args.verify:
                checked, accepted, problems = verify_rows(args.area, rows, args.split)
                print(f"проверено по PDF: {checked}, принято: {accepted}, "
                      f"не принято: {len(problems)}")
                for publication, issue_id, reason in problems:
                    print(f"  {publication} {issue_id}: {reason}")
            return 0
        written = write(args.area, connection, rows)
    finally:
        connection.close()
    print(f"записано находок: {written}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
