"""Правила страны пишет только право платформы, и это держит база (T165).

**Зачем отдельно от экранного теста.** Там проверяется интерфейс: формы правки
правил страны нет у того, кому не положено, а отказ приходит словами. Это одна
половина пары. Вторая — что запись не пройдёт **мимо** экрана: через будущий
API, через Telegram, через чужой скрипт с теми же доступами к базе. Половина
пары, проверенная за обе, — ровно тот способ, которым в этом проекте уже прожил
незамеченным дефект видимости регистров.

**Почему это важнее обычного права.** `rule_presets` — общий справочник без
`tenant_id` (`SHARED_TABLES`, `0004_rls`): одну и ту же строку читают все
партнёры страны. Ошибка здесь означает не «партнёр увидел лишнее», а «партнёр
поменял расчёт соседу» — и поменял задним числом. Именно поэтому миграция
`0180_rules_permissions` отказалась заводить сюда партнёрское право, и именно
поэтому право лежит в `platform_admins`, а не в роли.

**Ролью `app_user`.** Тесты подключаются владельцем схемы, а он в тестовой базе
суперпользователь: политики его не ограничивают, `force row level security` он
обходит. Запрет, проверенный без переключения роли, зелен всегда.
"""
from __future__ import annotations

import psycopg
import pytest

from conftest import (
    USER_ACCOUNTANT,
    USER_ADMIN,
    USER_DIRECTOR,
    as_app_user,
)

pytestmark = pytest.mark.usefixtures("db")

DENIED = psycopg.errors.InsufficientPrivilege

# Администратор сети партнёра — тот, у кого есть `rules.manage`. Он вправе вести
# переопределения СВОЕГО партнёра и не вправе трогать тело страны: это и есть
# граница, которую проверяет файл.
PARTNER_ROLES = [USER_ADMIN, USER_DIRECTOR, USER_ACCOUNTANT]

INSERT_PRESET = """
    insert into rule_presets (code, title, country_code, body, valid_from)
    values ('policy-test', 'Проверка политики', 'RS', '{"preset": "policy-test"}'::jsonb,
            '2027-01-01')
"""


def _preset(conn) -> str:
    """Версия правил страны, которую потом правят. Кладётся владельцем схемы.

    Здесь это правильный путь, а не обход: тело страны в продукт и приезжает
    мимо политик — командой `load_presets`, которая ходит в базу владельцем.
    """
    return conn.execute(INSERT_PRESET + " returning id").fetchone()[0]


def _grant_platform(conn, user_id: str) -> None:
    """Выдать право вести правила стран — владельцем схемы, как это делает команда.

    Из приложения эта таблица не пишется вовсе: политик на запись у неё нет, а
    права роли `app_user` отозваны. Поэтому подготовка здесь идёт настоящим
    путём продукта — `manage.py platform_admin` делает ровно это.
    """
    conn.execute("insert into platform_admins (user_id) values (%s)", (user_id,))


# --- запрет ------------------------------------------------------------------


@pytest.mark.parametrize("user", PARTNER_ROLES)
def test_a_partner_role_cannot_insert_a_country_rules_version(db, user):
    """Никакая роль партнёра не заводит версию правил страны."""
    with as_app_user(db, user) as conn:
        conn.execute("savepoint attempt")
        with pytest.raises(DENIED):
            conn.execute(INSERT_PRESET)
        conn.execute("rollback to savepoint attempt")


@pytest.mark.parametrize("user", PARTNER_ROLES)
def test_a_partner_role_cannot_change_a_country_rules_version(db, user):
    """И не правит уже лежащую: иначе закрытый месяц соседа переписывался бы."""
    preset = _preset(db)
    with as_app_user(db, user) as conn:
        conn.execute("savepoint attempt")
        with pytest.raises(DENIED):
            conn.execute(
                "update rule_presets set body = '{}'::jsonb where id = %s", (preset,)
            )
        conn.execute("rollback to savepoint attempt")


def test_nobody_deletes_a_country_rules_version(db):
    """Удаления нет ни у кого, включая право платформы.

    Версия правила страны объясняет, почему закрытый месяц посчитан именно так.
    Удалённая унесла бы объяснение с собой — тем же доводом, по которому на
    экране нет кнопки «удалить версию».
    """
    preset = _preset(db)
    _grant_platform(db, USER_ADMIN)
    with as_app_user(db, USER_ADMIN) as conn:
        assert conn.execute(
            "delete from rule_presets where id = %s", (preset,)
        ).rowcount == 0, "версию правил страны удалили — политики на delete быть не должно"


def test_the_right_cannot_be_granted_from_the_application(db):
    """Право вести правила стран из приложения не выдаётся — ни себе, ни другому.

    Это главное свойство таблицы: будь право колонкой в `users`, человек выписал
    бы его себе политикой `users_change_own_row`, которой он правит свой пароль.
    """
    with as_app_user(db, USER_ADMIN) as conn:
        conn.execute("savepoint attempt")
        with pytest.raises(psycopg.Error):
            conn.execute(
                "insert into platform_admins (user_id) values (%s)", (USER_ADMIN,)
            )
        conn.execute("rollback to savepoint attempt")


def test_without_a_context_the_right_is_absent(db):
    """Контекст не выставлен — права нет: запрет по умолчанию, как у всех функций."""
    with as_app_user(db, None) as conn:
        assert conn.execute("select app_is_platform_admin()").fetchone()[0] is False


def test_the_right_is_not_visible_to_others(db):
    """Список администраторов платформы партнёру не виден — только своя строка."""
    _grant_platform(db, USER_ADMIN)
    with as_app_user(db, USER_DIRECTOR) as conn:
        assert conn.execute("select count(*) from platform_admins").fetchone()[0] == 0
        assert conn.execute("select app_is_platform_admin()").fetchone()[0] is False
    with as_app_user(db, USER_ADMIN) as conn:
        assert conn.execute("select count(*) from platform_admins").fetchone()[0] == 1
        assert conn.execute("select app_is_platform_admin()").fetchone()[0] is True


# --- разрешение (без него запрет неотличим от сломанной таблицы) -------------


def test_the_platform_right_inserts_and_closes_a_country_rules_version(db):
    """Право платформы заводит версию и закрывает прежнюю датой.

    Обе операции проверяются вместе, потому что правка правила страны — это
    именно они две: прежняя версия закрывается днём начала новой, новая
    вставляется. Разрешить одну и забыть вторую значило бы получить отказ на
    середине правки.
    """
    preset = _preset(db)
    _grant_platform(db, USER_ADMIN)
    with as_app_user(db, USER_ADMIN) as conn:
        assert conn.execute(
            "update rule_presets set valid_to = '2027-06-01' where id = %s", (preset,)
        ).rowcount == 1
        assert conn.execute(
            """insert into rule_presets (code, title, country_code, body, valid_from, edited_at)
               values ('policy-test', 'Проверка политики', 'RS', '{}'::jsonb,
                       '2027-06-01', now())"""
        ).rowcount == 1
