"""Платформенная админка: пространства партнёров, их люди и роли (D065, issue #193).

Отдельная поверхность, а не раздел админки партнёра, и это решение владельца
после двух моих попыток свести их вместе. Довод, который я упускал: дублируются
кнопки, а не инструмент. Обзор над всеми пространствами и статистика по ним в
партнёрской админке отсутствуют **по определению** — она заперта в одном тенанте.

Граница между поверхностями:

| | здесь | админка партнёра (`roles_views`, модуль 11) |
|---|---|---|
| кто | администратор платформы | администратор партнёра |
| что ведёт | сами пространства и людей в любом из них | своих людей, точки, юрлица |
| видит | все пространства | только своё |

**Право — не роль.** Оно лежит в `platform_admins`, куда продукт не пишет вовсе
(`0248`), и спрашивается одной функцией `core.spaces.is_platform_admin`. Роль
партнёра, даже самая полная, платформенной не делает никогда.

**Финансов партнёров здесь нет и не будет.** Миграция `0261` открыла ровно четыре
таблицы — пространства, люди, членства, справочник ролей. Зарплаты, табели и
факты остаются невидимы, и это проверяется тестом на настоящих данных
(`test_platform_admin_access`). Управление доступом — не повод читать чужие
деньги.
"""
from __future__ import annotations

from django.contrib.auth.decorators import login_required
from django.db.models import Count
from django.shortcuts import redirect, render
from django.utils.translation import gettext as _

from core.models import Membership, Role, Tenant, User
from core.roles import DEFAULT_TITLES, ROLE_ORDER
from core.spaces import SpaceRefused, create_space, is_platform_admin

from .principal import get_current_principal


def _refuse(request):
    """Экран для того, кому платформенная админка не положена.

    Отказ словами, а не 404: страница, притворившаяся несуществующей, читается
    как поломка продукта. Человек должен понять, что дверь есть, но не его.
    """
    return render(
        request,
        "web/platform/denied.html",
        {
            "message": _(
                "Пространствами партнёров управляет администратор платформы. "
                "Это право не входит ни в одну роль партнёра и выдаётся вне продукта."
            )
        },
        status=403,
    )


def _spaces():
    """Пространства со счётчиком людей — то, из чего состоит первый экран.

    Счётчик считается запросом, а не перебором членств в цикле: партнёров будет
    много, и N+1 здесь стоил бы запроса на каждую строку списка.
    """
    return (
        Tenant.objects.annotate(people=Count("membership", distinct=True))
        .order_by("title")
    )


@login_required
def index(request):
    """Список пространств и форма заведения нового."""
    who = get_current_principal(request)
    if not is_platform_admin(who.user_id if who else None):
        return _refuse(request)

    return render(
        request,
        "web/platform/index.html",
        {
            "spaces": _spaces(),
            # Роли продукта, а не роли какого-то пространства: пространства ещё
            # нет. Названия — те же, что лягут в базу при заведении.
            "role_options": [
                {"code": code, "title": DEFAULT_TITLES[code], "selected": code == "admin"}
                for code in ROLE_ORDER
            ],
            "notice": request.session.pop("platform_notice", ""),
            "error": request.session.pop("platform_error", ""),
        },
    )


@login_required
def space_create(request):
    """Завести пространство и первого его человека. Только POST: это запись."""
    who = get_current_principal(request)
    if not is_platform_admin(who.user_id if who else None):
        return _refuse(request)
    if request.method != "POST":
        return redirect("platform-index")

    try:
        space = create_space(
            code=request.POST.get("code", ""),
            title=request.POST.get("title", ""),
            country_code=request.POST.get("country_code", "RS"),
            base_currency=request.POST.get("base_currency", "RSD"),
            report_currency=request.POST.get("report_currency", "EUR"),
            admin_username=request.POST.get("admin_username", ""),
            admin_password=request.POST.get("admin_password", ""),
            admin_full_name=request.POST.get("admin_full_name", ""),
            role_code=request.POST.get("role_code", "admin"),
        )
    except SpaceRefused as refusal:
        # Отказ кладётся в сессию и показывается на том же экране: форма длинная,
        # и человек должен увидеть причину рядом с ней, а не на пустой странице.
        request.session["platform_error"] = refusal.message
        return redirect("platform-index")

    request.session["platform_notice"] = _(
        "Пространство «%(title)s» заведено. Первый человек может входить."
    ) % {"title": request.POST.get("title", "").strip()}
    return redirect("platform-space", tenant_id=space.tenant_id)


def _people(tenant_id):
    """Кто состоит в пространстве и с какими ролями.

    Человек может иметь несколько ролей сразу (T170), поэтому строка — это
    человек со списком, а не членство: иначе один и тот же сотрудник появлялся
    бы в списке дважды и выглядел бы как двое.
    """
    found: dict = {}
    for membership in (
        Membership.objects.filter(tenant_id=tenant_id)
        .select_related("role")
        .order_by("role__title")
    ):
        row = found.setdefault(
            membership.user_id, {"user_id": membership.user_id, "roles": [], "name": ""}
        )
        row["roles"].append(membership.role)

    for user in User.objects.filter(pk__in=found):
        found[user.pk]["name"] = user.full_name or user.username
        found[user.pk]["username"] = user.username
        found[user.pk]["is_active"] = user.is_active

    return sorted(found.values(), key=lambda row: row["name"])


@login_required
def space(request, tenant_id):
    """Внутрь пространства: его люди и их роли."""
    who = get_current_principal(request)
    if not is_platform_admin(who.user_id if who else None):
        return _refuse(request)

    found = Tenant.objects.filter(pk=tenant_id).first()
    if found is None:
        return render(request, "web/platform/denied.html", {
            "message": _("Такого пространства нет.")
        }, status=404)

    return render(
        request,
        "web/platform/space.html",
        {
            "space": found,
            "people": _people(tenant_id),
            "role_options": [
                {"code": str(role.pk), "title": role.title, "selected": False}
                for role in Role.objects.filter(tenant_id=tenant_id).order_by("title")
            ],
            "notice": request.session.pop("platform_notice", ""),
            "error": request.session.pop("platform_error", ""),
        },
    )


@login_required
def member_role(request, tenant_id):
    """Выдать или снять роль человеку в этом пространстве. Только POST."""
    who = get_current_principal(request)
    if not is_platform_admin(who.user_id if who else None):
        return _refuse(request)
    if request.method != "POST":
        return redirect("platform-space", tenant_id=tenant_id)

    user_id = request.POST.get("user_id") or ""
    role_id = request.POST.get("role_id") or ""
    action = request.POST.get("action") or "grant"

    role = Role.objects.filter(pk=role_id, tenant_id=tenant_id).first()
    if role is None:
        # Роль чужого пространства сюда не приедет: фильтр по тенанту стоит в
        # запросе, а не проверяется после. Подмена идентификатора в форме даёт
        # отказ, а не выдачу роли соседа.
        request.session["platform_error"] = _("Такой роли в этом пространстве нет.")
        return redirect("platform-space", tenant_id=tenant_id)

    if action == "revoke":
        removed, _ignored = Membership.objects.filter(
            tenant_id=tenant_id, user_id=user_id, role=role
        ).delete()
        request.session["platform_notice"] = (
            _("Роль снята.") if removed else _("Этой роли у человека и не было.")
        )
        return redirect("platform-space", tenant_id=tenant_id)

    if not User.objects.filter(pk=user_id).exists():
        request.session["platform_error"] = _("Такого человека нет.")
        return redirect("platform-space", tenant_id=tenant_id)

    _created = Membership.objects.get_or_create(
        tenant_id=tenant_id, user_id=user_id, role=role
    )
    request.session["platform_notice"] = _("Роль выдана.")
    return redirect("platform-space", tenant_id=tenant_id)
