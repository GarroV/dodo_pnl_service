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
            """select p.unit_id, c.ledger::text, sum(c.amount)
                 from pay_components c
                 join payslips p on p.id = c.payslip_id
                where p.payrun_id = %s
                group by p.unit_id, c.ledger""",
            [str(payrun.id)],
        )
        for unit_id, ledger, amount in cursor.fetchall():
            lines[(unit_id, ledger, LABOUR)] = lines.get((unit_id, ledger, LABOUR),
                                                         Decimal("0")) + amount

        # Налоги и взносы — разница между полной стоимостью и тем, что дошло до
        # людей. Регистр официальный: налог с неофициальной части не платится
        # по определению, а не по выбору партнёра.
        cursor.execute(
            """select p.unit_id, sum(t.total_cost - t.net)
                 from payslip_totals t
                 join payslips p on p.id = t.payslip_id
                where p.payrun_id = %s
                group by p.unit_id""",
            [str(payrun.id)],
        )
        for unit_id, amount in cursor.fetchall():
            lines[(unit_id, "official", TAXES)] = lines.get((unit_id, "official", TAXES),
                                                            Decimal("0")) + amount
    return lines


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
