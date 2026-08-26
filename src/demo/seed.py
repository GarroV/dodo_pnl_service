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

import hashlib
import json
import uuid
from datetime import date
from decimal import Decimal
from pathlib import Path

from django.conf import settings
from django.db import connection, transaction
from django.utils.timezone import now

from core import models
from core.role_delivery import product_shape
from core.roles import ROLE_ORDER, ROLE_SHAPES
from core.rules import import_presets
from payrun.calc import calculate_period
from payrun.lifecycle import approve

from . import dataset
from .dataset import MONTHS, PEOPLE, UNITS, Month, Person
from .guard import require_demo_data

# Готовые снимки бумаг с точек (T174). Лежат файлами в репозитории, а не
# рисуются наполнением: пересобираются `tools/make_demo_scans.py`.
FIXTURES = Path(__file__).resolve().parent / "fixtures"

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


# Простой вход для того, кто оказался на форме пароля: логины ролей там не
# угадать. Значения намеренно простые и намеренно лежат в коде — в демо-базе
# только выдуманные люди, а вход в демо и так открыт одной кнопкой.
GUEST_LOGIN = "demodemo"
# Роль простой учётки — та же, которой открывается демо по кнопке. Константа
# своя, а не импорт из `views`: сид не должен зависеть от слоя представлений.
# Согласие двух значений держит тест — разъедься они молча, гость входил бы
# паролем в одну роль, а кнопкой в другую.
GUEST_ROLE = "director"

# Учётка, которой смотрят продукт целиком: логин и пароль `admin` (запрос
# владельца 2026-08-26, «для теста и отсмотра»). Отдельной учётки для этого не
# нужно — администратор сети и так может всё (D052), поэтому «осмотр» это
# просто его учётка с паролем, который не надо диктовать по буквам.
ADMIN_ROLE = "admin"


def demo_guest_password() -> str:
    """Пароль простой учётки демо. Меняется переменной, как и остальные."""
    return getattr(settings, "DEMO_GUEST_PASSWORD", None) or "password"


def role_password(code: str) -> str:
    """Пароль учётки роли демо. У администратора сети свой, у остальных общий.

    Одним местом, потому что спрашивают его двое — сид, который учётки заводит,
    и вход по кнопке демо, который ими входит. Разъехались бы — кнопка «Network
    administrator» перестала бы пускать, и виновата была бы не она.
    """
    return demo_admin_password() if code == ADMIN_ROLE else demo_password()


def demo_admin_password() -> str:
    """Пароль учётки осмотра. Простой намеренно: его диктуют вслух.

    Секретом не является ровно в той же мере, что и остальные пароли демо:
    база отдельная, люди в ней выдуманные, а вход и так открыт одной кнопкой.
    Переменной меняется на случай, если демо однажды покажут туда, где даже
    выдуманное лучше прикрыть.
    """
    return getattr(settings, "DEMO_ADMIN_PASSWORD", None) or "admin"


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
        _tills(tenant)
        _expense_items(tenant, items)
        spent = _expenses(tenant)
        say(f"расходов из кассы: {spent}")
        counterparties = _counterparties(tenant)
        say(f"контрагентов: {len(counterparties)}")
        invoiced, paid = _supplier_documents(tenant, counterparties)
        say(f"счетов поставщикам: {invoiced}, платежей: {paid}")
        handed, waiting = _papers(tenant, counterparties)
        say(f"бумаг с точек: {handed}, из них ждут разбора: {waiting}")

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
        # Касса — после фактов и раньше точек: ссылка факта на кассу `PROTECT`,
        # ссылка кассы на точку тоже. Тот же порядок и тот же довод, что у
        # статей расходов; ошибиться в нём — значит, что сброс демо перестанет
        # работать в тот день, когда в демо появится первая касса, и заметят это
        # по протухшему стенду, а не по красному тесту (T145).
        models.Till,
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
    for code, title, ledgers, unit, permissions in ROLES:
        role = models.Role.objects.create(
            id=det_id("role", code), tenant=tenant, code=code, title=title,
            visible_ledgers=list(ledgers), permissions=list(permissions),
            # Снимок формы — по тому же доводу, что в сиде разработки (T169):
            # роль без снимка доставка приняла бы за правку человека и обошла
            # стороной, то есть демо однажды осталось бы на старой форме.
            shipped_shape=product_shape(code),
        )
        user = models.User.objects.create_user(
            # Пароль у администратора сети свой: его учёткой продукт смотрят
            # целиком, и `admin` / `admin` диктуется вслух без запинки.
            username=code, password=role_password(code),
            id=det_id("user", code), full_name=title,
        )
        models.Membership.objects.create(
            id=det_id("membership", code), tenant=tenant,
            user_id=user.pk, role=role,
            unit_ids=[units[unit].id] if unit else None,
        )

    # Простая учётка на случай, когда человек вышел из демо и попал на форму
    # входа: логины ролей («accountant», «director») там не угадать, а спросить
    # не у кого. Заводится здесь, а не руками на площадке, — иначе исчезала бы
    # при каждом ночном сбросе демо, и «у меня вчера работало» повторялось бы
    # каждое утро. Права те же, что у оперативного директора: демо показывают
    # целиком, урезать смотрящему нечего.
    guest = models.User.objects.create_user(
        username=GUEST_LOGIN, password=demo_guest_password(),
        id=det_id("user", GUEST_LOGIN), full_name="Demo guest",
    )
    models.Membership.objects.create(
        id=det_id("membership", GUEST_LOGIN), tenant=tenant,
        user_id=guest.pk, role=models.Role.objects.get(tenant=tenant, code=GUEST_ROLE),
        unit_ids=None,
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


def _tills(tenant) -> None:
    """Кассы точек (T145, D039). Регистр расхода следует из кассы, а не из формы.

    В демо их четыре, и у NS1 их две — обычная и внутренняя. Одна касса на точку
    показала бы поле, но не показала бы правило: расход из внутренней кассы
    уходит во внутренний регистр сам.

    Остатка по кассе в демо нет, потому что его нет в продукте (D040): касса —
    источник денег и признак регистра, а не кассовая книга.
    """
    for till in dataset.TILLS:
        models.Till.objects.create(
            id=det_id("till", till.code), tenant=tenant, code=till.code,
            title=till.title, ledger=till.ledger,
            unit_id=det_id("unit", till.unit),
        )


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
        if spending.till:
            # Касса кладётся как есть, а регистр остаётся в поле рядом: в демо
            # видно и то, откуда деньги, и что регистр с кассой сходится (D039).
            payload["till_id"] = str(det_id("till", spending.till))
        if spending.vat:
            # Только ставка: сумму налога считает база (T146), и второй расчёт
            # рядом с первым разошёлся бы с ним на копейку.
            payload["vat_rate"] = spending.vat

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


# --- поставщики: контрагенты, счета, платежи (T151-T153) -----------------------


def _counterparties(tenant) -> dict[str, models.Counterparty]:
    """Контрагенты демо: поставщики со счетами и платежами.

    Ключ Dodo IS остаётся пустым (T150): сведение со справочником поставщиков
    Dodo IS случится только в шестой очереди, и заполненное поле сегодня
    соврало бы про то, что оно уже случилось.
    """
    accountant = det_id("user", "accountant")
    return {
        title: models.Counterparty.objects.create(
            id=det_id("counterparty", title), tenant=tenant, title=title,
            valid_from=date(2023, 1, 1), created_by=accountant,
        )
        for title in dataset.counterparties()
    }


def _service_pnl_item(code: str) -> models.PnlItem:
    """Служебная строка P&L, заведённая миграцией `0243`.

    Ищется по коду, а не по приколоченному uuid — тем же способом, каким её
    находит продукт (`web.suppliers.line`). Код — то, чем строка названа в
    схеме, и по нему же читается отказ, если строки вдруг не окажется.
    """
    return models.PnlItem.objects.get(tenant__isnull=True, code=code)


def _supplier_documents(tenant, counterparties: dict) -> tuple[int, int]:
    """Счета поставщикам и платежи по ним — через `upsert_document`/`upsert_fact`.

    Тем же путём, каким пишет продукт (`web.suppliers.record_invoice`, `.pay`):
    свой `insert` здесь не заводится, потому что идемпотентность,
    версионирование и защита закрытого месяца живут в этих двух функциях базы,
    а не в наполнении. Документ пишется первым, а строка — вторым: платёж
    ссылается на документ, которого до записи счёта ещё нет.

    Пять счетов показывают то, ради чего события в продукте разведены:
    разницу даты документа и периода учёта (№1), частичную оплату — остаток
    числом (№2), обязательство без единой оплаты на конец месяца (№3), и
    строку без статьи, из которой инбокс классификации (T152) вообще не пуст
    (№4).
    """
    expense_items = {item.code: item for item in dataset.EXPENSE_ITEMS}
    unclassified = _service_pnl_item("unclassified")
    supplier_payment = _service_pnl_item("supplier_payment")

    # Куда лёг счёт — нужно платежам: точка, регистр и автор берутся у счёта,
    # а не выбираются заново (см. `web.suppliers.pay`).
    landed: dict[str, dict] = {}

    for inv in dataset.invoices():
        counterparty = counterparties[inv.counterparty]
        unit_id = det_id("unit", inv.unit)
        author = det_id("user", "manager" if inv.unit == "NS1" else "accountant")

        doc_payload = {
            "tenant_id": str(tenant.id),
            "counterparty_id": str(counterparty.id),
            "kind": "invoice",
            "source": "manual",
            "external_id": inv.key,
            "doc_date": inv.doc_date.isoformat(),
            "period": inv.period.isoformat(),
            "total_amount": str(inv.amount),
            "created_by": str(author),
        }
        if inv.number:
            doc_payload["doc_number"] = inv.number
        with connection.cursor() as cursor:
            cursor.execute(
                "select upsert_document(%s::jsonb)", [json.dumps(doc_payload)]
            )
            document_id = cursor.fetchone()[0]

        if inv.item is None:
            pnl_item_id = unclassified.id
            expense_item_id = None
            title = counterparty.title
        else:
            expense_item = expense_items[inv.item]
            pnl_item_id = det_id("pnl_item", expense_item.pnl_code)
            expense_item_id = det_id("expense_item", expense_item.code)
            title = expense_item.title

        line_payload = {
            "tenant_id": str(tenant.id),
            "period": inv.period.isoformat(),
            "doc_date": inv.doc_date.isoformat(),
            "unit_id": str(unit_id),
            "pnl_item_id": str(pnl_item_id),
            "counterparty_id": str(counterparty.id),
            "ledger": "official",
            "amount": str(inv.amount),
            "title": title,
            "source": "manual",
            "document_id": str(document_id),
            "line_no": 1,
            "dedup_key": f"manual:invoice:{inv.key}",
            "allocation": "direct",
            "created_by": str(author),
        }
        if expense_item_id is not None:
            line_payload["expense_item_id"] = str(expense_item_id)
        if inv.vat:
            # Только ставка: сумму налога считает база (`vat_of`), а второй
            # расчёт рядом с первым однажды разошёлся бы с ним на копейку.
            line_payload["vat_rate"] = inv.vat
        if inv.note:
            line_payload["note"] = inv.note

        with connection.cursor() as cursor:
            cursor.execute(
                "select fact_id from upsert_fact(%s::jsonb)", [json.dumps(line_payload)]
            )

        landed[inv.key] = {
            "document_id": document_id, "unit_id": unit_id,
            "counterparty_id": counterparty.id, "author": author,
            "number": inv.number,
        }

    for pmt in dataset.payments():
        source_invoice = landed[pmt.invoice_key]
        payload = {
            "tenant_id": str(tenant.id),
            "period": pmt.on.replace(day=1).isoformat(),
            "doc_date": pmt.on.isoformat(),
            "unit_id": str(source_invoice["unit_id"]),
            "pnl_item_id": str(supplier_payment.id),
            "counterparty_id": str(source_invoice["counterparty_id"]),
            "ledger": "official",
            "amount": str(pmt.amount),
            # Название строки платежа — по счёту, а у счёта без номера по
            # поставщику: та же развилка, что в `web.suppliers._payment_title`.
            # Без неё демо однажды показало бы «Payment of invoice » с пустотой
            # на конце — счёт без номера в жизни обычное дело.
            "title": (f"Payment of invoice {source_invoice['number']}"
                      if source_invoice["number"] else "Payment to supplier"),
            # Канал денег для сверки кассы, а не для P&L — переводы из него
            # исключены по `kind` во всех отчётах разом (миграция `0243`).
            "channel": "bank",
            "source": "manual",
            "document_id": str(source_invoice["document_id"]),
            "dedup_key": f"manual:payment:{pmt.key}",
            "allocation": "direct",
            "created_by": str(source_invoice["author"]),
        }
        with connection.cursor() as cursor:
            cursor.execute(
                "select fact_id from upsert_fact(%s::jsonb)", [json.dumps(payload)]
            )

    return len(dataset.INVOICES), len(dataset.PAYMENTS)


def _papers(tenant, counterparties: dict) -> tuple[int, int]:
    """Бумаги, принесённые с точек: шапка, файл и — если разобрана — строка.

    Тем же путём, каким пишет продукт (`web.papers.hand_over`, а разбор —
    `web.suppliers.record_invoice`): своих `insert` в документы и факты здесь не
    заводится, потому что идемпотентность, версионирование и защита закрытого
    месяца живут в функциях базы, а не в наполнении.

    Автор у обеих бумаг — **управляющий**, и это не украшение. Роль управляющего
    в сборе первички ровно эта: донести бумагу (D047). Поставь автором
    бухгалтера, и демо показывало бы, что бумаги приносит сам разбирающий, то
    есть смысла в состоянии «ждёт разбора» не было бы видно.

    Возвращает «сколько принесли» и «сколько ждёт разбора» — второе число демо
    показывает в инбоксе, и наполнение обязано знать его само, а не выяснять
    повторной выборкой.
    """
    # Тип файла определяется тем же кодом, что и в продукте: второй ответ на
    # вопрос «что это за файл» однажды разошёлся бы с первым, и демо положило бы
    # в базу тип, которого продукт не принимает.
    from web.papers import PAPER_PREFIX, media_type_of

    expense_items = {item.code: item for item in dataset.EXPENSE_ITEMS}
    author = det_id("user", "manager")
    waiting = 0

    for paper in dataset.papers():
        content = (FIXTURES / paper.file).read_bytes()
        media_type = media_type_of(content)
        if not media_type:
            # Молча положить в демо файл, которого продукт не примет, нельзя:
            # карточка показала бы бумагу, а форма такую же отвергла бы — и
            # разошлись бы они только на глазах у смотрящего.
            raise RuntimeError(
                f"{paper.file}: продукт такой файл не принимает, "
                f"пересоберите tools/make_demo_scans.py"
            )

        counterparty = counterparties[paper.counterparty]
        doc_payload = {
            "tenant_id": str(tenant.id),
            "counterparty_id": str(counterparty.id),
            "kind": paper.kind,
            "source": "manual",
            "external_id": PAPER_PREFIX + paper.key,
            "doc_date": paper.doc_date.isoformat(),
            "unit_id": str(det_id("unit", paper.unit)),
            "total_amount": str(paper.stated) if paper.stated is not None else None,
            # Отметка «бумагу принесли»: по ней бумага стоит в инбоксе и по ней
            # же отличается от документа, у которого строки не записались.
            "handed_over_at": now().isoformat(),
            "payload": {"note": paper.note, "file_name": paper.file},
            "created_by": str(author),
        }
        with connection.cursor() as cursor:
            cursor.execute(
                "select upsert_document(%s::jsonb)",
                [json.dumps({k: v for k, v in doc_payload.items() if v is not None})],
            )
            document_id = cursor.fetchone()[0]

        models.DocumentFile.objects.update_or_create(
            document_id=document_id,
            defaults={
                "tenant_id": tenant.id,
                "media_type": media_type,
                "byte_size": len(content),
                "content": content,
                "sha256": hashlib.sha256(content).hexdigest(),
                "created_by": author,
            },
        )

        if paper.item is None:
            # Ни одной строки учёта — и это главное свойство состояния: суммы
            # 21 550.00 в P&L нет не потому, что отчёт её отфильтровал, а потому
            # что фильтровать нечего.
            waiting += 1
            continue

        expense_item = expense_items[paper.item]
        line_payload = {
            "tenant_id": str(tenant.id),
            "period": paper.period.isoformat(),
            "doc_date": paper.doc_date.isoformat(),
            "unit_id": str(det_id("unit", paper.unit)),
            "pnl_item_id": str(det_id("pnl_item", expense_item.pnl_code)),
            "expense_item_id": str(det_id("expense_item", expense_item.code)),
            "counterparty_id": str(counterparty.id),
            "ledger": "official",
            "amount": str(paper.stated),
            "title": expense_item.title,
            "source": "manual",
            "document_id": str(document_id),
            "line_no": 1,
            # Ключ строки — приставка счёта плюс внешний ключ бумаги ЦЕЛИКОМ,
            # ровно как его собирает разбор с карточки
            # (`papers.document_key` → `suppliers.record_invoice`).
            "dedup_key": f"manual:invoice:{PAPER_PREFIX}{paper.key}",
            "allocation": "direct",
            "vat_rate": "10",
            "created_by": str(author),
        }
        with connection.cursor() as cursor:
            cursor.execute(
                "select fact_id from upsert_fact(%s::jsonb)", [json.dumps(line_payload)]
            )

    return len(dataset.PAPERS), waiting


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
