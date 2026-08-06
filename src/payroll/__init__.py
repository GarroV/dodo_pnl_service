"""Зарплатный движок: правила в конфигурации, не в коде."""

from .engine import Component, Employee, PayrollEngine, Payslip, Timesheet, d
from .presets import list_presets, load_preset

__all__ = [
    "Component",
    "Employee",
    "PayrollEngine",
    "Payslip",
    "Timesheet",
    "d",
    "load_preset",
    "list_presets",
]
