"""Predictive Energy Management engine tests.

The engine is the only part of `/energy_*` that does real work - the bot
handlers are thin formatting shells - so these tests pin the storage
contract (hour bucketing, CSV shapes, tariff validation), the forecast
windows (an evening-peak household must forecast an evening peak) and the
failure modes the user actually hits (no data, bad values, sparse data).
No network, no Ollama, no Telegram: sqlite in a tmp_path only.
"""
from datetime import datetime, timedelta

import pytest

from energy_manager import EnergyManager


@pytest.fixture
def mgr(tmp_path):
    return EnergyManager(str(tmp_path / "energy.db"))


# ── Empty / sparse data ─────────────────────────────────────────────────
def test_empty_forecast_raises_with_guidance(mgr):
    with pytest.raises(ValueError, match="energy_demo"):
        mgr.forecast()


def test_empty_status_reports_zero(mgr):
    assert mgr.status() == {"n": 0}


def test_sparse_data_is_low_confidence(mgr):
    now = datetime.now().replace(minute=0, second=0, microsecond=0)
    for i in range(3):
        mgr.log_reading(0.5 + i, now - timedelta(hours=i))
    assert mgr.forecast(6)["confidence"] == "low"
    # The model still produces a usable shape from hour-of-day fallback.
    assert all(v >= 0 for _, v in mgr.forecast(6)["points"])


# ── Logging & storage ───────────────────────────────────────────────────
def test_log_rejects_bad_values(mgr):
    with pytest.raises(ValueError, match="must be a number"):
        mgr.log_reading("plenty")
    with pytest.raises(ValueError, match="out of range"):
        mgr.log_reading(-2)
    with pytest.raises(ValueError, match="out of range"):
        mgr.log_reading(99999)


def test_readings_are_bucketed_to_the_hour(mgr):
    key, kwh = mgr.log_reading(1.5, "2026-10-01 10:37:59")
    assert key == "2026-10-01 10:00:00"
    pairs = mgr.readings()
    assert len(pairs) == 1
    assert pairs[0][0].minute == 0 and pairs[0][1] == 1.5


def test_log_same_hour_replaces(mgr):
    mgr.log_reading(1.0, "2026-10-01 10:10")
    mgr.log_reading(2.0, "2026-10-01 10:50")
    assert mgr.readings() == [(datetime(2026, 10, 1, 10, 0), 2.0)]


# ── Demo seed ───────────────────────────────────────────────────────────
def test_seed_creates_full_month_and_is_idempotent(mgr):
    added = mgr.seed_demo(28)
    assert added == 28 * 24
    assert mgr.status()["n"] == 28 * 24
    # Re-seeding must not duplicate or clobber: rows already exist.
    mgr.seed_demo(28)
    assert mgr.status()["n"] == 28 * 24


def test_clear_keeps_tariff(mgr):
    mgr.seed_demo(7)
    mgr.set_tariff(0.5, 0.15, 17, 23)
    assert mgr.clear_readings() == 7 * 24
    assert mgr.status() == {"n": 0}
    assert mgr.get_tariff() is not None


# ── Forecast ────────────────────────────────────────────────────────────
def test_forecast_shape_and_windows_for_evening_peak_home(mgr):
    mgr.seed_demo(28)
    fc = mgr.forecast(24)
    assert len(fc["points"]) == 24
    assert all(v >= 0 for _, v in fc["points"])
    assert fc["total"] == pytest.approx(sum(v for _, v in fc["points"]), rel=1e-6)
    # Demo household peaks in the evening and sleeps in the small hours.
    assert 16 <= fc["peak"]["start"].hour <= 21
    assert 0 <= fc["valley"]["start"].hour <= 6
    assert fc["peak"]["kwh"] > fc["valley"]["kwh"]
    assert fc["confidence"] == "high"
    assert "cost" not in fc  # no tariff configured yet


def test_forecast_horizon_validation(mgr):
    mgr.seed_demo(3)
    with pytest.raises(ValueError, match="1-336"):
        mgr.forecast(0)
    with pytest.raises(ValueError, match="1-336"):
        mgr.forecast(400)
    assert len(mgr.forecast(48)["points"]) == 48
    assert len(mgr.forecast(1)["points"]) == 1


def test_forecast_points_start_at_the_next_hour(mgr):
    mgr.seed_demo(3)
    now = datetime(2026, 10, 6, 14, 37, 12)
    first = mgr.forecast(3, now=now)["points"][0][0]
    assert first == datetime(2026, 10, 6, 15, 0)


# ── CSV import ──────────────────────────────────────────────────────────
def test_import_with_header_skips_bad_rows(mgr, tmp_path):
    p = tmp_path / "meter.csv"
    p.write_text(
        "timestamp,kWh\n"
        "2026-10-01 00:00,0.4\n"
        "2026-10-01 01:00,0.35\n"
        "not-a-timestamp,x\n"
        "2026-10-01 02:00,-1\n",
        encoding="utf-8",
    )
    result = mgr.import_csv(str(p))
    assert result == {"imported": 2, "skipped": 2, "rows": 4}
    assert len(mgr.readings()) == 2


def test_import_without_header_processes_first_row(mgr, tmp_path):
    p = tmp_path / "raw.csv"
    p.write_text("2026-10-02 00:00,0.5\n2026-10-02 01:00,0.4\n", encoding="utf-8")
    result = mgr.import_csv(str(p))
    assert result == {"imported": 2, "skipped": 0, "rows": 2}


def test_import_missing_file_raises(mgr, tmp_path):
    with pytest.raises(ValueError, match="File not found"):
        mgr.import_csv(str(tmp_path / "nope.csv"))


# ── Tariff ──────────────────────────────────────────────────────────────
def test_tariff_roundtrip_and_validation(mgr):
    assert mgr.get_tariff() is None
    mgr.set_tariff(0.5, 0.15, 17, 23)
    t = mgr.get_tariff()
    assert t == {"peak_rate": 0.5, "offpeak_rate": 0.15,
                 "peak_start": 17, "peak_end": 23}
    with pytest.raises(ValueError, match="cannot be empty"):
        mgr.set_tariff(0.5, 0.15, 8, 8)
    with pytest.raises(ValueError, match="0-23"):
        mgr.set_tariff(0.5, 0.15, 8, 25)
    with pytest.raises(ValueError, match=">= 0"):
        mgr.set_tariff(-1, 0.15, 8, 10)


def test_peak_window_wraps_midnight(mgr):
    mgr.set_tariff(0.6, 0.1, 22, 6)
    assert mgr._is_peak(mgr.get_tariff(), 23) is True
    assert mgr._is_peak(mgr.get_tariff(), 2) is True
    assert mgr._is_peak(mgr.get_tariff(), 6) is False
    assert mgr._is_peak(mgr.get_tariff(), 12) is False


def test_tariff_adds_cost_to_forecast(mgr):
    mgr.seed_demo(10)
    without = mgr.forecast(24)
    mgr.set_tariff(0.5, 0.15, 17, 23)
    with_cost = mgr.forecast(24)
    assert "cost" not in without
    assert with_cost["cost"] > 0
    # Cost must sit between the two rates times consumption.
    assert 0.15 * with_cost["total"] <= with_cost["cost"] <= 0.5 * with_cost["total"]


# ── Anomalies & report ──────────────────────────────────────────────────
def test_spike_is_flagged_ignored_otherwise(mgr):
    mgr.seed_demo(14)
    assert mgr.anomalies() == []
    now = datetime.now().replace(minute=0, second=0, microsecond=0)
    mgr.log_reading(10.0, now - timedelta(hours=1))          # 10x a normal hour
    flagged = mgr.anomalies()
    assert flagged, "a 10x spike must be flagged"
    assert flagged[-1]["ratio"] >= 1.8
    assert flagged[-1]["kwh"] == 10.0


def test_report_bundle(mgr):
    mgr.seed_demo(21)
    mgr.set_tariff(0.5, 0.15, 17, 23)
    r = mgr.report(24)
    assert r["confidence"] == "high"
    assert r["coverage_pct"] == 100
    assert r["trend_week_pct"] is not None
    assert r["advice"], "at least the load-shift advice must be present"
    assert any("saves" in a for a in r["advice"])  # tariff-aware advice
    assert len(r["forecast"]["points"]) == 24


def test_trend_needs_a_week_of_data(mgr):
    mgr.seed_demo(5)
    assert mgr.report(24)["trend_week_pct"] is None
