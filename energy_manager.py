"""Predictive Energy Management for RAVEN.

Ingests hourly energy readings (manual logs, CSV exports or a synthetic
demo dataset), forecasts future consumption and turns the forecast into
operating advice: predicted peak/valley windows, load-shift suggestions,
anomaly flags and tariff-aware cost projections.

Everything here is pure standard library - sqlite3 for storage, math for
the model - so the module adds no dependencies to the bot, keeps its data
in its own WAL-mode database and answers in milliseconds, unlike the
model-backed commands that need minutes on this CPU-only host.

The forecast is a three-component model, deliberately simple enough to
reason about and to work with as little as two days of data:

1. Seasonal profile - mean consumption for each (weekday, hour) slot,
   degrading gracefully to hour-of-day means, then to the global mean
   when a slot has never been observed.
2. Level correction - an exponentially weighted moving average of the
   observed/predicted ratio, so the forecast tracks a meter that drifted
   or a household that changed habits.
3. Daily trend - least-squares slope over daily totals (used only with
   a week or more of data) to extrapolate growth or savings.

``forecast()`` composes them into per-hour points plus the peak/valley
windows; ``report()`` wraps them in plain-language advice. All datetimes
are naive local time, matching the rest of the bot.
"""
import csv
import itertools
import math
import os
import random
import sqlite3
import statistics
from collections import defaultdict
from contextlib import closing
from datetime import datetime, timedelta

TS_FMT = "%Y-%m-%d %H:%M:%S"
DEFAULT_DB = os.getenv("ENERGY_DB", "energy.db")

# Slot count of one full week of hourly slots.
_WEEK_SLOTS = 7 * 24
# EWMA smoothing for the level correction: higher = reacts faster to a
# change in habits, lower = trusts the seasonal shape more.
_LEVEL_ALPHA = 0.25
# A reading this far above the model (ratio >= 1.8) counts as a spike.
_ANOMALY_RATIO = 1.8


class EnergyManager:
    """Storage + forecasting engine. One instance per database file."""

    def __init__(self, db_path=DEFAULT_DB):
        self.db_path = str(db_path)
        self._init_db()

    # ── Storage ─────────────────────────────────────────────────────────
    def _connect(self):
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    def _init_db(self):
        with closing(self._connect()) as conn:
            conn.execute(
                """CREATE TABLE IF NOT EXISTS readings (
                       ts     TEXT PRIMARY KEY,
                       kwh    REAL NOT NULL CHECK (kwh >= 0),
                       source TEXT NOT NULL DEFAULT 'manual'
                   )"""
            )
            conn.execute(
                """CREATE TABLE IF NOT EXISTS tariff (
                       id          INTEGER PRIMARY KEY CHECK (id = 1),
                       peak_rate   REAL NOT NULL,
                       offpeak_rate REAL NOT NULL,
                       peak_start  INTEGER NOT NULL,
                       peak_end    INTEGER NOT NULL
                   )"""
            )
            conn.commit()

    @staticmethod
    def _parse_ts(value):
        """Accept a datetime or the common string shapes of a timestamp."""
        if isinstance(value, datetime):
            return value
        text = str(value).strip().replace("T", " ")
        try:
            return datetime.fromisoformat(text)
        except ValueError:
            pass
        for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d",
                    "%d/%m/%Y %H:%M", "%d/%m/%Y %H:%M:%S", "%d/%m/%Y"):
            try:
                return datetime.strptime(text, fmt)
            except ValueError:
                continue
        raise ValueError(f"Unrecognised timestamp: {value!r}")

    @staticmethod
    def _floor_hour(dt):
        return dt.replace(minute=0, second=0, microsecond=0)

    def log_reading(self, kwh, ts=None, source="manual"):
        """Store one reading. Returns (ts, kwh); raises ValueError when bad."""
        try:
            kwh = float(kwh)
        except (TypeError, ValueError):
            raise ValueError(f"kWh must be a number, got {kwh!r}")
        if not math.isfinite(kwh) or kwh < 0 or kwh > 10000:
            raise ValueError(f"kWh out of range (0-10000): {kwh}")
        dt = self._parse_ts(ts) if ts is not None else datetime.now()
        key = self._floor_hour(dt).strftime(TS_FMT)
        with closing(self._connect()) as conn:
            conn.execute(
                "INSERT OR REPLACE INTO readings (ts, kwh, source) VALUES (?, ?, ?)",
                (key, kwh, source),
            )
            conn.commit()
        return key, kwh

    def seed_demo(self, days=28, seed=42):
        """Insert a synthetic month of hourly data (existing readings win).

        The shape mirrors a household: quiet night valley, small morning
        bump, dominant 17:00-22:00 evening peak, flatter later-rising
        weekends, ~1%/week upward drift and +/-8% noise. Deterministic
        for a given seed so tests can rely on it.
        """
        days = max(1, int(days))
        rng = random.Random(seed)
        weekday_shape = [
            0.30, 0.26, 0.24, 0.24, 0.28, 0.40, 0.62, 0.70, 0.55, 0.48,
            0.46, 0.48, 0.52, 0.50, 0.52, 0.58, 0.75, 1.05, 1.35, 1.45,
            1.30, 1.05, 0.72, 0.45,
        ]
        weekend_shape = [
            0.34, 0.30, 0.27, 0.26, 0.28, 0.34, 0.48, 0.66, 0.74, 0.70,
            0.62, 0.58, 0.60, 0.58, 0.60, 0.66, 0.82, 1.00, 1.20, 1.30,
            1.22, 1.02, 0.76, 0.50,
        ]
        end = self._floor_hour(datetime.now())
        start = end - timedelta(hours=days * 24 - 1)
        rows = []
        total = days * 24
        for i in range(total):
            dt = start + timedelta(hours=i)
            weekend = dt.weekday() >= 5
            shape = weekend_shape if weekend else weekday_shape
            trend = 1.0 + 0.01 * (i / (7 * 24))       # ~1% growth per week
            day_factor = 1.08 if weekend else 1.0
            noise = 1.0 + rng.uniform(-0.08, 0.08)
            kwh = round(shape[dt.hour] * day_factor * trend * noise, 3)
            rows.append((dt.strftime(TS_FMT), max(kwh, 0.0), "demo"))
        with closing(self._connect()) as conn:
            before = conn.total_changes
            conn.executemany(
                "INSERT OR IGNORE INTO readings (ts, kwh, source) VALUES (?, ?, ?)",
                rows,
            )
            conn.commit()
            added = conn.total_changes - before
        return added

    def import_csv(self, path):
        """Load a CSV of `timestamp,kwh`. Returns {imported, skipped, rows}.

        A header row is detected (any of timestamp/date/time/datetime plus
        any of kwh/value/consumption/energy); without one the first two
        columns are assumed. Rows that do not parse are skipped, not fatal.
        """
        path = os.path.expandvars(os.path.expanduser(str(path).strip("\"'")))
        if not os.path.isfile(path):
            raise ValueError(f"File not found: {path}")
        ts_col, kwh_col = 0, 1
        imported = skipped = rows = 0
        with open(path, newline="", encoding="utf-8-sig") as fh:
            reader = csv.reader(fh)
            first = next(reader, None)
            if first is None:
                return {"imported": 0, "skipped": 0, "rows": 0}
            cells = [c.strip().lower() for c in first]
            ts_names = ("timestamp", "date", "time", "datetime")
            kwh_names = ("kwh", "value", "consumption", "energy", "load")
            if any(c in ts_names for c in cells) and any(c in kwh_names for c in cells):
                ts_col = next(i for i, c in enumerate(cells) if c in ts_names)
                kwh_col = next(i for i, c in enumerate(cells) if c in kwh_names)
                data = reader                      # first row was the header
            else:
                data = itertools.chain([first], reader)  # no header: all data
            with closing(self._connect()) as conn:
                for row in data:
                    rows += 1
                    try:
                        if len(row) <= max(ts_col, kwh_col):
                            raise ValueError("short row")
                        ts = self._parse_ts(row[ts_col]).replace(
                            minute=0, second=0, microsecond=0
                        )
                        kwh = float(row[kwh_col])
                        if not math.isfinite(kwh) or kwh < 0:
                            raise ValueError("bad value")
                        conn.execute(
                            "INSERT OR REPLACE INTO readings (ts, kwh, source) "
                            "VALUES (?, ?, 'csv')",
                            (ts.strftime(TS_FMT), kwh),
                        )
                        imported += 1
                    except ValueError:
                        skipped += 1
                conn.commit()
        return {"imported": imported, "skipped": skipped, "rows": rows}

    def clear_readings(self):
        """Delete every reading (tariff config is kept). Returns count."""
        with closing(self._connect()) as conn:
            count = conn.execute("SELECT COUNT(*) FROM readings").fetchone()[0]
            conn.execute("DELETE FROM readings")
            conn.commit()
        return count

    def readings(self, since=None):
        """All readings as (datetime, kwh), oldest first, optionally a
        `since` datetime or an int number of hours back from now."""
        if isinstance(since, int):
            since = datetime.now() - timedelta(hours=since)
        sql = "SELECT ts, kwh FROM readings"
        params = ()
        if since is not None:
            sql += " WHERE ts >= ?"
            params = (since.strftime(TS_FMT),)
        sql += " ORDER BY ts"
        with closing(self._connect()) as conn:
            rows = conn.execute(sql, params).fetchall()
        return [(datetime.strptime(r[0], TS_FMT), r[1]) for r in rows]

    # ── Tariff ──────────────────────────────────────────────────────────
    def set_tariff(self, peak_rate, offpeak_rate, peak_start, peak_end):
        """Configure cost rates (per kWh) and the peak window (hours).

        `peak_end` is exclusive; a window that wraps midnight is allowed
        (e.g. peak_start=22, peak_end=6).
        """
        peak_rate, offpeak_rate = float(peak_rate), float(offpeak_rate)
        peak_start, peak_end = int(peak_start), int(peak_end)
        if peak_rate < 0 or offpeak_rate < 0:
            raise ValueError("Rates must be >= 0")
        if not (0 <= peak_start <= 23 and 0 <= peak_end <= 23):
            raise ValueError("Peak hours must be 0-23")
        if peak_start == peak_end:
            raise ValueError("Peak window cannot be empty")
        with closing(self._connect()) as conn:
            conn.execute(
                "INSERT OR REPLACE INTO tariff (id, peak_rate, offpeak_rate, "
                "peak_start, peak_end) VALUES (1, ?, ?, ?, ?)",
                (peak_rate, offpeak_rate, peak_start, peak_end),
            )
            conn.commit()
        return self.get_tariff()

    def get_tariff(self):
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT peak_rate, offpeak_rate, peak_start, peak_end "
                "FROM tariff WHERE id = 1"
            ).fetchone()
        if not row:
            return None
        return {
            "peak_rate": row[0],
            "offpeak_rate": row[1],
            "peak_start": row[2],
            "peak_end": row[3],
        }

    @staticmethod
    def _is_peak(tariff, hour):
        start, end = tariff["peak_start"], tariff["peak_end"]
        if start < end:
            return start <= hour < end
        return hour >= start or hour < end

    def _rate(self, tariff, hour):
        return tariff["peak_rate"] if self._is_peak(tariff, hour) else tariff["offpeak_rate"]

    # ── Model ───────────────────────────────────────────────────────────
    @staticmethod
    def _mean(values):
        return sum(values) / len(values) if values else 0.0

    @staticmethod
    def _median(values):
        """Median is used for the seasonal shape: one logged spike must
        not drag the whole slot mean out of shape (with two observations
        a 10x spike would double the mean; the median also averages the
        middle pair, but anomalies() does not trust the profile for the
        point being tested - see the leave-one-out expectation there)."""
        return statistics.median(values) if values else 0.0

    @staticmethod
    def _daily_slope(day_totals):
        """Least-squares slope (kWh/day) over daily totals, or None."""
        n = len(day_totals)
        if n < 7:
            return None
        mean_x = (n - 1) / 2.0
        mean_y = sum(day_totals) / n
        sxx = sum((x - mean_x) ** 2 for x in range(n))
        if not sxx:
            return None
        sxy = sum((x - mean_x) * (y - mean_y) for x, y in zip(range(n), day_totals))
        return sxy / sxx

    def _build_model(self, pairs):
        """Fit profile + level + trend to (datetime, kwh) readings."""
        if not pairs:
            raise ValueError(
                "No readings yet - start with /energy_demo, "
                "/energy_log <kWh> or /energy_import <file.csv>."
            )
        slot_vals = defaultdict(list)
        hour_vals = defaultdict(list)
        for dt, v in pairs:
            slot_vals[(dt.weekday(), dt.hour)].append(v)
            hour_vals[dt.hour].append(v)
        global_mean = self._median([v for _, v in pairs]) or 0.1
        profile = {}
        for wd in range(7):
            for h in range(24):
                vals = slot_vals.get((wd, h)) or hour_vals.get(h)
                profile[(wd, h)] = self._median(vals) if vals else global_mean

        # Level: winsorized EWMA of observed / profile in chronological
        # order - individual ratios are capped at 3x so one extreme hour
        # cannot quietly double the level and mask itself.
        level = 1.0
        for dt, v in pairs:
            expected = max(profile[(dt.weekday(), dt.hour)], 1e-6)
            ratio = min(max(v / expected, 0.1), 3.0)
            level += _LEVEL_ALPHA * (ratio - level)

        # Trend on daily totals (needs a full week to be meaningful).
        day_totals_map = defaultdict(float)
        for dt, v in pairs:
            day_totals_map[dt.date()] += v
        days = sorted(day_totals_map)
        day_totals = [day_totals_map[d] for d in days]
        slope = self._daily_slope(day_totals)
        mean_daily = self._mean(day_totals) if day_totals else 1.0

        # Coverage of the most recent week of slots -> confidence input.
        if days:
            span_days = (pairs[-1][0] - pairs[0][0]).total_seconds() / 86400.0
            cutoff = pairs[-1][0] - timedelta(days=7)
            recent_slots = {
                (dt.weekday(), dt.hour) for dt, _ in pairs if dt >= cutoff
            }
            coverage = len(recent_slots) / _WEEK_SLOTS
        else:  # pragma: no cover - unreachable, pairs is non-empty
            span_days, coverage = 0.0, 0.0
        if span_days >= 7 and coverage >= 0.6:
            confidence = "high"
        elif span_days >= 3 or coverage >= 0.3:
            confidence = "medium"
        else:
            confidence = "low"
        return {
            "profile": profile,
            "level": level,
            "slope": slope,
            "mean_daily": mean_daily,
            "span_days": span_days,
            "coverage": coverage,
            "confidence": confidence,
        }

    @staticmethod
    def _trend_factor(model, hours_ahead):
        slope, mean_daily = model["slope"], model["mean_daily"]
        if not slope or not mean_daily or mean_daily <= 0:
            return 1.0
        days = hours_ahead / 24.0
        return min(2.0, max(0.5, 1.0 + slope * days / mean_daily))

    # ── Forecasting ─────────────────────────────────────────────────────
    def forecast(self, hours=24, now=None):
        """Predict the next `hours` hours.

        Returns points [(datetime, kwh)], total, peak/valley windows,
        confidence and - when a tariff is set - the projected cost.
        """
        hours = int(hours)
        if hours < 1 or hours > 24 * 14:
            raise ValueError("Forecast horizon must be 1-336 hours")
        model = self._build_model(self.readings())
        start = self._floor_hour(now or datetime.now()) + timedelta(hours=1)
        points = []
        for k in range(hours):
            dt = start + timedelta(hours=k)
            base = model["profile"][(dt.weekday(), dt.hour)]
            value = max(0.0, base * model["level"] * self._trend_factor(model, k))
            points.append((dt, round(value, 3)))

        # Peak/valley: contiguous window, 2h when the horizon allows it.
        w = 2 if hours >= 4 else 1
        best_i, best_v = 0, -1.0
        worst_i, worst_v = 0, float("inf")
        for i in range(len(points) - w + 1):
            window = sum(v for _, v in points[i:i + w])
            if window > best_v:
                best_i, best_v = i, window
            if window < worst_v:
                worst_i, worst_v = i, window
        peak = {"start": points[best_i][0], "end": points[best_i + w - 1][0]
                + timedelta(hours=1), "kwh": round(best_v, 3)}
        valley = {"start": points[worst_i][0], "end": points[worst_i + w - 1][0]
                  + timedelta(hours=1), "kwh": round(worst_v, 3)}

        result = {
            "points": points,
            "total": round(sum(v for _, v in points), 3),
            "peak": peak,
            "valley": valley,
            "confidence": model["confidence"],
            "span_days": round(model["span_days"], 1),
        }
        tariff = self.get_tariff()
        if tariff:
            cost = sum(self._rate(tariff, dt.hour) * v for dt, v in points)
            result["cost"] = round(cost, 4)
            result["tariff"] = tariff
        return result

    def anomalies(self, hours=168, limit=5):
        """Recent spikes: readings >= 1.8x what the model expects.

        The expectation for each point is leave-one-out: the seasonal
        mean is computed from the *other* observations of that slot, so a
        spike cannot hide by inflating its own baseline. The level is a
        winsorized EWMA (ratios capped at 3x) for the same reason.
        """
        window = self.readings(since=hours) if hours else self.readings()
        if len(window) < 12:
            return []
        pairs = self.readings()
        slot_vals = defaultdict(list)
        hour_vals = defaultdict(list)
        for dt, v in pairs:
            slot_vals[(dt.weekday(), dt.hour)].append(v)
            hour_vals[dt.hour].append(v)
        slot_sum = {k: sum(v) for k, v in slot_vals.items()}
        hour_sum = {k: sum(v) for k, v in hour_vals.items()}
        global_sum = sum(v for _, v in pairs)
        global_n = len(pairs)

        # Winsorized level (see _build_model) so the spike cannot inflate
        # the expectation it is judged against.
        profile = {}
        for wd in range(7):
            for h in range(24):
                vals = slot_vals.get((wd, h)) or hour_vals.get(h)
                profile[(wd, h)] = self._median(vals) if vals else (
                    self._median([v for _, v in pairs]) or 0.1
                )
        level = 1.0
        for dt, v in pairs:
            expected = max(profile[(dt.weekday(), dt.hour)], 1e-6)
            ratio = min(max(v / expected, 0.1), 3.0)
            level += _LEVEL_ALPHA * (ratio - level)

        flagged = []
        for dt, v in window:
            key = (dt.weekday(), dt.hour)
            others = len(slot_vals.get(key, [])) - 1
            expected = slot_sum.get(key, 0.0) - v
            if others < 1 or expected <= 0:
                others = len(hour_vals.get(dt.hour, [])) - 1
                expected = hour_sum.get(dt.hour, 0.0) - v
                if others < 1 or expected <= 0:
                    others, expected = global_n - 1, global_sum - v
            expected = expected / max(others, 1) * level
            if expected < 0.05:
                continue  # near-zero baseline: nothing meaningful to spike from
            ratio = v / expected
            if ratio >= _ANOMALY_RATIO:
                flagged.append({
                    "ts": dt, "kwh": v, "expected": round(expected, 3),
                    "ratio": round(ratio, 2),
                })
        return flagged[-limit:]

    # ── Reporting ───────────────────────────────────────────────────────
    def status(self):
        """Summary numbers for /energy_status; {'n': 0} when empty."""
        pairs = self.readings()
        if not pairs:
            return {"n": 0}
        now = datetime.now()
        last7 = [(dt, v) for dt, v in pairs if dt >= now - timedelta(days=7)]
        prev7 = [
            (dt, v) for dt, v in pairs
            if now - timedelta(days=14) <= dt < now - timedelta(days=7)
        ]
        last_total = sum(v for _, v in last7)
        prev_total = sum(v for _, v in prev7)
        wow = None
        if prev_total > 0:
            wow = 100.0 * (last_total - prev_total) / prev_total
        hour_means = defaultdict(list)
        for dt, v in last7 or pairs:
            hour_means[dt.hour].append(v)
        peak_hour = max(hour_means, key=lambda h: self._median(hour_means[h]))
        busiest = max(pairs, key=lambda p: p[1])
        quietest = min(pairs, key=lambda p: p[1])
        first, last = pairs[0][0], pairs[-1][0]
        return {
            "n": len(pairs),
            "span_days": round((last - first).total_seconds() / 86400.0, 1),
            "first": first,
            "last": last,
            "last7_total": round(last_total, 3),
            "last7_count": len(last7),
            "wow_pct": round(wow, 1) if wow is not None else None,
            "avg_daily": round(last_total / 7.0, 2) if last7 else None,
            "peak_hour": peak_hour,
            "max": busiest,
            "min": quietest,
            "anomalies": self.anomalies(limit=3),
        }

    def report(self, horizon=24):
        """Insight bundle for /energy_report."""
        pairs = self.readings()
        fc = self.forecast(horizon)
        status = self.status() if pairs else {"n": 0}
        model = self._build_model(pairs)
        trend_week = None
        if model["slope"] and model["mean_daily"]:
            trend_week = 100.0 * 7.0 * model["slope"] / model["mean_daily"]

        advice = []
        peak, valley = fc["peak"], fc["valley"]
        peak_hours = max(1, int((peak["end"] - peak["start"]).total_seconds() // 3600))
        valley_hours = max(1, int((valley["end"] - valley["start"]).total_seconds() // 3600))
        peak_avg = peak["kwh"] / peak_hours
        valley_avg = valley["kwh"] / valley_hours
        tariff = fc.get("tariff")
        if tariff:
            # Average rate across the window hours (handles wrap-around).
            rates_peak, rates_valley = [], []
            for window, bucket in ((peak, rates_peak), (valley, rates_valley)):
                h = window["start"].hour
                end_h = window["end"].hour
                while h != end_h:
                    bucket.append(self._rate(tariff, h))
                    h = (h + 1) % 24
                if not bucket:
                    bucket.append(self._rate(tariff, window["start"].hour))
            saving = self._mean(rates_peak) - self._mean(rates_valley)
            advice.append(
                f"Move flexible load from {peak['start']:%H:00}-{peak['end']:%H:00} "
                f"to {valley['start']:%H:00}-{valley['end']:%H:00}: saves "
                f"{max(0.0, saving):.3f} per kWh shifted."
            )
        elif peak_avg > 0:
            relief = 100.0 * (peak_avg - valley_avg) / peak_avg
            advice.append(
                f"Move flexible load (laundry, dishwasher, water heating) from "
                f"{peak['start']:%H:00}-{peak['end']:%H:00} to "
                f"{valley['start']:%H:00}-{valley['end']:%H:00}: cuts that hour's "
                f"load by up to {max(0.0, relief):.0f}%."
            )
        if trend_week is not None:
            direction = "rising" if trend_week > 1 else "falling" if trend_week < -1 else "flat"
            if direction != "flat":
                advice.append(
                    f"Consumption is {direction} {abs(trend_week):.1f}% per week - "
                    f"check what changed on the schedule."
                )
        anomalies = self.anomalies(limit=5)
        if anomalies:
            latest = anomalies[-1]
            advice.append(
                f"{len(anomalies)} spike(s) detected, latest {latest['ts']:%a %d %H:%M}: "
                f"{latest['kwh']} kWh vs expected {latest['expected']} kWh "
                f"({latest['ratio']}x)."
            )
        if model["confidence"] == "low":
            advice.append(
                "Confidence is LOW - keep logging for a week "
                "(aim for 24x7 hourly slots) to firm up the forecast."
            )
        return {
            "forecast": fc,
            "status": status,
            "trend_week_pct": round(trend_week, 1) if trend_week is not None else None,
            "confidence": model["confidence"],
            "span_days": round(model["span_days"], 1),
            "coverage_pct": round(100.0 * model["coverage"]),
            "anomalies": anomalies,
            "advice": advice,
        }


# ── Module-level store (lazy, so importing this file has no side effects) ──
_store = None


def get_store():
    """The bot's shared EnergyManager (database: energy.db by default)."""
    global _store
    if _store is None:
        _store = EnergyManager()
    return _store
