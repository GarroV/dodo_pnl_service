"""Экран ролей: кто что вправе делать и у кого какая роль (T171, issue #77).

Зачем понадобился. Право `roles.manage` полгода лежало в ролях и в миграциях
без единого потребителя, и это было не косметикой: разграничение доступа
держалось на доводе «администратор в любой момент вправе выписать себе
недостающее право». Выписать было нечем. Владелец, вошедший администратором,
уткнулся в «Расчёт периода не входит в права вашей роли. Попросите того, у кого
это право есть» — и попросить оказалось некого.

Что здесь есть.

**Права роли галочками.** Список прав — из `web/permissions.py`, то есть ровно
тот, что стоит в политиках базы. Второй список в разметке разъехался бы с
первым, и экран обещал бы то, чего база не даст.

**Вторая роль человеку.** Ради этого всё и затевалось (D047): у партнёра
бухгалтер часто ведёт весь проект и должен быть ещё и администратором, а там,
где эти люди разные, разделение обязанностей остаётся.

Чего здесь нет и почему.

**Заведения ролей и учёток.** Роль — это набор прав, а не человек; новые роли
появляются пресетом страны. Экран правит то, что есть, и раздаёт это людям.
Заводить учётки отсюда тем более нельзя: это отдельный разговор про пароли.

**Своей проверки прав.** Право спрашивается один раз (`permissions.check`), а
гарантия стоит в базе (`0242`): роль без `roles.manage` не запишет строку, даже
если этот модуль однажды забудет спросить. Тихого отказа при этом нет —
представление смотрит, изменилась ли строка на самом деле, и говорит словами.
"""
from __future__ import annotations

from django.contrib.auth.decorators import login_required
from django.db import connection
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils.translation import gettext as _

from core.models import Membership, Role

from . import permissions
from .principal import get_current_principal

# Права, которые можно выдать с экрана. Порядок — от ежедневного к редкому:
# табель правят каждый день, роли меняют раз в год.
GRANTABLE = (
    permissions.TIMESHEET_EDIT,
    permissions.PAYRUN_CALCULATE,
    permissions.PERIOD_APPROVE,
    permissions.PERIOD_REOPEN,
    permissions.UNIT_CLOSE,
    permissions.PAYSLIP_FREEZE,
    permissions.RETRO_POST,
    permissions.DIRECTORY_MANAGE,
    permissions.RULES_MANAGE,
    permissions.ROLES_MANAGE,
)


def _people(tenant_id):
    """Люди партнёра с их ролями — одним запросом, а не по строке на человека.

    Имена приходят из `users`, которую `0243` открыла тому, кто ведёт роли.
    Учётка без имени показывается логином: пусто в этом столбце означало бы,
    что человека нет, — а он есть.
    """
    with connection.cursor() as cur:
        cur.execute(
            """
            select m.user_id,
                   coalesce(nullif(u.full_name, ''), u.username, m.user_id::text) as who,
                   array_agg(r.id::text order by r.title)    as role_ids,
                   array_agg(r.title order by r.title)       as role_titles
              from memberships m
              join roles r on r.id = m.role_id
              left join users u on u.id = m.user_id
             where m.tenant_id = %s
             group by m.user_id, who
             order by who
            """,
            [tenant_id],
        )
        return [
            {
                "user_id": row[0],
                "who": row[1],
                "roles": [
                    {"id": rid, "title": title}
                    for rid, title in zip(row[2], row[3], strict=True)
                ],
            }
            for row in cur.fetchall()
        ]


def _page(request, who, *, notice: str = "", error: str = "", status: int = 200):
    roles = list(Role.objects.filter(tenant_id=who.tenant_id).order_by("title"))
    rows = [
        {
            "role": role,
            "rights": [
                {
                    "code": code,
                    "title": permissions.title(code),
                    "granted": code in (role.permissions or []),
                }
                for code in GRANTABLE
            ],
        }
        for role in roles
    ]
    return render(
        request,
        "web/roles/index.html",
        {
            "rows": rows,
            "roles": roles,
            "people": _people(who.tenant_id),
            "notice": notice,
            "error": error,
        },
        status=status,
    )


@login_required
def index(request):
    """Список ролей и людей. Кому не положено — отказ словами, а не пустой экран."""
    who = get_current_principal(request)
    try:
        permissions.check(who, permissions.ROLES_MANAGE)
    except permissions.PermissionRefused as refusal:
        return render(
            request,
            "web/roles/denied.html",
            {"message": refusal.message},
            status=refusal.http_status,
        )
    return _page(request, who, notice=request.session.pop("roles_notice", ""))


@login_required
def role_rights(request, role_id):
    """Сохранить права роли. Только POST: это запись, а не просмотр."""
    who = get_current_principal(request)
    try:
        permissions.check(who, permissions.ROLES_MANAGE)
    except permissions.PermissionRefused as refusal:
        return render(
            request,
            "web/roles/denied.html",
            {"message": refusal.message},
            status=refusal.http_status,
        )

    role = Role.objects.filter(pk=role_id, tenant_id=who.tenant_id).first()
    if role is None:
        # Роль чужого партнёра или общая: политика её всё равно не отдаст на
        # запись, но человеку надо сказать словами, а не кодом ошибки.
        return _page(
            request, who,
            error=_("Такой роли у этого партнёра нет."),
            status=404,
        )

    chosen = [code for code in GRANTABLE if request.POST.get(f"right:{code}") == "on"]
    changed = Role.objects.filter(pk=role.pk, tenant_id=who.tenant_id).update(permissions=chosen)
    if not changed:
        # Политика отказала молча — «изменено 0 строк». Такое молчание и есть
        # худший исход: человек уверен, что право выдано.
        return _page(
            request, who,
            error=_("Права роли не изменены: база не приняла запись."),
            status=403,
        )

    request.session["roles_notice"] = _("Права роли «%(role)s» сохранены.") % {"role": role.title}
    return redirect(reverse("roles"))


@login_required
def person_roles(request, user_id):
    """Выдать человеку роль или снять её."""
    who = get_current_principal(request)
    try:
        permissions.check(who, permissions.ROLES_MANAGE)
    except permissions.PermissionRefused as refusal:
        return render(
            request,
            "web/roles/denied.html",
            {"message": refusal.message},
            status=refusal.http_status,
        )

    role_id = request.POST.get("role") or ""
    role = Role.objects.filter(pk=role_id, tenant_id=who.tenant_id).first() if role_id else None
    if role is None:
        return _page(request, who, error=_("Такой роли у этого партнёра нет."), status=404)

    if request.POST.get("action") == "remove":
        held = Membership.objects.filter(tenant_id=who.tenant_id, user_id=user_id)
        if held.count() <= 1:
            # Человек без единой роли перестаёт существовать для продукта: он
            # входит и не видит ничего, включая объяснения почему.
            return _page(
                request, who,
                error=_("Это единственная роль человека — снимать её некуда."),
                status=409,
            )
        held.filter(role_id=role.pk).delete()
        request.session["roles_notice"] = _("Роль «%(role)s» снята.") % {"role": role.title}
        return redirect(reverse("roles"))

    already = Membership.objects.filter(
        tenant_id=who.tenant_id, user_id=user_id, role_id=role.pk,
    ).exists()
    if already:
        return _page(request, who, error=_("Эта роль у человека уже есть."), status=409)

    Membership.objects.create(tenant_id=who.tenant_id, user_id=user_id, role_id=role.pk)
    request.session["roles_notice"] = _("Роль «%(role)s» выдана.") % {"role": role.title}
    return redirect(reverse("roles"))
