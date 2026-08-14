"""Сид демо и сид разработки описывают ОДНИ роли (T140, issue #91).

`src/demo/seed.py` объявлял прямо над списком ролей, что набор регистров и точка
повторяют роли сида разработки намеренно, — и для администратора сети это было
неправдой: в `seed_dev` у него все три регистра (T089, подробный довод), в демо
остался один официальный. Комментарий утверждал обратное тому, что делал код, и
следующий читатель поверил бы комментарию.

Кусалось это не «в демо всё равно не видно»: кнопки администратора на титульной
странице нет, но учётка есть и войти ею можно (`/demo/enter/admin/`), а
справочник в демо показывается. То есть демо показывало **сломанное** состояние
продукта, которого у партнёра не будет: администратор, не видящий карточек
курьеров и кухни, — тупик, в котором ставку менять некому.

Закрыто одним источником, а не правкой обоих списков: два списка одного и того
же расходятся молча, и второй раз это было бы обнаружено так же случайно.
Источник — `core.roles.ROLE_SHAPES`; оба сида берут оттуда регистры, точку и
права, а своё держат только название роли (русское у продукта, английское у
демо — D035).

Проверка сравнивает два сида напрямую, а не «оба зовут одну функцию»: если
завтра кто-то снова напишет набор руками, разъехаться он всё равно не сможет.
"""
from __future__ import annotations


def shapes_of_dev() -> dict:
    from core.management.commands.seed_dev import ROLES

    return {
        role.code: (tuple(role.ledgers), role.unit, tuple(sorted(role.permissions)))
        for role in ROLES
    }


def shapes_of_demo() -> dict:
    from demo.seed import ROLES

    return {
        code: (tuple(ledgers), unit, tuple(sorted(permissions)))
        for code, _title, ledgers, unit, permissions in ROLES
    }


def test_both_seeds_know_the_same_roles():
    assert sorted(shapes_of_dev()) == sorted(shapes_of_demo())


def test_the_admin_sees_the_same_ledgers_in_the_demo_as_in_the_product():
    """Тот самый разъезд: один официальный регистр против всех трёх (T089)."""
    assert shapes_of_demo()["admin"][0] == ("official", "supplementary", "internal")


def test_every_role_matches_ledger_by_ledger_and_right_by_right():
    """Целиком, а не только у администратора: разъехаться может любая строка."""
    assert shapes_of_demo() == shapes_of_dev()


def test_the_roles_come_from_one_place():
    """Один источник, а не две копии, случайно совпавшие сегодня."""
    from core.roles import ROLE_SHAPES

    from_source = {
        code: (shape.ledgers, shape.unit, tuple(sorted(shape.permissions)))
        for code, shape in ROLE_SHAPES.items()
    }
    assert shapes_of_dev() == from_source
    assert shapes_of_demo() == from_source


def test_the_titles_stay_different():
    """Демо англоязычно целиком (D035), продукт — русский. Это не разъезд, а замысел."""
    from core.management.commands.seed_dev import ROLES as DEV
    from demo.seed import ROLES as DEMO

    dev_titles = {role.code: role.title for role in DEV}
    demo_titles = {code: title for code, title, *_rest in DEMO}
    assert dev_titles["admin"] != demo_titles["admin"]
    assert demo_titles["admin"].isascii()
