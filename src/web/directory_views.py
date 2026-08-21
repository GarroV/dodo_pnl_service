"""Экраны справочников: сотрудники, условия найма, группы, точки, юрлица, календарь (T018).

Что здесь есть и чего здесь нет.

**Сотрудник заводится экраном (T164). Прежнее «нет и не будет» (D029) больше не
действует.** D029 говорил: карточки приезжают загрузкой таблицы партнёра, админка
нужна только для правки. Для стройки на тестовых данных этого хватало, для
передачи продукта партнёру — нет. Человек выходит на работу пятнадцатого числа, и
до следующей загрузки таблицы его в системе **не существует**: ни в табеле, ни в
ведомости, ни в справочнике. Обходной путь — просить бухгалтера перезалить
таблицу ради одного человека, то есть ровно та ручная работа, ради избавления от
которой продукт и пишется.

Поэтому заведение есть, а загрузка таблицы никуда не делась: это два входа для
двух разных случаев — месяц целиком и один человек. Оба пишут в те же
`employees` и `employment_terms`, и второй не отменяет первого.

**Заводится человек ВМЕСТЕ с первой версией условий найма, а не «карточкой, а
условия потом».** Карточка без условий найма — это человек, которого расчёт не
знает: ни группы, ни ставки, ни точки. Он не попадёт ни в табель, ни в
ведомость, и узнается это на расчёте месяца, а не при заведении. Форма спрашивает
и то и другое сразу, потому что «завести сотрудника» продуктово означает «с этого
числа он работает и стоит столько».

У точек, юрлиц и групп заведение было и раньше: их в таблице партнёра нет вовсе,
взяться им больше неоткуда.

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

**Сотрудники — единственный справочник, который читают все, а ведёт один**
(T173, D047). Управляющему точки нужны имена, должности и ставки своих людей:
ставок в Dodo IS нет вовсе — это условия найма, наши данные, — и проверить их
можно только здесь. Поэтому `directory.manage` решает у этих двух экранов не
«видно или нет», а **правка или чтение**:

* строки режет база — `unit_visibility` на `employees` (`0020`) и на
  `employment_terms` (`0011`) плюс отбор по регистру роли (D023). Управляющий
  физически не может прочитать человека чужой точки, и это не свойство того,
  что экран не спросил лишнего;
* право решает, показывать формы или нет. Формы нет — есть объяснение, теми же
  словами, которыми ответит сам отказ: пропавшая без слов форма читается как
  поломка продукта (T072);
* адрес при этом остаётся рабочим на чтение, а `POST` отвечает 403 словами.
  Прятать адрес значило бы завести третий контур доступа — в разметке, где его
  никто не проверит.

Остальные семь справочников по-прежнему целиком под `directory.manage`: у них
нет читателя, которому они нужны для своей работы. Восьмой — контрагенты —
открыт на чтение по тому же доводу, но своим (`counterparties_views`).

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
    Counterparty,
    Employee,
    EmployeeGroup,
    EmploymentTerm,
    ExpenseItem,
    LegalEntity,
    Till,
    Unit,
)

from . import directory, permissions, rules
from .dbrefusal import BadInput, ConstraintRefused, saving
from .format import EMPTY, exact, hours, ledger_title
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
        # Заведение (T164) — свой ответ, а не «карточка сохранена»: человек
        # только что появился в продукте, и следующий его шаг другой — внести
        # часы. Сказать об этом надо здесь, иначе он ждёт, что сотрудник сам
        # окажется в ведомости.
        "hired": _(
            "Сотрудник заведён вместе с первой версией условий найма. "
            "Чтобы он попал в ведомость, внесите ему часы в табеле месяца."
        ),
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


def _reader(request):
    """Пропустить любого, кого завели к партнёру: строки режет база, а не право (T173).

    Отдельно от `_guard`, потому что решает другой вопрос. `_guard` спрашивает
    «вправе ли этот человек вести справочник» — и отвечает отказом на весь
    экран. Здесь спрашивается только «есть ли партнёр, чьи данные показывать»:
    без членства политики пусты, и открывать пустой экран с поиском значило бы
    предлагать искать в ничём.

    Всё остальное решает база. Управляющий точки видит своих людей и не видит
    чужих (`0020`), регистр отбирается по роли (D023) — и ни одна строка этого
    отбора не написана здесь. Забытый фильтр в новом экране обязан давать пустой
    список, а не чужой.
    """
    who = _who(request)
    if who is None or who.tenant_id is None:
        return None, render(request, "web/directory/denied.html", {
            "message": _(
                "Вас ещё не завели ни к одному партнёру, поэтому сотрудников у вас нет. "
                "Попросите администратора сети добавить вас."
            ),
        }, status=403)
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
            "url": reverse("directory-tills"),
            "title": _("Кассы"),
            "about": _("Коробки, из которых платят наличными: точка и регистр учёта"),
            # Считается ровно то, что человек увидит в разделе: политики
            # `tills` уже сузили выборку по точке и регистру. Иначе цифра сама
            # становится утечкой (D023).
            "count": Till.objects.count(),
        },
        {
            "url": reverse("directory-counterparties"),
            "title": _("Контрагенты"),
            "about": _("Поставщики и получатели платежей: название, номера, ключ Dodo IS"),
            # Считается то же, что покажет раздел: контрагенты партнёра целиком.
            # Регистра у контрагента нет — он словарь названий, а не данные о
            # деньгах, и политика на нём одна, по партнёру.
            "count": Counterparty.objects.count(),
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


# --- правила страны на экранах справочников -----------------------------------
#
# Схема расчёта и мера работы перечислены в правилах страны, а не в коде: страна
# заводит свою схему обычным пресетом, и экран обязан предложить её, не дожидаясь
# правки интерфейса. Отсюда и общий раздел: спрашивают правила два экрана —
# карточка человека и форма группы, — и второй источник списка означал бы, что на
# соседних экранах предлагаются разные наборы схем.


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


def _effective_measure(preset, term) -> str:
    """Чем на самом деле меряется работа этого человека сейчас (T164).

    Одним вызовом `payroll.work_measure`, а не своей веткой «есть своё — бери
    своё»: порядок «человек сильнее группы» записан в движке, и вторая его копия
    здесь означала бы экран, который показывает не то, что посчитает расчёт.
    """
    from payroll import work_measure

    if term is None:
        return "hours"
    return work_measure(
        ((preset or {}).get("groups") or {}).get(term.group.code),
        employee=term.work_measure,
    )


def _with_current(rows: list, current: str | None) -> list:
    """Список выбора плюс то значение, которое уже стоит, если его в списке нет.

    Выбросить незнакомое значение из списка нельзя, и это не вежливость к
    опечаткам. Схема, которой в правилах страны нет, уже лежит в базе, и человек
    сегодня открывает карточку не ради неё — ради даты увольнения. Если такого
    варианта в списке не будет, браузер отметит первый попавшийся, и «Сохранить»
    молча переведёт человека на другую схему расчёта. Помечаем словами и
    оставляем: показать проблему дороже, чем спрятать её подменой.
    """
    if not current or any(str(code) == current for code, _title in rows):
        return rows
    return [
        *rows,
        (current, _("%(code)s — в правилах страны такой нет") % {"code": current}),
    ]


def _choice_field(name: str, label: str, rows: list, current: str | None, *,
                  required: bool, empty_label: str = "", help: str = "",
                  no_rules_help: str = "") -> dict:
    """Поле выбора из правил страны — или текстовое поле, если правил нет вовсе.

    Пустого списка выбора здесь быть не должно: `<select>` без вариантов
    означает «задать нельзя», а правил страны может не быть по причине, к
    условиям найма отношения не имеющей (пресет не загружен). Тогда поле
    остаётся текстовым и **говорит**, почему списка нет: молча подменённый вид
    поля читается как поломка.
    """
    options = _with_current(rows, current)
    if not options:
        return {"kind": "text", "name": name, "label": label,
                "value": current or "", "required": required, "help": no_rules_help}
    return _select(name, label, options, current, required=required,
                   empty_label=empty_label, help=help)


def _from_rules(request, name: str, label: str, rows: list, current: str | None, *,
                required: bool):
    """Прочитать значение, выбранное из правил страны. Чужое — отказ словами.

    Разбор в паре с `_choice_field`: список допустимого один и тот же, иначе
    экран предлагал бы вариант, который сам же отвергнет. Правил нет —
    принимается текст, ровно как их и вводили до появления списка.
    """
    options = _with_current(rows, current)
    if not options:
        value = _text(request, name, label, required=required)
        return value or None
    allowed = [code for code, _title in options]
    return _choice(request, name, label, allowed, required=required)


# --- сотрудники ---------------------------------------------------------------


@login_required
def employees(request):
    who, denied = _reader(request)
    if denied is not None:
        return denied
    may_manage = permissions.has(who, permissions.DIRECTORY_MANAGE)

    query = (request.GET.get("q") or "").strip()
    rows = _employee_rows(who, query, with_key=may_manage)

    # Сквозной ключ показывается только тому, кто ведёт справочник. Это не
    # оформление: в Сербии там JMBG — национальный идентификатор, и загрузка
    # табеля сходится по нему, то есть нужен он администратору, а не
    # управляющему. Управляющему по D047 нужны имена, должности и ставки; лишний
    # столбец персональных данных к работе точки не добавляет ничего.
    #
    # Поиск сужается вместе со столбцом (`with_key`): искать по значению,
    # которого на экране нет, — это способ его подобрать.
    columns = [{"label": _("Сотрудник")}]
    if may_manage:
        columns.append({"label": _("Внешний ключ")})
    columns += [
        {"label": _("Группа")},
        {"label": _("Точка")},
        {"label": _("Ставка"), "num": True},
        {"label": _("Уволен")},
    ]

    return render(request, "web/directory/list.html", {
        "heading": _("Сотрудники"),
        "about": _(
            "Кто работает у партнёра и по каким условиям. Месяц целиком приносит "
            "загрузка таблицы, одного человека — кнопка ниже: вышел на работу "
            "пятнадцатого числа, завели пятнадцатым."
        ) if may_manage else _(
            "Люди вашей точки: в какой группе человек считается и по какой ставке. "
            "Только чтение — карточки ведёт администратор сети."
        ),
        "search_value": query,
        "search_label": (
            _("Поиск по имени или внешнему ключу") if may_manage else _("Поиск по имени")
        ),
        # Куда возвращаться, зависит от того, кто смотрит: администратор сети
        # пришёл сюда из справочников, остальные — из навигации, и раздела
        # справочников у них нет вовсе. Ссылка на раздел, который ответит
        # отказом, была бы обещанием отказа (то же решение, что у контрагентов).
        "back_url": reverse("directory") if may_manage else "",
        "back_label": _("← К справочникам"),
        "standalone": not may_manage,
        # Кнопка есть у того, кто ведёт справочник (T164). Читателю её не
        # рисуют вовсе: экран не предлагает того, что сам же отвергнет (T072).
        "add_url": reverse("directory-employee-new") if may_manage else "",
        "add_label": _("Завести сотрудника"),
        "columns": columns,
        "rows": rows,
        # Пустое состояние говорит, что делать дальше, а не что данных нет:
        # заголовок — факт, тело — следующий шаг. Шагов теперь два, и назван
        # каждый: месяц целиком приносит загрузка таблицы, одного человека —
        # кнопка (её подставляет сам шаблон, когда есть `add_url`).
        #
        # Читателю следующий шаг называется другой: ни загрузить табель, ни
        # завести человека он не может, и совет сделать это отправил бы его на
        # экран, который ответит отказом.
        "empty": _("Сотрудников нет.") if not query else _("Ничего не нашлось."),
        "empty_next": (
            _(
                "Месяц целиком приносит загрузка таблицы партнёра: откройте месяц "
                "и загрузите табель — люди появятся вместе с часами. Одного "
                "человека заведите здесь:"
            ) if may_manage else _(
                "Здесь появятся люди вашей точки — вместе с их условиями найма. "
                "Пока их нет, спросите администратора сети."
            )
        ) if not query else _("Попробуйте другое написание имени или фамилии."),
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


def _employee_rows(who, query: str = "", *, with_key: bool = True) -> list[dict]:
    """Строки списка сотрудников — уже отобранные по регистру роли (D023).

    Человек без условий найма показывается всем: регистра у него ещё нет, и
    скрывать нечего. Спрятать его было бы хуже прямой ошибки — именно такой
    человек и требует внимания администратора: без условий найма он не попадёт
    ни в табель, ни в ведомость.

    Точку здесь никто не проверяет — и не должен: управляющему её режет политика
    `unit_visibility` (`0020`), то есть человека чужой точки не отдаёт база.
    Второй фильтр в коде был бы второй копией правила «свой человек», а
    разъехавшиеся копии одного правила и есть способ, которым доступ ломается
    незаметно.

    `with_key` — показывать ли сквозной ключ (JMBG). Он же решает, ищет ли поиск
    по нему: искать по значению, которого на экране нет, — способ его подобрать.
    """
    found = Employee.objects.order_by("last_name", "first_name")
    if query:
        # Поиск по тому, что человек видит глазами в списке: имени, фамилии и
        # внешнему ключу. Тридцать человек листаются, три тысячи — нет.
        from django.db.models import Q

        where = Q(last_name__icontains=query) | Q(first_name__icontains=query)
        if with_key:
            where |= Q(external_id__icontains=query)
        found = found.filter(where)

    terms = _current_terms(who)
    seen = set(who.visible_ledgers)
    rows = []
    for person in found:
        term = terms.get(person.id)
        if term is not None and _effective_ledger(term) not in seen:
            continue
        cells = [{"text": f"{person.last_name} {person.first_name}".strip()}]
        if with_key:
            cells.append({"text": person.external_id})
        cells += [
            {"text": term.group.title if term else EMPTY},
            {"text": term.unit.code if term and term.unit else EMPTY},
            # Ставка — не деньги, а основание расчёта: показывается как есть,
            # без округления до копеек (`format.exact`, T116). 152 × 421,08
            # даёт не ту сумму, что 152 × 421,085, и человек, повторяющий
            # расчёт на калькуляторе, обязан видеть настоящее основание.
            {"text": exact(term.base_rate) if term else EMPTY, "num": True},
            {"text": person.dismissed_at.isoformat() if person.dismissed_at else EMPTY},
        ]
        rows.append({
            "url": reverse("directory-employee", args=[person.id]),
            "cells": cells,
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
    who, denied = _reader(request)
    if denied is not None:
        return denied
    # Человек ищется ДО проверки права (T173): чужой сотрудник обязан отвечать
    # 404 всем одинаково. Если бы право спрашивалось первым, читатель получал бы
    # на своего человека 200, а на чужого — 403, то есть узнавал бы о его
    # существовании ровно по коду ответа (D023).
    person = _employee_or_404(who, employee_id)
    may_manage = permissions.has(who, permissions.DIRECTORY_MANAGE)

    notice = error = ""
    # Код ответа формы: 200, пока ничего не отклонено. Отказ по состоянию данных
    # (закрытый месяц) обязан отвечать 409 — иначе «сохранено» и «отказано»
    # неразличимы для всего, что смотрит на ответ, а не на разметку: смоук,
    # журнал сервера, будущий API.
    status = 200
    if request.method == "POST":
        # Правка — по праву, и отказ здесь громкий: 403 словами. База отвергла
        # бы запись и сама (`0130`), но человеку из её ошибки не понятно ничего,
        # а «сохранено» и «отказано» не должны быть неразличимы для того, кто
        # смотрит на код ответа.
        try:
            permissions.check(who, permissions.DIRECTORY_MANAGE)
        except permissions.PermissionRefused as refusal:
            return _refusal(request, refusal)
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
        # Раньше `BadInput`, потому что отказ базы — его частный случай, а слова
        # у него свои. Код ответа у обоих один и тот же — 400 (`BadInput`
        # несёт его сам): «сохранено» и «отказано» не должны быть неразличимы
        # для того, кто смотрит на ответ, а не на разметку (T142, issue #112).
        except ConstraintRefused as refused:
            error, status = refused.message, refused.http_status
        except BadInput as bad:
            error, status = bad.message, bad.http_status
        except directory.DirectoryRefused as refusal:
            error, status = refusal.message, refusal.http_status

    notice = _saved_notices().get(request.GET.get("saved", ""), "")
    if request.GET.get("retro") == "1":
        # Версия завелась с датой внутри утверждённого месяца (T121). Одной
        # фразой «версия заведена» тут не обойтись: человек обязан узнать, что
        # закрытый месяц остался прежним и где искать разницу.
        notice = " ".join(filter(None, [notice, directory.closed_month_notice(who.tenant_id)]))

    return render(request, "web/directory/employee.html", _employee_context(
        request, who, person, notice=notice, error=error, may_manage=may_manage,
    ), status=status)


@login_required
def employee_new(request):
    """Завести сотрудника (T164). Прежде такого адреса не было вовсе — D029.

    Почему право спрашивается `_guard`, а не `_reader`, как у списка и карточки.
    У списка право решает «правка или чтение» — читателю нужны ставки своих
    людей. Здесь читать нечего: это форма записи целиком, и открывать её тому,
    кто не вправе записать, значило бы предлагать работу, которая закончится
    отказом. Отказ при этом словами и на весь экран, а не пропавшая ссылка:
    адрес остаётся рабочим (T072).

    База, как и раньше, не полагается на этот экран: `employees` и
    `employment_terms` закрыты политиками `directory_manage_insert`
    (`0130_directory_permissions`), то есть запись мимо интерфейса тоже не
    пройдёт.
    """
    who, denied = _guard(request)
    if denied is not None:
        return denied

    error, status = "", 200
    if request.method == "POST":
        try:
            # Человек и его первая версия условий найма — одной точкой
            # сохранения: отвергнутая форма не должна оставить за собой карточку
            # без условий найма, то есть человека, которого расчёт не знает (T136).
            with saving():
                person, valid_from = _create_employee(request, who)
            carried = "&retro=1" if directory.touches_closed_month(
                who.tenant_id, valid_from,
            ) else ""
            return redirect(
                reverse("directory-employee", args=[person.id]) + f"?saved=hired{carried}"
            )
        except ConstraintRefused as refused:
            error, status = refused.message, refused.http_status
        except BadInput as bad:
            error, status = bad.message, bad.http_status
        except directory.DirectoryRefused as refusal:
            error, status = refusal.message, refusal.http_status

    return render(request, "web/directory/form.html", {
        "heading": _("Новый сотрудник"),
        "back_url": reverse("directory-employees"),
        "back_label": _("← К сотрудникам"),
        "error": error,
        # Дата приёма может лежать внутри утверждённого месяца — например,
        # человека завели с опозданием. Правка проходит, закрытый месяц не
        # двигается, и сказать об этом надо ДО кнопки (T121).
        "closed_note": directory.closed_month_warning(who.tenant_id),
        "submit_label": _("Завести сотрудника"),
        "fields": [
            *_person_fields(None),
            # Дата одна на карточку и на условия найма, и это не экономия поля.
            # «Принят» — это и есть дата, с которой человек работает и стоит
            # столько: два разных числа здесь означали бы человека, принятого
            # первого числа и посчитанного с пятнадцатого, — расхождение,
            # которое никто не заметит до расчёта.
            {"kind": "date", "name": "hired_at", "label": _("Принят"), "required": True,
             "value": "",
             "help": _("С этой даты человек работает — и с неё же действует первая "
                       "версия условий найма. Середина месяца работает: расчёт "
                       "берёт версию, действующую в месяце.")},
            *_terms_fields(who, None),
        ],
    }, status=status)


def _create_employee(request, who) -> tuple[Employee, date]:
    """Карточка человека и первая версия его условий найма. Возвращает обе даты.

    Условия найма заводятся здесь же, а не «потом на карточке»: человек без них
    не попадёт ни в табель, ни в ведомость, и узнается это на расчёте месяца.
    Заводить экраном заведомо неполную запись — это молчаливый сбой, отложенный
    на две недели.
    """
    person = Employee(tenant_id=who.tenant_id)
    hired_at = _date(request, "hired_at", _("Принят"), required=True)
    person.last_name = _text(request, "last_name", _("Фамилия"))
    person.first_name = _text(request, "first_name", _("Имя"))
    person.external_id = _text(request, "external_id", _("Внешний ключ"))
    person.hired_at = hired_at
    # Разбор условий найма — ДО записи карточки: отказ по ним не должен оставить
    # за собой человека без условий. Точка сохранения это тоже прикрывает, но
    # порядок здесь дешевле, чем откат.
    wanted = _wanted_terms(request, who, None)
    person.save()
    directory.save_terms(who.tenant_id, person.id, valid_from=hired_at, wanted=wanted)
    return person, hired_at


def _person_fields(person: Employee | None) -> list[dict]:
    """Поля самой карточки: имя и сквозной ключ. Одни на заведение и на правку.

    Даты приёма и увольнения здесь нет намеренно: при заведении дата приёма
    обязательна и служит началом условий найма, а даты увольнения не бывает
    вовсе — заводить уже уволенного незачем. На карточке обе есть и обе
    необязательны.
    """
    return [
        {"kind": "text", "name": "last_name", "label": _("Фамилия"),
         "value": person.last_name if person else "", "required": True},
        {"kind": "text", "name": "first_name", "label": _("Имя"),
         "value": person.first_name if person else "", "required": True},
        {"kind": "text", "name": "external_id", "label": _("Внешний ключ"),
         "value": person.external_id if person else "", "required": True,
         "help": _("Сквозной ключ между системами, например JMBG. "
                   "По нему сходится загрузка табеля.")},
    ]


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
    change = directory.save_terms(
        who.tenant_id, person.id, valid_from=valid_from,
        wanted=_wanted_terms(request, who, _last_term(person.id)),
    )
    if not change.changed:
        return "same", ""
    carried = directory.touches_closed_month(who.tenant_id, valid_from)
    return "terms", ("&retro=1" if carried else "")


def _last_term(employee_id) -> EmploymentTerm | None:
    """Последняя версия условий найма человека — та, которую показала форма.

    Нужна разбору ввода, а не показу: список допустимых схем и мер включает то,
    что уже стоит (см. `_with_current`), и составлять его при чтении формы надо
    из того же источника, из которого он составлялся при её показе. Иначе
    сохранение отвергало бы ровно то значение, которое экран сам и предложил.
    """
    return (
        EmploymentTerm.objects.filter(employee_id=employee_id)
        .order_by("valid_from")
        .last()
    )


def _wanted_terms(request, who, current: EmploymentTerm | None) -> dict:
    """Условия найма, заявленные формой. Одни на правку и на заведение (T164).

    Одной функцией, а не двумя похожими: у формы заведения и формы новой версии
    поля условий найма одни и те же, и разъехавшийся разбор означал бы, что
    завести можно то, чего нельзя изменить (или наоборот).
    """
    preset = _preset_now(who)
    # Разрешены только группы видимого регистра: иначе человека можно было бы
    # перевести в группу, о существовании которой роли знать не положено, —
    # подбором значения в форме (D023).
    groups = list(_visible_groups(who).values_list("id", flat=True))
    units = list(Unit.objects.values_list("id", flat=True))
    return {
        "group_id": _choice(request, "group", _("Группа"), groups),
        "unit_id": _choice(request, "unit", _("Точка"), units, required=False),
        "base_rate": _number(request, "base_rate", _("Ставка")),
        "coefficient": _number(request, "coefficient", _("Коэффициент")),
        # Схема расчёта — выбор из правил страны, а не набранный текст (T164).
        # Опечатка в ключе означала молча несчитанного человека: расчёт узнаёт о
        # ней на месяце, а не при наборе.
        "scheme": _from_rules(
            request, "scheme", _("Схема расчёта"), rules.scheme_choices(preset or {}),
            current.scheme if current else None, required=False,
        ),
        # Чем меряется работа именно этого человека (T164). Пусто — как у группы.
        "work_measure": _from_rules(
            request, "work_measure", _("Чем меряется работа"),
            rules.measure_choices(preset or {}),
            current.work_measure if current else None, required=False,
        ),
        "ledger": _choice(request, "ledger", _("Регистр учёта"), LEDGER_CODES, required=False),
    }


def _employee_context(
    request, who, person: Employee, *, notice: str, error: str, may_manage: bool = True
) -> dict:
    versions = _visible_terms(who, list(
        EmploymentTerm.objects.filter(employee_id=person.id)
        .select_related("group", "unit")
        .order_by("valid_from")
    ))
    current = versions[-1] if versions else None
    edge = directory.closed_through(who.tenant_id)
    # Правила нужны обоим режимам экрана: подписать меру работы словом («По
    # часам»), а не ключом, — иначе на карточке стоял бы `fixed_amount`.
    preset = _preset_now(who)
    shown = {
        "person": person,
        "back_url": reverse("directory-employees"),
        "notice": notice,
        "error": error,
        "may_manage": may_manage,
        "versions": [
            {
                "from": term.valid_from.isoformat(),
                "to": term.valid_to.isoformat() if term.valid_to else EMPTY,
                "group": term.group.title,
                "unit": term.unit.code if term.unit else EMPTY,
                # Ставка и коэффициент — основания расчёта, а не деньги: как
                # есть, без округления до копеек (`format.exact`, T116).
                "rate": exact(term.base_rate),
                "coefficient": exact(term.coefficient),
                "scheme": term.scheme or term.group.scheme,
                # Мера версии — только своя, а не унаследованная (T164).
                # Подставить сюда меру группы значило бы показать в июньской
                # строке сегодняшнее правило: мера группы версионируется
                # отдельно, своими датами, и на дату версии условий найма она
                # могла быть другой. «Как у группы» — честный ответ: в этой
                # версии человеку меру не задавали.
                "measure": (
                    _measure_title(preset, term.work_measure) if term.work_measure
                    else _("как у группы")
                ),
                "ledger": ledger_title(term.ledger or term.group.ledger),
            }
            for term in versions
        ],
    }
    if not may_manage:
        # Чтение (T173, D047). Форм нет — вместо них факты и объяснение, почему
        # правки нет: пропавшая без слов форма читается как поломка продукта, а
        # не как запрет (T072). Слова берутся из `permissions.explain`, то есть
        # те же самые, которыми ответит сам отказ на `POST`.
        return {
            **shown,
            "denied": permissions.explain(who, permissions.DIRECTORY_MANAGE),
            # Сквозного ключа среди фактов нет намеренно — там JMBG, и на этом
            # экране он нужен тому, кто сводит загрузку табеля, а не точке.
            "facts": [
                {"label": _("Группа"), "value": current.group.title if current else EMPTY},
                {
                    "label": _("Точка"),
                    "value": current.unit.code if current and current.unit else EMPTY,
                },
                {
                    "label": _("Ставка"),
                    "value": exact(current.base_rate) if current else EMPTY,
                    "num": True,
                },
                {
                    "label": _("Коэффициент"),
                    "value": exact(current.coefficient) if current else EMPTY,
                    "num": True,
                },
                # Чем меряется работа — управляющему это нужно так же, как
                # ставка: по этому он понимает, что вводить в табель — часы или
                # величину за месяц (T164, D047). Здесь показывается
                # действующее значение целиком, включая унаследованное от
                # группы: факты отвечают на «как считается сейчас», а не «что
                # записано в версии».
                {
                    "label": _("Чем меряется работа"),
                    "value": (
                        _measure_title(preset, _effective_measure(preset, current))
                        if current else EMPTY
                    ),
                },
                {
                    "label": _("Принят"),
                    "value": person.hired_at.isoformat() if person.hired_at else EMPTY,
                },
                {
                    "label": _("Уволен"),
                    "value": person.dismissed_at.isoformat() if person.dismissed_at else EMPTY,
                },
            ],
        }
    return {
        **shown,
        "closed_through": edge,
        "closed_note": directory.closed_month_warning(who.tenant_id),
        "person_fields": [
            *_person_fields(person),
            {"kind": "date", "name": "hired_at", "label": _("Принят"),
             "value": person.hired_at.isoformat() if person.hired_at else ""},
            # Увольнение — это дата, а не удаление, и сказать об этом надо здесь
            # же. Пустое поле «Уволен» без слов читается как «уволить нечем»:
            # человек ищет кнопку, которой нет и не будет. Строка не исчезает
            # никуда — по ней считаются закрытые месяцы, в которых человек
            # работал, и вернуть его можно, очистив дату.
            {"kind": "date", "name": "dismissed_at", "label": _("Уволен"),
             "value": person.dismissed_at.isoformat() if person.dismissed_at else "",
             "help": _("Уволить — значит поставить дату. Карточка и история "
                       "остаются: месяцы, в которые человек работал, считаются "
                       "по ним. Очистить дату можно — увольнение обратимо.")},
        ],
        "terms_fields": [
            {"kind": "date", "name": "valid_from", "label": _("Действует с"),
             "value": "", "required": True,
             "help": _("С этой даты действует новая версия. Прошлая закрывается "
                       "этим же днём и остаётся в истории.")},
            *_terms_fields(who, current),
        ],
    }


def _terms_fields(who, current: EmploymentTerm | None) -> list[dict]:
    """Поля условий найма: группа, точка, ставка, коэффициент, схема, мера, регистр.

    Одни и те же на карточке (новая версия) и на форме заведения (T164) — как и
    их разбор в `_wanted_terms`. Даты здесь нет намеренно: на карточке она
    называется «Действует с», при заведении это дата приёма, и спрашивать её
    дважды значило бы разрешить завести человека, который принят одним числом, а
    считается с другого.
    """
    preset = _preset_now(who)
    return [
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
         # Единица подставлена заранее: коэффициент есть у каждого, и у
         # большинства он ровно один. Пустое обязательное поле на форме
         # заведения означало бы, что человека нельзя завести, не зная слова
         # «коэффициент».
         "value": current.coefficient if current else "1"},
        # Схема расчёта — выбором из правил страны, а не набором ключа руками
        # (T164). Прежде здесь стояло текстовое поле, и знать надо было ключ из
        # YAML: `standard`, `half_time`, `half_time_min_base`. Опечатка в нём —
        # это молча несчитанный человек: расчёт отказывает по имени, но узнаётся
        # это на закрытии месяца, а не при наборе.
        _choice_field(
            "scheme", _("Схема расчёта"), rules.scheme_choices(preset or {}),
            (current.scheme if current else None) or None,
            required=False, empty_label=_("как у группы"),
            help=_("Пусто — как у группы. Заполняется там, где человек считается "
                   "иначе своей группы."),
            no_rules_help=_("Правил страны в базе нет, поэтому и списка схем нет: "
                            "загрузите пресет страны. Пока — ключ схемы, как он "
                            "написан в правилах."),
        ),
        # Чем меряется работа этого человека (T164). Прежде задавалось только на
        # всю группу и только правом `rules.manage`; здесь это условие найма —
        # ведёт его тот, кто ведёт условия найма, и версия у него та же.
        _choice_field(
            "work_measure", _("Чем меряется работа"), rules.measure_choices(preset or {}),
            (current.work_measure if current else None) or None,
            required=False, empty_label=_("как у группы"),
            help=_("Часовая или сдельная — у этого человека. Пусто — как у группы. "
                   "Сдельному человеку табель спросит величину за месяц вместо часов."),
            no_rules_help=_("Правил страны в базе нет, поэтому и списка способов нет: "
                            "загрузите пресет страны."),
        ),
        _select(
            "ledger", _("Регистр учёта"),
            [(code, ledger_title(code)) for code in LEDGER_CODES
             if code in who.visible_ledgers],
            current.ledger if current else None, empty_label=_("как у группы"),
        ),
    ]


# --- группы, точки, юрлица ----------------------------------------------------

# Путь правила, которым задан способ работы группы (D032). Один на продукт:
# экран группы и экран правил обязаны править одно и то же, а собранный в двух
# местах путь разъехался бы молча — и правка на одном экране не была бы видна на
# другом.
WORK_MEASURE_PATH = "groups.%s.work_measure"


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
            # Схема группы — выбором из правил страны, тем же списком и тем же
            # разбором, что у человека (T164): два набора схем на соседних
            # экранах разъехались бы молча.
            scheme = _from_rules(
                request, "scheme", _("Схема расчёта"), rules.scheme_choices(preset or {}),
                item.scheme if item else None, required=True,
            )
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
            error, status = bad.message, bad.http_status
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
            _choice_field(
                "scheme", _("Схема расчёта"), rules.scheme_choices(preset or {}),
                item.scheme if item else None, required=True,
                help=_("По ней движок считает людей группы. Список — из правил "
                       "страны: отдельному человеку схема переопределяется в его "
                       "условиях найма, с датой."),
                no_rules_help=_("Правил страны в базе нет, поэтому и списка схем нет: "
                                "загрузите пресет страны. Пока — ключ схемы, как он "
                                "написан в правилах."),
            ),
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
            error, status = refused.message, refused.http_status
        except BadInput as bad:
            # Тот же код, что у отказа базы выше: разбор ввода и ограничение —
            # две причины одного события «форма не принята» (T142, issue #112).
            error, status = bad.message, bad.http_status

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
            error, status = bad.message, bad.http_status

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
        # Что случилось с правкой — одним ответом на один вопрос: кого она
        # задела (T139) и что стало с утверждённым месяцем (T121). Признаки
        # приезжают адресом, а не готовой фразой: фраза в адресе не переводится
        # и подставляется кем угодно.
        "notice": " ".join(filter(None, [
            directory.shared_calendar_notice() if request.GET.get("shared") == "1" else "",
            (
                directory.closed_month_notice(who.tenant_id)
                if request.GET.get("retro") == "1" else ""
            ),
        ])),
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
            _working_days_fit(wanted, days)
            # Норма часов закрытого месяца правится (T121, D020): закрытый
            # расчёт от этого не двигается, а разница едет вперёд помеченной
            # строкой. Человеку об этом говорится словами — до правки на самой
            # форме и после неё на странице календаря.
            carried = directory.touches_closed_month(who.tenant_id, wanted)
            with saving():
                if period is None:
                    # «Завести месяц» именно заводит: занятый ключ отвергает
                    # база, и человек читает то же самое, что на любом другом
                    # справочнике (T136). Раньше здесь стоял `update_or_create`
                    # на обе формы сразу — и кнопка «Завести месяц» молча
                    # переписывала уже заведённый месяц: 302, ни слова, прежние
                    # числа не показаны. Календарь при этом общий на страну, то
                    # есть промахнувшийся месяцем администратор одного партнёра
                    # переписывал норму часов всем (T155, находка Н5).
                    Calendar.objects.create(
                        country_code=country, period=wanted,
                        norm_hours=norm, working_days=int(days),
                    )
                else:
                    Calendar.objects.update_or_create(
                        country_code=country, period=wanted,
                        defaults={"norm_hours": norm, "working_days": int(days)},
                    )
            # `shared=1` — всегда: календарь общий для страны при любой дате, и
            # человек обязан прочитать, что задел не только своего партнёра
            # (T139, issue #100). Признаки едут адресом, а не готовой фразой:
            # фраза в адресе не переводится и подставляется кем угодно.
            return redirect(
                reverse("directory-calendar")
                + "?shared=1" + ("&retro=1" if carried else "")
            )
        except ConstraintRefused as refused:
            error, status = refused.message, refused.http_status
        except BadInput as bad:
            error, status = bad.message, bad.http_status
        except directory.DirectoryRefused as refusal:
            error, status = refusal.message, refusal.http_status

    return render(request, "web/directory/form.html", {
        "heading": month_title(period) if period else _("Новый месяц календаря"),
        "back_url": reverse("directory-calendar"),
        "back_label": _("← К календарю"),
        "error": error,
        "closed_note": directory.closed_month_warning(who.tenant_id),
        # Сказано на обеих формах — и нового месяца, и правки существующего.
        # Раньше строка про общий календарь стояла подсказкой у поля месяца, то
        # есть только при заведении: тот, кто правил норму часов уже заведённого
        # месяца, о соседях по стране не читал ничего (issue #100).
        "shared_note": directory.shared_calendar_warning(),
        "submit_label": _("Сохранить"),
        "fields": ([] if period else [
            {"kind": "month", "name": "month", "label": _("Месяц"), "required": True,
             "value": ""},
        ]) + [
            {"kind": "number", "name": "norm_hours", "label": _("Норма часов"), "required": True,
             "value": item.norm_hours if item else ""},
            {"kind": "number", "name": "working_days", "label": _("Рабочих дней"),
             "required": True, "value": item.working_days if item else ""},
        ],
    }, status=status)


def _working_days_fit(period: date, days) -> None:
    """Рабочих дней не бывает больше, чем дней в месяце.

    Форма принимала 40 рабочих дней в июне с 302 и без вопросов (найдено той же
    сверкой рядом с Н5). Это не спорное правило и не вкус: столько дней в месяце
    физически нет, а рабочие дни — вход расчёта недоработки. Верхняя граница
    считается по самому месяцу, а не константой 31: тогда февраль отличается от
    января, и проверка остаётся утверждением о данных.
    """
    from calendar import monthrange

    if days is None:
        return
    limit = monthrange(period.year, period.month)[1]
    if days > limit:
        raise BadInput(
            _("«%(label)s»: в месяце %(month)s столько дней не бывает — "
              "их всего %(limit)s.")
            % {"label": _("Рабочих дней"), "month": f"{period:%Y-%m}", "limit": limit}
        )


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
