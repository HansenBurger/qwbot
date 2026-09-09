"""Dispatch date rules: template override > global override > date rule."""

from __future__ import annotations

from datetime import date
from typing import Any

from qwbot.cli import should_dispatch

WEDNESDAY = date(2026, 9, 9)
SATURDAY = date(2026, 9, 12)
HOLIDAY = date(2026, 10, 1)  # Thursday flagged as a holiday in the batch plan

BATCH_PLAN = [
    {"natural_date": "2026-10-01", "holiday_flag": "Y", "description": "国庆节"},
    {"natural_date": "2026-09-09", "holiday_flag": "N", "description": "跑批一"},
]


def entry(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "template_date_rule": "business_day",
        "schedule_date_rule": "",
        "skip_dates": [],
        "force_dates": [],
        "global_skip_dates": [],
        "global_force_dates": [],
    }
    base.update(overrides)
    return base


def test_business_day_rule() -> None:
    assert should_dispatch(entry(), BATCH_PLAN, WEDNESDAY) is True
    assert should_dispatch(entry(), BATCH_PLAN, SATURDAY) is False
    assert should_dispatch(entry(), BATCH_PLAN, HOLIDAY) is False


def test_all_rule_sends_every_day() -> None:
    rule = entry(template_date_rule="all")
    assert should_dispatch(rule, BATCH_PLAN, WEDNESDAY) is True
    assert should_dispatch(rule, BATCH_PLAN, SATURDAY) is True
    assert should_dispatch(rule, BATCH_PLAN, HOLIDAY) is True


def test_holiday_only_rule_keeps_weekends() -> None:
    rule = entry(template_date_rule="holiday_only")
    assert should_dispatch(rule, BATCH_PLAN, WEDNESDAY) is True
    assert should_dispatch(rule, BATCH_PLAN, SATURDAY) is True
    assert should_dispatch(rule, BATCH_PLAN, HOLIDAY) is False


def test_schedule_rule_overrides_template_rule() -> None:
    # Same template, two times: weekday morning and weekend morning.
    weekday = entry(template_date_rule="business_day", schedule_date_rule="business_day")
    weekend = entry(template_date_rule="business_day", schedule_date_rule="all")
    assert should_dispatch(weekday, BATCH_PLAN, SATURDAY) is False
    assert should_dispatch(weekend, BATCH_PLAN, SATURDAY) is True


def test_template_override_beats_everything() -> None:
    forced = entry(template_date_rule="business_day", force_dates=["2026-09-12"])
    assert should_dispatch(forced, BATCH_PLAN, SATURDAY) is True

    skipped = entry(template_date_rule="all", skip_dates=["2026-09-09"])
    assert should_dispatch(skipped, BATCH_PLAN, WEDNESDAY) is False

    # A template-level skip still wins over a global force date.
    both = entry(
        template_date_rule="all",
        skip_dates=["2026-09-09"],
        global_force_dates=["2026-09-09"],
    )
    assert should_dispatch(both, BATCH_PLAN, WEDNESDAY) is False


def test_global_overrides_apply_to_rules_other_than_all() -> None:
    skipped = entry(global_skip_dates=["2026-09-09"])
    assert should_dispatch(skipped, BATCH_PLAN, WEDNESDAY) is False

    forced = entry(global_force_dates=["2026-09-12"])
    assert should_dispatch(forced, BATCH_PLAN, SATURDAY) is True

    # `all` returns before the global overrides are consulted.
    everything = entry(template_date_rule="all", global_skip_dates=["2026-09-09"])
    assert should_dispatch(everything, BATCH_PLAN, WEDNESDAY) is True


def test_unknown_rule_falls_back_to_business_day() -> None:
    assert should_dispatch(entry(template_date_rule=""), BATCH_PLAN, WEDNESDAY) is True
    assert should_dispatch(entry(template_date_rule=""), BATCH_PLAN, SATURDAY) is False
