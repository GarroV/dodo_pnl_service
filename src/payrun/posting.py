"""Утверждённая ведомость становится строками P&L (issue #201, T208).

Разрыв, который это закрывает, нашёлся при постройке отчёта: за посчитанный июнь
в системе 35 ведомостей и **ноль** фактов. Зарплата — самая большая статья
расходов партнёра, и в P&L она не приходила вовсе; цепочка «данные → отчёт» была
разорвана ровно там, где всё уже посчитано.

Решения и их причины.

**Переносим при утверждении, а не при расчёте.** До утверждения ведомость —
черновик: её пересчитывают по десять раз, и каждый пересчёт двигал бы P&L.
После утверждения расчёт заморожен, и число в отчёте перестаёт дышать.

**Две строки отчёта, обе уже есть в справочнике.** `labour_cost` — то, что
начислено людям; `payroll_taxes` — то, что уходит государству. Партнёр смотрит
на них по-разному, и складывать их в одну строку значило бы прятать половину
расхода.

**Начисления идут по регистрам, налоги — в официальный.** Начисления мы знаем
по компонентам, у каждого свой регистр; налоги и взносы считаются от расчёта
целиком и платятся только официально — по определению, а не по выбору. Обратное
означало бы «налог с неофициальной части», чего не бывает.

**Точка — от ведомости.** У офисного персонала её нет, и такой факт становится
`pending`: его разнесёт правило разнесения, как и любой расход юрлица (D055).

**Перенос идемпотентен.** Ключ выводится из расчёта, поэтому повторное
утверждение (после отката и пересчёта) заменяет строки новой версией, а не
добавляет второй расход.
"""
from __future__ import annotations

from decimal import Decimal

# Строки P&L, в которые ложится зарплата. Коды справочника, а не выдуманные:
# они заведены миграцией и уже используются выгрузками.
LABOUR = "labour_cost"
TAXES = "payroll_taxes"

# Приставка ключа идемпотентности. По ней же строки находятся при пересчёте.
PREFIX = "payrun:"


def post(payrun) -> int:
    """Перенести утверждённый расчёт в факты. Возвращает число строк.

    Пишет через `upsert_fact` — ту же функцию, что и все остальные деньги: у неё
    же живут версионирование, идемпотентность и проверка периода. Второй способ
    записи фактов означал бы второй набор этих правил.
    """
    from django.db import connection

    from web import cash

    rows = _lines_of(payrun)
    titles = {code: _line_title(code) for code in (LABOUR, TAXES)}
    written = 0
    for (unit_id, ledger, code), amount in sorted(rows.items(), key=lambda row: str(row[0])):
        if amount == 0:
            continue
        payload = {
            "tenant_id": str(payrun.tenant_id),
            "period": payrun.period.isoformat(),
            "doc_date": _last_day(payrun.period).isoformat(),
            "unit_id": str(unit_id) if unit_id else None,
            "pnl_item_id": str(_pnl_item(code)),
            "ledger": ledger,
            # Расход отрицателен: положительная сумма сложилась бы с выручкой, и
            # результат месяца оказался бы завышен вдвое на величину ФОТ.
            "amount": str(-abs(amount)),
            # Название берётся из самой строки P&L, а не из литерала здесь:
            # у демо эти строки названы по-английски (правило «демо всегда на
            # английском»), и приколоченное русское слово уехало бы в его
            # данные. Поймано сторожем демо, а не глазами.
            "title": titles[code],
            "source": "payroll",
            "source_ref": str(payrun.id),
            # Ключ выводится из ПЕРИОДА, а не из номера расчёта. У месяца расчёт
            # один, а номер меняется, если расчёт снесли и завели заново — и
            # тогда ключ, привязанный к номеру, дал бы второй набор строк рядом
            # с первым. Поймано прогоном: ФОТ удвоился ровно вдвое.
            "dedup_key": (f"{PREFIX}{payrun.period:%Y-%m}:{code}:{ledger}:"
                          f"{unit_id or 'network'}"),
            "allocation": "direct" if unit_id else "pending",
        }
        cash.write_fact({key: value for key, value in payload.items() if value is not None})
        written += 1

    if written:
        # Строки без точки разносятся сразу — тем же доводом, что у расходов:
        # узнать через месяц, что ФОТ офиса висел нераспределённым, хуже, чем
        # разнести его в момент утверждения.
        with connection.cursor() as cursor:
            cursor.execute(
                "select allocate_fact(id) from facts "
                "where dedup_key like %s and allocation = 'pending' "
                "and superseded_at is null",
                [f"{PREFIX}{payrun.period:%Y-%m}:%"],
            )
    return written


def _lines_of(payrun) -> dict:
    """Суммы будущих фактов: (точка, регистр, строка P&L) → сумма.

    Считается одним запросом на весь расчёт: ведомостей три десятка, и запрос на
    каждую дал бы столько же обращений к базе за тем же числом.
    """
    from django.db import connection

    lines: dict = {}
    with connection.cursor() as cursor:
        # Начисления — по компонентам: у каждого свой регистр, и это
        # единственное место, где регистр известен точно.
        cursor.execute(
            """select p.employee_id, p.unit_id, c.ledger::text, sum(c.amount)
                 from pay_components c
                 join payslips p on p.id = c.payslip_id
                where p.payrun_id = %s
                group by p.employee_id, p.unit_id, c.ledger""",
            [str(payrun.id)],
        )
        accruals = cursor.fetchall()

        # Налоги и взносы — разница между полной стоимостью и тем, что дошло до
        # людей. Регистр официальный: налог с неофициальной части не платится
        # по определению, а не по выбору партнёра.
        cursor.execute(
            """select p.employee_id, p.unit_id, sum(t.total_cost - t.net)
                 from payslip_totals t
                 join payslips p on p.id = t.payslip_id
                where p.payrun_id = %s
                group by p.employee_id, p.unit_id""",
            [str(payrun.id)],
        )
        taxes = cursor.fetchall()

    across = _units_of(payrun)
    for employee_id, unit_id, ledger, amount in accruals:
        for unit, part in _shares(across, employee_id, unit_id, amount):
            key = (unit, ledger, LABOUR)
            lines[key] = lines.get(key, Decimal("0")) + part
    for employee_id, unit_id, amount in taxes:
        for unit, part in _shares(across, employee_id, unit_id, amount):
            key = (unit, "official", TAXES)
            lines[key] = lines.get(key, Decimal("0")) + part
    return lines


def _units_of(payrun) -> dict:
    """Точки каждого человека на этот период (D055).

    Спрашивается одним запросом на весь расчёт: людей три десятка, и запрос на
    каждого дал бы столько же обращений к базе за тем же ответом.
    """
    from django.db import connection

    with connection.cursor() as cursor:
        cursor.execute(
            """select employee_id, unit_id, share
                 from employee_units
                where tenant_id = %s
                  and valid_from <= %s
                  and (valid_to is null or valid_to > %s)""",
            [str(payrun.tenant_id), payrun.period, payrun.period],
        )
        found: dict = {}
        for employee_id, unit_id, share in cursor.fetchall():
            found.setdefault(employee_id, []).append((unit_id, share))
    return found


def _shares(across: dict, employee_id, unit_id, amount: Decimal) -> list:
    """Как разделить сумму человека между его точками (D055).

    Три случая, и все три названы владельцем:

    * точек несколько — делим между ними (умолчание «поровну», доля у строки
      переопределяет);
    * точка одна (или привязок нет, но ведомость знает точку) — всё туда;
    * точек нет вовсе — это офис: сумма уходит без точки и разносится общим
      правилом, «потому что офис на всех работает, вне зависимости».

    Копейки раскладываются накопленной суммой — тем же приёмом, что в
    `allocation_plan` и в ручном разнесении: иначе на трёх точках сумма долей
    не сойдётся с целым.
    """
    mine = across.get(employee_id) or []
    if not mine:
        return [(unit_id, amount)]
    if len(mine) == 1:
        return [(mine[0][0], amount)]

    weights = [(unit, share if share is not None else Decimal("1")) for unit, share in mine]
    total = sum(weight for _unit, weight in weights)
    if total <= 0:
        return [(unit_id, amount)]

    parts, done, carried = [], Decimal("0"), Decimal("0")
    for unit, weight in sorted(weights, key=lambda row: str(row[0])):
        carried += weight
        upto = (amount * carried / total).quantize(Decimal("0.01"))
        parts.append((unit, upto - done))
        done = upto
    return parts


def _line(code: str):
    from core.models import PnlItem

    found = PnlItem.objects.filter(tenant__isnull=True, code=code).first()
    if found is None:
        # Отсутствие строки справочника — дефект схемы, а не ввод: молча
        # положить зарплату «куда-нибудь» хуже, чем упасть здесь.
        raise LookupError(f"строки P&L «{code}» нет в справочнике")
    return found


def _pnl_item(code: str):
    return _line(code).id


def _line_title(code: str) -> str:
    """Название строки факта — то же, что у строки отчёта.

    Снимок на момент записи, как у остальных денег. Берётся из справочника, а
    не пишется здесь словами: у демо строки P&L названы по-английски, и русский
    литерал уехал бы в его данные (правило «демо всегда на английском»).
    """
    return _line(code).title


def _last_day(period):
    from calendar import monthrange
    from datetime import date

    return date(period.year, period.month, monthrange(period.year, period.month)[1])
