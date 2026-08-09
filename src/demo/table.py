"""
Таблица бухгалтера для демо: как из расчёта демо собрать файл для сверки.

Экран сверки принимает книгу Excel в формате партнёра и сравнивает её с
расчётом. В демо взять такую книгу неоткуда: настоящую таблицу партнёра класть
нельзя (D028), а выдуманная «просто книга» разошлась бы с расчётом по каждой
строке — посетитель увидел бы сплошной красный экран и решил бы, что продукт не
считает.

Поэтому файл собирается **из самого демо-расчёта**, и в него нарочно вносятся
три расхождения — по одному на каждое состояние, которое сверка умеет
показывать:

* существенное — бухгалтер не увидел одну смену: у человека в файле меньше часов
  и, как следствие, меньше «к выплате». Сверка покажет и разницу, и её причину;
* копеечное — расхождение меньше динара. Оно называется округлением и живёт
  отдельной группой: известное расхождение, которое видно, но не мешает читать
  существенное;
* человек, которого нет в расчёте, — уволенный, оставшийся в файле бухгалтера.

Четвёртое состояние получается само: курьеров в официальной таблице бухгалтера
нет и быть не может — они во внутреннем регистре. Сверка честно скажет, что в
загруженной таблице таких строк нет.

Ничего из этого не «подкручено под красивый экран»: каждое расхождение — то, что
у партнёра случается каждый месяц, и ровно ради них экран сверки существует.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal

from .accountant_table import TableRow
from .dataset import PEOPLE, Person

__all__ = ["DEVIATIONS", "accountant_rows", "sheet_for"]

D = Decimal

# Группа и схема человека → лист таблицы партнёра. Имена листов взяты из
# `payroll.importers.plata_xlsx.SHEET_MAP` дословно, вместе с двойными пробелами:
# формат чужой, и «поправить очевидную опечатку» здесь означает файл, который не
# прочитается.
#
# Курьеров в этой таблице нет намеренно — см. заголовок модуля.
STANDARD_BY_UNIT = {
    "BG1": "BG1 pun obracun ",
    "NS1": "NS 1 Bulevar ",
    "NS2": "NS 2 Dunavska ",
}
HALF_TIME_MIN_BASE_BY_UNIT = {
    "BG1": "BG 1 pola radnog vremena  puno",
    "NS1": "NS pola radnog  vremena puno ",
    "NS2": "NS pola radnog  vremena puno ",
}
OFFICE_SHEET = "NS  kancelarija "
HALF_TIME_SHEET = "BG1  pola radnog  vremena"
TEMPORARY_SHEET = "NS privremeni poslovi "


def sheet_for(person: Person) -> tuple[str, str] | None:
    """Лист и схема, под которыми человек попадает в таблицу бухгалтера.

    None — человека в этой таблице нет вовсе (курьеры: внутренний регистр в
    официальную таблицу не попадает).
    """
    scheme = person.scheme or ("temporary" if person.group == "temporary" else "standard")
    if person.group == "couriers":
        return None
    if person.group == "temporary":
        return TEMPORARY_SHEET, "temporary"
    if scheme == "half_time":
        return HALF_TIME_SHEET, "half_time"
    if scheme == "half_time_min_base":
        return HALF_TIME_MIN_BASE_BY_UNIT[person.unit], "half_time_min_base"
    if person.group == "office":
        return OFFICE_SHEET, "standard"
    return STANDARD_BY_UNIT[person.unit], "standard"


# Нарочные расхождения: ключ человека → что бухгалтер записал иначе.
# `hours` — сколько часов он не увидел, `net` — на сколько разошлась выплата.
DEVIATIONS = {
    # Смена, не дошедшая до таблицы: и часы, и деньги меньше. Причина
    # объяснится сама — сверка покажет расхождение по часам.
    "Owen Fletcher": {"hours": D(8), "net": D("-4200.00")},
    # Копейки: округление на стороне бухгалтерии.
    "Chloe Ramsey": {"net": D("-0.40")},
}

# Человек, оставшийся в таблице бухгалтера, но не в расчёте. Уволился в мае,
# строку из файла не убрали — обычное дело, и сверка обязана его показать.
LEFT_BEHIND = TableRow(
    first="Peter", last="Ashford", sheet=OFFICE_SHEET, scheme="standard",
    coefficient=D("1.20"), base_rate=D("760.00"),
    insured=D(176), regular=D(176), holiday=D(0), vacation=D(0), sick=D(0),
    deduction=D(0), cash=D(0), correction=None, meal=D("1500.00"),
    net=D("142560.00"), gross=D("196812.00"),
    contributions=D("83912.00"), total_cost=D("226628.00"),
)


def _num(value) -> Decimal:
    return Decimal(str(value or 0))


def accountant_rows(period: date) -> list[TableRow]:
    """Строки таблицы бухгалтера за месяц — из того, что база отдала этой роли.

    Берётся тот же срез, которым сверка собирает «нашу» сторону
    (`reports.reconcile.collect_run`). Это не экономия кода: собери мы файл
    другой выборкой, он расходился бы с расчётом там, где база отдала роли не
    всё, — и демо показывало бы расхождение, которого в данных нет.
    """
    from core.models import Tenant
    from reports.reconcile import collect_run

    from .seed import TENANT_CODE

    tenant = Tenant.objects.filter(code=TENANT_CODE).first()
    if tenant is None:
        return []

    run = collect_run(tenant.id, period)
    rows: list[TableRow] = []
    for person in PEOPLE:
        placed = sheet_for(person)
        if placed is None:
            continue
        ours = run.get(person.key)
        if ours is None or not ours.totals:
            # Строки без итогов в файл не попадают: выдумывать за бухгалтера
            # сумму, которой мы не видели, значит показать расхождение, которого
            # нет в данных.
            continue

        sheet, scheme = placed
        shift = DEVIATIONS.get(person.key, {})
        hours = {k: _num(v) for k, v in ours.hours.items()}
        regular = hours.get("regular", D(0)) - _num(shift.get("hours"))
        insured = _num(ours.insured_hours) - _num(shift.get("hours"))

        rows.append(TableRow(
            first=person.first, last=person.last, sheet=sheet, scheme=scheme,
            coefficient=_num(ours.coefficient), base_rate=_num(ours.base_rate),
            insured=insured, regular=regular,
            holiday=hours.get("holiday", D(0)),
            vacation=hours.get("vacation", D(0)),
            sick=hours.get("sick", D(0)),
            deduction=D(0), cash=D(0), correction=None,
            meal=ours.meal,
            net=_num(ours.totals.get("net")) + _num(shift.get("net")),
            gross=_num(ours.totals.get("gross")),
            contributions=_num(ours.totals.get("contributions")),
            total_cost=_num(ours.totals.get("total_cost")),
        ))

    if rows:
        rows.append(LEFT_BEHIND)
    return rows
