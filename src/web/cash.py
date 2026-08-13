"""Правила внесения расхода из кассы (T109).

Экран здесь только один, а правил — три, и все три про деньги. Разложенные по
представлению, они разъехались бы с проверками при первой правке формы.

**Правило первое: точку отвергает база, а не форма.** Представление точку не
фильтрует и не подставляет её списком выбора — оно передаёт то, что пришло, в
`upsert_fact`, а тот пишет под политиками (`unit_visibility` на `facts`). Так
работает D014: разграничение живёт в базе целиком, и забытый фильтр в новом
экране даёт отказ, а не чужую строку. Фильтр в представлении был бы вторым
источником истины о доступе — и однажды разошёлся бы с первым молча.

Отсюда же следует вид отказа: чужая точка, несуществующая и точка чужого
партнёра отвечают **одинаково**. Разный ответ означал бы, что перебором
значений в форме составляется список чужих точек, ни одной из них не увидев
(D023).

**Правило второе: закрытый месяц не принимает записей вовсе.** Это не решение
этого модуля, а свойство схемы: триггер `facts_guard` (миграция `0230_facts`)
отвергает и вставку, и правку, и удаление факта в закрытом периоде — причём для
всех, включая суперпользователя. Значит расход, датированный внутри
утверждённого месяца, деть в него некуда, и вариантов ровно два: отказать или
положить его рядом.

Кладём рядом — по D020: разница задним числом ложится в **текущий** период со
ссылкой на исходный. Ссылка здесь не текстом в примечании, а строением строки:
`doc_date` остаётся исходной датой расхода, а `period` становится текущим
месяцем. То есть «за июнь, учтено в августе» читается из самих данных, а не из
слов, которые никто не разберёт запросом. Человеку при этом сказано словами —
молчаливой подмены месяца быть не должно.

**Правило третье: повторная запись идёт заменой, а не правкой на месте.** У
формы есть ключ записи (`entry_key`), он же — ключ идемпотентности факта. Тот же
ключ с теми же данными не пишет ничего (двойное нажатие «Сохранить» не
превращает один расход в два), тот же ключ с другими данными заводит новую
версию: старая помечается заменённой (`revision`, `superseded_at`,
`superseded_by`), новая встаёт рядом, а действующей остаётся одна — поэтому
сумма не удваивается. Всё это умеет `upsert_fact`; здесь только ключ.
"""
from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import date

from django.conf import settings
from django.db import Error as DatabaseError
from django.db import connection, transaction
from django.utils.translation import gettext as _

from core.models import ExpenseItem, Period

# Канал денег и источник факта. Строками, а не настройкой: это и есть определение
# того, что такое «расход из кассы, внесённый руками».
CASH_CHANNEL = "cash"
MANUAL_SOURCE = "manual"

# Приставка ключа идемпотентности. Она же не даёт ключу из формы столкнуться с
# ключами других источников: коннектор и импорт выписки считают свои ключи сами,
# и совпадение означало бы, что ручной расход заменил собой строку выписки.
DEDUP_PREFIX = "manual:cash:"


class CashRefused(Exception):
    """Расход не принят по состоянию данных. Сообщение показывается как есть.

    409, а не 400: с формой всё в порядке, не в порядке положение дел — месяцу
    некуда лечь. Тот же код и по тому же доводу, что у отказов справочника.
    """

    http_status = 409

    def __init__(self, message: str):
        self.message = message
        super().__init__(message)


def item_title(titles: dict | None, language: str | None = None) -> str:
    """Название статьи на языке страницы; нет его — первое непустое.

    Пустая строка в списке хуже чужого языка: статью без названия человек не
    выберет глазами и не поймёт, что именно он видит. Порядок отката — порядок
    языков в настройках, чтобы он был один на весь продукт, а не свой у каждого
    экрана.
    """
    titles = titles or {}
    wanted = language or _current_language()
    chosen = (titles.get(wanted) or "").strip()
    if chosen:
        return chosen
    for code, _name in settings.LANGUAGES:
        chosen = (titles.get(code) or "").strip()
        if chosen:
            return chosen
    return ""


def _current_language() -> str:
    from django.utils import translation

    return translation.get_language() or settings.LANGUAGE_CODE


def items_on(tenant_id, moment: date):
    """Статьи расходов, действующие на дату. Выборка идёт под политиками базы."""
    return (
        ExpenseItem.objects.filter(tenant_id=tenant_id, valid_from__lte=moment)
        .exclude(valid_to__lte=moment)
        .select_related("pnl_item")
        .order_by("code")
    )


def _month_is_closed(tenant_id, period: date) -> bool:
    return Period.objects.filter(
        tenant_id=tenant_id, period=period, status="closed"
    ).exists()


@dataclass(frozen=True)
class Landing:
    """Куда лёг расход: период учёта и месяц, из которого его пришлось перенести."""

    period: date
    moved_from: date | None


def landing_for(tenant_id, on: date) -> Landing:
    """Период учёта расхода этой даты.

    Месяц даты открыт — расход учитывается в нём. Закрыт — в текущем, а исходный
    месяц остаётся у `doc_date` (D020). Закрыт и текущий — отказ словами: класть
    расход в произвольный третий месяц значило бы выдумать за человека, к чему
    он относится.
    """
    wanted = on.replace(day=1)
    if not _month_is_closed(tenant_id, wanted):
        return Landing(period=wanted, moved_from=None)

    current = date.today().replace(day=1)
    if _month_is_closed(tenant_id, current):
        raise CashRefused(
            _(
                "Месяц %(month)s закрыт, и текущий месяц %(current)s тоже — "
                "расход записать некуда. Откройте месяц заново с причиной."
            )
            % {"month": wanted.strftime("%Y-%m"), "current": current.strftime("%Y-%m")}
        )
    return Landing(period=current, moved_from=wanted)


@dataclass(frozen=True)
class Recorded:
    """Что случилось с расходом: куда лёг и завёл ли новую версию."""

    fact_id: str
    action: str  # inserted | updated | unchanged
    landing: Landing


class UnitRefused(Exception):
    """Точка не принята базой. Формулировка одна на все причины — см. модуль."""

    http_status = 400

    def __init__(self):
        self.message = _("Точка не найдена.")
        super().__init__(self.message)


def record_expense(
    who,
    *,
    on: date,
    amount,
    item: ExpenseItem,
    unit_id,
    ledger: str,
    note: str,
    entry_key: str,
) -> Recorded:
    """Записать расход из кассы. Единственный путь записи — `upsert_fact`.

    Своего `insert` здесь нет и быть не должно: идемпотентность, версионирование
    и защита закрытого месяца живут в этой функции базы, и второй путь записи
    обошёл бы их все сразу.
    """
    landing = landing_for(who.tenant_id, on)
    payload = {
        "tenant_id": str(who.tenant_id),
        "period": landing.period.isoformat(),
        # Дата расхода остаётся при факте всегда, даже когда период учёта уехал
        # в текущий месяц: именно она отвечает на вопрос «когда это было».
        "doc_date": on.isoformat(),
        "unit_id": str(unit_id),
        "pnl_item_id": str(item.pnl_item_id),
        "expense_item_id": str(item.id),
        "ledger": ledger,
        "amount": str(amount),
        # Название позиции — снимок названия статьи на момент записи. Читать его
        # обратно можно и из статьи (`expense_item_id` при факте есть), но снимок
        # обязан остаться: статью могут переименовать, а закрытый отчёт должен
        # выглядеть так же, как в день закрытия.
        #
        # Язык снимка — язык исходника, а не язык страницы. Иначе одна и та же
        # статья попадала бы в данные то «Вода», то «Voda» в зависимости от того,
        # кто вносил расход, и в отчёте это выглядело бы двумя разными строками.
        # Читателю название всё равно показывается на его языке — из статьи.
        "title": item_title(item.titles, language=settings.LANGUAGE_CODE),
        "note": note or None,
        "channel": CASH_CHANNEL,
        "source": MANUAL_SOURCE,
        "dedup_key": DEDUP_PREFIX + entry_key,
        "allocation": "direct",
    }
    payload = {name: value for name, value in payload.items() if value is not None}

    fact_id, action = _upsert(payload)
    return Recorded(fact_id=str(fact_id), action=action, landing=landing)


def _upsert(payload: dict) -> tuple[str, str]:
    """Вызов `upsert_fact` с отказом базы, переведённым в отказ продукта.

    Точка сохранения (`transaction.atomic`) обязательна: весь запрос идёт одной
    транзакцией (`DbContextMiddleware`), и отказ политики без точки сохранения
    оборвал бы её целиком — человек вместо объяснения увидел бы 500.
    """
    try:
        with transaction.atomic(), connection.cursor() as cursor:
            cursor.execute(
                "select fact_id, action from upsert_fact(%s::jsonb)", [json.dumps(payload)]
            )
            written = cursor.fetchone()
            # Внешние ключи Django объявляет отложенными, то есть проверяет их
            # **на коммите** — уже за пределами этой точки сохранения и за
            # пределами `except` ниже. Роли, у которой точки не ограничены,
            # политика пропустит любой uuid (`app_unit_is_visible` отвечает «да»
            # тому, у кого все точки), и выдуманный номер точки доезжал бы до
            # коммита, обрывая запрос: человек вместо «точка не найдена» видел
            # бы 500. Проверяем здесь и сейчас — тогда отказ приходит на месте и
            # теми же словами, что у чужой точки (D023).
            cursor.execute("set constraints all immediate")
            return written
    except DatabaseError as refusal:
        state = getattr(getattr(refusal, "__cause__", None), "sqlstate", "") or ""
        # 42501 — политика не пропустила запись (чужая точка, невидимый регистр);
        # 23503 — такой строки нет вовсе. Ответ у них один и тот же намеренно:
        # по нему нельзя понять, существует ли точка (D023).
        if state in ("42501", "23503"):
            raise UnitRefused() from refusal
        if state == "P0001":
            # Закрытый месяц. Сюда почти не попадают — период выбирается заранее,
            # — но период мог закрыться между выбором и записью.
            raise CashRefused(
                _("Месяц закрылся, пока расход вносили. Откройте форму заново.")
            ) from refusal
        raise


def new_entry_key() -> str:
    """Ключ записи для новой формы. Случайный, потому что расходов много одинаковых.

    Выводить его из содержимого нельзя: две одинаковые траты в один день по одной
    статье — обычное дело («два раза покупали воду»), и общий ключ склеил бы их
    в одну, потеряв половину денег.
    """
    return str(uuid.uuid4())


def parse_entry_key(raw: str) -> str:
    """Ключ из формы. Не ключ — отказ, а не тихая подстановка нового.

    Тихая подстановка означала бы, что испорченная форма молча теряет
    идемпотентность: повторная отправка завела бы второй расход теми же деньгами.
    """
    raw = (raw or "").strip()
    if not raw:
        return new_entry_key()
    try:
        return str(uuid.UUID(raw))
    except ValueError:
        raise CashRefused(
            _("Форма устарела или испорчена. Откройте её заново и внесите расход ещё раз.")
        ) from None
