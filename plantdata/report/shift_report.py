"""Shift report: one self-contained HTML file per shift.

    plant-report --database-url postgresql://... [--date 2026-11-18 --shift A] [--out reports/]

Without --date/--shift it reports on the most recent complete shift.
"""
from __future__ import annotations

import argparse
import base64
import io
import logging
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import matplotlib

matplotlib.use("Agg")
import matplotlib.dates as mdates  # noqa: E402
import matplotlib.pyplot as plt  # noqa: E402
import psycopg  # noqa: E402
from jinja2 import Environment, FileSystemLoader, select_autoescape  # noqa: E402
from psycopg.rows import dict_row  # noqa: E402

log = logging.getLogger("plant-report")
TZ = ZoneInfo("Africa/Johannesburg")
TEMPLATES = Path(__file__).resolve().parent / "templates"
STATE_COLOURS = {"Running": "#2e8b57", "Starting": "#9ccc65", "Stopped": "#c9ced6", "Faulted": "#d64545"}
INK = "#1f2933"
TREND_TAGS = [
    ("CV101.Throughput", "Throughput (t/h)", "#2f6fb0"),
    ("CR101.MotorCurrent", "Crusher motor current (A)", "#7a4fb5"),
    ("CR101.BearingTemp", "Crusher bearing temperature (°C)", "#c0582b"),
    ("CR101.Vibration", "Crusher vibration (mm/s)", "#3b8c7a"),
]


def pick_shift(conn, shift_date: date | None, shift: str | None) -> dict:
    """The requested shift, else the latest complete shift with data, else the current one."""
    if shift_date and shift:
        row = conn.execute(
            "SELECT * FROM plant.shift_windows() WHERE shift_date = %s AND shift = %s", (shift_date, shift)
        ).fetchone()
    else:
        row = conn.execute(
            """SELECT w.* FROM plant.shift_windows() w
               WHERE w.shift_end <= (SELECT max(ts) FROM plant.readings)
                 AND w.shift_end > (SELECT min(ts) FROM plant.readings)
               ORDER BY w.shift_start DESC LIMIT 1"""
        ).fetchone() or conn.execute(
            """SELECT w.* FROM plant.shift_windows() w
               WHERE w.shift_start <= (SELECT max(ts) FROM plant.readings)
               ORDER BY w.shift_start DESC LIMIT 1"""
        ).fetchone()
    if not row:
        raise SystemExit("No data for that shift.")
    bounds = conn.execute("SELECT min(ts) AS t0, max(ts) AS t1 FROM plant.readings").fetchone()
    row["partial"] = bool(bounds["t0"] and (bounds["t0"] > row["shift_start"] or bounds["t1"] < row["shift_end"]))
    return row


def fetch(conn, window: dict) -> dict:
    s, e = window["shift_start"], window["shift_end"]
    kpis = conn.execute(
        "SELECT * FROM plant.v_shift_kpis WHERE shift_date = %s AND shift = %s ORDER BY equipment",
        (window["shift_date"], window["shift"]),
    ).fetchall()
    downtime = conn.execute(
        """SELECT equipment, greatest(start_ts, %(s)s) AS start_ts, least(end_ts, %(e)s) AS end_ts,
                  round(extract(epoch FROM least(end_ts, %(e)s) - greatest(start_ts, %(s)s)) / 60.0, 1) AS minutes,
                  reason, category
           FROM plant.v_downtime_events
           WHERE start_ts < %(e)s AND end_ts > %(s)s
           ORDER BY start_ts""",
        {"s": s, "e": e},
    ).fetchall()
    intervals = conn.execute(
        """SELECT equipment, state_name, greatest(start_ts, %(s)s) AS start_ts, least(end_ts, %(e)s) AS end_ts
           FROM plant.v_state_intervals WHERE start_ts < %(e)s AND end_ts > %(s)s ORDER BY start_ts""",
        {"s": s, "e": e},
    ).fetchall()
    alarms = conn.execute(
        "SELECT * FROM plant.v_alarm_events WHERE raised_at >= %s AND raised_at < %s ORDER BY raised_at", (s, e)
    ).fetchall()
    trends = conn.execute(
        """SELECT tag, minute, avg_value FROM plant.v_minute
           WHERE tag IN ('CV101.Throughput', 'CR101.MotorCurrent', 'CR101.BearingTemp', 'CR101.Vibration')
             AND minute >= %s AND minute < %s ORDER BY minute""",
        (s, e),
    ).fetchall()
    limits = {r["tag"]: r for r in conn.execute("SELECT * FROM plant.alarm_limits").fetchall()}
    return {"kpis": kpis, "downtime": downtime, "intervals": intervals, "alarms": alarms,
            "trends": trends, "limits": limits}


def _png(fig) -> str:
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=130, bbox_inches="tight")
    plt.close(fig)
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()


def _style(ax):
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    ax.spines["left"].set_color("#c9ced6")
    ax.spines["bottom"].set_color("#c9ced6")
    ax.tick_params(colors=INK, labelsize=8)
    ax.grid(axis="y", color="#e6e9ee", linewidth=0.8)
    ax.set_axisbelow(True)


def timeline_chart(intervals, window) -> str:
    fig, ax = plt.subplots(figsize=(10, 1.6))
    equipment = ["CV101", "CR101"]
    for row in intervals:
        y = equipment.index(row["equipment"])
        start = mdates.date2num(row["start_ts"])
        width = mdates.date2num(row["end_ts"]) - start
        ax.barh(y, width, left=start, height=0.6, color=STATE_COLOURS[row["state_name"]], linewidth=0)
    ax.set_yticks(range(len(equipment)), equipment)
    ax.set_xlim(mdates.date2num(window["shift_start"]), mdates.date2num(window["shift_end"]))
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M", tz=TZ))
    _style(ax)
    ax.grid(False)
    handles = [plt.Rectangle((0, 0), 1, 1, color=c) for c in STATE_COLOURS.values()]
    ax.legend(handles, STATE_COLOURS.keys(), ncol=4, fontsize=8, frameon=False,
              loc="upper center", bbox_to_anchor=(0.5, 1.45))
    return _png(fig)


def trend_chart(trends, tag, label, colour, limits, window) -> str | None:
    points = [(r["minute"], r["avg_value"]) for r in trends if r["tag"] == tag]
    if not points:
        return None
    fig, ax = plt.subplots(figsize=(5, 2.1))
    xs, ys = zip(*points, strict=True)
    ax.plot(xs, ys, color=colour, linewidth=1.2)
    lim = limits.get(tag)
    if lim:
        ax.axhline(lim["high"], color="#e0a100", linewidth=1, linestyle="--")
        ax.text(xs[0], lim["high"], " High", color="#b58100", fontsize=7, va="bottom")
    ax.set_title(label, fontsize=9, color=INK, loc="left")
    ax.set_xlim(window["shift_start"], window["shift_end"])
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M", tz=TZ))
    _style(ax)
    return _png(fig)


def pareto_chart(downtime) -> str | None:
    totals: dict[str, float] = {}
    for row in downtime:
        totals[row["reason"]] = totals.get(row["reason"], 0) + float(row["minutes"])
    if not totals:
        return None
    items = sorted(totals.items(), key=lambda kv: kv[1])
    fig, ax = plt.subplots(figsize=(5, 0.45 * len(items) + 0.6))
    ax.barh([k for k, _ in items], [v for _, v in items], color="#d64545", height=0.55)
    for i, (_k, v) in enumerate(items):
        ax.text(v, i, f" {v:.0f} min", va="center", fontsize=8, color=INK)
    ax.set_xlabel("minutes", fontsize=8, color=INK)
    _style(ax)
    ax.grid(axis="x", color="#e6e9ee")
    ax.grid(axis="y", visible=False)
    return _png(fig)


def build_report(conn, shift_date: date | None = None, shift: str | None = None) -> tuple[str, dict]:
    conn.row_factory = dict_row
    window = pick_shift(conn, shift_date, shift)
    data = fetch(conn, window)
    crusher = next((k for k in data["kpis"] if k["equipment"] == "CR101"), None)
    charts = {
        "timeline": timeline_chart(data["intervals"], window),
        "pareto": pareto_chart(data["downtime"]),
        "trends": [c for c in (
            trend_chart(data["trends"], tag, label, colour, data["limits"], window)
            for tag, label, colour in TREND_TAGS
        ) if c],
    }
    env = Environment(loader=FileSystemLoader(TEMPLATES), autoescape=select_autoescape())
    env.filters["local"] = lambda ts: ts.astimezone(TZ).strftime("%H:%M")
    env.filters["pct"] = lambda v: "–" if v is None else f"{float(v) * 100:.1f}%"
    html = env.get_template("shift_report.html").render(
        window=window, crusher=crusher, generated=datetime.now(TZ).strftime("%Y-%m-%d %H:%M"),
        start_local=window["shift_start"].astimezone(TZ), end_local=window["shift_end"].astimezone(TZ),
        charts=charts, **data,
    )
    return html, window


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--database-url", required=True)
    p.add_argument("--date", type=date.fromisoformat)
    p.add_argument("--shift", choices=["A", "B", "C"])
    p.add_argument("--out", default="reports")
    a = p.parse_args()
    with psycopg.connect(a.database_url) as conn:
        html, window = build_report(conn, a.date, a.shift)
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    path = out / f"shift_{window['shift_date']}_{window['shift']}.html"
    path.write_text(html, encoding="utf-8")
    log.info("wrote %s", path)


if __name__ == "__main__":
    main()
