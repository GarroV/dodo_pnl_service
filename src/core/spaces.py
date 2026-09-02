"""Завести пространство партнёра целиком — одной операцией (D065, issue #193).

Партнёр не заводится входом (D064): пространство и первого человека создаёт
администратор платформы. До этого модуля такой операции не существовало вовсе —
инженер делал её SQL-ом, то есть подключить партнёра без разработчика было
нельзя.

**Почему одной операцией, а не четырьмя формами.** Пространство без ролей —
пустая оболочка: человека в него не пустить, потому что членство ссылается на
роль, а ролей нет. Пространство с ролями, но без человека — то же самое с другой
стороны. Разделив это на шаги, мы бы получили состояния, в которых партнёр
наполовину заведён, и каждое пришлось бы уметь чинить. Здесь всё либо
получилось, либо не осталось следов: операция идёт одной транзакцией.

**Роли берутся из формы продукта** (`core.roles.ROLE_SHAPES`), а не выдумываются
на месте, и вместе с ними кладётся снимок `shipped_shape` — иначе доставка формы
(`role_delivery`, T169) приняла бы свежие роли за правленные партнёром и
перестала бы до них доезжать.

**Чего здесь нет.** Ни юрлиц, ни точек, ни первого периода: это работа партнёра,
и у первого же человека есть право `directory.manage`, чтобы её сделать. Заводить
их за него значило бы придумывать данные, которых мы не знаем.
"""
from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from django.db import transaction
from django.utils.translation import gettext as _

from .models import Membership, PlatformAdmin, Role, Tenant, User
from .role_delivery import product_shape
from .roles import DEFAULT_TITLES, ROLE_ORDER, ROLE_SHAPES, permission_states

__all__ = ["SpaceRefused", "NewSpace", "create_space", "is_platform_admin"]


def is_platform_admin(user_id: UUID | None) -> bool:
    """Ведёт ли этот человек платформу.

    Спрашивается у базы, а не у прав роли: право не партнёрское, в
    `memberships` его нет и быть не должно (T165). Своя строка `platform_admins`
    человеку видна политикой, чужие — нет, поэтому вопрос честный и без обхода
    разграничения.

    Одно место на весь продукт: правило «кто ведёт платформу» решает и экран
    правил стран, и платформенная админка, и разъехавшись, они дали бы человека,
    который правила страны менять может, а пространства — нет.
    """
    return user_id is not None and PlatformAdmin.objects.filter(user_id=user_id).exists()


class SpaceRefused(Exception):
    """Пространство не заведено, и человеку сказано почему.

    Отдельный класс, а не `ValueError`: экран показывает `message` человеку, и
    текст обязан быть о том, что делать, а не о том, что сломалось.
    """

    def __init__(self, message: str):
        self.message = message
        super().__init__(message)


@dataclass(frozen=True)
class NewSpace:
    """Что получилось: пространство и первый его человек."""

    tenant_id: UUID
    user_id: UUID
    role_code: str


def create_space(
    *,
    code: str,
    title: str,
    country_code: str,
    base_currency: str,
    report_currency: str,
    admin_username: str,
    admin_password: str,
    admin_full_name: str = "",
    role_code: str = "admin",
) -> NewSpace:
    """Завести партнёра: пространство, его роли и первого человека.

    `role_code` — роль первого человека. По умолчанию администратор сети: у него
    есть право вести роли, то есть он сможет завести остальных сам. Но владелец
    прямо сказал, что администратором партнёра бывает и оперативный директор, и
    бухгалтер — поэтому роль выбирается, а не назначается молча.
    """
    code = (code or "").strip()
    title = (title or "").strip()
    admin_username = (admin_username or "").strip()

    if not code:
        raise SpaceRefused(_("У пространства должен быть код — по нему его находят в системе."))
    if not title:
        raise SpaceRefused(_("Дайте пространству название: его увидят люди партнёра."))
    if not admin_username:
        raise SpaceRefused(_("Первому человеку нужен логин — иначе в пространство некому войти."))
    if not admin_password:
        raise SpaceRefused(_("Первому человеку нужен пароль."))
    if role_code not in ROLE_SHAPES:
        raise SpaceRefused(
            _("Роли «%(role)s» в продукте нет. Выберите одну из: %(known)s")
            % {"role": role_code, "known": ", ".join(ROLE_ORDER)}
        )
    if Tenant.objects.filter(code=code).exists():
        raise SpaceRefused(
            _("Пространство с кодом «%(code)s» уже есть.") % {"code": code}
        )
    if User.objects.filter(username=admin_username).exists():
        # Логин уникален во всей системе, а не внутри партнёра (`0010_users`):
        # человек может работать у двух партнёров одной учёткой.
        raise SpaceRefused(
            _("Логин «%(login)s» уже занят. Если это тот же человек, "
              "заведите пространство и выдайте ему роль отдельно.")
            % {"login": admin_username}
        )

    with transaction.atomic():
        tenant = Tenant.objects.create(
            code=code,
            title=title,
            country_code=country_code.upper(),
            base_currency=base_currency.upper(),
            report_currency=report_currency.upper(),
        )

        roles = {
            role.code: role
            for role in Role.objects.bulk_create(
                Role(
                    tenant=tenant,
                    code=name,
                    title=DEFAULT_TITLES[name],
                    visible_ledgers=list(ROLE_SHAPES[name].ledgers),
                    permissions=list(ROLE_SHAPES[name].permissions),
                    # Матрица роли: где стена, а где отличие, которое
                    # партнёр вправе внести сам (T203, D060).
                    permission_states=permission_states(name),
                    # Снимок формы обязателен: без него доставка (T169) примет
                    # свежую роль за правленную партнёром и обойдёт её стороной.
                    shipped_shape=product_shape(name),
                )
                for name in ROLE_ORDER
            )
        }

        user = User.objects.create_user(
            username=admin_username,
            password=admin_password,
            full_name=admin_full_name or admin_username,
        )
        Membership.objects.create(
            tenant=tenant,
            user_id=user.pk,
            role=roles[role_code],
            # Точки не заданы — значит все. У первого человека их пока нет
            # вовсе, и ограничивать его списком, который он сам же заведёт
            # завтра, значило бы запереть его в пустом пространстве.
            unit_ids=None,
        )

    return NewSpace(tenant_id=tenant.pk, user_id=user.pk, role_code=role_code)
