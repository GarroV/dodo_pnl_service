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

**Выбор уровня: партнёр, группа, человек** (T165). Раньше уровень был один и
зашит константой, хотя база умела все четыре, а расчёт их применял. Значение
«сейчас действует» и проверка «а изменилось ли» считаются для выбранного
адресата тем же кодом, каким слои складывает расчёт.

**Слой страны — рядом, но своим правом** (T165). На карточке правила виден
слой страны: значение, с какого числа, история версий и кто их ведёт. Править
его вправе не партнёр: тело общее для всех партнёров страны (`0004_rls`,
`SHARED_TABLES`), и правка с экрана одного поменяла бы расчёт другому. Право
лежит в `platform_admins`, механика версий — в `web/rules_country.py`.

Чего здесь нет.

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

from . import directory, permissions, rules, rules_country
from .dbrefusal import ConstraintRefused, saving
from .format import EMPTY
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


def _assembled_at(who, on_date):
    """Правила партнёра на дату целиком: общая часть и слои по объектам.

    Возвращает `(RuleSet, hidden)` либо `(None, ())`, если правил страны в базе
    нет вовсе. Второй случай — не ошибка продукта, а несделанная первичная
    загрузка, и сказать об этом надо словами, а не пятисотой.

    Отдаётся весь набор, а не только общая часть: правка ложится на любой из
    уровней, и значение, действующее для группы, собирается тем же кодом, каким
    его собирает расчёт (`RuleSet.preset`). Собрать его здесь своим способом
    значило бы завести второй порядок наложения слоёв рядом с настоящим.
    """
    country = directory.country_of(who.tenant_id)
    try:
        assembled = load_rules_at(who.tenant_id, country, on_date)
    except PresetNotFound:
        return None, ()
    return assembled, rules.hidden_paths(assembled.base, who.visible_ledgers)


def _rules_at(who, on_date):
    """Общая часть правил партнёра на дату — страна плюс сам партнёр."""
    assembled, hidden = _assembled_at(who, on_date)
    return (None, ()) if assembled is None else (assembled.base, hidden)


def _preset_for(who, assembled, target, on_date):
    """Правила, действующие для адресата правки: тем же кодом, что у расчёта.

    У человека к его слою добавляется слой его группы — так же, как это делает
    расчёт (`payrun.calc`: `rules.preset(group_id=case.group_id,
    employee_id=case.employee_id)`). Без группы «сейчас действует» показывало бы
    человеку значение партнёра, хотя расчёт взял бы значение его группы, и
    правка «на то же самое» заводила бы пустую версию.
    """
    if target.scope_type == "group":
        return assembled.preset(group_id=target.scope_id)
    if target.scope_type == "employee":
        term = directory.term_at(who.tenant_id, target.scope_id, on_date)
        return assembled.preset(
            group_id=term.group_id if term is not None else None,
            employee_id=target.scope_id,
        )
    return assembled.base


def _no_rules_notice(request, who, on_date, *, heading):
    return render(request, "web/rules/index.html", {
        "heading": heading,
        "on_date": on_date.isoformat(),
        "country": directory.country_of(who.tenant_id),
        "sections": [],
        "closed_note": directory.closed_month_warning(who.tenant_id),
        "notice": _carried_notice(request, who),
    })


def _carried_notice(request, who) -> str:
    """Слова о том, что случилось с только что заведённой версией.

    Двое, и оба про охват правки: задет ли утверждённый месяц (T121) и с какого
    месяца версия подействует на самом деле (T139, issue #99). Складываются
    одной строкой — человеку это один ответ на один вопрос «что я сейчас
    сделал», а не две плашки.

    Признаки приезжают адресом, а не готовой фразой: фраза в адресе не
    переводится и подставляется кем угодно. Дата — это данные, и разбирается она
    строго: мусор в адресе молчит, а не рождает фразу о месяце, которого не было.
    """
    parts = []
    if request.GET.get("retro") == "1":
        parts.append(directory.closed_month_notice(who.tenant_id))
    raw = (request.GET.get("from") or "").strip()
    if raw:
        try:
            started = datetime.strptime(raw, "%Y-%m-%d").date()
        except ValueError:
            started = None
        if started is not None:
            parts.append(rules.effective_month_notice(started))
    return " ".join(filter(None, parts))


@login_required
def index(request):
    who, denied = _guard(request)
    if denied is not None:
        return denied

    on_date = _on_date(request)
    preset, hidden = _rules_at(who, on_date)
    if preset is None:
        return _no_rules_notice(request, who, on_date, heading=_("Правила расчёта"))

    # Правила, у которых ниже партнёра есть своё значение. Список показывает
    # общую часть — то, что действует у всех, — и без этой пометки версия,
    # заведённая одной группе, не была бы видна ни здесь, ни где-либо ещё.
    lower = rules.override_counts(
        who.tenant_id, on_date, rules.titles_of_targets(who, on_date),
    )
    # Искомое приезжает адресом и возвращается в поле формы: иначе после
    # «Показать» поле оказывалось бы пустым при отфильтрованной таблице, и
    # человек читал бы неполный список как полный.
    query = (request.GET.get("q") or "").strip()
    return render(request, "web/rules/index.html", {
        "heading": _("Правила расчёта"),
        "on_date": on_date.isoformat(),
        "query": query,
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
                        "lower": rules.counts_words(lower.get(leaf.path, {})),
                        "url": (
                            f"{reverse('rule', args=[leaf.path])}?on={on_date.isoformat()}"
                            if leaf.editable else ""
                        ),
                    }
                    for leaf in section["rows"]
                ],
            }
            for section in rules.sections(preset, hidden=hidden, query=query)
        ],
        "closed_note": directory.closed_month_warning(who.tenant_id),
        "notice": _carried_notice(request, who),
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


def _back_to_list(who, on_date: date, valid_from: date):
    """Назад к списку правил — со словами о том, что случилось с этой правкой.

    Одна на оба слоя (партнёр и страна): что произойдёт с утверждённым месяцем и
    с какого месяца версия подействует, зависит только от даты, а не от того,
    чьё правило меняли. Две копии этого решения разъехались бы на первой правке.

    Версия с датой внутри утверждённого месяца заводится (T121): закрытый месяц
    не переписывается, разница едет вперёд. Но молча этого не делаем — признак
    уезжает адресом, и список правил объясняет случившееся словами. Дата уезжает
    только тогда, когда о ней есть что сказать: версия с первого числа работает
    ровно так, как человек и ожидал, и предупреждение о ней обесценило бы
    остальные.
    """
    carried = directory.touches_closed_month(who.tenant_id, valid_from)
    shifted = rules.effective_month(valid_from) != valid_from
    return redirect(
        f"{reverse('rules')}?on={on_date.isoformat()}"
        + ("&retro=1" if carried else "")
        + (f"&from={valid_from.isoformat()}" if shifted else "")
    )


def _save_country(request, who, path: str, on_date: date, *, valid_from: date):
    """Завести новую версию правил страны. Не вправе — отказ словами.

    Право спрашивается здесь, а не только на разметке: форма, показанная не
    тому, — это удобство, а запрет держит проверка. И база держит его третьим
    рубежом: политики `country_rules_insert/update` пускают только право
    платформы (миграция `0248`), поэтому даже ошибка в этой проверке не
    обернулась бы записью.
    """
    if not rules_country.may_edit(who):
        raise rules_country.CountryRulesRefused(rules_country.explain_refusal())

    country = directory.country_of(who.tenant_id)
    version = rules_country.in_force_at(country, valid_from)
    if version is None:
        raise rules_country.CountryRulesRefused(
            _("Правил этой страны на указанную дату в базе нет — менять нечего."),
            status=400,
        )
    # Тип и список допустимого берутся у ТЕЛА СТРАНЫ на эту дату, а не у
    # собранного пресета: собранный несёт поверх себя настройки партнёра, и
    # правило, которое партнёр уже переопределил, получило бы тип и список от
    # чужого слоя.
    try:
        current = rules.value_at(version.body, path)
    except KeyError:
        raise Http404("правило не найдено") from None
    wanted = rules.parse(
        request.POST.get("value", ""), current, path,
        allowed=tuple(
            code for code, _title in rules.choices_for(version.body, path, who=who)
        ),
    )
    with saving():
        change = rules_country.save_country_value(
            country, path, wanted,
            valid_from=valid_from, actor_id=who.user_id, effective=current,
        )
    if not change.changed:
        raise rules_country.CountryRulesUnchanged(
            _("Правило страны и так такое — новая версия не заведена.")
        )
    return _back_to_list(who, on_date, valid_from)


@login_required
def rule(request, path: str):
    who, denied = _guard(request)
    if denied is not None:
        return denied

    on_date = _on_date(request)
    assembled, hidden = _assembled_at(who, on_date)
    if assembled is None:
        raise Http404("правил страны на эту дату нет")
    preset = assembled.base

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
    target = rules.TENANT_TARGET
    if request.method == "POST":
        try:
            valid_from = _posted_date(request)
            # Двумя формами на одной странице: «переопределить для себя» и
            # «поменять правило страны». Слой приходит полем, а не отдельным
            # адресом, потому что решение это одно и то же — что станет с этим
            # правилом с такого-то числа, — и объяснения у обеих форм общие
            # (закрытый месяц, помесячное действие). Разными адресами их пришлось
            # бы дублировать.
            if request.POST.get("layer") == "country":
                return _save_country(request, who, path, on_date, valid_from=valid_from)
            # Адресат разбирается ДО значения: список допустимых значений тот же
            # на всех уровнях, но отказ «нет такой группы» человеку понятнее,
            # чем отказ про значение, набранное для этой группы.
            target = rules.target_from(
                who, request.POST.get("target", ""), path=path, on_date=on_date,
            )
            for_target = _preset_for(who, assembled, target, on_date)
            current = rules.value_at(for_target, path)
            wanted = rules.parse(
                request.POST.get("value", ""), current, path,
                allowed=tuple(
                    code for code, _title in rules.choices_for(preset, path, who=who)
                ),
            )
            # Запись целиком внутри `saving()` — тем же приёмом, что на шести
            # формах справочников (T136, T142). Пересечение версий здесь
            # отсекает сама `save_override`, поэтому ограничение базы
            # `rule_overrides_no_overlap` стоит последним рубежом: пока логика
            # верна, человек его не видит, а ошибётся логика — прочитает
            # «поправьте даты» вместо белой страницы (issue #111). Внутрь
            # попадает вся запись одной кнопки: `save_override` закрывает
            # прежнюю версию и заводит новую, и отвергнутая форма не должна
            # оставлять за собой закрытую версию без пришедшей ей на смену.
            with saving():
                change = rules.save_override(
                    who.tenant_id, path, wanted, target=target,
                    valid_from=valid_from, actor_id=who.user_id, effective=current,
                )
            if not change.changed:
                notice = _("Ничего не изменилось — новая версия не заведена.")
            else:
                return _back_to_list(who, on_date, valid_from)
        # Раньше `RuleInputRefused`: родства между ними нет, но порядок тот же,
        # что на справочниках, — сначала отказ базы, потом разбор ввода. Оба
        # отвечают 400: для того, кто смотрит на код ответа, «набрано не то» и
        # «база не приняла» — одно событие.
        except ConstraintRefused as refused:
            error, status = refused.message, refused.http_status
        except rules.RuleInputRefused as bad:
            error, status = bad.message, bad.http_status
        except directory.DirectoryRefused as refusal:
            error, status = refusal.message, refusal.http_status
        except rules_country.CountryRulesRefused as refusal:
            error, status = refusal.message, refusal.http_status
        # Не отказ, а спокойный ответ: человек ничего не сделал неправильно,
        # правило и так такое.
        except rules_country.CountryRulesUnchanged as same:
            notice = same.message
        # Значение могло поменяться этим же запросом — перечитываем, чтобы
        # страница показывала базу, а не то, что было до неё.
        assembled, hidden = _assembled_at(who, on_date)
        preset = assembled.base
        current = rules.value_at(preset, path)

    where = preset.origin_of(path)
    options = rules.choices_for(preset, path, who=who)
    names = rules.titles_of_targets(who, on_date)
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
        # Подсказка под полем даты приходит готовой — из того же места, что и
        # слова после правки. Две формулировки одного правила разъехались бы.
        "from_help": rules.monthly_help(),
        # Кому кладём правку. Список собран с уровнями и объектами сразу, а
        # выбранное сохраняется через отвергнутую форму: человек, ошибшийся в
        # дате, не должен заново искать свою группу среди тридцати строк.
        "targets": rules.targets_for(who, path, on_date, chosen=target.key),
        # Уровни, на которых это правило не заводится, названы словами — до
        # правки, а не отказом после. Пусто, если заводится на всех.
        "scope_note": rules.scope_refusal(path, "group"),
        "versions": [
            {
                "from": row.valid_from.isoformat(),
                "to": row.valid_to.isoformat() if row.valid_to else "—",
                "value": rules.show(row.value),
                "level": rules.level_title(row.scope_type),
                # Имя объекта, а не его uuid: строка истории должна читаться
                # («группе Курьеры»), а не расшифровываться. У уровня партнёра
                # объекта нет, и шаблон ставит прочерк — пустая ячейка читалась
                # бы как потерянное название.
                "who": names.get((row.scope_type, row.scope_id), ""),
            }
            for row in rules.visible_only(rules.versions(who.tenant_id, path), names)
        ],
        "closed_note": directory.closed_month_warning(who.tenant_id),
        **_country_block(who, path, on_date),
    }, status=status)


def _country_block(who, path: str, on_date: date) -> dict:
    """Слой страны на карточке правила: значение, версии и — кому можно — форма.

    Показывается **всем**, у кого есть право вести правила, а не только тому,
    кто вправе страну править. Смысл в том, чтобы правило нашлось: до этой
    задачи ночные часы и ставки взносов жили в YAML, и человек, искавший их в
    продукте, видел только «правила страны» без ответа на «а где они и кто их
    меняет». Теперь ответ на экране: вот значение, вот с какого числа, вот кто
    его ведёт (`explain_refusal`), а для себя переопределите формой ниже.
    """
    country = directory.country_of(who.tenant_id)
    version = rules_country.in_force_at(country, on_date)
    if version is None:
        return {"country_layer": False}
    try:
        value = rules.value_at(version.body, path)
    except KeyError:
        # Правило есть в собранном пресете, но не в теле страны: значит его
        # завёл сам партнёр переопределением. Слоя страны у него нет, и врать о
        # нём нечего.
        return {"country_layer": False}
    may = rules_country.may_edit(who)
    return {
        "country_layer": True,
        "country_code": country,
        "country_value": rules.show(value),
        "country_since": version.valid_from.isoformat(),
        "country_preset": version.code,
        "country_may_edit": may,
        # Правится ли это значение вообще с экрана. Подпись правила несёт все
        # языки сразу, и записать её строкой значило бы затереть остальные.
        "country_locked": path.rsplit(".", 1)[-1] in rules_country.LOCALIZED_KEYS,
        "country_note": "" if may else rules_country.explain_refusal(),
        # Список и отметка «выбрано» считаются от значения СТРАНЫ, а не от
        # собранного. Иначе у правила, которое партнёр себе переопределил, форма
        # страны открывалась бы с чужим значением наготове — и нажатие кнопки
        # положило бы в правила страны то, чего человек не выбирал.
        "country_options": [
            {"code": code, "title": title, "selected": code == value}
            for code, title in rules.choices_for(version.body, path, who=who)
        ],
        "country_bool": [
            {"code": "true", "title": _("да"), "selected": value is True},
            {"code": "false", "title": _("нет"), "selected": value is False},
        ],
        "country_kind": rules.kind_of(value),
        "country_versions": [
            {
                "from": row.valid_from.isoformat(),
                "to": row.valid_to.isoformat() if row.valid_to else "—",
                # Прочерк, а не пустая ячейка: правила в той версии могло
                # ещё не быть, и это разные вещи — «не задано» и «пусто»
                # (`format.EMPTY`, тот же источник прочерка на весь продукт).
                "value": rules.show(_country_value_or_dash(row, path)) or EMPTY,
                "edited": bool(row.edited_at),
            }
            for row in rules_country.country_versions(country)
        ],
    }


def _country_value_or_dash(row, path: str):
    """Значение правила в этой версии страны. Правила в ней ещё не было — пусто."""
    try:
        return rules.value_at(row.body, path)
    except KeyError:
        return None


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
