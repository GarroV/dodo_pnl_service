#!/usr/bin/env python3
"""
Паспорт стенда: что из работающего — НАШЕ, какой оно версии и что видно снаружи.

Зачем этот скрипт существует. Площадка MUSPELHEIM общая: на ней рядом живут
чужие проекты, и один из них занимает корень публичного адреса. 22.08.2026 из-за
этого случились две ошибки подряд, обе — утверждения без проверки:

  1. «Демо уже выглядит как продукт» — на стенде стоял образ пятидневной
     давности, куда дизайн-система не доехала вовсе;
  2. «Публичный адрес уже включён» — включён он был для ЧУЖОГО проекта, и
     владелец, открыв ссылку, увидел не наш продукт.

Оба факта проверяются одной командой за десять секунд. Поэтому: прежде чем
сказать что-либо о стенде — прогнать этот скрипт и говорить по его выводу.

    python tools/stand.py                # площадка MUSPELHEIM
    python tools/stand.py --host local   # своя машина

Скрипт только читает: ни одного docker-действия, ни одной записи.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REMOTE_PATH = r"C:\projects\dodo_pnl_service"

# Имя compose-проекта = префикс имён наших контейнеров. Всё, что не начинается
# с него, принадлежит чужому проекту и не наше дело — ни смотреть, ни трогать.
DEFAULT_PREFIX = "dodo-pnl"

OK, WARN, BAD, DIM = "\033[32m", "\033[33m", "\033[31m", "\033[90m"
BOLD, OFF = "\033[1m", "\033[0m"


def run(cmd: list[str], timeout: int = 40) -> str:
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return (out.stdout or "") + (out.stderr or "")
    except subprocess.TimeoutExpired:
        return "__TIMEOUT__"


def remote(ps: str, timeout: int = 40) -> str:
    """Выполнить PowerShell на площадке. Дефолтный шелл там cmd — отсюда обёртка."""
    return run(["ssh", "-o", "ConnectTimeout=10", "muspelheim",
                f'powershell -NoProfile -Command "{ps}"'], timeout)


def local(sh: str, timeout: int = 40) -> str:
    return run(["bash", "-lc", sh], timeout)


def head(text: str) -> None:
    print(f"\n{BOLD}{text}{OFF}")


def containers(call, prefix: str) -> tuple[list[str], int]:
    """Наши контейнеры отдельно, чужие — только числом."""
    raw = call("docker ps --format '{{.Names}}|{{.Status}}|{{.Ports}}'")
    ours, alien = [], 0
    for line in raw.splitlines():
        line = line.strip()
        if "|" not in line:
            continue
        name = line.split("|", 1)[0]
        (ours.append(line) if name.startswith(prefix) else None)
        alien += 0 if name.startswith(prefix) else 1
    return ours, alien


def version(call, is_remote: bool) -> dict:
    """Версия кода на стенде против origin/master."""
    cd = f"cd {REMOTE_PATH};" if is_remote else f"cd {ROOT} &&"
    here = call(f"{cd} git log -1 --format='%h|%ad|%s' --date=short").strip().splitlines()
    behind = call(f"{cd} git fetch origin -q; {cd} git rev-list --count HEAD..origin/master").strip().splitlines()
    line = next((l for l in here if "|" in l), "?|?|?")
    n = next((l for l in reversed(behind) if l.strip().isdigit()), "?")
    return {"commit": line, "behind": n}


def design_system(call, prefix: str, service: str = "app") -> str:
    """Доехала ли дизайн-система в работающий образ — считаем токены внутри."""
    # Через grep, а не python -c: вложенные кавычки не переживают путь
    # bash → ssh → PowerShell → docker exec и молча ломаются (проверено).
    out = call(f"docker exec {prefix}-{service}-1 grep -c -E '^[[:space:]]*--' "
               f"/app/src/web/static/web/tokens.css")
    m = re.search(r"^\s*(\d+)\s*$", out, re.M)
    return m.group(1) if m else "нет файла"


def funnel(prefix: str) -> list[tuple[str, str]]:
    """Что опубликовано наружу и чьё это. Только для площадки."""
    raw = remote("tailscale funnel status")
    rows: list[tuple[str, str]] = []
    addr = ""
    for line in raw.splitlines():
        line = line.strip()
        if line.startswith("https://"):
            addr = line.split()[0]
        m = re.search(r"proxy\s+(http://\S+)", line)
        if m:
            rows.append((addr or "?", m.group(1)))
    return rows


def main() -> int:
    ap = argparse.ArgumentParser(description="Паспорт стенда: наше, версия, публичность")
    ap.add_argument("--host", choices=["muspelheim", "local"], default="muspelheim")
    ap.add_argument("--prefix", default=DEFAULT_PREFIX, help="имя compose-проекта (префикс контейнеров)")
    args = ap.parse_args()

    is_remote = args.host == "muspelheim"
    call = remote if is_remote else local
    prefix = args.prefix

    print(f"{BOLD}Стенд {args.host}{OFF} · наше = контейнеры с префиксом {prefix!r}")

    probe = call("docker ps --format '{{.Names}}'")
    if probe == "__TIMEOUT__" or "docker" in probe and "not recognized" in probe:
        print(f"{BAD}Площадка недоступна или docker не отвечает.{OFF}")
        return 2

    head("Наши контейнеры")
    ours, alien = containers(call, prefix)
    if not ours:
        print(f"  {BAD}ни одного — стенд не поднят{OFF}")
    for line in ours:
        name, status, ports = (line.split("|") + ["", ""])[:3]
        mark = OK if "Up" in status else BAD
        print(f"  {mark}●{OFF} {name:26} {status:22} {DIM}{ports}{OFF}")
    print(f"  {DIM}рядом чужих контейнеров: {alien} — не наши, не трогать{OFF}")

    head("Версия кода на стенде")
    v = version(call, is_remote)
    c, d, subj = (v["commit"].split("|") + ["", ""])[:3]
    behind = v["behind"]
    fresh = behind == "0"
    print(f"  {c} от {d} — {subj[:70]}")
    print(f"  {(OK + 'совпадает с origin/master' if fresh else WARN + f'ОТСТАЁТ от origin/master на {behind} коммитов')}{OFF}")

    head("Дизайн-система в работающих образах")
    ref = sum(1 for l in (ROOT / "src/web/static/web/tokens.css").read_text().splitlines()
              if l.strip().startswith("--"))
    # Демо живёт под отдельным профилем compose и на обычном `up -d` остаётся
    # на прежнем образе — молча. Именно так стенд и разъехался 22.08.
    for service, title in (("app", "приложение"), ("demo", "демо")):
        tokens = design_system(call, prefix, service)
        if tokens.isdigit():
            same = int(tokens) == ref
            print(f"  {title:12} токенов {tokens} против {ref} в репозитории "
                  f"{OK + '— совпадает' if same else WARN + '— РАСХОДЯТСЯ'}{OFF}")
        else:
            print(f"  {title:12} {BAD}tokens.css нет — образ старее дизайн-системы{OFF}")

    if is_remote:
        head("Что видно из интернета")
        rows = funnel(prefix)
        if not rows:
            print(f"  {DIM}Funnel ничего не публикует{OFF}")
        for addr, target in rows:
            port = re.search(r":(\d+)", target)
            p = port.group(1) if port else ""
            ours_ports = {"8010", "8030"}          # демо и приложение P&L
            mine = p in ours_ports
            tag = f"{OK}НАШЕ{OFF}" if mine else f"{BAD}ЧУЖОЙ ПРОЕКТ{OFF}"
            print(f"  {addr}  →  {target}   {tag}")
        if not any(re.search(r":(8010|8030)", t) for _, t in rows):
            print(f"  {WARN}наш продукт наружу НЕ опубликован — по публичному адресу отвечает не он{OFF}")

    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
