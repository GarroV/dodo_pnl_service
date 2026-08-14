"""Экраны справочников: сотрудники, условия найма, группы, точки, юрлица, календарь (T018).

Что здесь есть и чего здесь нет.

**Заведения сотрудников нет — это решение D029, а не пропуск.** Карточки
появляются из данных партнёра, админка нужна для правки. Поэтому у сотрудников
есть список и карточка, но нет кнопки «Добавить». У точек, юрлиц и групп
заведение есть: их в таблице партнёра нет вовсе, взяться им больше неоткуда.

**Удаления нет ни у одного справочника, и это тоже решение.** Точка закрывается
датой (`closed_at`), человек увольняется датой (`dismissed_at`) — и то и другое
обратимо и сохраняет историю. Удалённая строка уносит с собой смысл ссылок из
закрытых месяцев: ведомость июня ссылается на точку, которой больше нет.
Политики базы удаление тем не менее покрывают (миграция `0130`) — гарантия
должна стоять на действии, а не на том, что экран его не предлагает.

**Право проверяется дважды и по-разному.** База (`0130`) не даёт записать —
это гарантия. Здесь (`permissions.check`) отказ объясняется словами, и ссылки на
админку нет вовсе у того, кому она запрещена: экран не предлагает того, что сам
же отвергнет (T072).

**Правка с датой внутри закрытого месяца проходит, и продукт объясняет, что при
этом произошло** (T121, D020): закрытый месяц остаётся прежним, разница едет
вперёд помеченной строкой. Отказ остался только у того, у чего версий по датам
нет вовсе, — схемы и регистра группы. Правило и его «почему» лежат в
`web/directory.py`, здесь показ слов и отказа.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation

from django.contrib.auth.decorators import login_required
from django.http import Http404
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils.translation import gettext as _

from core.models import (
    Calendar,
    Employee,
    EmployeeGroup,
    EmploymentTerm,
    ExpenseItem,
    LegalEntity,
    Unit,
)

from . import directory, permissions, rules
from .dbrefusal import BadInput, ConstraintRefused, saving
from .format import hours, ledger_title
from .i18n import month_title
from .principal import get_current_principal

# Регистры учёта, которые можно назначить группе. Список не из справочника, а
# из того, что видно роли (D023): предложить назначить группе регистр, которого
# человек не видит, значило бы дать ему завести данные, которые он потом не
# найдёт на своих же экранах.
LEDGER_CODES = ("official", "supplementary", "internal")

# Что сказать после сохранения. Словарём, а не готовой фразой в адресе: фразу в
# адресе не перевести и подставить в неё можно что угодно.
#
# «Ничего не изменилось» — отдельный ответ, а не «сохранено»: человек, нажавший
# «Сохранить» и получивший «сохранено», уверен, что завёл новую версию условий
# найма. Её нет, и он узнает об этом, когда расчёт даст прежние числа.
def _saved_notices() -> dict:
    return {
        "person": _("Карточка сохранена."),
        "terms": _("Заведена новая версия условий найма."),
        "same": _("Ничего не изменилось — новая версия не заведена."),
    }


def _who(request):
    return get_current_principal(request)


def _country_of(who) -> str:
    """Страна партнёра. Сам запрос — в `web/directory.py`: спрашивает его не только этот экран."""
    return directory.country_of(who.tenant_id)


def _effective_ledger(term) -> str:
    """Регистр учёта строки условий найма: свой или унаследованный от группы."""
    return term.ledger or term.group.ledger


def _visible_groups(who):
    """Группы, регистр которых видит роль (D023).

    Почему справочник фильтруется так же, как ведомость. Группа несёт регистр
    учёта, и её строка на экране называет его словом. Роль, которая регистра не
    видит, не должна узнать о нём ни из ведомости, ни из справочника — иначе
    разграничение держится на том, в какой раздел человек не заглянул. Тот же
    довод, по которому переключатель разрезов не рисует пустую кнопку для
    невидимого регистра.

    Что человек видит, ему уже сказано: шапка перечисляет его регистры на каждой
    странице. Поэтому отдельной надписи «показано не всё» здесь нет — она
    называла бы существование того, что и скрывается.
    """
    return EmployeeGroup.objects.filter(ledger__in=list(who.visible_ledgers))


def _visible_terms(who, terms: list) -> list:
    """Версии условий найма, регистр которых видит роль."""
    seen = set(who.visible_ledgers)
    return [term for term in terms if _effective_ledger(term) in seen]


def _refusal(request, refusal, *, status=None):
    """Страница отказа: одними и теми же словами, что и сам отказ."""
    return render(
        request,
        "web/directory/denied.html",
        {"message": refusal.message},
        status=status or getattr(refusal, "http_status", 403),
    )


def _guard(request):
    """Пропустить того, у кого есть право вести справочники; иначе — отказ страницей."""
    who = _who(request)
    try:
        permissions.check(who, permissions.DIRECTORY_MANAGE)
    except permissions.PermissionRefused as refusal:
        return who, _refusal(request, refusal)
    return who, None


# --- оглавление ---------------------------------------------------------------

# Из чего состоит админка. Список здесь, а не в шаблоне: подписи переводятся, а
# счётчики считаются — и то и другое в разметке было бы не на месте.
#
# Счётчики считают ровно то, что человек увидит, открыв раздел. Иначе цифра сама
# становится утечкой: «групп 6», а внутри три — и роль узнаёт, что где-то есть
# ещё три, которых ей не видно (D023).
def _sections(who) -> list[dict]:
    return [
        {
            "url": reverse("directory-employees"),
            "title": _("Сотрудники"),
            "about": _("Карточки людей и условия найма: группа, точка, ставка, коэффициент"),
            "count": len(_employee_rows(who)),
        },
        {
            "url": reverse("directory-groups"),
            "title": _("Группы сотрудников"),
            "about": _("Схема расчёта и регистр учёта по умолчанию"),
            "count": _visible_groups(who).count(),
        },
        {
            "url": reverse("directory-units"),
            "title": _("Точки"),
            "about": _("Пиццерии: код, название, юрлицо, даты открытия и закрытия"),
            "count": Unit.objects.count(),
        },
        {
            "url": reverse("directory-legal-entities"),
            "title": _("Юрлица"),
            "about": _("С кем работает бухгалтерия: название и налоговый номер"),
            "count": LegalEntity.objects.count(),
        },
        {
            "url": reverse("directory-expense-items"),
            "title": _("Статьи расходов"),
            "about": _("Чем называют траты и в какую строку P&L они попадают"),
            # Счётчик считает то же, что покажет раздел: статьи тенанта целиком.
            # Регистра у статьи нет — она словарь названий, а не данные о деньгах.
            "count": ExpenseItem.objects.count(),
        },
        {
            "url": reverse("directory-calendar"),
            "title": _("Производственный календарь"),
            "about": _("Норма часов и рабочие дни месяца — отсюда их берёт страница месяца"),
            "count": Calendar.objects.count(),
        },
    ]


@login_required
def index(request):
    who, denied = _guard(request)
    if denied is not None:
        return denied
    return render(
        request,
        "web/directory/index.html",
        {
            "sections": _sections(who),
            "closed_note": directory.closed_month_warning(who.tenant_id),
        },
    )


# --- разбор ввода -------------------------------------------------------------


# `BadInput` живёт в `web/dbrefusal.py` и берётся оттуда (см. импорт выше).
# Переехал он туда вместе с отказом базы: совпадение уникального ключа и ссылка
# в никуда — тот же «введено не то», только сказанный базой, а модуль отказа
# базы не может импортировать этот, потому что этот зовёт его. Адрес
# `directory_views.BadInput` остался рабочим намеренно: его знают шесть форм,
# вызов по HTTP и разбор ввода расхода.


def _text(request, name: str, label: str, *, required: bool = True) -> str:
    value = (request.POST.get(name) or "").strip()
    if required and not value:
        raise BadInput(_("Поле «%(label)s» обязательно.") % {"label": label})
    return value


def _date(request, name: str, label: str, *, required: bool = False) -> date | None:
    raw = (request.POST.get(name) or "").strip()
    if not raw:
        if required:
            raise BadInput(_("Поле «%(label)s» обязательно.") % {"label": label})
        return None
    try:
        return datetime.strptime(raw, "%Y-%m-%d").date()
    except ValueError:
        raise BadInput(
            _("«%(label)s»: дата пишется как 2026-06-01, а не «%(value)s».")
            % {"label": label, "value": raw}
        ) from None


def _number(request, name: str, label: str, *, required: bool = True) -> Decimal | None:
    raw = (request.POST.get(name) or "").strip().replace(",", ".")
    if not raw:
        if required:
            raise BadInput(_("Поле «%(label)s» обязательно.") % {"label": label})
        return None
    try:
        value = Decimal(raw)
    except InvalidOperation:
        raise BadInput(
            _("«%(label)s»: нужно число, а не «%(value)s».")
            % {"label": label, "value": raw}
        ) from None
    if value < 0:
        raise BadInput(_("«%(label)s»: отрицательное значение.") % {"label": label})
    return value


def _choice(request, name: str, label: str, allowed, *, required: bool = True):
    """Выбор из списка. Чужое значение — отказ, а не молчаливая подстановка.

    Возвращается **значение из списка**, а не строка из формы. Разница не
    косметическая: список групп и точек состоит из `UUID`, а форма приносит их
    текстом, и `UUID(...) != "..."`. На такой паре сравнение «что было — что
    стало» всегда говорило «изменилось», и «Сохранить» без единой правки
    заводило новую версию условий найма. Заметно это не сразу: история просто
    обрастала одинаковыми строками, а при закрытом месяце пустая правка ещё и
    получала отказ.
    """
    raw = (request.POST.get(name) or "").strip()
    if not raw:
        if required:
            raise BadInput(_("Поле «%(label)s» обязательно.") % {"label": label})
        return None
    for item in allowed:
        if str(item) == raw:
            return item
    raise BadInput(_("«%(label)s»: такого варианта нет.") % {"label": label})


def _select(name: str, label: str, rows, selected, **extra) -> dict:
    """Поле выбора для `directory/fields.html` из пар (значение, подпись).

    `empty_selected` считается здесь, а не в шаблоне: «ни один вариант не
    отмечен» — это ответ на вопрос обо всём списке сразу, и посчитать его в
    цикле шаблона нельзя. Не посчитать его вовсе значило бы показывать первый
    вариант списка выбранным при пустом значении — то есть предлагать выбор,
    которого человек не делал.
    """
    options = [
        {"code": str(code), "title": title, "selected": str(code) == str(selected or "")}
        for code, title in rows
    ]
    return {
        "kind": "select", "name": name, "label": label, "options": options,
        "empty_selected": not any(option["selected"] for option in options),
        **extra,
    }


# --- сотрудники ---------------------------------------------------------------


@login_required
def employees(request):
    who, denied = _guard(request)
    if denied is not None:
        return denied

    query = (request.GET.get("q") or "").strip()
    rows = _employee_rows(who, query)

    return render(request, "web/directory/list.html", {
        "heading": _("Сотрудники"),
        "about": _(
            "Карточки заводятся вместе с данными партнёра, а не здесь: "
            "имя, коэффициент и ставку приносит загрузка таблицы. Править — можно."
        ),
        "search_value": query,
        "search_label": _("Поиск по имени или внешнему ключу"),
        "columns": [
            {"label": _("Сотрудник")},
            {"label": _("Внешний ключ")},
            {"label": _("Группа")},
            {"label": _("Точка")},
            {"label": _("Уволен")},
        ],
        "rows": rows,
        # Пустое состояние говорит, что делать дальше, а не что данных нет:
        # заголовок — факт, тело — следующий шаг. Кнопки «Завести» у сотрудников
        # нет (D029), поэтому шаг здесь — загрузка таблицы партнёра.
        "empty": _("Сотрудников нет."),
        "empty_next": _(
            "Карточки заводит загрузка таблицы партнёра: откройте месяц и "
            "загрузите табель — люди появятся вместе с часами."
        ),
    })


def _current_terms(who) -> dict:
    """Действующая версия условий найма на человека — та же, что берёт расчёт.

    Порядок по `valid_from` возрастающий, поэтому последняя запись побеждает.
    Тот же приём, что в `payrun.calc.collect_cases`: справочник обязан называть
    ту версию, по которой человека посчитают, а не первую попавшуюся.
    """
    terms = {}
    for term in EmploymentTerm.objects.select_related("group", "unit").order_by("valid_from"):
        terms[term.employee_id] = term
    return terms


def _employee_rows(who, query: str = "") -> list[dict]:
    """Строки списка сотрудников — уже отобранные по регистру роли (D023).

    Человек без условий найма показывается всем: регистра у него ещё нет, и
    скрывать нечего. Спрятать его было бы хуже прямой ошибки — именно такой
    человек и требует внимания администратора: без условий найма он не попадёт
    ни в табель, ни в ведомость.
    """
    found = Employee.objects.order_by("last_name", "first_name")
    if query:
        # Поиск по тому, что человек видит глазами в списке: имени, фамилии и
        # внешнему ключу. Тридцать человек листаются, три тысячи — нет.
        from django.db.models import Q

        found = found.filter(
            Q(last_name__icontains=query)
            | Q(first_name__icontains=query)
            | Q(external_id__icontains=query)
        )

    terms = _current_terms(who)
    seen = set(who.visible_ledgers)
    rows = []
    for person in found:
        term = terms.get(person.id)
        if term is not None and _effective_ledger(term) not in seen:
            continue
        rows.append({
            "url": reverse("directory-employee", args=[person.id]),
            "cells": [
                {"text": f"{person.last_name} {person.first_name}".strip()},
                {"text": person.external_id},
                {"text": term.group.title if term else "—"},
                {"text": term.unit.code if term and term.unit else "—"},
                {"text": person.dismissed_at.isoformat() if person.dismissed_at else "—"},
            ],
        })
    return rows


def _employee_or_404(who, employee_id) -> Employee:
    person = Employee.objects.filter(pk=employee_id).first()
    # Чужой сотрудник, несуществующий и человек чужого регистра отвечают
    # одинаково: по ответу нельзя понять, что он вообще есть. 404, а не 403, —
    # 403 сказал бы «такой есть, но не для вас», то есть выдал бы ровно то, что
    # скрывается (D023).
    if person is None:
        raise Http404("сотрудник не найден")
    term = _current_terms(who).get(person.id)
    if term is not None and _effective_ledger(term) not in set(who.visible_ledgers):
        raise Http404("сотрудник не найден")
    return person


@login_required
def employee(request, employee_id):
    who, denied = _guard(request)
    if denied is not None:
        return denied
    person = _employee_or_404(who, employee_id)

    notice = error = ""
    # Код ответа формы: 200, пока ничего не отклонено. Отказ по состоянию данных
    # (закрытый месяц) обязан отвечать 409 — иначе «сохранено» и «отказано»
    # неразличимы для всего, что смотрит на ответ, а не на разметку: смоук,
    # журнал сервера, будущий API.
    status = 200
    if request.method == "POST":
        try:
            carried = ""
            # Запись целиком внутри `saving()`: отказ базы по ограничению
            # (повторный сквозной ключ, ссылка в никуда) становится отказом
            # формы, а не оборванным запросом (T136, issue #109 и #98).
            with saving():
                if request.POST.get("what") == "person":
                    _save_person(request, person)
                    saved = "person"
                else:
                    saved, carried = _save_terms(request, who, person)
            # Перенаправление после записи: обновление страницы не сохраняет
            # второй раз. Что именно случилось, уезжает в адрес кодом, а не
            # готовой фразой: фраза в адресе не переводится и подставляется
            # кем угодно.
            return redirect(
                reverse("directory-employee", args=[person.id])
                + f"?saved={saved}{carried}"
            )
        # Раньше `BadInput`, потому что отказ базы — его частный случай, а
        # ответить на него обязаны кодом 400: «сохранено» и «отказано» не
        # должны быть неразличимы для того, кто смотрит на ответ.
        except ConstraintRefused as refused:
            error, status = refused.message, refused.http_status
        except BadInput as bad:
            error = bad.message
        except directory.DirectoryRefused as refusal:
            error, status = refusal.message, refusal.http_status

    notice = _saved_notices().get(request.GET.get("saved", ""), "")
    if request.GET.get("retro") == "1":
        # Версия завелась с датой внутри утверждённого месяца (T121). Одной
        # фразой «версия заведена» тут не обойтись: человек обязан узнать, что
        # закрытый месяц остался прежним и где искать разницу.
        notice = " ".join(filter(None, [notice, directory.closed_month_notice(who.tenant_id)]))

    return render(request, "web/directory/employee.html", _employee_context(
        request, who, person, notice=notice, error=error,
    ), status=status)


def _save_person(request, person: Employee) -> None:
    person.last_name = _text(request, "last_name", _("Фамилия"))
    person.first_name = _text(request, "first_name", _("Имя"))
    person.external_id = _text(request, "external_id", _("Внешний ключ"))
    person.hired_at = _date(request, "hired_at", _("Принят"))
    person.dismissed_at = _date(request, "dismissed_at", _("Уволен"))
    if person.hired_at and person.dismissed_at and person.dismissed_at < person.hired_at:
        raise BadInput(_("Дата увольнения раньше даты приёма."))
    person.save(update_fields=[
        "last_name", "first_name", "external_id", "hired_at", "dismissed_at",
    ])


def _save_terms(request, who, person: Employee) -> tuple[str, str]:
    """Новая версия условий найма. Возвращает код случившегося и хвост адреса.

    Хвост — признак того, что версия задела утверждённый месяц (T121): о таком
    продукт обязан сказать словами, а не молча завести версию, после которой
    закрытый месяц и сегодняшние данные расходятся.

    Правило и его «почему» — в `web/directory.py`.
    """
    valid_from = _date(request, "valid_from", _("Действует с"), required=True)
    # Разрешены только группы видимого регистра: иначе человека можно было бы
    # перевести в группу, о существовании которой роли знать не положено, —
    # подбором значения в форме (D023).
    groups = list(_visible_groups(who).values_list("id", flat=True))
    units = list(Unit.objects.values_list("id", flat=True))
    wanted = {
        "group_id": _choice(request, "group", _("Группа"), groups),
        "unit_id": _choice(request, "unit", _("Точка"), units, required=False),
        "base_rate": _number(request, "base_rate", _("Ставка")),
        "coefficient": _number(request, "coefficient", _("Коэффициент")),
        "scheme": _text(request, "scheme", _("Схема расчёта"), required=False) or None,
        "ledger": _choice(request, "ledger", _("Регистр учёта"), LEDGER_CODES, required=False),
    }
    change = directory.save_terms(
        who.tenant_id, person.id, valid_from=valid_from, wanted=wanted,
    )
    if not change.changed:
        return "same", ""
    carried = directory.touches_closed_month(who.tenant_id, valid_from)
    return "terms", ("&retro=1" if carried else "")


def _employee_context(request, who, person: Employee, *, notice: str, error: str) -> dict:
    versions = _visible_terms(who, list(
        EmploymentTerm.objects.filter(employee_id=person.id)
        .select_related("group", "unit")
        .order_by("valid_from")
    ))
    current = versions[-1] if versions else None
    edge = directory.closed_through(who.tenant_id)
    return {
        "person": person,
        "back_url": reverse("directory-employees"),
        "notice": notice,
        "error": error,
        "closed_through": edge,
        "closed_note": directory.closed_month_warning(who.tenant_id),
        "person_fields": [
            {"kind": "text", "name": "last_name", "label": _("Фамилия"),
             "value": person.last_name, "required": True},
            {"kind": "text", "name": "first_name", "label": _("Имя"),
             "value": person.first_name, "required": True},
            {"kind": "text", "name": "external_id", "label": _("Внешний ключ"),
             "value": person.external_id, "required": True,
             "help": _("Сквозной ключ между системами, например JMBG. "
                       "По нему сходится загрузка табеля.")},
            {"kind": "date", "name": "hired_at", "label": _("Принят"),
             "value": person.hired_at.isoformat() if person.hired_at else ""},
            {"kind": "date", "name": "dismissed_at", "label": _("Уволен"),
             "value": person.dismissed_at.isoformat() if person.dismissed_at else ""},
        ],
        "versions": [
            {
                "from": term.valid_from.isoformat(),
                "to": term.valid_to.isoformat() if term.valid_to else "—",
                "group": term.group.title,
                "unit": term.unit.code if term.unit else "—",
                "rate": term.base_rate,
                "coefficient": term.coefficient,
                "scheme": term.scheme or term.group.scheme,
                "ledger": ledger_title(term.ledger or term.group.ledger),
            }
            for term in versions
        ],
        "terms_fields": [
            {"kind": "date", "name": "valid_from", "label": _("Действует с"),
             "value": "", "required": True,
             "help": _("С этой даты действует новая версия. Прошлая закрывается "
                       "этим же днём и остаётся в истории.")},
            _select(
                "group", _("Группа"),
                _visible_groups(who).order_by("title").values_list("id", "title"),
                current.group_id if current else None, required=True,
            ),
            _select(
                "unit", _("Точка"),
                Unit.objects.order_by("code").values_list("id", "code"),
                current.unit_id if current else None, empty_label=_("не задана"),
            ),
            {"kind": "number", "name": "base_rate", "label": _("Ставка"), "required": True,
             "value": current.base_rate if current else ""},
            {"kind": "number", "name": "coefficient", "label": _("Коэффициент"), "required": True,
             "value": current.coefficient if current else ""},
            {"kind": "text", "name": "scheme", "label": _("Схема расчёта"),
             "value": (current.scheme if current else "") or "",
             "help": _("Пусто — как у группы. Заполняется только там, где человек "
                       "считается иначе своей группы.")},
            _select(
                "ledger", _("Регистр учёта"),
                [(code, ledger_title(code)) for code in LEDGER_CODES
                 if code in who.visible_ledgers],
                current.ledger if current else None, empty_label=_("как у группы"),
            ),
        ],
    }


# --- группы, точки, юрлица ----------------------------------------------------

# Путь правила, которым задан способ работы группы (D032). Один на продукт:
# экран группы и экран правил обязаны править одно и то же, а собранный в двух
# местах путь разъехался бы молча — и правка на одном экране не была бы видна на
# другом.
WORK_MEASURE_PATH = "groups.%s.work_measure"


def _preset_now(who):
    """Правила партнёра, действующие сегодня. Нет правил страны — None.

    Сегодня, а не на дату периода: справочник ведут «сейчас», и способ работы
    показывается тот, по которому считается ближайший месяц. Дата новой версии
    при этом спрашивается отдельно — см. форму группы.
    """
    from core.rules import PresetNotFound, load_rules_at

    try:
        return load_rules_at(who.tenant_id, _country_of(who), date.today()).base
    except PresetNotFound:
        return None


def _measure_of(preset, code: str) -> str:
    """Чем меряется работа группы по действующим правилам.

    Тем же способом, каким её берут табель и расчёт (`payroll.work_measure`):
    пусто и отсутствие узла означают часы. Своя ветка здесь дала бы третье
    прочтение одного правила.
    """
    from payroll import work_measure

    if preset is None:
        return "hours"
    return work_measure(((preset.get("groups") or {}).get(code)) or {})


def _measure_title(preset, measure: str) -> str:
    if preset is None:
        return measure
    return ((preset.get("work_measures") or {}).get(measure) or {}).get("title") or measure


@login_required
def groups(request):
    who, denied = _guard(request)
    if denied is not None:
        return denied
    preset = _preset_now(who)
    rows = [
        {
            "url": reverse("directory-group", args=[group.id]),
            "cells": [
                {"text": group.code},
                {"text": group.title},
                {"text": group.scheme},
                {"text": ledger_title(group.ledger)},
                # Способ работы стоит в списке, а не только в карточке (D032):
                # у одного партнёра рядом живут почасовая кухня и сдельные
                # курьеры, и «чем меряют работу» — первое, что спрашивают у
                # справочника групп, а не подробность второго экрана.
                {"text": _measure_title(preset, _measure_of(preset, group.code))},
            ],
        }
        for group in _visible_groups(who).order_by("title")
    ]
    return render(request, "web/directory/list.html", {
        "heading": _("Группы сотрудников"),
        "about": _(
            "Группа задаёт схему расчёта и регистр учёта по умолчанию. "
            "Отдельному человеку и то и другое переопределяется в условиях найма — с датой."
        ),
        "add_url": reverse("directory-group-new"),
        "add_label": _("Завести группу"),
        "columns": [
            {"label": _("Код")}, {"label": _("Название")},
            {"label": _("Схема расчёта")}, {"label": _("Регистр учёта")},
            {"label": _("Чем меряется работа")},
        ],
        "rows": rows,
        "empty": _("Групп нет."),
        "empty_next": _("Без группы человека не посчитать: она задаёт схему расчёта."),
    })


@login_required
def group(request, group_id=None):
    who, denied = _guard(request)
    if denied is not None:
        return denied
    item = None
    if group_id is not None:
        item = _visible_groups(who).filter(pk=group_id).first()
        if item is None:
            # Группа чужого регистра и несуществующая отвечают одинаково — тот
            # же довод, что у карточки сотрудника (D023).
            raise Http404("группа не найдена")

    preset = _preset_now(who)
    error, status = "", 200
    if request.method == "POST":
        try:
            code = _text(request, "code", _("Код"))
            title = _text(request, "title", _("Название"))
            scheme = _text(request, "scheme", _("Схема расчёта"))
            ledger = _choice(request, "ledger", _("Регистр учёта"), LEDGER_CODES)
            if item is not None and (item.scheme != scheme or item.ledger != ledger):
                # Схема и регистр группы участвуют в расчёте, а версий у группы
                # нет: правка изменила бы правило для всех месяцев сразу,
                # включая утверждённые. Отказ поэтому свой, а не общий с
                # условиями найма: общий звался отсюда с `date.min` и выдавал
                # человеку «нельзя изменить с 0001-01-01» и совет взять дату
                # позже — при том что поля даты в этой форме нет. Обходится
                # правка по-прежнему двумя способами, и оба названы в отказе:
                # переопределением с датой в карточке человека либо
                # переоткрытием месяца с причиной.
                directory.refuse_if_unversioned_touches_closed_month(
                    who.tenant_id, _("схема расчёта и регистр группы"),
                )
            if item is None:
                item = EmployeeGroup(tenant_id=who.tenant_id)
            item.code, item.title, item.scheme, item.ledger = code, title, scheme, ledger
            # Группа и её мера — одной точкой сохранения: отвергнутая форма не
            # оставляет за собой группу без меры (T136).
            with saving():
                item.save()
                _save_measure(request, who, item, preset)
            return redirect(reverse("directory-groups"))
        except ConstraintRefused as refused:
            error, status = refused.message, refused.http_status
        except BadInput as bad:
            error = bad.message
        except rules.RuleInputRefused as bad:
            error, status = bad.message, bad.http_status
        except permissions.PermissionRefused as refusal:
            error, status = refusal.message, refusal.http_status
        except directory.DirectoryRefused as refusal:
            error, status = refusal.message, refusal.http_status
        # Правила могли измениться этим же запросом: форма ниже обязана
        # показывать базу, а не то, что было до сохранения.
        preset = _preset_now(who)

    return render(request, "web/directory/form.html", {
        "heading": item.title if item else _("Новая группа"),
        "back_url": reverse("directory-groups"),
        "back_label": _("← К группам"),
        "error": error,
        # У формы есть поле даты («способ действует с»), и правка с датой внутри
        # утверждённого месяца проходит (T121). Сказать об этом надо здесь же:
        # человек не обязан помнить, что часть этой формы версионируется, а
        # часть — нет.
        "closed_note": directory.closed_month_warning(who.tenant_id),
        "submit_label": _("Сохранить"),
        "fields": [
            {"kind": "text", "name": "code", "label": _("Код"), "required": True,
             "value": item.code if item else "",
             "help": _("По нему группа названа в правилах страны. "
                       "Менять — только вместе с правилами.")},
            {"kind": "text", "name": "title", "label": _("Название"), "required": True,
             "value": item.title if item else ""},
            {"kind": "text", "name": "scheme", "label": _("Схема расчёта"), "required": True,
             "value": item.scheme if item else "",
             "help": _("Ключ схемы из правил страны: по ней движок считает людей группы.")},
            _select(
                "ledger", _("Регистр учёта"),
                [(code, ledger_title(code)) for code in LEDGER_CODES
                 if code in who.visible_ledgers],
                item.ledger if item else "official", required=True,
            ),
            *_measure_fields(who, item, preset),
        ],
    }, status=status)


def _measure_fields(who, item, preset) -> list[dict]:
    """Способ работы группы: выбор с датой — или объяснение, почему его нет (D032, T091).

    Почему поле стоит здесь, а не только на экране правил. Способ работы —
    свойство группы в глазах человека («курьерам платим за доставки»), и искать
    его в списке из ста тридцати правил он не станет. Хранится он при этом
    правилом, а не колонкой: правило версионируется по дате, и смена способа не
    переписывает уже посчитанный месяц. Оба экрана поэтому пишут **одной и той
    же** функцией `rules.save_override` — второй путь записи разъехался бы с
    первым на первой правке.

    Право спрашивается своё — `rules.manage`, а не `directory.manage`: это
    правка правила, и партнёр вправе развести ведение справочников и ведение
    правил по разным людям. У кого права нет — читает значение и объяснение,
    а не видит пропавшее поле (T072).

    У новой группы полей нет вовсе: правило адресуется кодом группы, а код ещё
    не сохранён — предлагать выбор, который некуда записать, значило бы обещать
    несделанное. Сказано об этом словами, а не молчанием.
    """
    if preset is None:
        return []
    if item is None or not item.pk:
        return [{
            "kind": "note", "name": "work_measure", "label": _("Чем меряется работа"),
            "value": _("Задаётся после сохранения группы: правило адресуется её кодом."),
        }]

    measure = _measure_of(preset, item.code)
    if not permissions.has(who, permissions.RULES_MANAGE):
        return [{
            "kind": "note", "name": "work_measure", "label": _("Чем меряется работа"),
            "value": _measure_title(preset, measure),
            "help": permissions.explain(who, permissions.RULES_MANAGE),
        }]

    return [
        _select(
            "work_measure", _("Чем меряется работа"),
            rules.choices_for(preset, WORK_MEASURE_PATH % item.code),
            measure, required=True,
            help=_("Правило страны, а не колонка справочника: у смены способа "
                   "есть дата, и закрытый месяц от неё не двигается."),
        ),
        {"kind": "date", "name": "measure_from", "label": _("Способ действует с"),
         "value": "",
         "help": _("Нужна только при смене способа. Пусто — берётся первый день, "
                   "который не задевает утверждённый месяц.")},
    ]


def _save_measure(request, who, item, preset) -> None:
    """Записать способ работы группы, если его поменяли.

    Не поменяли — не пишем ничего и даты не спрашиваем: требовать дату у того,
    кто правил название группы, значило бы отказывать на пустом месте.
    """
    if preset is None or not request.POST.get("work_measure"):
        return
    wanted = _choice(
        request, "work_measure", _("Чем меряется работа"),
        [code for code, _title in rules.choices_for(preset, WORK_MEASURE_PATH % item.code)],
    )
    if wanted == _measure_of(preset, item.code):
        return

    permissions.check(who, permissions.RULES_MANAGE)
    valid_from = _date(request, "measure_from", _("Способ действует с")) or _first_free_day(who)
    rules.save_override(
        who.tenant_id, WORK_MEASURE_PATH % item.code, wanted,
        valid_from=valid_from, actor_id=who.user_id,
    )


def _first_free_day(who) -> date:
    """Первый день, который не задевает утверждённый месяц.

    Умолчание именно такое, а не «сегодня»: сегодня может лежать внутри
    закрытого месяца, и человек получал бы отказ, ничего не сделав неправильно.
    """
    edge = directory.closed_through(who.tenant_id)
    today = date.today()
    return today if edge is None else max(today, edge + timedelta(days=1))


@login_required
def units(request):
    who, denied = _guard(request)
    if denied is not None:
        return denied
    rows = [
        {
            "url": reverse("directory-unit", args=[unit.id]),
            "cells": [
                {"text": unit.code},
                {"text": unit.title},
                {"text": unit.legal_entity.title if unit.legal_entity else "—"},
                {"text": unit.opened_at.isoformat() if unit.opened_at else "—"},
                {"text": unit.closed_at.isoformat() if unit.closed_at else "—"},
            ],
        }
        for unit in Unit.objects.select_related("legal_entity").order_by("code")
    ]
    return render(request, "web/directory/list.html", {
        "heading": _("Точки"),
        "about": _("Расходы разносятся на точку, а не на юрлицо. "
                   "Закрытая точка остаётся в истории."),
        "add_url": reverse("directory-unit-new"),
        "add_label": _("Завести точку"),
        "columns": [
            {"label": _("Код")}, {"label": _("Название")}, {"label": _("Юрлицо")},
            {"label": _("Открыта")}, {"label": _("Закрыта")},
        ],
        "rows": rows,
        "empty": _("Точек нет."),
        "empty_next": _("На точку разносятся часы и расходы — с неё и начинается учёт."),
    })


@login_required
def unit(request, unit_id=None):
    who, denied = _guard(request)
    if denied is not None:
        return denied
    item = None
    if unit_id is not None:
        item = Unit.objects.filter(pk=unit_id).first()
        if item is None:
            raise Http404("точка не найдена")

    error, status = "", 200
    if request.method == "POST":
        try:
            code = _text(request, "code", _("Код"))
            title = _text(request, "title", _("Название"))
            entities = list(LegalEntity.objects.values_list("id", flat=True))
            entity_id = _choice(request, "legal_entity", _("Юрлицо"), entities, required=False)
            opened_at = _date(request, "opened_at", _("Открыта"))
            closed_at = _date(request, "closed_at", _("Закрыта"))
            if opened_at and closed_at and closed_at < opened_at:
                raise BadInput(_("Дата закрытия раньше даты открытия."))
            if item is None:
                item = Unit(tenant_id=who.tenant_id)
            item.code, item.title = code, title
            item.legal_entity_id = entity_id
            item.opened_at, item.closed_at = opened_at, closed_at
            with saving():
                item.save()
            return redirect(reverse("directory-units"))
        except ConstraintRefused as refused:
            # Отказ базы отвечает 400 — в отличие от разбора ввода выше, у
            # которого свой (пока 200) код: их коды разводятся задачей, которая
            # возьмётся за форму целиком, а не этой.
            error, status = refused.message, refused.http_status
        except BadInput as bad:
            error = bad.message

    return render(request, "web/directory/form.html", {
        "heading": item.title if item else _("Новая точка"),
        "back_url": reverse("directory-units"),
        "back_label": _("← К точкам"),
        "error": error,
        "submit_label": _("Сохранить"),
        "fields": [
            {"kind": "text", "name": "code", "label": _("Код"), "required": True,
             "value": item.code if item else "",
             "help": _("Короткий код точки, например NS1. "
                       "Им точка названа в таблице партнёра.")},
            {"kind": "text", "name": "title", "label": _("Название"), "required": True,
             "value": item.title if item else ""},
            _select(
                "legal_entity", _("Юрлицо"),
                LegalEntity.objects.order_by("title").values_list("id", "title"),
                item.legal_entity_id if item else None, empty_label=_("не задано"),
            ),
            {"kind": "date", "name": "opened_at", "label": _("Открыта"),
             "value": item.opened_at.isoformat() if item and item.opened_at else ""},
            {"kind": "date", "name": "closed_at", "label": _("Закрыта"),
             "value": item.closed_at.isoformat() if item and item.closed_at else "",
             "help": _("Точка закрывается датой, а не удалением: "
                       "закрытые месяцы ссылаются на неё.")},
        ],
    }, status=status)


@login_required
def legal_entities(request):
    who, denied = _guard(request)
    if denied is not None:
        return denied
    rows = [
        {
            "url": reverse("directory-legal-entity", args=[entity.id]),
            "cells": [{"text": entity.title}, {"text": entity.tax_number or "—"}],
        }
        for entity in LegalEntity.objects.order_by("title")
    ]
    return render(request, "web/directory/list.html", {
        "heading": _("Юрлица"),
        "about": _("Бухгалтерия работает с юрлицом, пиццерий для неё нет."),
        "add_url": reverse("directory-legal-entity-new"),
        "add_label": _("Завести юрлицо"),
        "columns": [{"label": _("Название")}, {"label": _("Налоговый номер")}],
        "rows": rows,
        "empty": _("Юрлиц нет."),
        "empty_next": _("Юрлицо нужно точке: с ним бухгалтерия работает в официальном регистре."),
    })


@login_required
def legal_entity(request, entity_id=None):
    who, denied = _guard(request)
    if denied is not None:
        return denied
    item = None
    if entity_id is not None:
        item = LegalEntity.objects.filter(pk=entity_id).first()
        if item is None:
            raise Http404("юрлицо не найдено")

    error, status = "", 200
    if request.method == "POST":
        try:
            title = _text(request, "title", _("Название"))
            tax_number = _text(request, "tax_number", _("Налоговый номер"), required=False)
            if item is None:
                item = LegalEntity(tenant_id=who.tenant_id)
            item.title, item.tax_number = title, tax_number or None
            with saving():
                item.save()
            return redirect(reverse("directory-legal-entities"))
        except ConstraintRefused as refused:
            error, status = refused.message, refused.http_status
        except BadInput as bad:
            error = bad.message

    return render(request, "web/directory/form.html", {
        "heading": item.title if item else _("Новое юрлицо"),
        "back_url": reverse("directory-legal-entities"),
        "back_label": _("← К юрлицам"),
        "error": error,
        "submit_label": _("Сохранить"),
        "fields": [
            {"kind": "text", "name": "title", "label": _("Название"), "required": True,
             "value": item.title if item else ""},
            {"kind": "text", "name": "tax_number", "label": _("Налоговый номер"),
             "value": (item.tax_number if item else "") or ""},
        ],
    }, status=status)


# --- производственный календарь -----------------------------------------------


def _month_or_400(raw: str) -> date:
    try:
        return datetime.strptime(raw, "%Y-%m").date().replace(day=1)
    except ValueError:
        raise Http404("месяц не разобран") from None


@login_required
def calendar(request):
    who, denied = _guard(request)
    if denied is not None:
        return denied
    country = _country_of(who)
    edge = directory.closed_through(who.tenant_id)
    rows = [
        {
            "url": reverse("directory-calendar-month", args=[month.period.strftime("%Y-%m")]),
            "cells": [
                {"text": month_title(month.period)},
                {"text": hours(month.norm_hours), "num": True},
                {"text": str(month.working_days), "num": True},
                {"text": _("закрыт") if edge and month.period <= edge else ""},
            ],
        }
        for month in Calendar.objects.filter(country_code=country).order_by("-period")
    ]
    return render(request, "web/directory/list.html", {
        "heading": _("Производственный календарь"),
        "about": _(
            "Норма часов месяца берётся отсюда — её показывает страница месяца. "
            "Календаря на месяц нет — там стоит прочерк, а не правдоподобное число."
        ),
        "add_url": reverse("directory-calendar-new"),
        "add_label": _("Завести месяц"),
        "columns": [
            {"label": _("Месяц")}, {"label": _("Норма часов"), "num": True},
            {"label": _("Рабочих дней"), "num": True}, {"label": _("Зарплата")},
        ],
        "rows": rows,
        "empty": _("Календарь пуст."),
        "empty_next": _(
            "Пока месяца нет в календаре, норма часов на его странице — прочерк, "
            "и недоработку считать не от чего."
        ),
        # Правка задела утверждённый месяц: сказать, что с ним стало и где
        # искать разницу (T121). Признак приезжает адресом, а не готовой
        # фразой: фраза в адресе не переводится и подставляется кем угодно.
        "notice": (
            directory.closed_month_notice(who.tenant_id)
            if request.GET.get("retro") == "1" else ""
        ),
    })


@login_required
def calendar_month(request, month=None):
    who, denied = _guard(request)
    if denied is not None:
        return denied
    country = _country_of(who)
    period = _month_or_400(month) if month else None
    item = (
        Calendar.objects.filter(country_code=country, period=period).first()
        if period is not None else None
    )
    if period is not None and item is None:
        raise Http404("месяца в календаре нет")

    error, status = "", 200
    if request.method == "POST":
        try:
            wanted = period or _month_or_new(request)
            norm = _number(request, "norm_hours", _("Норма часов"))
            days = _number(request, "working_days", _("Рабочих дней"))
            # Норма часов закрытого месяца правится (T121, D020): закрытый
            # расчёт от этого не двигается, а разница едет вперёд помеченной
            # строкой. Человеку об этом говорится словами — до правки на самой
            # форме и после неё на странице календаря.
            carried = directory.touches_closed_month(who.tenant_id, wanted)
            with saving():
                Calendar.objects.update_or_create(
                    country_code=country, period=wanted,
                    defaults={"norm_hours": norm, "working_days": int(days)},
                )
            return redirect(
                reverse("directory-calendar") + ("?retro=1" if carried else "")
            )
        except ConstraintRefused as refused:
            error, status = refused.message, refused.http_status
        except BadInput as bad:
            error = bad.message
        except directory.DirectoryRefused as refusal:
            error, status = refusal.message, refusal.http_status

    return render(request, "web/directory/form.html", {
        "heading": month_title(period) if period else _("Новый месяц календаря"),
        "back_url": reverse("directory-calendar"),
        "back_label": _("← К календарю"),
        "error": error,
        "closed_note": directory.closed_month_warning(who.tenant_id),
        "submit_label": _("Сохранить"),
        "fields": ([] if period else [
            {"kind": "month", "name": "month", "label": _("Месяц"), "required": True,
             "value": "",
             "help": _("Календарь общий для страны: его видят все партнёры этой страны.")},
        ]) + [
            {"kind": "number", "name": "norm_hours", "label": _("Норма часов"), "required": True,
             "value": item.norm_hours if item else ""},
            {"kind": "number", "name": "working_days", "label": _("Рабочих дней"),
             "required": True, "value": item.working_days if item else ""},
        ],
    }, status=status)


def _month_or_new(request) -> date:
    raw = (request.POST.get("month") or "").strip()
    if not raw:
        raise BadInput(_("Поле «%(label)s» обязательно.") % {"label": _("Месяц")})
    try:
        return datetime.strptime(raw, "%Y-%m").date().replace(day=1)
    except ValueError:
        raise BadInput(
            _("«%(label)s»: месяц пишется как 2026-06, а не «%(value)s».")
            % {"label": _("Месяц"), "value": raw}
        ) from None
