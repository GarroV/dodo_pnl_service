"""
Импорт зарплатной таблицы партнёра (Сербия, формат PLATA).

Нужен для двух вещей: перенести существующие данные при подключении партнёра
и сверять движок с их расчётом. Формат сербский, поэтому лежит в importers —
у другого партнёра будет свой.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

import openpyxl

from ..engine import Employee, Timesheet, d

# лист → (схема расчёта, группа)
SHEET_MAP: dict[str, tuple[str, str]] = {
    "NS  kancelarija ":               ("standard",           "office"),
    "BG1 pun obracun ":               ("standard",           "kitchen"),
    "NS 1 Bulevar ":                  ("standard",           "kitchen"),
    "NS 2 Dunavska ":                 ("standard",           "kitchen"),
    "BG1  pola radnog  vremena":      ("half_time",          "kitchen"),
    "BG 1 pola radnog vremena  puno": ("half_time_min_base", "kitchen"),
    "NS pola radnog  vremena puno ":  ("half_time_min_base", "kitchen"),
    "NS privremeni poslovi ":         ("temporary",          "temporary"),
}

# точные заголовки, первый найденный выигрывает
FIELDS: dict[str, list[str]] = {
    "coefficient": ["KOEFICIJENT"],
    "base_rate":   ["CENA RADA"],
    "insured":     ["UKUPNO SATI ZA OBRACUN DOPRINOSA", "UKUPNO SATI"],
    "regular":     ["SATI RADA ZA OBRACUN NETA", "SATI RADA"],
    "holiday":     ["SAT NA RAD PRAZNIKA"],
    "vacation":    ["GODISNJI ODMOR"],
    "sick":        ["SATI BOLOVANJE"],
    "correction":  ["KOREKCIJA DO MINIMALCA"],
    "deduction":   ["OBUSTAVA"],
    "cash":        ["ISPLATA U KES"],
    "meal":        ["TOPLI OBROK I REGRES"],
    "exp_net":     ["UKUPNO ZA ISPLATU"],
    "exp_contrib": ["DOPRINOSI"],
    "exp_total":   ["UKUPAN TROSAK PO RADNIKU"],
}

# «бруто» в разных схемах называется по-разному
GROSS_HEADER: dict[str, list[str]] = {
    "standard":           ["BRUTO"],
    "half_time":          ["BRUTO DOPRINOSI"],
    "half_time_min_base": ["BRUTO ZA POREZ"],
    "temporary":          ["BRUTO"],
}

HOUR_FIELDS = ("regular", "holiday", "vacation", "sick")


@dataclass
class ImportedRow:
    """Строка таблицы: входные данные плюс то, что посчитала бухгалтерия."""
    sheet: str
    scheme: str
    group: str
    name: str
    employee: Employee
    timesheet: Timesheet
    sheet_meal: Decimal | None
    expected: dict[str, Decimal | None]


def _norm(value) -> str:
    return re.sub(r"\s+", " ", str(value)).strip().upper()


def _map_columns(ws, scheme: str) -> dict[str, int]:
    headers: dict[str, int] = {}
    for col in range(1, 45):
        value = ws.cell(1, col).value
        if value and _norm(value) not in headers:
            headers[_norm(value)] = col

    fields = dict(FIELDS, exp_gross=GROSS_HEADER[scheme])
    mapped: dict[str, int] = {}
    for key, names in fields.items():
        for name in names:
            if name in headers:
                mapped[key] = headers[name]
                break
    return mapped


def read_plata(path: Path | str, default_rate: Decimal | float = 371) -> list[ImportedRow]:
    wb = openpyxl.load_workbook(path, data_only=True)
    rows: list[ImportedRow] = []

    for sheet, (scheme, group) in SHEET_MAP.items():
        if sheet not in wb.sheetnames:
            continue
        ws = wb[sheet]
        cols = _map_columns(ws, scheme)

        for r in range(2, 20):
            if not isinstance(ws.cell(r, 1).value, (int, float)):
                continue
            first, last = ws.cell(r, 2).value, ws.cell(r, 3).value
            if not first or not last:
                continue

            raw = {k: ws.cell(r, c).value for k, c in cols.items()}
            name = f"{str(first).strip()} {str(last).strip()}"
            insured = d(raw.get("insured") or 176)

            rows.append(ImportedRow(
                sheet=sheet.strip(),
                scheme=scheme,
                group=group,
                name=name,
                employee=Employee(
                    ext_id=name, name=name, group=group, scheme=scheme,
                    base_rate=d(raw.get("base_rate") or default_rate),
                    coefficient=d(raw.get("coefficient") or 1),
                ),
                timesheet=Timesheet(
                    hours={k: d(raw.get(k)) for k in HOUR_FIELDS},
                    insured_hours=insured,
                    norm_hours=insured,
                    deduction=d(raw.get("deduction")),
                    cash_payout=d(raw.get("cash")),
                    manual_correction=d(raw["correction"]) if raw.get("correction") else None,
                ),
                sheet_meal=d(raw["meal"]) if raw.get("meal") is not None else None,
                expected={
                    "net":           d(raw["exp_net"])     if raw.get("exp_net")     else None,
                    "gross":         d(raw["exp_gross"])   if raw.get("exp_gross")   else None,
                    "contributions": d(raw["exp_contrib"]) if raw.get("exp_contrib") else None,
                    "total_cost":    d(raw["exp_total"])   if raw.get("exp_total")   else None,
                },
            ))
    return rows
