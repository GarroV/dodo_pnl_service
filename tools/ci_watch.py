"""Красный прогон на master доходит до человека (T200, issue #196).

Двое суток и 25 запусков подряд CI был красным, и об этом узнали случайно.
Проверка, о результате которой никто не узнаёт, — не проверка: она стоит денег и
времени, а решение принимает всё равно тот, кто случайно открыл вкладку.

## Почему сторож здесь, а не шагом в workflow

Самое очевидное решение — дописать в `.github/workflows/ci.yml` шаг «упало —
позвони в канал» — **не работает, и это проверено, а не предположено**. Канал
живёт на домашнем сервере по адресу тайлнета (`100.64.0.0/10`, CGNAT Tailscale)
и наружу не опубликован. Раннер GitHub в тайлнет не входит: маршрута до этого
адреса у него нет вовсе, поэтому никакие `secrets.CHANNEL_URL` делу не помогут —
секрет нужен тому, кто может дозвониться, а раннер не может.

Три выхода из этого, и выбран третий:

1. **Опубликовать канал наружу.** Личный канал уведомлений на публичном адресе —
   плохой размен ради одного сообщения, и решать это не сторожу.
2. **Завести раннер в тайлнет** (`tailscale/github-action`). Работает, но просит
   отдельный OAuth-клиент Tailscale и поднимает узел тайлнета на каждое падение.
3. **Спрашивать GitHub оттуда, где канал доступен.** Ровно это и делает файл:
   он ходит на машине владельца, спрашивает у GitHub состояние master и, если
   там красное, пишет в канал. Никаких новых секретов, ничего наружу.

## Что считается красным

Только приговор, а не любое непопадание в зелёное:

* **красное** — `failure`, `timed_out`, `startup_failure`;
* **зелёное** — `success`;
* **не приговор** — `cancelled`, `skipped`, `neutral`, `stale` и прогон, который
  ещё идёт. У master таких много: в `ci.yml` включён `cancel-in-progress`, и
  каждый пуш вдогонку отменяет предыдущий прогон. Отменённый прогон не говорит о
  коде ничего, поэтому сторож смотрит дальше в глубину истории, а не объявляет
  тревогу и не считает молчание за зелёное.

## Тишина — тоже повод позвонить (D067)

Владелец: «надо предусмотреть на будущее». Это про класс, а не про случай, и
разница практическая. Уведомление о **падении** молчит ровно тогда, когда молчит
сам прогон: отменённый по concurrency, не запустившийся вовсе, упавший до шага с
тестами — всё это не «падение», а **отсутствие результата**, и человек о нём не
узнает точно так же, как не узнал в тот раз. Двадцать пять запусков подряд без
единого зелёного — это ровно тишина, а не серия падений.

Поэтому сторож умеет сказать три вещи, а не одну: «master красный», «master
зелёный» и «по master давно нет никакого результата». Тишиной считается:

* среди последних прогонов **нет ни одного с приговором** — все отменены или
  идут;
* свежий приговор есть, но ему больше `STALE_HOURS` часов;
* на верхушке ветки **нет прогона вовсе**, а коммит уже старше
  `NO_RUN_GRACE_MIN` минут, — то есть workflow не запустился. Отсрочка нужна,
  чтобы не звонить в те секунды, пока GitHub только заводит прогон.

Состав пропусков сторож **не** разбирает сам, и это намеренно: за него это уже
делает `.github/scripts/check_skips.py` внутри прогона — пропуск без объявленной
причины валит шаг, то есть приходит сюда обычным `failure`. Вторая копия той же
логики разъехалась бы с первой молча.

## Повторов нет

Сторож помнит последний прогон, о котором доложил, и молчит, пока красный тот же
самый. Иначе опрос раз в четверть часа превратил бы канал в шум, а шум читают
ровно так же, как не читают ничего. О возвращении к зелёному сообщается один
раз — и только если до этого была тревога: иначе человек не знает, чинить ещё
или уже нет.

## Как гонять

    python tools/ci_watch.py              # спросить и, если надо, отправить
    python tools/ci_watch.py --dry-run    # показать, что отправилось бы
    python tools/ci_watch.py --run <id>   # разобрать конкретный прогон

Чтобы это происходило само, а не когда вспомнили, сторож вешается на расписание
машины владельца — она же и есть та, у которой есть тайлнет. Как именно —
в README, раздел «Сигнал о красном прогоне».
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
from datetime import UTC, datetime
from pathlib import Path

# Приговор прогона. Всё, чего нет ни в одном множестве, — не приговор.
RED = {"failure", "timed_out", "startup_failure"}
GREEN = {"success"}

# Сколько прогонов истории просматривать в поисках приговора. Отменённых подряд
# бывает много (каждый пуш отменяет предыдущий), но не десятки.
DEPTH = 30

# Сколько ветка вправе молчать, прежде чем молчание само станет новостью.
# Сутки: за это время рабочий день успевает пройти целиком, а история #196
# длилась двое суток — то есть порог поймал бы её на первом же дне.
STALE_HOURS = float(os.environ.get("CI_WATCH_STALE_HOURS", "24"))

# Отсрочка для верхушки без прогона: GitHub заводит прогон не мгновенно, и
# звонить в эти секунды значило бы звонить на каждый пуш.
NO_RUN_GRACE_MIN = float(os.environ.get("CI_WATCH_GRACE_MIN", "30"))

# Состояние живёт рядом с профилем канала, а не в репозитории: это память
# конкретной машины о том, о чём она уже докладывала, а не код проекта.
STATE_HOME = Path.home() / ".claude" / "forge" / "ci-watch"

CHANNEL_ENV = Path.home() / ".claude" / "forge" / "channel.env"


class Failed(Exception):
    """Сторож не смог выполнить работу. Молча такое не проглатывается."""


# --- откуда что берётся ------------------------------------------------------


def channel() -> tuple[str, str]:
    """Адрес канала и секрет: из окружения, иначе из личного профиля.

    В репозитории их нет и быть не может — он публичный (issue #167).
    """
    url = os.environ.get("CHANNEL_URL")
    secret = os.environ.get("FORGE_SECRET")
    if not (url and secret) and CHANNEL_ENV.exists():
        for line in CHANNEL_ENV.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            value = value.strip().strip('"').strip("'")
            if key.strip() == "CHANNEL_URL" and not url:
                url = value
            elif key.strip() == "FORGE_SECRET" and not secret:
                secret = value
    if not (url and secret):
        raise Failed(
            f"нет адреса канала или секрета: ни в окружении, ни в {CHANNEL_ENV}. "
            "Без них сообщение отправить некуда"
        )
    return url.rstrip("/"), secret


def repository() -> str:
    """`владелец/имя` того репозитория, за прогонами которого следим.

    Берётся из адреса `origin`, а не из имени каталога: у рабочих копий (git
    worktree) каталог называется по ветке, и имя разъехалось бы молча.
    """
    try:
        remote = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
    except (subprocess.CalledProcessError, FileNotFoundError) as error:
        raise Failed(f"не удалось спросить адрес origin: {error}") from error
    name = remote.removesuffix(".git")
    if name.startswith("git@"):
        name = name.split(":", 1)[-1]
    else:
        name = "/".join(name.split("/")[-2:])
    if name.count("/") != 1:
        raise Failed(f"адрес origin не похож на репозиторий GitHub: {remote}")
    return name


def github(path: str) -> dict:
    """Спросить GitHub. Через `gh`, если он есть, иначе напрямую.

    `gh` предпочтительнее: он уже с токеном, и лимит запросов у него человеческий.
    Прямой путь — запасной, для машины без `gh`; репозиторий публичный, поэтому
    он работает и без токена.
    """
    try:
        done = subprocess.run(
            ["gh", "api", path], capture_output=True, text=True,
        )
        if done.returncode == 0:
            return json.loads(done.stdout)
    except FileNotFoundError:
        pass

    request = urllib.request.Request(
        f"https://api.github.com/{path}",
        headers={"Accept": "application/vnd.github+json", "User-Agent": "ci-watch"},
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as answer:
            return json.loads(answer.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as error:
        raise Failed(f"GitHub не ответил на {path}: {error}") from error


# --- приговор ----------------------------------------------------------------


def verdict(runs: list[dict]) -> dict | None:
    """Свежий прогон, который что-то говорит о коде. Иначе None.

    Отменённые и идущие пропускаются: они не приговор. Именно поэтому сторож
    смотрит вглубь, а не судит по самому верхнему прогону.
    """
    for run in runs:
        if run.get("status") != "completed":
            continue
        if run.get("conclusion") in RED | GREEN:
            return run
    return None


def failed_jobs(repo: str, run_id: int) -> list[str]:
    """Названия упавших работ прогона: «красный» превращается в «красный где».

    Необязательная роскошь, поэтому неудача этого запроса тревогу не отменяет —
    сообщение просто выйдет без этой строки.
    """
    try:
        answer = github(f"repos/{repo}/actions/runs/{run_id}/jobs?per_page=50")
    except Failed:
        return []
    return [
        job.get("name", "?")
        for job in answer.get("jobs", [])
        if job.get("conclusion") in RED
    ]


def moment(value: str | None) -> datetime | None:
    """Время GitHub (`2026-09-02T15:02:15Z`) в datetime. Не разобралось — None.

    Нет времени — нет и суждения о свежести: сторож в таком случае молчит про
    тишину, а не выдумывает её. Неверная тревога дороже пропущенной проверки
    свежести, потому что от неверных отучаются читать канал целиком.
    """
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def age(hours: float) -> str:
    """Возраст словами. «нет уже 0 ч» само выглядит как поломка сторожа."""
    if hours >= 24:
        return f"{int(hours // 24)} сут"
    if hours >= 1:
        return f"{int(hours)} ч"
    return f"{max(1, int(hours * 60))} мин"


def silence(runs: list[dict], tip_sha: str | None, tip_time: str | None,
            now: datetime) -> str | None:
    """Почему ветка молчит, или None, если не молчит (D067).

    Тишина — отдельная беда, а не разновидность падения: сигнал о падении молчит
    ровно тогда, когда молчит прогон, и человек не узнаёт ничего. См. шапку.
    """
    decisive = verdict(runs)
    if decisive is None:
        return (
            "ни один из последних прогонов не дал приговора — "
            "все отменены или ещё идут"
        )

    when = moment(decisive.get("created_at"))
    if when is not None:
        hours = (now - when).total_seconds() / 3600
        if hours >= STALE_HOURS:
            return f"свежего результата нет уже {age(hours)}"

    # Верхушка ветки без прогона — это «workflow не запустился». Отдельный
    # случай: приговор есть и он свежий, но относится к прошлому коммиту.
    if tip_sha and not any(run.get("head_sha") == tip_sha for run in runs):
        pushed = moment(tip_time)
        if pushed is not None:
            minutes = (now - pushed).total_seconds() / 60
            if minutes >= NO_RUN_GRACE_MIN:
                return (
                    f"на верхушке ветки ({tip_sha[:7]}) прогона нет вовсе, "
                    f"а коммиту уже {int(minutes)} мин — workflow не запустился"
                )
    return None


def silence_text(branch: str, reason: str, repo: str) -> str:
    return "\n".join([
        f"По {branch} нет результата проверок.",
        reason,
        "Красным это не считается — результата нет вовсе, а значит и поломку "
        "показать некому.",
        f"https://github.com/{repo}/actions",
    ])


def branch_tip(repo: str, branch: str) -> tuple[str | None, str | None]:
    """Верхушка ветки: `sha` и когда закоммичена. Не вышло — (None, None).

    Необязательная часть разбора тишины: без неё сторож всё равно заметит и
    отсутствие приговора, и его возраст, — поэтому неудача запроса не должна
    ронять весь прогон.
    """
    try:
        answer = github(f"repos/{repo}/commits/{branch}")
    except Failed:
        return None, None
    sha = answer.get("sha")
    when = (answer.get("commit") or {}).get("committer", {}).get("date")
    return (sha if isinstance(sha, str) else None,
            when if isinstance(when, str) else None)


def alert_text(run: dict, jobs: list[str]) -> str:
    """Текст тревоги. Обычный текст без разметки — так велит протокол канала."""
    lines = [
        "Прогон на master красный.",
        f"«{run.get('name', 'прогон')}» на коммите {(run.get('head_sha') or '')[:7]}",
        (run.get("display_title") or "").strip()[:80],
    ]
    if jobs:
        lines.append("упало: " + ", ".join(jobs))
    lines.append(run.get("html_url", ""))
    return "\n".join(line for line in lines if line)


def recovery_text(run: dict) -> str:
    """Отбой. Пустые поля выбрасываются так же, как в `alert_text`.

    Без фильтра прогон без `display_title` давал пустую строку посреди
    сообщения — нашёл исполнитель, писавший тесты к этому файлу.
    """
    lines = [
        "master снова зелёный.",
        (run.get("display_title") or "").strip()[:80],
        run.get("html_url", ""),
    ]
    return "\n".join(line for line in lines if line)


# --- отправка ----------------------------------------------------------------


def notify(project: str, kind: str, text: str) -> None:
    """`POST $CHANNEL_URL/notify`. Только 2xx считается отправкой."""
    url, secret = channel()
    body = json.dumps(
        {"project": project, "kind": kind, "text": text}, ensure_ascii=False
    ).encode("utf-8")
    request = urllib.request.Request(
        f"{url}/notify",
        data=body,
        headers={
            "Authorization": f"Bearer {secret}",
            "content-type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as answer:
            if not 200 <= answer.status < 300:
                raise Failed(f"канал ответил {answer.status} — сообщение не ушло")
    except urllib.error.HTTPError as error:
        raise Failed(f"канал ответил {error.code} — сообщение не ушло") from error
    except (urllib.error.URLError, TimeoutError) as error:
        # Самый вероятный случай — машина вне тайлнета. Молчать об этом нельзя:
        # незамеченное отсутствие сигнала ровно та беда, ради которой всё это.
        raise Failed(
            f"канал недоступен ({error}). Сторож обязан ходить с машины, "
            "у которой есть тайлнет"
        ) from error


# --- память ------------------------------------------------------------------


def state_file(repo: str) -> Path:
    return STATE_HOME / (repo.replace("/", "_") + ".json")


def remembered(repo: str) -> dict:
    path = state_file(repo)
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        # Испорченную память чинить нечем; хуже неё только тишина, поэтому
        # считаем, что не докладывали ни о чём, и доложим заново.
        return {}


def remember(repo: str, data: dict) -> None:
    path = state_file(repo)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


# --- сборка ------------------------------------------------------------------


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Сигнал о красном прогоне на master")
    parser.add_argument("--dry-run", action="store_true",
                        help="показать сообщение, но не отправлять и не запоминать")
    parser.add_argument("--run", type=int, default=None,
                        help="разобрать конкретный прогон по его номеру")
    parser.add_argument("--branch", default="master", help="ветка (по умолчанию master)")
    args = parser.parse_args(argv)

    try:
        repo = repository()
        project = repo.split("/")[-1]

        was = remembered(repo)

        if args.run is not None:
            run = github(f"repos/{repo}/actions/runs/{args.run}")
        else:
            answer = github(
                f"repos/{repo}/actions/runs?branch={args.branch}&per_page={DEPTH}"
            )
            runs = answer.get("workflow_runs", [])
            run = verdict(runs)

            # Тишина проверяется раньше приговора: приговор может быть и
            # зелёным, и старым одновременно — тогда новость именно в возрасте.
            quiet = silence(runs, *branch_tip(repo, args.branch), datetime.now(UTC))
            if quiet is not None:
                text = silence_text(args.branch, quiet, repo)
                print(f"--- тишина, проект {project} ---\n{text}\n---")
                if args.dry_run:
                    print("(--dry-run: не отправлено, память не тронута)")
                    return 0
                if was.get("state") == "silent":
                    print(f"{repo}: о тишине уже доложено")
                    return 0
                notify(project, "alert", text)
                remember(repo, {"reported": was.get("reported"), "state": "silent"})
                print(f"{repo}: отправлено в канал")
                return 0

        if run is None:
            print(f"{repo}: приговора нет — все свежие прогоны отменены или идут")
            return 0

        conclusion = run.get("conclusion")
        run_id = run.get("id")

        # Приговор проверяется и здесь, а не только в `verdict()`. Путь `--run`
        # берёт прогон по номеру, минуя отбор, и отменённый прогон доезжал до
        # ветки «зелёное» — сторож бодро сообщал «master снова зелёный» про
        # прогон, который никто не доводил до конца. Найдено прогоном руками.
        if conclusion not in RED | GREEN:
            print(f"{repo}: прогон {run_id} приговора не дал ({conclusion})")
            return 0

        if conclusion in RED:
            if was.get("reported") == run_id:
                print(f"{repo}: прогон {run_id} красный, о нём уже доложено")
                return 0
            text = alert_text(run, failed_jobs(repo, run_id))
            print(f"--- тревога, проект {project} ---\n{text}\n---")
            if args.dry_run:
                print("(--dry-run: не отправлено, память не тронута)")
                return 0
            notify(project, "alert", text)
            remember(repo, {"reported": run_id, "state": "red"})
            print(f"{repo}: отправлено в канал")
            return 0

        # Зелёное. Сообщаем, только если до этого была тревога — красная или про
        # тишину: иначе сторож писал бы «всё хорошо» каждые пятнадцать минут.
        if was.get("state") not in ("red", "silent"):
            print(f"{repo}: прогон {run_id} зелёный, докладывать не о чем")
            return 0
        text = recovery_text(run)
        print(f"--- поправилось, проект {project} ---\n{text}\n---")
        if args.dry_run:
            print("(--dry-run: не отправлено, память не тронута)")
            return 0
        notify(project, "alert", text)
        remember(repo, {"reported": run_id, "state": "green"})
        print(f"{repo}: отправлено в канал")
        return 0

    except Failed as error:
        print(f"СТОРОЖ НЕ СРАБОТАЛ: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
