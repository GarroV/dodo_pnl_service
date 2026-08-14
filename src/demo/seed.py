"""
Наполнение демо-базы: организация, люди, три месяца и посчитанные ведомости.

Это не второй `seed_dev`. Разница не в объёме, а в назначении: сид разработки
воспроизводит формат таблицы партнёра (восемь сербских листов, один месяц) и на
нём стоит вся приёмка стройки, а демо показывает **продукт** человеку, который
видит его впервые, — на английском, за три месяца, с закрытыми периодами и
живым отчётом расхождений. Свести их в одну команду значило бы, что каждая
правка ради показа двигает числа приёмки.

Порядок шагов важен и объяснён по месту. Главное, что нельзя переставить:
периоды считаются **по возрастанию месяцев**, а утверждаются сразу после своего
расчёта. Иначе перенос разницы задним числом и запрет на изменение утверждённого
расчёта начинают спорить друг с другом (та же ловушка, что описана в уборке
`seed_dev`, только с другой стороны).
"""
from __future__ import annotations

import json
import uuid
from datetime import date
from decimal import Decimal

from django.conf import settings
from django.db import connection, transaction
from django.utils.timezone import now

from core import models
from core.roles import ROLE_ORDER, ROLE_SHAPES
from core.rules import import_presets
from payrun.calc import calculate_period
from payrun.lifecycle import approve

from . import dataset
from .dataset import MONTHS, PEOPLE, UNITS, Month, Person
from .guard import require_demo_data

__all__ = ["ROLES", "TENANT_CODE", "det_id", "seed_demo"]

TENANT_CODE = "demo"
PRESET_CODE = "serbia-2026"
COUNTRY = "RS"

# Своё пространство имён для детерминированных id: демо и сид разработки не
# должны совпадать ссылками даже случайно — они живут в разных базах, и
# одинаковые id создали бы ложное впечатление, что это одни и те же данные.
NS = uuid.UUID("6f2b7f7e-0000-4000-8000-0000000000de")

ALL_LEDGERS = ["official", "supplementary", "internal"]

# Статьи P&L. По-английски, как и всё в демо: посетитель видит их в выгрузке.
PNL_ITEMS = [
    ("revenue", "Revenue", "revenue", 10),
    ("food_cost", "Food cost", "expense", 20),
    ("labour_cost", "Labour cost", "expense", 40),
    ("payroll_taxes", "Payroll taxes", "expense", 50),
    # Расходная часть, ради которой продукт собирает траты из кассы (T113).
    # Строк отчёта единицы, а статей расхода под ними десятки — см.
    # `dataset.EXPENSE_ITEMS`.
    ("utilities", "Utilities", "expense", 60),
    ("rent", "Rent", "expense", 62),
    ("other_opex", "Other operating costs", "expense", 64),
    ("total", "Result", "subtotal", 90),
]

# Язык демо. Правило владельца: демо всегда англоязычное, независимо от языка
# продукта. Отсюда и берутся английские подписи правил — из самого пресета
# страны, который с T092 несёт все языки продукта сразу.
#
# Раньше здесь лежал словарь из двадцати английских подписей, который клался в
# базу переопределениями партнёра. Он больше не нужен и удалён намеренно: это
# была вторая правда рядом с пресетом, и расходиться они начали бы молча —
# поменянная подпись в пресете просто не доехала бы до демо. А главное, словарь
# **маскировал дефект**: демо выглядело английским даже тогда, когда сам продукт
# показывал колонки по-русски, и полтора месяца никто этого не видел.
DEMO_LANGUAGE = "en"

# Роли демо. Коды те же, что у продукта, — на них стоят политики базы и права;
# по-английски здесь только подписи. Форма роли (регистры, точка, права) НЕ
# переписывается рядом, а берётся из `core.roles`: демо обязано показывать ту же
# разницу видимости, которую партнёр получит у себя, а не выдуманную.
#
# Пока форма была записана здесь второй раз, обещание разъехалось с делом
# (issue #91): у администратора сети в продукте все три регистра (T089), а в
# демо остался один официальный. Демо показывало сломанное состояние продукта —
# администратора, которому некому поменять ставку курьеру. Найдено чтением
# комментария, а не проверкой: два списка одного и того же расходятся молча.
ROLE_TITLES = {
    "director": "Operations director",
    "accountant": "Accountant",
    "manager": "Unit manager (Novi Sad Bulevar)",
    "admin": "Network administrator",
}

ROLES = [
    (
        code, ROLE_TITLES[code],
        list(ROLE_SHAPES[code].ledgers),
        ROLE_SHAPES[code].unit,
        list(ROLE_SHAPES[code].permissions),
    )
    for code in ROLE_ORDER
]


def det_id(*parts: str) -> uuid.UUID:
    """Детерминированный id: каждый сброс демо даёт те же ссылки.

    Не мелочь: ссылку на конкретную ведомость показывают заказчику, и после
    ночного сброса она обязана открывать ту же страницу, а не 404.
    """
    return uuid.uuid5(NS, "/".join(parts))


def demo_password() -> str:
    """Пароль демо-учёток.

    Секретом не является и не притворяется: в демо-базе нет ничего, кроме
    выдуманных людей, а вход и так открыт одной кнопкой. Переменная нужна не для
    тайны, а чтобы на площадке пароль можно было сменить, не пересобирая образ.
    """
    return getattr(settings, "DEMO_USER_PASSWORD", None) or "demo-only-not-a-secret"


def seed_demo(*, log=None) -> dict:
    """Наполнить демо-базу целиком. Идемпотентна: тенант пересобирается.

    Предохранитель по данным вызывается **здесь**, а не только в команде: это
    единственная точка, через которую наполнение попадает в базу, и проверка
    обязана стоять на ней, а не на одном из путей к ней. Проверка адресов живёт
    в команде — там известно окружение.
    """
    say = log or (lambda *_: None)

    with transaction.atomic():
        # Раньше первой записи: в базе с чужим партнёром наполнение демо не
        # выполняется вовсе (см. demo.guard).
        require_demo_data(connection, TENANT_CODE)
        wipe()
        import_presets()
        tenant = _org()
        items = _pnl_items()
        groups = _groups(tenant, items)
        _roles_and_users(tenant)
        _calendar()
        people = _people(tenant, groups)
        say(f"организация и {people} сотрудников готовы")
        _expense_items(tenant, items)
        spent = _expenses(tenant)
        say(f"расходов из кассы: {spent}")

    # Расчёт каждого месяца — своей транзакцией, а не общей: `calculate_period`
    # открывает свою и утверждает результат отдельным переходом, у которого
    # своя проверка цикла. Одна транзакция на всё склеила бы состояния, которых
    # в жизни не бывает.
    calculated = []
    for month in MONTHS:
        outcome = calculate_period(
            tenant_id=tenant.id, period=month.period, visible_ledgers=ALL_LEDGERS
        )
        say(f"{month.period:%Y-%m}: посчитано строк {outcome.slips}")
        if month.approved:
            _approve(tenant, month)
            say(f"{month.period:%Y-%m}: утверждён")
        calculated.append(month.period)

    return {
        "tenant": tenant,
        "people": people,
        "periods": calculated,
        "password": demo_password(),
    }


# --- шаги ---------------------------------------------------------------------


def wipe() -> None:
    """Снести данные демо-тенанта.

    Обычный путь сброса — пересоздание базы из эталона (D016), и уборка ему не
    нужна. Она нужна повторному `seed_demo` на уже наполненной базе: без неё
    команда падала бы на первом же уникальном ключе, и «пересобрать демо» стало
    бы «сначала удалить базу руками».

    Порядок повторяет уборку сида разработки, и по тем же причинам: утверждённый
    расчёт держит триггер (его нужно открыть с причиной), переносы разницы не
    дают открыть месяц-источник (поэтому от позднего месяца к раннему),
    замороженные строки не удаляются, пока заморозка не снята.
    """
    tenants = models.Tenant.objects.filter(code=TENANT_CODE)
    if not tenants.exists():
        return

    approved = models.Payrun.objects.filter(tenant__in=tenants, status="approved")
    if approved.exists():
        with connection.cursor() as cursor:
            for payrun_id in list(
                approved.order_by("-period").values_list("id", flat=True)
            ):
                cursor.execute(
                    "select set_config('app.transition_reason', %s, true)",
                    ["пересборка демо-стенда"],
                )
                models.Payrun.objects.filter(pk=payrun_id).update(status="reopened")

    models.RetroAdjustment.objects.filter(tenant__in=tenants).delete()
    models.PayslipFreeze.objects.filter(
        tenant__in=tenants, released_at__isnull=True
    ).update(released_at=now())

    for model in (
        # Факты идут первыми: ссылки на точку, юрлицо и статью у факта
        # `PROTECT`, и пока факт жив, точку не удалить. Удалять их можно только
        # **после** отката утверждённых расчётов выше — факт закрытого месяца не
        # даёт удалить `facts_guard`, а откат расчёта открывает месяц обратно
        # тем же триггером, которым закрыл. Тот же порядок и по той же причине
        # стоит в сиде разработки: там он стоил падения на каждом стенде, где
        # хоть раз внесли расход.
        models.Fact, models.SourceDocument, models.FactBatch,
        models.PayComponent, models.Payslip, models.Payrun, models.Timesheet,
        models.EmploymentTerm, models.Employee, models.EmployeeGroup,
        models.Membership, models.Role, models.Period, models.AllocationRule,
        # Статья — после правил разнесения: ссылка правила на статью `PROTECT`
        # (T111), и правило, оставшееся без статьи, разносило бы неизвестно что.
        models.ExpenseItem,
        models.Counterparty, models.Unit, models.LegalEntity,
        # Английские подписи правил — тоже данные тенанта, и повторный прогон
        # обязан их пересоздать, а не наткнуться на прежние.
        models.RuleOverride,
    ):
        model.objects.filter(tenant__in=tenants).delete()
    models.PnlItem.objects.filter(tenant__in=tenants).delete()
    tenants.delete()
    models.User.objects.filter(
        pk__in=[det_id("user", role[0]) for role in ROLES]
    ).delete()


def _org() -> models.Tenant:
    tenant = models.Tenant.objects.create(
        id=det_id("tenant", TENANT_CODE),
        code=TENANT_CODE,
        title="Dodo Serbia (demo)",
        country_code=COUNTRY,
        base_currency="RSD",
        report_currency="EUR",
    )
    entities = {
        title: models.LegalEntity.objects.create(
            id=det_id("legal_entity", title), tenant=tenant,
            title=title, tax_number=tax_number,
        )
        for title, tax_number in dataset.LEGAL_ENTITIES
    }
    for code, title, entity_title in UNITS:
        models.Unit.objects.create(
            id=det_id("unit", code), tenant=tenant,
            legal_entity=entities[entity_title],
            code=code, title=title, opened_at=date(2023, 1, 1),
        )
    for month in MONTHS:
        models.Period.objects.create(
            id=det_id("period", str(month.period)), tenant=tenant,
            period=month.period,
            # **Все месяцы заводятся открытыми, включая те, что станут
            # закрытыми.** Состояние учётного месяца — следствие состояния
            # расчёта (T094): его выставляет триггер, когда расчёт утверждают,
            # и руками его менять запрещено (`period_status_guard`). Раньше
            # здесь стояло `closed` для утверждаемых месяцев, и это было
            # безобидно ровно до появления расходов: `facts_guard` не принимает
            # факт в закрытый месяц ни от кого, включая владельца схемы, — то
            # есть траты июня и июля наполнение положить бы уже не смогло.
            # Закрывает их теперь утверждение расчёта, тем же путём, каким это
            # происходит у партнёра.
            status="open",
            closed_at=None,
        )
    return tenant


def _pnl_items() -> dict[str, models.PnlItem]:
    items = {}
    for code, title, kind, order in PNL_ITEMS:
        items[code], _ = models.PnlItem.objects.get_or_create(
            tenant=None, code=code,
            defaults={"id": det_id("pnl_item", code), "title": title,
                      "kind": kind, "sort_order": order},
        )
    return items


def _groups(tenant, items: dict) -> dict[str, models.EmployeeGroup]:
    """Группы — из пресета страны, подписи — английские.

    Схема и регистр берутся из того же пресета, по которому считает движок:
    второй источник истины здесь означал бы, что демо показывает не тот регистр,
    в котором лежит строка.
    """
    from payroll import load_preset

    # Пресет читается на языке демо: подписи групп ложатся в базу английскими
    # оттуда же, откуда движок берёт схему и регистр (T092). Второго списка
    # названий в этом файле больше нет.
    preset = load_preset(PRESET_CODE, DEMO_LANGUAGE)
    groups = {}
    for code, body in preset["groups"].items():
        groups[code] = models.EmployeeGroup.objects.create(
            id=det_id("group", code), tenant=tenant, code=code,
            title=body.get("title") or code,
            scheme=body.get("scheme", "standard"),
            ledger=body.get("ledger", "official"),
            pnl_item=items["labour_cost"],
        )
    return groups


def _roles_and_users(tenant) -> None:
    units = {u.code: u for u in models.Unit.objects.filter(tenant=tenant)}
    password = demo_password()
    for code, title, ledgers, unit, permissions in ROLES:
        role = models.Role.objects.create(
            id=det_id("role", code), tenant=tenant, code=code, title=title,
            visible_ledgers=list(ledgers), permissions=list(permissions),
        )
        user = models.User.objects.create_user(
            username=code, password=password,
            id=det_id("user", code), full_name=title,
        )
        models.Membership.objects.create(
            id=det_id("membership", code), tenant=tenant,
            user_id=user.pk, role=role,
            unit_ids=[units[unit].id] if unit else None,
        )


def _calendar() -> None:
    """Производственный календарь на все три месяца.

    Пресет страны знает только июнь, а демо показывает три месяца. Норму месяца
    видно на странице периода, и «норма 176» в августе, где её 168, — ровно то
    правдоподобное неверное число, от которого продукт отказывается везде.
    """
    for month in MONTHS:
        models.Calendar.objects.update_or_create(
            country_code=COUNTRY, period=month.period,
            defaults={
                "norm_hours": month.norm_hours,
                "working_days": month.working_days,
                "holidays": [],
            },
        )


def _people(tenant, groups: dict) -> int:
    units = {u.code: u for u in models.Unit.objects.filter(tenant=tenant)}
    director = det_id("user", "director")

    for person in PEOPLE:
        employee = models.Employee.objects.create(
            id=det_id("employee", person.key), tenant=tenant,
            external_id=person.key,
            first_name=person.first, last_name=person.last,
            hired_at=person.hired,
        )
        for index, (valid_from, valid_to, rate) in enumerate(
            dataset.employment_versions(person)
        ):
            models.EmploymentTerm.objects.create(
                id=det_id("term", person.key, str(index)), tenant=tenant,
                employee=employee, group=groups[person.group],
                unit=units[person.unit],
                base_rate=rate, coefficient=person.coefficient,
                scheme=person.scheme, valid_from=valid_from, valid_to=valid_to,
            )
        for month in MONTHS:
            row = dataset.timesheet_for(person, month)
            if row is None:
                continue
            models.Timesheet.objects.create(
                id=det_id("timesheet", person.key, f"{month.period:%Y-%m}"),
                tenant=tenant, employee=employee, unit=units[person.unit],
                period=month.period,
                insured_hours=row.insured_hours,
                norm_hours=row.norm_hours,
                # Decimal в jsonb уходит строкой: чисел с плавающей точкой в
                # деньгах и часах в этом продукте нет нигде.
                hours={k: str(v) for k, v in row.hours.items()},
                deduction=row.deduction,
                cash_payout=row.cash_payout,
                manual_correction=row.manual_correction,
                correction_reason=row.correction_reason or None,
                # Правка руками обязана иметь автора (D025): правка без следа —
                # это сумма, которую через полгода никто не объяснит.
                corrected_by=director if row.manual_correction is not None else None,
                source="manual",
            )
    return len(PEOPLE)


# --- расходы из кассы (T113) ---------------------------------------------------


def _expense_items(tenant, items: dict) -> None:
    """Справочник статей расхода и правила их разнесения.

    У партнёра этот справочник поставляется **пустым** (Q015): список придёт с
    файла бухгалтера Сербии, а выдуманный дал бы разные названия одной трате.
    Демо — единственное место, где он наполнен, и это не противоречие: демо
    показывает продукт наполненным, иначе показывать нечего.

    Названия кладутся сразу тремя языками, а не одним английским. Демо
    открывается по-английски всегда (`UI_LANGUAGE=en`), но статья — это данные
    партнёра, и правило «названия по кодам языков интерфейса» проверяется здесь
    тоже: одноязычная статья в демо скрыла бы поломку выбора языка.
    """
    for item in dataset.EXPENSE_ITEMS:
        row = models.ExpenseItem.objects.create(
            id=det_id("expense_item", item.code), tenant=tenant, code=item.code,
            titles={code: item.title for code, _name in settings.LANGUAGES},
            pnl_item=items[item.pnl_code], valid_from=date(2023, 1, 1),
        )
        if item.spread is None:
            continue
        models.AllocationRule.objects.create(
            id=det_id("allocation_rule", item.code), tenant=tenant,
            expense_item=row, pnl_item=items[item.pnl_code],
            method=item.spread, ledger="official", valid_from=date(2023, 1, 1),
        )


def _expenses(tenant) -> int:
    """Траты из кассы — тем же путём, каким их пишет продукт.

    Через `upsert_fact`, а не прямым `insert`: идемпотентность, версионирование
    и защита закрытого месяца живут в этой функции базы, и второй путь записи
    обошёл бы их все сразу. Наполнение, которое кладёт данные мимо продукта,
    рано или поздно кладёт то, чего продукт положить не может, — и демо
    начинает показывать невозможное.

    Автор у каждой траты настоящий: свою точку заводит её управляющий, остальное
    — бухгалтер. В демо на это смотрят: «кто это внёс» — первый вопрос к
    незнакомой сумме.
    """
    items = {item.code: item for item in dataset.EXPENSE_ITEMS}
    written = 0
    for spending in dataset.expenses():
        item = items[spending.item]
        unit_id = det_id("unit", spending.unit) if spending.unit else None
        payload = {
            "tenant_id": str(tenant.id),
            "period": spending.on.replace(day=1).isoformat(),
            "doc_date": spending.on.isoformat(),
            "pnl_item_id": str(det_id("pnl_item", item.pnl_code)),
            "expense_item_id": str(det_id("expense_item", item.code)),
            "ledger": spending.ledger,
            "amount": str(spending.amount),
            "title": item.title,
            "note": spending.note,
            "channel": "cash",
            "source": "manual",
            # Ключ идемпотентности выводится из самой траты, а не случайный:
            # повторный `seed_demo` на наполненной базе обязан не удвоить
            # расходы. Приставка та же, что у формы, — расход демо ничем не
            # отличается от расхода, внесённого руками.
            "dedup_key": f"manual:cash:demo-{item.code}-{spending.on:%Y%m%d}"
                         f"-{spending.unit or 'network'}",
            "created_by": str(det_id("user", "manager" if spending.unit == "NS1"
                                     else "accountant")),
            "allocation": "direct" if unit_id else "pending",
        }
        if unit_id:
            payload["unit_id"] = str(unit_id)

        with connection.cursor() as cursor:
            cursor.execute(
                "select fact_id from upsert_fact(%s::jsonb)", [json.dumps(payload)]
            )
            fact_id = cursor.fetchone()[0]
            # Расход на всю сеть разносится сразу при внесении — ровно так же,
            # как это делает форма (T111). Правила нет — сумма остаётся
            # нераспределённой и **видимой**; это состояние в демо нужно не
            # меньше разнесённого.
            if unit_id is None:
                cursor.execute("select allocate_fact(%s)", [fact_id])
        written += 1
    return written


def _approve(tenant, month: Month) -> None:
    payrun = models.Payrun.objects.filter(
        tenant_id=tenant.id, period=month.period
    ).first()
    approve(payrun, actor_id=det_id("user", "director"))


def person_by_key(key: str) -> Person | None:
    for person in PEOPLE:
        if person.key == key:
            return person
    return None


def money(value) -> Decimal:
    return Decimal(str(value or 0))
