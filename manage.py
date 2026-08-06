#!/usr/bin/env python
"""Точка входа Django. Всё, что делается с проектом руками, идёт через неё."""
import os
import sys
from pathlib import Path

# Код лежит в src/, а manage.py — в корне: так пакеты не смешиваются
# с настройками репозитория, а движок payroll остаётся обычным пакетом.
sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))


def main() -> None:
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
    from django.core.management import execute_from_command_line

    execute_from_command_line(sys.argv)


if __name__ == "__main__":
    main()
