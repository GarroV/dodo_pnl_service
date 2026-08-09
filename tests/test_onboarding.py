"""Онбординг: человек со стороны понимает, что делать дальше (T077).

Замечание владельца после первого живого просмотра: «очень интересно, но
онбординг явно нужен». Он про то, что экраны написаны для того, кто их строил —
данные показаны честно, запреты объяснены честно, а вот **с чего месяц
начинается**, не сказано нигде.

Проверять здесь надо проходимость пути, а не наличие текста: «на странице есть
слово „часы“» зелёное и на экране, который человека никуда не ведёт. Поэтому
проверки такие:

1. **Полоса шагов честна.** Текущий шаг ровно один, и он первый невыполненный:
   полоса отвечает на вопрос «что делать сейчас», а два выделенных шага — это
   снова выбор, которого новичок сделать не может.
2. **Путь проходится по ссылкам.** Вход → список месяцев → месяц → табель, и
   каждый следующий шаг найден **на предыдущей странице**, а не собран тестом по
   известному адресу. Экран, на который нельзя дойти нажатиями, для человека
   со стороны не существует.
3. **Пустое состояние называет следующее действие.** Пустая таблица без слов —
   это ровно то, на что жаловался владелец.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from conftest import body, login_as, period_url
from web import onboarding

ROOT = Path(__file__).resolve().parent.parent
SCREENS = sorted(ROOT.glob("src/*/templates/**/*.html"))


# --- 1. полоса шагов ---------------------------------------------------------


def test_steps_are_the_order_of_the_month():
    """Порядок жёсткий: без часов нечего считать, без расчёта нечего утверждать."""
    assert [step["code"] for step in onboarding.month_steps()] == [
        "hours",
        "calculate",
        "approve",
    ]


@pytest.mark.parametrize(
    "state,expected",
    [
        ({"has_hours": False, "calculated": False, "approved": False}, "hours"),
        ({"has_hours": True, "calculated": False, "approved": False}, "calculate"),
        ({"has_hours": True, "calculated": True, "approved": False}, "approve"),
    ],
)
def test_exactly_one_step_is_current_and_it_is_the_first_undone(state, expected):
    steps = onboarding.month_steps(**state)
    current = [step["code"] for step in steps if step["now"]]
    assert current == [expected]


def test_a_finished_month_has_no_current_step():
    """Всё сделано — подсказывать нечего, и выделять нечего.

    Выделенный шаг у закрытого месяца читался бы как незаконченная работа.
    """
    steps = onboarding.month_steps(has_hours=True, calculated=True, approved=True)
    assert not [step for step in steps if step["now"]]
    assert all(step["done"] for step in steps)


def test_without_knowing_the_month_nothing_is_highlighted():
    """На списке периодов не видно, о каком месяце речь, — и наугад не выделяем.

    Полоса там объясняет порядок работы, а не состояние: выделить шаг, ничего не
    зная про месяц, значило бы соврать первым же, что человек читает.
    """
    steps = onboarding.month_steps()
    assert not [step for step in steps if step["now"] or step["done"]]


# --- 2. путь новичка ---------------------------------------------------------


def test_a_newcomer_walks_from_the_entrance_to_the_sheet_by_links(client, web_env):
    """Вход → месяцы → месяц → табель, и каждый шаг найден на прошлой странице.

    Ровно то, что делает человек, впервые открывший продукт: он не знает ни
    одного адреса и ходит нажатиями. Адреса в этом тесте не написаны — они
    вычитываются из разметки, и экран, до которого нельзя дойти, тест валит.
    """
    entrance = body(client.get("/"), )
    assert entrance is not None

    login_as(client, "director")

    months = body(client.get("/periods/"))
    assert "steps" in months, "порядок работы за месяц не показан на первом же экране"
    month = re.search(r'href="(/periods/[0-9a-f-]+/)"', months)
    assert month, "со списка месяцев нельзя перейти в месяц"

    page = body(client.get(month.group(1)))
    assert 'class="steps"' in page, "на странице месяца нет полосы шагов"
    assert 'aria-current="step"' in page, "не видно, на каком шаге месяц сейчас"

    timesheet = re.search(r'href="(/timesheets/[^"]+)"', page)
    assert timesheet, "со страницы месяца нельзя попасть в табель — первый шаг недостижим"
    grid = body(client.get(timesheet.group(1)))
    assert re.search(r'href="/periods/[0-9a-f-]+/"', grid), "из табеля нет дороги назад"


def test_the_first_step_of_the_month_is_named_on_the_period_page(client, web_env):
    """На странице месяца написано, что месяц начинается с часов.

    Не «есть слово часы», а именно подсказка первого шага: она приезжает из
    `onboarding.STEPS`, то есть из одного места, а не из разметки экрана.
    """
    login_as(client, "director")
    page = body(client.get(period_url(client)))
    hint = str(onboarding.STEPS[0]["hint"])
    assert hint in page, f"на странице месяца нет подсказки первого шага: {hint}"


# --- 3. пустые состояния -----------------------------------------------------


EMPTY = re.compile(r"\{%\s*notice\s+\"empty\"(.*?)\{%\s*endnotice\s*%\}", re.S)


def test_every_empty_state_says_what_to_do_next():
    """Пустое состояние объясняет следующий шаг, а не сообщает о пустоте.

    Признак объяснения — заголовок (`title=`) либо действие (ссылка внутри):
    одинокая фраза «данных нет» ни на один вопрос человека не отвечает.

    Проверка держит правило файлом, а не договорённостью: следующий экран
    напишет своё пустое состояние, и оно обязано быть таким же.
    """
    assert SCREENS, "шаблоны не нашлись — проверка проверяет пустоту"
    silent = []
    for path in SCREENS:
        source = path.read_text(encoding="utf-8")
        for block in EMPTY.findall(source):
            head, _, rest = block.partition("%}")
            has_title = "title=" in head
            has_action = "<a " in rest or "{% url" in rest
            if not (has_title or has_action):
                silent.append(f"{path.relative_to(ROOT)}: {rest.strip()[:70]}")
    assert not silent, (
        "пустые состояния молчат о следующем шаге:\n" + "\n".join(silent)
    )


@pytest.fixture
def untouched_month(web_env):
    """Месяц, с которым ещё ничего не делали: ни часов, ни расчёта.

    Заводится и убирается здесь же, потому что в сиде такого месяца нет — а
    именно его и видит человек, впервые открывший продукт. Добавление, а не
    правка существующего: соседние тесты считают суммы июня, и трогать его
    значило бы ломать их через общую базу.
    """
    import psycopg

    period = "2026-07-01"
    with psycopg.connect(web_env, autocommit=True) as conn:
        tenant = conn.execute("select id from tenants where code = 'rs-dev'").fetchone()[0]
        row = conn.execute(
            """insert into periods (tenant_id, period, status)
               values (%s, %s, 'open') returning id""",
            (tenant, period),
        ).fetchone()[0]
    try:
        yield f"/periods/{row}/"
    finally:
        with psycopg.connect(web_env, autocommit=True) as conn:
            conn.execute("delete from periods where id = %s", (row,))


def test_a_month_without_hours_sends_to_the_timesheet(client, web_env, untouched_month):
    """Месяц, в котором ещё ничего нет, зовёт туда, где месяц начинается.

    Это самый первый экран новичка после списка месяцев, и пустая рамка на нём
    и есть та самая жалоба владельца. Проверяется не текст, а ссылка: подсказка
    без дороги — это по-прежнему «догадайся сам».
    """
    login_as(client, "director")
    page = body(client.get(untouched_month))
    assert 'class="empty"' in page, "ведомости нет, а пустого состояния тоже нет"
    assert re.search(r'class="btn next" href="/timesheets/', page), (
        "пустая ведомость не предлагает пойти туда, где месяц начинается"
    )
    # И полоса шагов честно стоит на первом шаге, а не на середине месяца.
    assert str(onboarding.STEPS[0]["hint"]) in page


def test_a_month_with_hours_but_no_payrun_points_at_the_calculation(client, web_env):
    """Часы уже есть — значит следующий шаг расчёт, и сказано именно это.

    Одно пустое состояние на оба случая отправляло бы человека с внесёнными
    часами обратно в табель, где делать уже нечего.
    """
    import psycopg

    from conftest import wipe_payruns

    wipe_payruns(web_env)
    login_as(client, "director")
    page = body(client.get(period_url(client)))
    assert 'class="empty"' in page
    assert 'class="btn next" href="/timesheets/' not in page, (
        "часы внесены, а человека всё равно отправляют вносить часы"
    )
    assert str(onboarding.STEPS[1]["hint"])[:20] in page or "посчитать период" in page

    with psycopg.connect(web_env, autocommit=True) as conn:
        assert conn.execute("select count(*) from payruns").fetchone()[0] == 0
