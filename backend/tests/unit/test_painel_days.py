"""Dias do painel do admin no horário de Boa Vista (UTC-4), calculados em Python (zoneinfo)."""

from datetime import UTC, date, datetime, timedelta

from app.repositories.painel import DAYS, day_windows


def test_seven_days_ending_today_in_boa_vista() -> None:
    # 03:30 UTC do dia 25 = 23:30 do dia 24 em Boa Vista: "hoje" ainda é 24.
    windows = day_windows(datetime(2026, 10, 25, 3, 30, tzinfo=UTC))

    assert windows["dias"] == [date(2026, 10, 18) + timedelta(days=n) for n in range(DAYS)]
    assert windows["inicios"][0] == datetime(2026, 10, 18, 4, 0, tzinfo=UTC)
    assert windows["fins"][-1] == datetime(2026, 10, 25, 4, 0, tzinfo=UTC)


def test_windows_are_contiguous_days_of_24_hours() -> None:
    windows = day_windows(datetime(2026, 10, 14, 12, 0, tzinfo=UTC))

    for start, end in zip(windows["inicios"], windows["fins"], strict=True):
        assert end - start == timedelta(hours=24)
    assert windows["fins"][:-1] == windows["inicios"][1:]
