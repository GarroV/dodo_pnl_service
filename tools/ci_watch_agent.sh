#!/bin/bash
# Повесить сторожа красного прогона на расписание macOS (T200, issue #196).
#
# Сторож `tools/ci_watch.py` сам по себе — команда, которую надо вспомнить
# запустить. Ровно поэтому красноту и не замечали двое суток: механизм, который
# срабатывает по памяти человека, не механизм. Здесь он вешается на launchd и
# спрашивает GitHub раз в четверть часа.
#
#   tools/ci_watch_agent.sh render    показать, что будет поставлено (ничего не трогает)
#   tools/ci_watch_agent.sh install   поставить и запустить
#   tools/ci_watch_agent.sh status    показать состояние и хвост журнала
#   tools/ci_watch_agent.sh remove    снять
#
# Почему launchd, а не cron: у cron на macOS нет доступа к сети из-под TCC без
# бубна, а сторожу нужен тайлнет — иначе до канала он не дозвонится.
set -euo pipefail

LABEL="io.dodobrands.dodo-pnl.ci-watch"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
LOG_DIR="$HOME/.claude/forge/ci-watch"
INTERVAL="${CI_WATCH_INTERVAL:-900}"

# Каталог ОСНОВНОЙ копии репозитория, а не той, откуда позвали. Рабочие копии
# (git worktree) живут неделю и удаляются после слияния; агент, нацеленный на
# такую, однажды молча перестал бы находить файл. `--git-common-dir` у worktree
# указывает на `.git` основной копии — от него и пляшем.
common_dir="$(git rev-parse --git-common-dir)"
REPO="${CI_WATCH_REPO:-$(cd "$(dirname "$common_dir")" && pwd)}"

# Интерпретатор: venv основной копии, если он есть, иначе системный python3.
PYTHON="$REPO/.venv/bin/python3"
[ -x "$PYTHON" ] || PYTHON="$(command -v python3)"

render_plist() {
  cat <<PLISTEOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>$PYTHON</string>
    <string>$REPO/tools/ci_watch.py</string>
  </array>
  <key>WorkingDirectory</key><string>$REPO</string>
  <key>StartInterval</key><integer>$INTERVAL</integer>
  <key>RunAtLoad</key><true/>
  <key>StandardOutPath</key><string>$LOG_DIR/agent.log</string>
  <key>StandardErrorPath</key><string>$LOG_DIR/agent.log</string>
</dict>
</plist>
PLISTEOF
}

case "${1:-}" in
  render)
    # Показать, что будет поставлено, ничего не трогая. Заодно этим проверяется
    # разбор путей: в рабочей копии (worktree) репозиторий обязан выйти
    # основной, а не той, откуда позвали.
    render_plist
    ;;
  install)
    mkdir -p "$LOG_DIR" "$(dirname "$PLIST")"
    render_plist > "$PLIST"
    launchctl bootout "gui/$UID/$LABEL" 2>/dev/null || true
    launchctl bootstrap "gui/$UID" "$PLIST"
    echo "поставлен: $LABEL"
    echo "  репозиторий: $REPO"
    echo "  интерпретатор: $PYTHON"
    echo "  раз в $INTERVAL с, журнал: $LOG_DIR/agent.log"
    ;;
  remove)
    launchctl bootout "gui/$UID/$LABEL" 2>/dev/null || true
    rm -f "$PLIST"
    echo "снят: $LABEL"
    ;;
  status)
    if launchctl print "gui/$UID/$LABEL" >/dev/null 2>&1; then
      echo "сторож стоит:"
      launchctl print "gui/$UID/$LABEL" | grep -E "state|last exit|program =" || true
    else
      echo "сторож НЕ стоит — красный прогон никто не заметит"
    fi
    echo "--- хвост журнала ---"
    tail -n 15 "$LOG_DIR/agent.log" 2>/dev/null || echo "(журнала ещё нет)"
    ;;
  *)
    echo "нужен аргумент: render | install | status | remove" >&2
    exit 2
    ;;
esac
