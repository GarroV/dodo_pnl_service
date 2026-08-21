"""Чья бумага и кому виден её файл (T174, D047).

Всё гоняется **ролью `app_user`**. Владелец таблиц обходит `force row level
security`, а суперпользователь обходит её всегда: тот же набор проверок,
выполненный владельцем, был бы зелёным при снятых политиках. На этом проекте так
уже прожил незамеченным дефект видимости регистров, поэтому здесь у каждой
проверки доступа есть парная — «тот же запрос владельцем видит оба», — и без неё
проверка ничего не доказывает.

Проверяется четыре утверждения, и каждое ломается одной строкой:

1. управляющий видит бумаги своей точки и **не видит** чужие;
2. записать бумагу на чужую точку он не может — отвергает `with check`, а не
   форма (D014): забытый фильтр на записи обходится подменой поля;
3. бумага **обязана** назвать точку: сдать её «на всю сеть» нельзя, иначе она
   была бы видна всем, кто ведёт партнёра;
4. файл виден ровно тогда, когда виден его документ, — и на чтении, и на записи.
"""
from __future__ import annotations

import psycopg
import pytest

from conftest import (
    T1,
    U_BG1,
    U_NS1,
    USER_ACCOUNTANT,
    USER_MANAGER,
    as_app_user,
)

# Байты «файла»: настоящая подпись JPEG и три байта содержимого. Схеме
# безразлично, что внутри, а подпись оставлена настоящей, чтобы строка
# отличалась от заведомо невозможной.
JPEG = b"\xff\xd8\xff" + b"paper"


def paper(conn, *, unit, external, handed=True):
    """Принесённая бумага: документ с точкой и отметкой о передаче."""
    return str(conn.execute(
        """insert into source_documents
                (tenant_id, kind, source, external_id, doc_date, unit_id,
                 handed_over_at)
           values (%s, 'invoice', 'manual', %s, '2026-07-03', %s,
                   case when %s then now() else null end)
           returning id""",
        (T1, external, unit, handed),
    ).fetchone()[0])


def attach(conn, document_id):
    """Файл к бумаге — тем же путём, каким его пишет продукт."""
    return conn.execute(
        """insert into document_files
                (document_id, tenant_id, media_type, byte_size, content, sha256)
           values (%s, %s, 'image/jpeg', %s, %s, 'x')
           returning document_id""",
        (document_id, T1, len(JPEG), JPEG),
    ).fetchone()[0]


# --- чья бумага ---------------------------------------------------------------


def test_the_manager_sees_only_the_papers_of_his_unit(db):
    """Бумага чужой точки управляющему не видна ни строкой, ни номером."""
    paper(db, unit=U_NS1, external="paper:mine")
    paper(db, unit=U_BG1, external="paper:alien")

    with as_app_user(db, USER_MANAGER) as conn:
        seen = {
            row[0] for row in conn.execute(
                "select external_id from source_documents where external_id like 'paper:%'"
            ).fetchall()
        }
    assert seen == {"paper:mine"}


def test_the_unit_check_is_meaningful(db):
    """Тот же запрос владельцем видит обе бумаги — значит выше отсекала RLS."""
    paper(db, unit=U_NS1, external="paper:mine")
    paper(db, unit=U_BG1, external="paper:alien")

    seen = {
        row[0] for row in db.execute(
            "select external_id from source_documents where external_id like 'paper:%'"
        ).fetchall()
    }
    assert seen == {"paper:mine", "paper:alien"}


def test_the_accountant_sees_the_papers_of_every_unit(db):
    """Разбирает бухгалтер, и для этого ему видны бумаги всех точек (D036)."""
    paper(db, unit=U_NS1, external="paper:mine")
    paper(db, unit=U_BG1, external="paper:alien")

    with as_app_user(db, USER_ACCOUNTANT) as conn:
        counted = conn.execute(
            "select count(*) from source_documents where external_id like 'paper:%'"
        ).fetchone()[0]
    assert counted == 2


def test_the_manager_cannot_hand_over_a_paper_for_another_unit(db):
    """Подмена точки в форме не проходит: закрывает `with check`, а не форма.

    Это и есть проверка подменой. Список точек в форме — удобство; будь защита
    только в нём, управляющий сдавал бы бумагу за соседнюю точку одним POST.
    """
    with as_app_user(db, USER_MANAGER) as conn:
        with pytest.raises(psycopg.errors.InsufficientPrivilege), conn.transaction():
            paper(conn, unit=U_BG1, external="paper:hack")


def test_a_paper_must_name_its_unit(db):
    """Бумага «на всю сеть» не сдаётся: у неё есть точка, с которой её принесли.

    Без этого ограничения управляющий обошёл бы политику точки пустым полем:
    строка без точки видна всем в тенанте намеренно (там это означает «точка
    живёт у строки»), и его бумага стала бы видна всем.
    """
    with as_app_user(db, USER_MANAGER) as conn:
        with pytest.raises(psycopg.errors.CheckViolation), conn.transaction():
            paper(conn, unit=None, external="paper:networkwide")


def test_an_ordinary_document_still_needs_no_unit(db):
    """Обратная сторона: у счёта и у выписки точки нет, и это законно.

    Ограничение про точку касается только принесённой бумаги. Распространись
    оно на все документы — сломался бы весь четвёртый блок: у счёта точка живёт
    у строки, а не у шапки.
    """
    with as_app_user(db, USER_MANAGER) as conn, conn.transaction():
        assert paper(conn, unit=None, external="inv:ordinary", handed=False)


# --- файл ---------------------------------------------------------------------


def test_the_file_follows_its_document(db):
    """Файл чужой бумаги не читается: политика зовёт сам документ."""
    mine = paper(db, unit=U_NS1, external="paper:mine")
    alien = paper(db, unit=U_BG1, external="paper:alien")
    attach(db, mine)
    attach(db, alien)

    with as_app_user(db, USER_MANAGER) as conn:
        seen = {
            str(row[0]) for row in conn.execute(
                "select document_id from document_files"
            ).fetchall()
        }
    assert seen == {mine}


def test_the_file_check_is_meaningful(db):
    """Тот же запрос владельцем видит оба файла — значит выше отсекала RLS."""
    mine = paper(db, unit=U_NS1, external="paper:mine")
    alien = paper(db, unit=U_BG1, external="paper:alien")
    attach(db, mine)
    attach(db, alien)

    seen = {
        str(row[0]) for row in db.execute("select document_id from document_files").fetchall()
    }
    assert seen == {mine, alien}


def test_the_manager_cannot_attach_a_file_to_a_stranger_paper(db):
    """Запись файла закрыта тем же правилом, что чтение.

    Иначе управляющий подложил бы свою фотографию к бумаге чужой точки —
    прочитать бы не смог, а подменить смог бы.
    """
    alien = paper(db, unit=U_BG1, external="paper:alien")

    with as_app_user(db, USER_MANAGER) as conn:
        with pytest.raises(psycopg.errors.InsufficientPrivilege), conn.transaction():
            attach(conn, alien)


def test_the_manager_attaches_a_file_to_his_own_paper(db):
    """И наоборот: своя бумага со своим файлом записывается без всяких прав.

    Права на внесение первичных данных в продукте нет ни у кого (их отвергают
    точка и регистр, а не отдельное право), и эта проверка сторожит, чтобы
    предыдущие не «позеленели» отказом на любую запись подряд.
    """
    with as_app_user(db, USER_MANAGER) as conn, conn.transaction():
        mine = paper(conn, unit=U_NS1, external="paper:mine")
        assert attach(conn, mine)


def test_a_paper_without_a_tenant_context_is_invisible(db):
    """Без выставленного контекста не видно ничего — ни бумаги, ни файла."""
    mine = paper(db, unit=U_NS1, external="paper:mine")
    attach(db, mine)

    with as_app_user(db, None) as conn:
        assert conn.execute("select count(*) from source_documents").fetchone()[0] == 0
        assert conn.execute("select count(*) from document_files").fetchone()[0] == 0
