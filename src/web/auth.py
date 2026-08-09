"""
Вход: узкий интерфейс, за которым спрятана реализация (D013).

Личность проверяется ровно одним способом — паролем штатным механизмом Django.
Всё остальное в продукте знает не про пароли, а про две вещи:

    session_user_id(request) -> UUID | None    кем представиться базе
    get_current_principal(request) -> Principal | None   (модуль principal)

Поэтому замена входа на корпоративный позже — это подмена этого модуля, а не
правка представлений.

Три тонкости, каждая из которых уже стоила бы дефекта:

1. **Личность берётся из сессии, а не из базы.** Контекст надо выставить до
   первого запроса к данным, а сама таблица учёток тоже закрыта политиками:
   строку пользователя видно только под его же контекстом. Замкнутый круг
   разрывается тем, что в сессии Django уже лежит идентификатор.
2. **Вход читает учётку по разрешению на один логин.** Пока никто не вошёл,
   таблица `users` не отдаёт ни строки — иначе её мог бы вычитать любой
   запрос. На время проверки пароля выставляется `app.login_username`, и
   политика открывает ровно одну строку, ровно на одну транзакцию.
3. **Dev-вход не второй способ проверки личности.** Кнопка подставляет пароль
   учётки сида и идёт тем же путём. Выключается настройкой, и выданные ею
   сессии перестают действовать в тот же момент — иначе ярлык оставался бы
   работающим обходным путём после того, как его «выключили».
"""
from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from django.conf import settings
from django.contrib import auth as django_auth
from django.db import connection

from core.management.commands.seed_dev import ROLES, det_id
from core.models import User

from .dbcontext import set_db_context
from .flags import dev_login_enabled  # noqa: F401  — переэкспорт для тестов и настроек

# Ключ, под которым Django auth держит идентификатор в сессии. Читаем его сами,
# чтобы выставить контекст базы до того, как кто-нибудь тронет данные.
SESSION_USER_KEY = "_auth_user_id"

# Пометка «сессия выдана ярлыком разработчика».
DEV_SESSION_KEY = "dev_login"

LOGIN_SETTING = "app.login_username"

BACKEND = "django.contrib.auth.backends.ModelBackend"


@dataclass(frozen=True)
class DevUser:
    """Учётка сида для страницы-ярлыка: что показать и под каким логином войти."""

    code: str
    seeded_title: str
    ledgers: tuple[str, ...]
    unit: str | None

    @property
    def username(self) -> str:
        return self.code

    @property
    def title(self) -> str:
        """Название роли на языке страницы входа (T017).

        Свойством, а не полем: список учёток собирается один раз при импорте
        модуля, а язык у каждого запроса свой — записанное в поле название
        осталось бы русским навсегда, в том числе на демо, которое обязано быть
        английским. Название из сида остаётся запасным: сид вправе завести роль,
        которой продукт не знает, и выдумывать ей перевод неоткуда.
        """
        from .i18n import role_title

        return role_title(self.code, self.seeded_title)

    @property
    def user_id(self) -> UUID:
        return det_id("user", self.code)


DEV_USERS: dict[str, DevUser] = {
    role.code: DevUser(
        code=role.code,
        seeded_title=role.title,
        ledgers=tuple(role.ledgers),
        unit=role.unit,
    )
    for role in ROLES
}


def dev_login_is_enabled() -> bool:
    return bool(getattr(settings, "DEV_LOGIN_ENABLED", False))


def _set_login_scope(username: str) -> None:
    """Открыть политике ровно одну учётку — на время проверки пароля."""
    with connection.cursor() as cursor:
        cursor.execute("select set_config(%s, %s, true)", (LOGIN_SETTING, username))


def session_user_id(request) -> UUID | None:
    """Кем представляться базе. None — контекста нет, выборки будут пустыми.

    Мусор в сессии — это тоже None: подставлять его в контекст нельзя (запрос
    упал бы на приведении к uuid), а угадывать за пользователя нечего.
    """
    raw = request.session.get(SESSION_USER_KEY)
    if not raw:
        return None
    try:
        return UUID(str(raw))
    except (ValueError, AttributeError, TypeError):
        return None


def drop_dev_session_if_disabled(request) -> None:
    """Выключили dev-вход — сессия, выданная ярлыком, перестаёт действовать.

    Без этого «выключение» было бы бумажным: уже выданная cookie продолжала бы
    пускать в продукт, и на площадке ярлык остался бы живым обходным путём.
    """
    if request.session.get(DEV_SESSION_KEY) and not dev_login_is_enabled():
        django_auth.logout(request)


def login_with_password(request, username: str, password: str) -> User | None:
    """Проверить пароль и войти. None — не пустили, причину наружу не уточняем."""
    _set_login_scope(username)
    try:
        user = django_auth.authenticate(request, username=username, password=password)
    finally:
        # Окно, в котором видна чужая строка учётки, закрывается сразу же,
        # а не «до конца запроса».
        _set_login_scope("")

    if user is None:
        return None

    # Дальше запрос идёт уже от его имени: отметку последнего входа пишет сам
    # Django, и записать её в свою же строку можно только под своим контекстом.
    set_db_context(connection, user.pk)
    django_auth.login(request, user)
    return user


def dev_login(request, code: str) -> User | None:
    """Войти кнопкой: тот же вход, пароль подставлен за человека."""
    if not dev_login_is_enabled():
        return None
    dev_user = DEV_USERS.get(code)
    if dev_user is None:
        return None

    user = login_with_password(request, dev_user.username, settings.DEV_LOGIN_PASSWORD)
    if user is not None:
        request.session[DEV_SESSION_KEY] = True
    return user


def logout(request) -> None:
    django_auth.logout(request)
