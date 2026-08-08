"""Жизненный цикл расчёта периода со стороны приложения.

Правила переходов живут в базе (миграции `0041_payrun_lifecycle` и
`0042_payrun_approval`) и здесь намеренно не повторяются: второй список статусов
рядом с первым разошёлся бы с ним молча, и приложение предлагало бы то, что база
отвергнет. Поэтому «куда можно перейти» и «нужна ли причина» тут не написаны, а
**спрошены** — `payrun_next_statuses()` и `payrun_reason_required()`.

Отсюда только то, чего у базы быть не может: отказ словами до записи, отметка
«расчёт прошёл», подстановка причины перехода и история, пригодная для показа.
"""
from __future__ import annotations

from django.db import connection, transaction
from django.db.models import F, Func, TextField

from core.models import Payrun, PayrunTransition

from . import retro
from .errors import PayrunRefused, ReasonRequired

DRAFT = "draft"
CALCULATED = "calculated"
APPROVED = "approved"
REOPENED = "reopened"

# Названия статусов расчёта для человека. Одно место на весь продукт: интерфейс
# берёт их отсюда же, чем отказ, — иначе на кнопке и в отказе об одном и том же
# состоянии было бы написано по-разному.
STATUS_TITLES = {
    DRAFT: "Черновик",
    CALCULATED: "Посчитан",
    APPROVED: "Утверждён",
    REOPENED: "Открыт заново",
    "paid": "Выплачен",
}

# Настройка транзакции, которой причина доезжает до триггера журнала. Та же
# механика, что у `app.user_id`: разовая, живёт до конца транзакции.
REASON_SETTING = "app.transition_reason"

APPROVED_REFUSAL = (
    "Период утверждён: расчёт и его данные заморожены. "
    "Чтобы пересчитать, период нужно сначала открыть заново."
)

REASON_REFUSAL = (
    "Откат периода требует причины: напишите, зачем открываете период. "
    "Причина попадёт в историю рядом с вашим именем."
)


def status_title(status: str | None) -> str:
    return STATUS_TITLES.get(status, status or "")


def refuse_if_approved(tenant_id, period) -> None:
    """Отказать до записи, если расчёт периода уже утверждён.

    База отвергнет запись и без этого — но ошибкой драйвера, из которой человеку
    ничего не понятно. Порядок тот же, что с регистрами учёта: объясняет
    приложение, гарантирует база.
    """
    status = (
        Payrun.objects.filter(tenant_id=tenant_id, period=period)
        .values_list("status", flat=True)
        .first()
    )
    if status == APPROVED:
        raise PayrunRefused(APPROVED_REFUSAL)


def mark_calculated(payrun_id, *, calculated_at) -> None:
    """Отметить, что расчёт прошёл: время и статус «посчитан».

    Статус выставляется безусловно, без проверки текущего. Так и задумано:
    `draft → calculated` и `reopened → calculated` разрешены, повторный расчёт
    статуса не меняет вовсе, а из утверждённого база не выпустит. Законность
    решает она — условие в этой строке было бы вторым экземпляром правила.
    """
    Payrun.objects.filter(pk=payrun_id).update(
        status=CALCULATED, calculated_at=calculated_at
    )


# --- переходы, которые делает человек ----------------------------------------


def next_statuses(status: str | None) -> list[str]:
    """Куда расчёт может перейти отсюда. Ответ даёт база, а не список в Python.

    Отсюда же страница узнаёт, показывать ли кнопку: единственный источник
    истины о цикле объявлен в `0041`, и «что предлагать человеку» — такой же
    вопрос о цикле, как «что разрешить».

    Расчёта ещё нет (`None`) — переходить нечему.
    """
    if status is None:
        return []
    with connection.cursor() as cursor:
        cursor.execute("select payrun_next_statuses(%s::payrun_status)", [status])
        return list(cursor.fetchone()[0] or [])


def reason_required(status: str) -> bool:
    """Требует ли переход в этот статус объяснения. Тоже вопрос к базе."""
    with connection.cursor() as cursor:
        cursor.execute("select payrun_reason_required(%s::payrun_status)", [status])
        return bool(cursor.fetchone()[0])


def refuse_if_cycle_forbids(payrun: Payrun | None, to_status: str) -> None:
    """Отказать словами, если цикл сюда не пускает.

    База отвергнет переход и без этого — сообщением триггера, в котором человеку
    непонятно ни что случилось, ни что делать. Гарантия остаётся за базой,
    объяснение — здесь.
    """
    if payrun is None:
        raise PayrunRefused(
            "Расчёта за этот период ещё не было: сначала посчитайте период."
        )
    if to_status not in next_statuses(payrun.status):
        raise PayrunRefused(
            f"Период сейчас в состоянии «{status_title(payrun.status)}», "
            f"перевести его в «{status_title(to_status)}» нельзя. "
            "Открытый заново период нужно пересчитать, прежде чем утверждать снова."
        )


def _set_reason(reason: str) -> None:
    """Передать причину триггеру журнала — настройкой текущей транзакции.

    Вне транзакции Postgres на `set_config(..., true)` только пишет
    предупреждение в свой лог, и причина не доехала бы до триггера. Отказ здесь
    по той же причине, что в `web/dbcontext`: молчаливая потеря настройки хуже
    громкого отказа.
    """
    if not connection.in_atomic_block:
        raise RuntimeError(
            "причина перехода выставляется только внутри транзакции: "
            "вне её set_config(..., true) не действует"
        )
    with connection.cursor() as cursor:
        cursor.execute(f"select set_config('{REASON_SETTING}', %s, true)", [reason])


def _switch(payrun: Payrun, to_status: str, **fields) -> None:
    """Перевести расчёт, если он всё ещё в том состоянии, которое мы прочли.

    Условие по прежнему статусу — не украшение: между чтением страницы и
    нажатием кнопки период мог утвердить кто-то другой, и без условия второй
    оператор молча переписал бы чужой переход. Ноль изменённых строк — это
    «состояние уехало», и об этом говорится вслух.
    """
    changed = Payrun.objects.filter(pk=payrun.pk, status=payrun.status).update(
        status=to_status, **fields
    )
    if not changed:
        raise PayrunRefused(
            "Состояние периода изменилось, пока страница была открыта. "
            "Обновите страницу и посмотрите, что с ним стало."
        )


def approve(payrun: Payrun | None, *, actor_id) -> None:
    """Утвердить расчёт. Кто утвердил — остаётся в самой строке и в журнале."""
    refuse_if_cycle_forbids(payrun, APPROVED)
    with transaction.atomic():
        _switch(payrun, APPROVED, approved_by=actor_id)


def reopen(payrun: Payrun | None, *, reason: str) -> str:
    """Открыть период заново. Без причины — отказ, и не только в форме.

    Причина проверяется здесь, чтобы человек получил текст, а не ошибку
    драйвера, и **обязательность спрашивается у базы**: список переходов,
    требующих объяснения, живёт там одной функцией. Сам запрет держит триггер —
    он же ловит любой путь записи мимо этой формы.
    """
    refuse_if_cycle_forbids(payrun, REOPENED)
    # Раньше причины: если разница за этот месяц уже выплачена у получателя,
    # никакое объяснение отката не поможет — пересчёт означал бы заплатить
    # дважды (T026). Заставлять человека сначала написать причину, чтобы потом
    # отказать по другому поводу, — трата его времени.
    retro.refuse_if_locked(payrun.tenant_id, payrun.period)

    reason = (reason or "").strip()
    if reason_required(REOPENED) and not reason:
        raise ReasonRequired(REASON_REFUSAL)

    with transaction.atomic():
        _set_reason(reason)
        _switch(payrun, REOPENED)
    return reason


# --- история -----------------------------------------------------------------


def history(payrun: Payrun | None) -> list[dict]:
    """История переходов расчёта, готовая к показу: когда, откуда, куда, кто, зачем.

    Второго журнала не заводится: строки пишет триггер базы (T023), здесь только
    чтение под теми же политиками. Имя автора достаётся
    `app_user_display_name()` — `select` на чужие строки `users` роли не выдан,
    и открывать его ради одного поля нельзя: там же хэш пароля и почта.

    Порядок — по ключу, а не по времени: у всех переходов одной транзакции
    `now()` одинаковый, и по времени они встали бы как попало.
    """
    if payrun is None:
        return []
    rows = (
        PayrunTransition.objects.filter(payrun_id=payrun.pk)
        .annotate(
            actor_name=Func(
                F("actor_id"),
                function="app_user_display_name",
                output_field=TextField(),
            )
        )
        .order_by("id")
        .values("from_status", "to_status", "actor_id", "actor_name", "reason", "at")
    )
    return [
        {
            "at": row["at"],
            "from_title": status_title(row["from_status"]) if row["from_status"] else "",
            "to_title": status_title(row["to_status"]),
            # Автора нет — перевод сделан мимо приложения (обслуживание, сид).
            # Прочерк честнее выдуманного имени: см. комментарий в схеме.
            "actor": row["actor_name"] or "",
            "reason": row["reason"] or "",
        }
        for row in rows
    ]
