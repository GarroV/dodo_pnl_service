"""Экран правил расчёта: смотреть и править настройками с датами (T090).

Спека давала этот экран как **must** («не лезть в YAML и не ломать закрытые
периоды»), а задачи на него в графе не было — пропуск планирования, а не чей-то
недосмотр. Из-за него право `rules.manage` полгода лежало в ролях и в миграциях
и ни разу не спрашивалось.

Что здесь есть.

**Список правил на дату.** Пресет собирается ровно тем же кодом, каким его
собирает расчёт (`core.rules.load_rules_at`), и показывается со следом: у
каждого значения написано, кто его положил — правила страны или настройка
партнёра — и с какого числа. Иначе экран показывал бы то, что якобы действует,
а расчёт брал бы другое.

**Правка одного правила.** Отдельным адресом, а не полем прямо в списке: правка
правила меняет деньги всем, кого оно касается, и человек должен видеть, что
именно он открыл, вместе с историей версий — до того, как наберёт новое
значение.

Чего здесь нет.

**Правки тела пресета страны.** Она общая для всех партнёров страны (`0004_rls`,
`SHARED_TABLES`), и правка с экрана одного поменяла бы расчёт другому. Правки
ложатся слоем партнёра — см. `web/rules.py`, правило первое.

**Экрана ролей.** `roles.manage` по-прежнему без потребителя, и это не забыто:
задача T090 про правила, а заводить экран, которого никто не просил, — тот же
способ промахнуться мимо спеки, каким этот экран и был пропущен.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta

from django.contrib.auth.decorators import login_required
from django.http import Http404
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils.translation import gettext as _

from core.rules import PresetNotFound, load_rules_at

from . import directory, permissions, rules
from .principal import get_current_principal


def _who(request):
    return get_current_principal(request)


def _guard(request):
    """Пропустить того, у кого есть право вести правила; иначе — отказ страницей.

    Отказ той же страницей и теми же словами, что у справочников: два запрета
    одного веса, объяснённые по-разному, читаются как два разных продукта.
    """
    who = _who(request)
    try:
        permissions.check(who, permissions.RULES_MANAGE)
    except permissions.PermissionRefused as refusal:
        return who, render(
            request,
            "web/rules/denied.html",
            {"message": refusal.message},
            status=refusal.http_status,
        )
    return who, None


def _on_date(request) -> date:
    """На какое число смотрим. Умолчание — сегодня, как и у расчёта «сейчас».

    Мусор в адресе не молчит и не подставляет сегодня втихую: человек, пришедший
    по ссылке с испорченной датой, обязан увидеть, что смотрит не туда, куда
    думал.
    """
    raw = (request.GET.get("on") or "").strip()
    if not raw:
        return date.today()
    try:
        return datetime.strptime(raw, "%Y-%m-%d").date()
    except ValueError:
        raise Http404("дата пишется как 2026-06-01") from None


def _rules_at(who, on_date):
    """Правила партнёра на дату вместе с их видимой частью.

    Возвращает `(preset, hidden)` либо `(None, ())`, если правил страны в базе
    нет вовсе. Второй случай — не ошибка продукта, а несделанная первичная
    загрузка, и сказать об этом надо словами, а не пятисотой.
    """
    country = directory.country_of(who.tenant_id)
    try:
        assembled = load_rules_at(who.tenant_id, country, on_date)
    except PresetNotFound:
        return None, ()
    preset = assembled.base
    return preset, rules.hidden_paths(preset, who.visible_ledgers)


def _no_rules_notice(request, who, on_date, *, heading):
    return render(request, "web/rules/index.html", {
        "heading": heading,
        "on_date": on_date.isoformat(),
        "country": directory.country_of(who.tenant_id),
        "sections": [],
        "closed_through": directory.closed_through(who.tenant_id),
    })


@login_required
def index(request):
    who, denied = _guard(request)
    if denied is not None:
        return denied

    on_date = _on_date(request)
    preset, hidden = _rules_at(who, on_date)
    if preset is None:
        return _no_rules_notice(request, who, on_date, heading=_("Правила расчёта"))

    return render(request, "web/rules/index.html", {
        "heading": _("Правила расчёта"),
        "on_date": on_date.isoformat(),
        "country": directory.country_of(who.tenant_id),
        "preset_code": preset.get("preset", ""),
        "sections": [
            {
                **section,
                "rows": [
                    {
                        "path": leaf.path,
                        "title": leaf.title,
                        "value": leaf.value,
                        "origin": rules.level_title(leaf.level),
                        "since": leaf.valid_from.isoformat() if leaf.valid_from else "",
                        "url": (
                            f"{reverse('rule', args=[leaf.path])}?on={on_date.isoformat()}"
                            if leaf.editable else ""
                        ),
                    }
                    for leaf in section["rows"]
                ],
            }
            for section in rules.sections(preset, hidden=hidden)
        ],
        "closed_through": directory.closed_through(who.tenant_id),
    })


def _default_from(who, on_date: date) -> str:
    """С какого числа предлагается новая версия.

    Не «сегодня» и не пусто, а первый день после последнего утверждённого
    месяца: подставленная дата внутри закрытого месяца получила бы отказ, и
    человек читал бы его на каждой правке, ничего не сделав неправильно.
    """
    edge = directory.closed_through(who.tenant_id)
    if edge is None:
        return on_date.isoformat()
    return max(edge + timedelta(days=1), on_date).isoformat()


@login_required
def rule(request, path: str):
    who, denied = _guard(request)
    if denied is not None:
        return denied

    on_date = _on_date(request)
    preset, hidden = _rules_at(who, on_date)
    if preset is None:
        raise Http404("правил страны на эту дату нет")

    # Скрытое роли правило и несуществующее отвечают одинаково (D023): «нельзя
    # смотреть» и «нет такого» отличались бы кодом ответа, и по нему можно было
    # бы перебрать, какие группы существуют.
    if not rules.is_visible(path, hidden) or path.split(".")[0] in rules.IDENTITY:
        raise Http404("правило не найдено")
    try:
        current = rules.value_at(preset, path)
    except KeyError:
        raise Http404("правило не найдено") from None

    error, status, notice = "", 200, ""
    if request.method == "POST":
        try:
            valid_from = _posted_date(request)
            wanted = rules.parse(request.POST.get("value", ""), current, path)
            change = rules.save_override(
                who.tenant_id, path, wanted,
                valid_from=valid_from, actor_id=who.user_id, effective=current,
            )
            if not change.changed:
                notice = _("Ничего не изменилось — новая версия не заведена.")
            else:
                return redirect(f"{reverse('rules')}?on={on_date.isoformat()}")
        except rules.RuleInputRefused as bad:
            error, status = bad.message, bad.http_status
        except directory.DirectoryRefused as refusal:
            error, status = refusal.message, refusal.http_status
        # Значение могло поменяться этим же запросом — перечитываем, чтобы
        # страница показывала базу, а не то, что было до неё.
        preset, hidden = _rules_at(who, on_date)
        current = rules.value_at(preset, path)

    where = preset.origin_of(path)
    options = rules.choices_for(preset, path)
    return render(request, "web/rules/rule.html", {
        "heading": path,
        "on_date": on_date.isoformat(),
        "back_url": f"{reverse('rules')}?on={on_date.isoformat()}",
        "value": rules.show(current),
        "origin": rules.level_title(where.level),
        "since": where.valid_from.isoformat() if where.valid_from else "",
        "error": error,
        "notice": notice,
        "kind": rules.kind_of(current),
        "options": [
            {"code": code, "title": title, "selected": code == current}
            for code, title in options
        ],
        "bool_options": [
            {"code": "true", "title": _("да"), "selected": current is True},
            {"code": "false", "title": _("нет"), "selected": current is False},
        ],
        "default_from": _default_from(who, on_date),
        "versions": [
            {
                "from": row.valid_from.isoformat(),
                "to": row.valid_to.isoformat() if row.valid_to else "—",
                "value": rules.show(row.value),
            }
            for row in rules.versions(who.tenant_id, path)
        ],
        "closed_through": directory.closed_through(who.tenant_id),
    }, status=status)


def _posted_date(request) -> date:
    raw = (request.POST.get("valid_from") or "").strip()
    if not raw:
        raise rules.RuleInputRefused(_("Поле «Действует с» обязательно."))
    try:
        return datetime.strptime(raw, "%Y-%m-%d").date()
    except ValueError:
        raise rules.RuleInputRefused(
            _("«Действует с»: дата пишется как 2026-06-01, а не «%(value)s».") % {"value": raw}
        ) from None
