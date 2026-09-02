"""Сторож красного прогона не должен молчать и не должен кричать зря (T200).

`tools/ci_watch.py` ходит на GitHub с машины владельца и пишет в канал уведомлений,
до которого раннер GitHub дотянуться не может (см. шапку модуля). Раз сторож сам
решает, когда писать в личный канал, ошибка в этом решении обходится не крахом
теста, а сообщением, которое человек либо не увидит, либо увидит зря — и то, и
другое стоит доверия к сторожу как к проверке.

Три вещи проверяются здесь особенно строго, а не как попутные детали:

1. **Приговор ищется вглубь истории, а не берётся с вершины.** У master включён
   `cancel-in-progress`, поэтому отменённых прогонов подряд бывает много, и самый
   свежий прогон почти никогда не самый информативный.
2. **Сеть подменена полностью.** Ни один тест не должен реально сходить на GitHub
   или написать в канал — тестовый прогон, отправивший настоящее сообщение,
   не проверка, а инцидент.
3. **Память не даёт повторов, но и не глотает случаи, о которых надо сказать
   заново** (новая поломка, возврат к зелёному после тревоги, честный сбой).

Модуль тестами не правится: если тест обнаруживает дефект `ci_watch.py`, это
описано в докстроке теста и в отчёте, а не спрятано подгонкой ожидания под факт.
"""
from __future__ import annotations

import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))
import ci_watch  # noqa: E402 — путь к tools/ добавляется прямо перед этим импортом

# --- 1. verdict(runs) — какой прогон вообще что-то говорит о коде ------------


def _run(run_id: int, status: str, conclusion: str | None = None) -> dict:
    """Минимальный прогон GitHub API — только те поля, что смотрит verdict()."""
    return {"id": run_id, "status": status, "conclusion": conclusion}


@pytest.mark.parametrize(
    "runs, expected_id",
    [
        pytest.param(
            [_run(1, "completed", "failure")],
            1,
            id="первый_же_красный_приговор",
        ),
        pytest.param(
            [_run(1, "completed", "success")],
            1,
            id="первый_же_зелёный_приговор",
        ),
        pytest.param(
            [_run(1, "completed", "timed_out")],
            1,
            id="timed_out_тоже_красный",
        ),
        pytest.param(
            [_run(1, "completed", "startup_failure")],
            1,
            id="startup_failure_тоже_красный",
        ),
        pytest.param(
            [_run(1, "completed", "cancelled"), _run(2, "completed", "failure")],
            2,
            id="отменённый_пропускается_ищем_дальше",
        ),
        pytest.param(
            [_run(1, "completed", "skipped"), _run(2, "completed", "success")],
            2,
            id="skipped_пропускается",
        ),
        pytest.param(
            [_run(1, "completed", "neutral"), _run(2, "completed", "failure")],
            2,
            id="neutral_пропускается",
        ),
        pytest.param(
            [_run(1, "completed", "stale"), _run(2, "completed", "success")],
            2,
            id="stale_пропускается",
        ),
        pytest.param(
            [_run(1, "in_progress", "failure"), _run(2, "completed", "success")],
            2,
            id="ещё_идущий_прогон_не_приговор_даже_с_conclusion",
        ),
        pytest.param(
            [_run(1, "queued", "success"), _run(2, "completed", "failure")],
            2,
            id="queued_не_приговор_даже_с_conclusion",
        ),
        pytest.param([], None, id="пустой_список_прогонов"),
        pytest.param(
            [_run(1, "completed", "cancelled"), _run(2, "completed", "cancelled")],
            None,
            id="одни_отменённые_подряд_дают_None",
        ),
    ],
)
def test_verdict_выбирает_первый_прогон_с_приговором(runs, expected_id):
    """verdict() обязан искать вглубь, а не судить по верхнему элементу списка.

    Если бы сторож брал runs[0] не глядя, отменённый прогон (обычное дело на
    master с cancel-in-progress) читался бы как «нет новостей», хотя настоящая
    новость — красный прогон — лежит на пару позиций ниже.
    """
    result = ci_watch.verdict(runs)
    if expected_id is None:
        assert result is None, (
            f"ожидали None (нет приговора в списке), а verdict() вернул {result!r}"
        )
    else:
        assert result is not None, (
            f"ожидали прогон с id={expected_id}, а verdict() вернул None"
        )
        assert result["id"] == expected_id, (
            f"verdict() выбрал прогон {result['id']}, а должен был {expected_id} — "
            "правило пропуска не приговоров работает не так, как задумано"
        )


def test_verdict_три_отменённых_потом_красный_живой_случай_master():
    """Тот самый случай, что был на живом master: серия cancel-in-progress перед

    настоящей поломкой. Отдельный тест — потому что это не абстрактный пример
    из таблицы, а воспроизведение конкретного инцидента, ради которого писался
    сторож (см. шапку модуля).
    """
    runs = [
        _run(101, "completed", "cancelled"),
        _run(102, "completed", "cancelled"),
        _run(103, "completed", "cancelled"),
        _run(104, "completed", "failure"),
        _run(105, "completed", "success"),
    ]

    result = ci_watch.verdict(runs)

    assert result is not None, "три отменённых подряд не должны съедать красный приговор"
    assert result["id"] == 104, (
        f"должен был найтись именно прогон 104 (первый настоящий приговор), "
        f"а нашёлся {result['id']}"
    )


# --- 2. Тексты сообщений ------------------------------------------------------


def test_alert_text_содержит_ссылку_коммит_и_упавшие_работы():
    """Тревога бесполезна, если по ней нельзя ни перейти к прогону, ни понять,

    что именно упало — человек читает её в Telegram, где кликнуть в html_url
    единственный способ разобраться.
    """
    run = {
        "name": "CI",
        "head_sha": "abcdef1234567890",
        "html_url": "http://example.invalid/runs/1",
        "display_title": "fix: починили поломку",
    }

    text = ci_watch.alert_text(run, ["tests", "lint"])

    assert "красн" in text.lower(), "текст тревоги обязан говорить, что прогон красный"
    assert "http://example.invalid/runs/1" in text, "без ссылки на прогон некуда перейти"
    assert "abcdef1" in text, "первые 7 символов head_sha должны быть в тексте"
    assert "abcdef1234567890" not in text, "весь head_sha в сообщение класть незачем"
    assert "упало: tests, lint" in text, "список упавших работ должен попасть в текст"


def test_alert_text_без_упавших_работ_остаётся_осмысленным():
    """Запрос списка упавших работ необязательная роскошь (см. failed_jobs) —

    если он пуст, тревога не должна превращаться в пустой шум без объяснений.
    """
    run = {
        "name": "CI",
        "head_sha": "1234567890abcdef",
        "html_url": "http://example.invalid/runs/2",
    }

    text = ci_watch.alert_text(run, [])

    assert "упало:" not in text, "без списка работ строки «упало:» быть не должно"
    assert "http://example.invalid/runs/2" in text, "ссылка обязана остаться и без jobs"
    assert "красн" in text.lower(), "сообщение остаётся тревогой даже без деталей"


def test_recovery_text_говорит_про_зелёный_и_даёт_ссылку():
    """Сообщение о починке — единственный шанс человека узнать, что чинить больше

    не надо; без ссылки и явных слов про зелёный это не отличить от шума.
    """
    run = {
        "html_url": "http://example.invalid/runs/3",
        "display_title": "fix: почитили набор тестов",
    }

    text = ci_watch.recovery_text(run)

    assert "зелён" in text.lower(), "текст обязан явно говорить, что master зелёный"
    assert "http://example.invalid/runs/3" in text, "без ссылки не проверить, что починилось"


@pytest.mark.parametrize(
    "make_text",
    [
        pytest.param(
            lambda run: ci_watch.alert_text(run, []),
            id="alert_text",
        ),
        pytest.param(
            ci_watch.recovery_text,
            id="recovery_text",
        ),
    ],
)
def test_тексты_каналом_не_размечены_markdown(make_text):
    """Протокол канала — обычный текст. Таблица Markdown или жирный шрифт в

    Telegram-подобном канале превращаются в мусор из звёздочек и труб, поэтому
    в обоих текстах не должно быть таких символов разметки.
    """
    run = {
        "name": "CI",
        "head_sha": "abcdef1234567890",
        "html_url": "http://example.invalid/runs/4",
        "display_title": "обычная строка без разметки",
    }

    text = make_text(run)

    for marker in ("|", "**", "```", "##"):
        assert marker not in text, f"текст не должен содержать разметку {marker!r}: {text!r}"


def test_alert_text_без_display_title_не_даёт_пустых_строк_подряд():
    """Пустое поле не должно оставлять в тексте пустую строку — пустая строка

    посреди сообщения выглядит как оборванный текст, а не как «этого поля нет».
    """
    run = {
        "name": "CI",
        "head_sha": "abcdef1234567890",
        "html_url": "http://example.invalid/runs/5",
        # display_title сознательно отсутствует — так бывает у части прогонов.
    }

    text = ci_watch.alert_text(run, [])

    assert "\n\n" not in text, (
        f"пустое поле display_title оставило пустую строку в тексте тревоги: {text!r}"
    )


def test_recovery_text_без_display_title_не_даёт_пустых_строк_подряд():
    """Пустое поле не должно оставлять пустую строку посреди сообщения.

    Дефект нашёлся именно так: `alert_text` пустые поля отбрасывал, а
    `recovery_text` — нет, и прогон без `display_title` давал «master снова
    зелёный.\n\nссылка». Мелочь, но ровно из таких мелочей складывается
    впечатление, что сообщение отправил сломанный робот, а не проверка.
    """
    run = {
        "html_url": "http://example.invalid/runs/6",
        # display_title отсутствует, как и в прогонах без заголовка коммита.
    }

    text = ci_watch.recovery_text(run)

    assert "\n\n" not in text, (
        f"пустое поле display_title оставило пустую строку в тексте о починке: {text!r}"
    )


# --- 3. Память: remembered / remember / state_file ----------------------------


def test_remember_и_remembered_возвращают_то_же_самое(monkeypatch, tmp_path):
    """Память сторожа — единственное, что не даёт ему повторяться; если запись

    и чтение расходятся, повторы (или, наоборот, потерянные тревоги) вернутся.
    """
    monkeypatch.setattr(ci_watch, "STATE_HOME", tmp_path)
    data = {"reported": 104, "state": "red"}

    ci_watch.remember("owner/repo", data)
    result = ci_watch.remembered("owner/repo")

    assert result == data, f"записали {data!r}, прочитали {result!r} — память не совпадает"


def test_remembered_без_файла_даёт_пустой_словарь(monkeypatch, tmp_path):
    """Первый запуск на чистой машине не должен падать — файла памяти ещё нет,

    и это не ошибка, а нормальное «пока ни о чём не докладывали».
    """
    monkeypatch.setattr(ci_watch, "STATE_HOME", tmp_path)

    result = ci_watch.remembered("owner/repo")

    assert result == {}, f"без файла памяти ожидали {{}}, получили {result!r}"


def test_remembered_с_битым_json_даёт_пустой_словарь_без_исключения(monkeypatch, tmp_path):
    """Модуль намеренно не падает на испорченной памяти (см. комментарий в коде):

    испорченный файл хуже вылеченного молчания только тем, что уже случился,
    а не тем, что его дальше нельзя починить повторным докладом.
    """
    monkeypatch.setattr(ci_watch, "STATE_HOME", tmp_path)
    path = ci_watch.state_file("owner/repo")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{это не json", encoding="utf-8")

    result = ci_watch.remembered("owner/repo")

    assert result == {}, f"битый JSON обязан читаться как {{}}, получили {result!r}"


# --- 4. channel() — адрес и секрет --------------------------------------------


def test_channel_берёт_адрес_и_секрет_из_окружения(monkeypatch, tmp_path):
    """Переменные окружения — приоритетный источник (например, для CI-подобного

    запуска), и они обязаны побеждать даже при непустом файле профиля.
    """
    monkeypatch.setenv("CHANNEL_URL", "http://example.invalid/")
    monkeypatch.setenv("FORGE_SECRET", "test-secret")
    monkeypatch.setattr(ci_watch, "CHANNEL_ENV", tmp_path / "не-существует.env")

    url, secret = ci_watch.channel()

    assert url == "http://example.invalid", (
        f"хвостовой слэш обязан обрезаться, получили {url!r}"
    )
    assert secret == "test-secret", (
        f"секрет из окружения ожидали 'test-secret', получили {secret!r}"
    )


def test_channel_читает_файл_профиля_когда_окружение_пусто(monkeypatch, tmp_path):
    """Основной сценарий на машине владельца: переменных окружения нет, профиль

    лежит в CHANNEL_ENV — комментарии и пустые строки не должны ломать разбор.
    """
    monkeypatch.delenv("CHANNEL_URL", raising=False)
    monkeypatch.delenv("FORGE_SECRET", raising=False)
    env_file = tmp_path / "channel.env"
    env_file.write_text(
        "\n".join([
            "# профиль канала — выдуманные значения",
            "",
            "CHANNEL_URL=http://example.invalid",
            "",
            "# секрет ниже",
            "FORGE_SECRET=test-secret",
            "",
        ]),
        encoding="utf-8",
    )
    monkeypatch.setattr(ci_watch, "CHANNEL_ENV", env_file)

    url, secret = ci_watch.channel()

    assert url == "http://example.invalid", f"адрес из файла не разобрался: {url!r}"
    assert secret == "test-secret", f"секрет из файла не разобрался: {secret!r}"


@pytest.mark.parametrize(
    "quote",
    [pytest.param('"', id="двойные_кавычки"), pytest.param("'", id="одинарные_кавычки")],
)
def test_channel_снимает_кавычки_со_значений_в_файле(monkeypatch, tmp_path, quote):
    """Человек, руками правящий профиль, кавычки ставит по привычке из shell —

    разбор обязан их снять, а не унести кавычки внутрь секрета.
    """
    monkeypatch.delenv("CHANNEL_URL", raising=False)
    monkeypatch.delenv("FORGE_SECRET", raising=False)
    env_file = tmp_path / "channel.env"
    env_file.write_text(
        f"CHANNEL_URL={quote}http://example.invalid{quote}\n"
        f"FORGE_SECRET={quote}test-secret{quote}\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(ci_watch, "CHANNEL_ENV", env_file)

    url, secret = ci_watch.channel()

    assert url == "http://example.invalid", f"кавычки вокруг адреса не снялись: {url!r}"
    assert secret == "test-secret", f"кавычки вокруг секрета не снялись: {secret!r}"


def test_channel_без_окружения_и_без_файла_поднимает_Failed(monkeypatch, tmp_path):
    """Отправлять сообщение некуда — молчаливый пропуск здесь хуже явного отказа:

    сторож без адреса канала не должен притворяться, что всё прошло нормально.
    """
    monkeypatch.delenv("CHANNEL_URL", raising=False)
    monkeypatch.delenv("FORGE_SECRET", raising=False)
    monkeypatch.setattr(ci_watch, "CHANNEL_ENV", tmp_path / "нет-такого-файла.env")

    with pytest.raises(ci_watch.Failed):
        ci_watch.channel()


# --- 5 и 6. main() целиком, с подменённым GitHub и каналом -------------------


def _patch_main(monkeypatch, tmp_path, runs, jobs=None):
    """Подменяет весь внешний мир main(): GitHub, канал, репозиторий, память.

    Возвращает список вызовов notify() — по нему тесты проверяют, писал ли
    сторож в канал, и если писал, то что именно.
    """
    calls: list[dict] = []

    monkeypatch.setattr(ci_watch, "STATE_HOME", tmp_path)
    monkeypatch.setattr(ci_watch, "repository", lambda: "owner/repo")
    monkeypatch.setattr(ci_watch, "github", lambda path: {"workflow_runs": runs})
    monkeypatch.setattr(ci_watch, "failed_jobs", lambda repo, run_id: jobs or [])

    def fake_notify(project: str, kind: str, text: str) -> None:
        calls.append({"project": project, "kind": kind, "text": text})

    monkeypatch.setattr(ci_watch, "notify", fake_notify)
    return calls


def test_main_красный_прогон_шлёт_ровно_одну_тревогу(monkeypatch, tmp_path):
    """Первая поломка — единственный случай, когда сторож обязан написать сам,

    без того, чтобы кто-то у него об этом спросил.
    """
    runs = [_run(104, "completed", "failure") | {
        "html_url": "http://example.invalid/runs/104",
        "head_sha": "deadbeef00",
    }]
    calls = _patch_main(monkeypatch, tmp_path, runs)

    code = ci_watch.main([])

    assert code == 0, f"успешная отправка тревоги не должна давать код возврата {code}"
    assert len(calls) == 1, f"ожидали ровно один вызов notify, было {len(calls)}: {calls}"
    assert calls[0]["kind"] == "alert", f"тревога обязана идти с kind='alert', было {calls[0]}"
    assert calls[0]["project"] == "repo", (
        f"project обязан быть именем без владельца ('repo'), было {calls[0]['project']!r}"
    )
    state = ci_watch.remembered("owner/repo")
    assert state == {"reported": 104, "state": "red"}, (
        f"после тревоги память обязана хранить прогон и состояние red, было {state!r}"
    )


def test_main_тот_же_красный_прогон_второй_раз_молчит(monkeypatch, tmp_path):
    """Ради этого и существует память: без неё сторож писал бы об одной и той

    же поломке каждый свой прогон по расписанию — раз в четверть часа.
    """
    runs = [_run(104, "completed", "failure") | {"html_url": "http://x", "head_sha": "abc"}]
    calls = _patch_main(monkeypatch, tmp_path, runs)
    first_code = ci_watch.main([])

    second_code = ci_watch.main([])

    assert first_code == 0 and second_code == 0, "оба запуска обязаны завершаться успешно"
    assert len(calls) == 1, (
        f"второй прогон того же id не должен звать notify снова, всего вызовов: {len(calls)}"
    )


def test_main_другой_красный_прогон_после_первого_шлёт_снова(monkeypatch, tmp_path):
    """Новая поломка — это новая новость, даже если предыдущая ещё не факс

    зелёная: человек должен узнать, что сломалось что-то ещё (или иначе).
    """
    first_runs = [_run(104, "completed", "failure") | {"html_url": "http://x", "head_sha": "a"}]
    calls = _patch_main(monkeypatch, tmp_path, first_runs)
    ci_watch.main([])

    second_runs = [_run(105, "completed", "failure") | {"html_url": "http://y", "head_sha": "b"}]
    monkeypatch.setattr(ci_watch, "github", lambda path: {"workflow_runs": second_runs})

    code = ci_watch.main([])

    assert code == 0, f"код возврата второго прогона обязан быть 0, был {code}"
    assert len(calls) == 2, (
        f"новый id прогона обязан породить новый вызов notify, всего вызовов: {len(calls)}"
    )


def test_main_зелёный_после_тревоги_шлёт_одно_сообщение_о_починке(monkeypatch, tmp_path):
    """Человек, чинивший поломку, должен узнать, что чинить больше не надо —

    ровно одним сообщением, а не тишиной и не потоком «всё ещё зелёное».
    """
    red_runs = [_run(104, "completed", "failure") | {"html_url": "http://x", "head_sha": "a"}]
    calls = _patch_main(monkeypatch, tmp_path, red_runs)
    ci_watch.main([])
    assert len(calls) == 1, "подготовка теста: тревога должна была уйти один раз"

    green_runs = [_run(105, "completed", "success") | {"html_url": "http://y"}]
    monkeypatch.setattr(ci_watch, "github", lambda path: {"workflow_runs": green_runs})

    code = ci_watch.main([])

    assert code == 0, f"починка обязана давать код возврата 0, был {code}"
    assert len(calls) == 2, f"о починке обязано уйти ровно одно сообщение, вызовов: {len(calls)}"
    assert "зелён" in calls[1]["text"].lower(), (
        f"второе сообщение обязано говорить про зелёный master: {calls[1]['text']!r}"
    )
    state = ci_watch.remembered("owner/repo")
    assert state == {"reported": 105, "state": "green"}, (
        f"после починки память обязана перейти в state=green, было {state!r}"
    )


def test_main_зелёный_без_предыдущей_тревоги_молчит(monkeypatch, tmp_path):
    """Если сторож писал бы «всё хорошо» на каждый зелёный прогон, канал раз в

    пятнадцать минут превратился бы в шум, который никто не читает.
    """
    runs = [_run(1, "completed", "success") | {"html_url": "http://x"}]
    calls = _patch_main(monkeypatch, tmp_path, runs)

    code = ci_watch.main([])

    assert code == 0, f"зелёный прогон без истории тревог обязан давать код 0, был {code}"
    assert calls == [], f"notify не должен был зваться вовсе, вызовы: {calls}"


def test_main_dry_run_не_отправляет_и_не_запоминает(monkeypatch, tmp_path):
    """--dry-run существует, чтобы посмотреть, что отправилось бы, не трогая ни

    канал, ни память — иначе проверочный прогон сам стал бы источником шума
    и заодно съел бы настоящую будущую тревогу (пометив прогон как доложенный).
    """
    runs = [_run(104, "completed", "failure") | {"html_url": "http://x", "head_sha": "a"}]
    calls = _patch_main(monkeypatch, tmp_path, runs)

    code = ci_watch.main(["--dry-run"])

    assert code == 0, f"--dry-run на красном прогоне обязан давать код 0, был {code}"
    assert calls == [], f"--dry-run не должен звать notify, вызовы: {calls}"
    assert not ci_watch.state_file("owner/repo").exists(), (
        "--dry-run не должен создавать файл памяти — иначе следующий настоящий "
        "запуск решит, что об этом прогоне уже доложено"
    )


def test_main_без_приговора_звонит_про_тишину(monkeypatch, tmp_path):
    """Ни одного приговора — это тревога, а не спокойствие (D067).

    Здесь постановка поменялась по ответу владельца «надо предусмотреть на
    будущее». Раньше сторож на серии отменённых молчал — и молчал бы ровно в том
    случае, который его и породил: 25 запусков подряд без единого зелёного это
    не серия падений, а отсутствие результата. Сигнал, который молчит вместе с
    прогоном, бесполезен именно тогда, когда нужен.
    """
    runs = [
        _run(1, "completed", "cancelled"),
        _run(2, "completed", "cancelled"),
        _run(3, "in_progress", None),
    ]
    calls = _patch_main(monkeypatch, tmp_path, runs)

    code = ci_watch.main([])

    assert code == 0, f"код возврата обязан быть 0, был {code}"
    assert len(calls) == 1, f"про тишину положено сообщить ровно раз: {calls}"
    assert "нет результата" in calls[0]["text"], calls[0]["text"]


def test_main_про_тишину_не_звонит_дважды(monkeypatch, tmp_path):
    """Тишина длится сутками — сообщение о ней должно быть одно.

    Иначе опрос раз в четверть часа превратит канал в шум, а шум не читают.
    """
    runs = [_run(1, "completed", "cancelled")]
    calls = _patch_main(monkeypatch, tmp_path, runs)

    ci_watch.main([])
    ci_watch.main([])

    assert len(calls) == 1, f"о той же тишине доложено больше одного раза: {calls}"


def test_main_после_тишины_даёт_отбой(monkeypatch, tmp_path):
    """Появился приговор — человек обязан узнать, что можно перестать чинить.

    Без отбоя тревога о тишине висит в голове до конца дня.
    """
    calls = _patch_main(monkeypatch, tmp_path, [_run(1, "completed", "cancelled")])
    ci_watch.main([])
    assert len(calls) == 1

    monkeypatch.setattr(
        ci_watch, "github", lambda path: {"workflow_runs": [_run(2, "completed", "success")]}
    )
    ci_watch.main([])

    assert len(calls) == 2, f"после тишины отбоя не было: {calls}"
    assert "зелёный" in calls[1]["text"], calls[1]["text"]


# --- 6. Отказ виден, а не проглочен -------------------------------------------


def test_main_возвращает_1_и_пишет_в_stderr_когда_github_не_отвечает(
    monkeypatch, tmp_path, capsys
):
    """Сторож, молча вернувший 0 при обрыве связи с GitHub, — ровно та беда,

    ради которой он написан: расписание решит, что всё в порядке, и не
    попробует снова раньше следующего слота.
    """
    monkeypatch.setattr(ci_watch, "STATE_HOME", tmp_path)
    monkeypatch.setattr(ci_watch, "repository", lambda: "owner/repo")

    def broken_github(path: str) -> dict:
        raise ci_watch.Failed("GitHub не ответил (тест)")

    monkeypatch.setattr(ci_watch, "github", broken_github)

    code = ci_watch.main([])

    assert code == 1, f"обрыв связи с GitHub обязан давать код возврата 1, был {code}"
    captured = capsys.readouterr()
    assert "GitHub не ответил (тест)" in captured.err, (
        f"причина отказа обязана попасть в stderr, там было: {captured.err!r}"
    )


def test_main_возвращает_1_и_не_запоминает_когда_канал_недоступен(monkeypatch, tmp_path, capsys):
    """Если notify() провалился, а remember() всё равно отметил прогон как

    доложенный — сообщение потерялось молча и больше никогда не повторится
    (пока прогон не сменится). Проверяем, что порядок в main() именно такой:
    remember() стоит ПОСЛЕ notify() и не выполняется при его отказе.
    """
    runs = [_run(104, "completed", "failure") | {"html_url": "http://x", "head_sha": "a"}]
    monkeypatch.setattr(ci_watch, "STATE_HOME", tmp_path)
    monkeypatch.setattr(ci_watch, "repository", lambda: "owner/repo")
    monkeypatch.setattr(ci_watch, "github", lambda path: {"workflow_runs": runs})
    monkeypatch.setattr(ci_watch, "failed_jobs", lambda repo, run_id: [])

    def broken_notify(project: str, kind: str, text: str) -> None:
        raise ci_watch.Failed("канал недоступен (тест)")

    monkeypatch.setattr(ci_watch, "notify", broken_notify)

    code = ci_watch.main([])

    assert code == 1, f"отказ канала обязан давать код возврата 1, был {code}"
    captured = capsys.readouterr()
    assert "канал недоступен (тест)" in captured.err, (
        f"причина отказа обязана попасть в stderr, там было: {captured.err!r}"
    )
    assert not ci_watch.state_file("owner/repo").exists(), (
        "прогон не должен быть запомнен как доложенный — сообщение не ушло, "
        "а память говорит обратное, и повторной попытки больше не будет"
    )


# --- 7. Тишина как отдельная беда (D067) --------------------------------------


NOW = datetime(2026, 9, 2, 12, 0, tzinfo=UTC)


def _dated(run_id: int, conclusion: str, when: datetime, sha: str = "aaa") -> dict:
    return {
        "id": run_id,
        "status": "completed",
        "conclusion": conclusion,
        "created_at": when.isoformat().replace("+00:00", "Z"),
        "head_sha": sha,
    }


def test_silence_молчит_когда_свежий_приговор_есть():
    """Свежий приговор — не тишина, каким бы он ни был. Иначе сторож звонил бы
    поверх обычной тревоги вторым сообщением о том же."""
    runs = [_dated(1, "failure", NOW - timedelta(hours=1))]
    assert ci_watch.silence(runs, "aaa", None, NOW) is None


def test_silence_видит_отсутствие_приговора():
    """Тот самый случай issue #196: прогоны идут, приговора нет."""
    runs = [
        {"id": 1, "status": "completed", "conclusion": "cancelled"},
        {"id": 2, "status": "in_progress", "conclusion": None},
    ]
    reason = ci_watch.silence(runs, None, None, NOW)
    assert reason and "не дал приговора" in reason, reason


def test_silence_видит_протухший_приговор():
    """Зелёный прогон недельной давности — это не «всё хорошо», а «никто не
    проверял неделю». Отличить одно от другого можно только по времени."""
    runs = [_dated(1, "success", NOW - timedelta(days=7))]
    reason = ci_watch.silence(runs, "aaa", None, NOW)
    assert reason and "свежего результата нет" in reason, reason


def test_silence_видит_верхушку_без_прогона():
    """Workflow не запустился вовсе: приговор свежий, но он про прошлый коммит.

    Самый тихий из всех случаев — на вкладке Actions всё зелёное, и только
    последний коммит там не представлен.
    """
    runs = [_dated(1, "success", NOW - timedelta(minutes=5), sha="old")]
    tip_time = (NOW - timedelta(hours=2)).isoformat().replace("+00:00", "Z")
    reason = ci_watch.silence(runs, "newsha1", tip_time, NOW)
    assert reason and "прогона нет вовсе" in reason, reason


def test_silence_даёт_верхушке_отсрочку():
    """Пуш минуту назад — не повод звонить: GitHub заводит прогон не мгновенно."""
    runs = [_dated(1, "success", NOW - timedelta(minutes=5), sha="old")]
    tip_time = (NOW - timedelta(minutes=1)).isoformat().replace("+00:00", "Z")
    assert ci_watch.silence(runs, "newsha1", tip_time, NOW) is None


@pytest.mark.parametrize("value", [None, "", "не время", "2026-13-45"])
def test_moment_не_падает_на_мусоре(value):
    """Неразобранное время — это «не знаю», а не исключение и не выдуманная
    тишина: ложная тревога отучает читать канал целиком."""
    assert ci_watch.moment(value) is None


@pytest.mark.parametrize(
    "hours, expected",
    [(0.02, "1 мин"), (0.5, "30 мин"), (1.0, "1 ч"), (5.9, "5 ч"),
     (24.0, "1 сут"), (50.0, "2 сут")],
)
def test_age_читается_человеком(hours, expected):
    """«нет уже 0 ч» само выглядит как поломка сторожа, а не как новость."""
    assert ci_watch.age(hours) == expected


def test_main_run_на_отменённом_прогоне_не_объявляет_его_зелёным(
    monkeypatch, tmp_path, capsys
):
    """`--run` берёт прогон по номеру, минуя отбор `verdict()`.

    Из-за этого отменённый прогон доезжал до ветки «зелёное», и сторож бодро
    сообщал «master снова зелёный» про прогон, который никто не довёл до конца.
    Найдено не тестом, а запуском руками — поэтому тест и появился.
    """
    calls = _patch_main(monkeypatch, tmp_path, [])
    ci_watch.remember("owner/repo", {"reported": 1, "state": "red"})
    monkeypatch.setattr(
        ci_watch, "github",
        lambda path: {"id": 7, "status": "completed", "conclusion": "cancelled"},
    )

    code = ci_watch.main(["--run", "7"])

    assert code == 0
    assert calls == [], f"отменённый прогон выдан за приговор: {calls}"
    assert "приговора не дал" in capsys.readouterr().out
