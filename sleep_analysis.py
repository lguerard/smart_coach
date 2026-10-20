#!/usr/bin/env python3
"""Smart Coach's own reading of the night, beyond the watch's score.

The watch decides "asleep" from movement and heart rate, so lying still
and awake with a low resting heart rate counts as sleep: a night broken
by a trip to the bathroom and an hour awake before the alarm can still
score 80. This module reads the night from what was recorded through it
(``sleep_samples``: heart rate every minute or two, blood oxygen and
breathing when the watch records them) next to Garmin's stages:

- probable awakenings: the heart rate rising clearly above the night's
  own level for a few minutes, or an awake stage the watch did log;
- the final early awakening: a rise in the last two hours after 3:30
  that never comes back down -- awake until getting up;
- an estimated sleep time with those taken out;
- over the last two weeks: how much, how regular, how broken, and
  plain-language conclusions.

An estimate, said as such: a rise in heart rate can also be a dream or a
turn in bed. Nothing here is a diagnosis.
"""

import datetime as dt
import sqlite3
import statistics
from typing import Optional

AWAKE_STAGES = {1, 3, 7}
DEEP, REM, LIGHT = 5, 6, 4
RISE_BPM = 10          # above the night's median heart rate
MIN_RISE_MIN = 2       # minutes the rise lasts, at least
MERGE_GAP_MIN = 10     # two rises this close are one awakening
SETTLE_MIN = 15        # the first minutes after falling asleep do not count
FINAL_WINDOW_MIN = 120 # a final awakening starts in the last two hours...
FINAL_AFTER_HOUR = 3.5 # ...after 3:30
FINAL_STAYS_BPM = 5    # ...and the heart rate stays this much above
SPO2_DIP = 4           # points under the night's median: a dip
PERIOD_DAYS = 14
TARGET_HOURS = 7.5


def _ts(value: str) -> dt.datetime:
    return dt.datetime.fromisoformat(value)


def _local(value: dt.datetime, tz) -> dt.datetime:
    return value.astimezone(tz)


def _hm(value: dt.datetime) -> str:
    return value.strftime("%H:%M")


def _night_row(conn: sqlite3.Connection, user_id: int, date: str, tz):
    """The longest sleep that ended on ``date`` (local), or None."""
    rows = [r for r in conn.execute(
        "SELECT uuid, start_utc, end_utc, sleep_score FROM sleep_sessions "
        "WHERE user_id = ? AND end_utc > start_utc", (user_id,))
        if _local(_ts(r["end_utc"]), tz).date().isoformat() == date]
    return max(rows, key=lambda r: _ts(r["end_utc"]) - _ts(r["start_utc"]),
               default=None)


def _minutes(samples: list[tuple[dt.datetime, float]]) -> list[tuple[dt.datetime, float]]:
    """Samples averaged per minute (two sources can overlap)."""
    buckets: dict[dt.datetime, list[float]] = {}
    for when, value in samples:
        buckets.setdefault(when.replace(second=0, microsecond=0), []).append(value)
    return sorted((m, sum(v) / len(v)) for m, v in buckets.items())


def _rises(series, base: float, start: dt.datetime) -> list[list]:
    """Stretches where the heart rate stays clearly above the night's level."""
    out, cur = [], None
    for when, bpm in series:
        if when < start + dt.timedelta(minutes=SETTLE_MIN):
            continue
        if bpm >= base + RISE_BPM:
            if cur and (when - cur[1]).total_seconds() <= 4 * 60:
                cur[1], cur[2] = when, max(cur[2], bpm)
            else:
                if cur:
                    out.append(cur)
                cur = [when, when, bpm]
        elif cur and (when - cur[1]).total_seconds() > 4 * 60:
            out.append(cur)
            cur = None
    if cur:
        out.append(cur)
    return [[a, b + dt.timedelta(minutes=1), peak] for a, b, peak in out
            if (b - a).total_seconds() >= (MIN_RISE_MIN - 1) * 60]


def _merge(intervals: list[list]) -> list[list]:
    merged = []
    for item in sorted(intervals, key=lambda x: x[0]):
        if merged and (item[0] - merged[-1][1]).total_seconds() <= MERGE_GAP_MIN * 60:
            merged[-1][1] = max(merged[-1][1], item[1])
            merged[-1][2] = max(merged[-1][2], item[2])
            merged[-1][3] = merged[-1][3] or item[3]
        else:
            merged.append(list(item))
    return merged


def night(conn: sqlite3.Connection, user_id: int, date: str) -> Optional[dict]:
    """The night that ended on ``date``, read beyond the watch's score.

    Returns:
        dict | None: ``bed`` / ``wake`` (local ``HH:MM``), ``in_bed_min``,
        ``watch_sleep_min`` (Garmin's asleep time), ``sleep_min`` (the
        estimate), ``score`` (Garmin's), ``deep_min`` / ``rem_min``,
        ``wakes`` (``{at, minutes, peak_bpm, watch}``: probable
        awakenings; ``watch`` = the watch logged it too), ``final_wake``
        (``HH:MM`` or None: awake from then until getting up),
        ``hr_base`` / ``hr_min``, ``spo2`` (``{median, min, dips,
        below_90_pct}`` or None), ``resp_avg``, ``restless``, ``felt``,
        ``has_hr`` (False: no heart rate recorded, the estimate is the
        watch's), plus ``start_iso`` / ``end_iso`` and ``wake_marks``
        (minutes after going to bed, for drawing).
        None when no sleep ended that day.
    """
    import metrics
    import sleep_feedback

    tz = metrics.local_tz(conn, user_id)
    row = _night_row(conn, user_id, date, tz)
    if row is None:
        return None
    start, end = _ts(row["start_utc"]), _ts(row["end_utc"])
    in_bed = (end - start).total_seconds() / 60
    stages = [(_ts(s["stage_start_utc"]), _ts(s["stage_end_utc"]), s["stage_type"])
              for s in conn.execute(
                  "SELECT stage_start_utc, stage_end_utc, stage_type FROM "
                  "sleep_stages WHERE parent_uuid = ? ORDER BY stage_start_utc",
                  (row["uuid"],))]
    length = lambda kinds: sum((b - a).total_seconds() / 60
                               for a, b, k in stages if k in kinds)
    watch_awake = length(AWAKE_STAGES)

    samples: dict[str, list] = {"hr": [], "spo2": [], "resp": [], "restless": []}
    for s in conn.execute(
        "SELECT ts_utc, kind, value FROM sleep_samples WHERE user_id = ? AND "
        "ts_utc BETWEEN ? AND ?", (user_id, row["start_utc"], row["end_utc"])):
        samples[s["kind"]].append((_ts(s["ts_utc"]), s["value"]))
    hr = _minutes(samples["hr"])
    base = statistics.median(v for _, v in hr) if len(hr) >= 30 else None

    candidates = [[a, b, 0.0, True] for a, b, k in stages
                  if k in AWAKE_STAGES and (b - a).total_seconds() >= 120
                  and a > start + dt.timedelta(minutes=SETTLE_MIN)
                  and b < end - dt.timedelta(minutes=2)]
    if base is not None:
        candidates += [[a, b, peak, False] for a, b, peak in _rises(hr, base, start)]
    wakes = _merge(candidates)

    # The last awakening: early, near the end, and the heart rate never
    # settles again -- awake from then until getting up.
    final = None
    if base is not None and wakes:
        last = wakes[-1]
        local = _local(last[0], tz)
        hour = local.hour + local.minute / 60
        after = [v for t, v in hr if t >= last[0]]
        if ((end - last[0]).total_seconds() <= FINAL_WINDOW_MIN * 60
                and FINAL_AFTER_HOUR <= hour < 12
                and after and statistics.mean(after) >= base + FINAL_STAYS_BPM):
            final = last
            final[1] = end

    awake = sum((b - a).total_seconds() / 60 for a, b, *_ in wakes)
    watch_sleep = in_bed - watch_awake
    # Never more than the watch says: its awake stages are in the union.
    sleep = min(watch_sleep, in_bed - awake) if wakes else watch_sleep

    spo2 = None
    if len(samples["spo2"]) >= 10:
        values = [v for _, v in sorted(samples["spo2"])]
        median = statistics.median(values)
        dips, last_dip = 0, None
        for when, value in sorted(samples["spo2"]):
            if value <= median - SPO2_DIP and (
                    last_dip is None or (when - last_dip).total_seconds() > 600):
                dips, last_dip = dips + 1, when
        spo2 = {"median": round(median), "min": round(min(values)),
                "dips": dips, "dips_per_hour": round(dips / (in_bed / 60), 1),
                "below_90_pct": round(100 * sum(v < 90 for v in values) / len(values))}
    resp = [v for _, v in samples["resp"]]

    return {
        "date": date,
        "bed": _hm(_local(start, tz)), "wake": _hm(_local(end, tz)),
        "start_iso": _local(start, tz).isoformat(),
        "end_iso": _local(end, tz).isoformat(),
        "in_bed_min": round(in_bed), "watch_sleep_min": round(watch_sleep),
        "sleep_min": round(sleep), "score": row["sleep_score"],
        "deep_min": round(length({DEEP})), "rem_min": round(length({REM})),
        "wakes": [{"at": _hm(_local(a, tz)),
                   "minutes": max(1, round((b - a).total_seconds() / 60)),
                   "peak_bpm": round(p) if p else None, "watch": w,
                   "offset_min": round((a - start).total_seconds() / 60)}
                  for a, b, p, w in wakes],
        "final_wake": _hm(_local(final[0], tz)) if final else None,
        "hr_base": round(base) if base is not None else None,
        "hr_min": round(min(v for _, v in hr)) if hr else None,
        "has_hr": base is not None,
        "spo2": spo2,
        "resp_avg": round(statistics.mean(resp), 1) if resp else None,
        "restless": len(samples["restless"]) or None,
        "felt": sleep_feedback.felt(conn, user_id, date),
    }


def _clock(minutes: float) -> str:
    minutes = round(minutes) % (24 * 60)
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def _duration(minutes: float) -> str:
    minutes = round(minutes)
    return f"{minutes // 60} h {minutes % 60:02d}"


def _n(value: float) -> str:
    """A number the French way: 2,2 -- and 2 rather than 2,0."""
    return f"{value:g}".replace(".", ",")


def _s(count: float, word: str, plural: str = "") -> str:
    return f"{_n(count)} {word if count < 2 else (plural or word + 's')}"


def _evening_minutes(hm: str) -> int:
    """Bedtime on one axis: 23:30 -> 1410, 00:30 -> 1470."""
    h, m = map(int, hm.split(":"))
    return (h + (24 if h < 12 else 0)) * 60 + m


def period(
    conn: sqlite3.Connection, user_id: int, date: str, days: int = PERIOD_DAYS,
) -> dict:
    """The last ``days`` nights read together, with conclusions.

    Returns:
        dict: ``nights`` (``night`` results, newest first), ``count``,
        averages (``sleep_min``, ``watch_sleep_min``, ``in_bed_min``,
        ``wakes_per_night``), ``bed`` (median and earliest/latest),
        ``wake`` (median), ``short_nights`` (under 6 h estimated),
        ``final_wakes`` (nights ending awake early), ``debt_min`` (a week
        against 7 h 30), ``wake_hours`` (when awakenings happen),
        ``spo2_nights``, and ``conclusions``: plain French sentences.
    """
    day = dt.date.fromisoformat(date)
    nights = [n for n in (
        night(conn, user_id, (day - dt.timedelta(days=k)).isoformat())
        for k in range(days)) if n]
    out: dict = {"nights": nights, "count": len(nights), "conclusions": []}
    if not nights:
        return out
    mean = lambda key: statistics.mean(n[key] for n in nights)
    out["sleep_min"] = round(mean("sleep_min"))
    out["watch_sleep_min"] = round(mean("watch_sleep_min"))
    out["in_bed_min"] = round(mean("in_bed_min"))
    with_hr = [n for n in nights if n["has_hr"]]
    out["wakes_per_night"] = (round(statistics.mean(len(n["wakes"]) for n in with_hr), 1)
                              if with_hr else None)
    beds = [_evening_minutes(n["bed"]) for n in nights]
    out["bed"] = {"median": _clock(statistics.median(beds)),
                  "earliest": _clock(min(beds)), "latest": _clock(max(beds)),
                  "spread_min": round(max(beds) - min(beds))}
    wakes_at = [_evening_minutes(n["wake"]) - 24 * 60 for n in nights]
    out["wake"] = {"median": _clock(statistics.median(wakes_at))}
    out["short_nights"] = sum(n["sleep_min"] < 360 for n in nights)
    out["final_wakes"] = [n["final_wake"] for n in nights if n["final_wake"]]
    out["debt_min"] = round(max(0, TARGET_HOURS * 60 - out["sleep_min"]) * 7)
    hours: dict[int, int] = {}
    for n in with_hr:
        for w in n["wakes"]:
            h = int(w["at"][:2])
            hours[h] = hours.get(h, 0) + 1
    out["wake_hours"] = dict(sorted(hours.items(), key=lambda kv: (kv[0] + 12) % 24))
    spo2 = [n["spo2"] for n in nights if n["spo2"]]
    out["spo2_nights"] = len(spo2)

    keyed: list[tuple[str, str]] = []
    out["keyed"] = keyed

    class _Said:
        """``said.append(text)`` with the key of the point being made."""
        key = ""

        def append(self, text: str) -> None:
            out["conclusions"].append(text)
            keyed.append((self.key, text))

    said = _Said()
    sleep = out["sleep_min"]
    said.key = "average"
    said.append(
        f"Tu dors en moyenne {_duration(sleep)} par nuit (estimation), pour "
        f"{_duration(out['in_bed_min'])} au lit"
        + (" : c'est trop peu, il faudrait 7 à 9 h. C'est le premier point "
           "à travailler." if sleep < 420 else "."))
    gap = out["watch_sleep_min"] - sleep
    said.key = "watch"
    if gap >= 15:
        said.append(f"La montre compte {_duration(out['watch_sleep_min'])} : "
                    f"elle surestime tes nuits d'environ {gap} min, le temps "
                    "éveillé qu'elle prend pour du sommeil.")
    said.key = "wakes"
    if out["wakes_per_night"] is not None and out["wakes_per_night"] >= 1:
        busiest = sorted(hours, key=lambda h: -hours[h])[:3]
        window = (f", surtout vers {', '.join(f'{h} h' for h in sorted(busiest, key=lambda h: (h + 12) % 24))}"
                  if busiest else "")
        said.append(f"{_s(out['wakes_per_night'], 'réveil probable', 'réveils probables')} "
                    f"par nuit en moyenne{window}, en général le moment où tu "
                    "te lèves. Le pouls redescend ensuite même si tu restes "
                    "éveillé : le temps passé à essayer de te rendormir ne se "
                    "voit pas, ta note du matin le dit mieux.")
    said.key = "final"
    if out["final_wakes"]:
        final_count = len(out["final_wakes"])
        said.append(f"{_s(final_count, 'nuit')} sur {len(nights)} se "
                    f"termine{'nt' if final_count > 1 else ''} par un réveil "
                    "sans te rendormir (vers "
                    f"{_clock(statistics.median(_evening_minutes(t) - 24 * 60 for t in out['final_wakes']))}) : "
                    "le sommeil de fin de nuit est celui qui manque.")
    said.key = "regular"
    if out["bed"]["spread_min"] >= 90:
        said.append(f"Ton heure de coucher varie beaucoup (de "
                    f"{out['bed']['earliest']} à {out['bed']['latest']}) : un "
                    "horaire régulier aide à dormir d'une traite.")
    said.key = "target"
    if sleep < 420:
        target = _clock(_evening_minutes(out["bed"]["median"]) - (420 - sleep))
        out["bed_target"] = target
        said.append(f"Pour viser 7 h, couche-toi vers {target} au lieu de "
                    f"{out['bed']['median']} (tu te lèves vers "
                    f"{out['wake']['median']}).")
    said.key = "spo2"
    if spo2:
        dips = statistics.mean(s["dips_per_hour"] for s in spo2)
        said.append(f"Oxygène la nuit (mesuré sur {_s(len(spo2), 'nuit')}) : "
                    f"{_s(round(dips, 1), 'baisse')} par heure en moyenne"
                    + (" ; des baisses fréquentes sont à montrer au médecin "
                       "(la montre n'est pas un examen médical)." if dips >= 5
                       else "."))
    elif nights:
        said.append("Pas de mesure d'oxygène la nuit : active l'oxymètre "
                    "pendant le sommeil dans Garmin Connect, c'est l'indice "
                    "qui manque pour parler d'apnée avec le médecin.")
    said.key = "felt"
    felt_bad = [n for n in nights if n["felt"] == "bad"]
    if felt_bad:
        scored = [n["score"] for n in felt_bad if n["score"] is not None]
        said.append(f"{_s(len(felt_bad), 'nuit notée', 'nuits notées')} « mal dormi » par toi"
                    + (f", que la montre notait en moyenne {statistics.mean(scored):.0f}/100."
                       if scored else "."))
    return out


MORNING_MIN_NIGHTS = 5


def morning_trend(report: dict, date: str) -> Optional[dict]:
    """What the morning message takes from the two weeks of nights.

    One or two points a day, not the whole page: the bedtime that would
    give 7 hours first when the nights are short (the one thing to do
    tonight), then one other conclusion that changes from day to day so
    the message does not say the same thing every morning.

    Returns:
        dict | None: ``nights``, ``sleep_min`` / ``watch_sleep_min``
        (averages), ``wakes_per_night``, ``bed_target`` (or None) and
        ``points`` (one or two French sentences). None with fewer than
        ``MORNING_MIN_NIGHTS`` nights.
    """
    if report.get("count", 0) < MORNING_MIN_NIGHTS:
        return None
    keyed = report.get("keyed") or []
    target = report.get("bed_target")
    points = []
    if target:
        points.append(
            f"Sur les {report['count']} dernières nuits, tu dors en moyenne "
            f"{_duration(report['sleep_min'])} : pour viser 7 h, couche-toi "
            f"vers {target} ce soir (au lieu de {report['bed']['median']}).")
    rest = [text for key, text in keyed
            if key not in ("average", "target") or (key == "average" and not target)]
    if rest:
        points.append(rest[dt.date.fromisoformat(date).toordinal() % len(rest)])
    return {
        "nights": report["count"], "sleep_min": report["sleep_min"],
        "watch_sleep_min": report["watch_sleep_min"],
        "wakes_per_night": report.get("wakes_per_night"),
        "bed_target": target, "points": points[:2],
    }


if __name__ == "__main__":
    import tempfile
    from pathlib import Path

    import db
    import sleep_feedback

    conn = db.connect(Path(tempfile.mkdtemp()) / "sleep.db")
    db.init_db(conn)
    uid = db.create_user(conn, "s", "pw-123456")
    other = db.create_user(conn, "o", "pw-123456")
    utc = dt.timezone.utc

    def add_night(day: str, bed_utc: str, wake_utc: str, rises=(), awake=(),
                  score=80, spo2=None):
        """A night of heart rate at 55, with rises at the given (start,
        minutes, final) and watch awake stages at (start, minutes)."""
        start, end = dt.datetime.fromisoformat(bed_utc), dt.datetime.fromisoformat(wake_utc)
        uuid = f"n-{day}"
        conn.execute(
            "INSERT INTO sleep_sessions (uuid, user_id, start_utc, end_utc, "
            "local_date, sleep_score) VALUES (?, ?, ?, ?, ?, ?)",
            (uuid, uid, start.isoformat(), end.isoformat(), day, score))
        conn.execute("INSERT INTO sleep_stages VALUES (?, ?, ?, ?, 4)",
                     (uuid, uid, start.isoformat(), end.isoformat()))
        for at, minutes in awake:
            a = dt.datetime.fromisoformat(at)
            conn.execute("INSERT INTO sleep_stages VALUES (?, ?, ?, ?, 1)",
                         (uuid, uid, a.isoformat(),
                          (a + dt.timedelta(minutes=minutes)).isoformat()))
        t = start
        high = []
        for at, minutes, final in rises:
            a = dt.datetime.fromisoformat(at)
            high.append((a, end if final else a + dt.timedelta(minutes=minutes)))
        while t < end:
            bpm = 75 if any(a <= t < b for a, b in high) else 55
            conn.execute("INSERT INTO sleep_samples VALUES (?, ?, 'hr', ?, 'hc')",
                         (uid, t.isoformat(), bpm))
            t += dt.timedelta(minutes=1)
        for i, value in enumerate(spo2 or []):
            conn.execute("INSERT INTO sleep_samples VALUES (?, ?, 'spo2', ?, 'garmin')",
                         (uid, (start + dt.timedelta(minutes=10 * i)).isoformat(), value))
        conn.commit()

    # Paris is UTC+2 in October. Bed 00:10, up 05:50; up to pee at 02:30
    # (heart rate up 8 min), awake for good from 04:40.
    add_night("2026-10-19", "2026-10-18T22:10:00+00:00", "2026-10-19T03:50:00+00:00",
              rises=[("2026-10-19T00:30:00+00:00", 8, False),
                     ("2026-10-19T02:40:00+00:00", 0, True)],
              spo2=[95, 94, 96, 89, 95, 95, 90, 95, 96, 95, 95, 94])
    n = night(conn, uid, "2026-10-19")
    assert (n["bed"], n["wake"]) == ("00:10", "05:50"), n
    assert [w["at"] for w in n["wakes"]] == ["02:30", "04:40"], n["wakes"]
    assert n["final_wake"] == "04:40", n
    # The watch says 5 h 40 asleep; really 8 min + 70 min less.
    assert n["watch_sleep_min"] == 340, n
    assert n["sleep_min"] == 340 - 8 - 70, n
    assert n["hr_base"] == 55 and n["has_hr"], n
    assert n["spo2"]["dips"] == 2 and n["spo2"]["min"] == 89, n["spo2"]
    assert night(conn, other, "2026-10-19") is None
    assert night(conn, uid, "2026-10-18") is None

    # A watch awake stage counts even without a heart-rate rise, and is
    # not counted twice when the heart rate rises at the same time.
    add_night("2026-10-18", "2026-10-17T21:00:00+00:00", "2026-10-18T04:00:00+00:00",
              rises=[("2026-10-18T01:00:00+00:00", 10, False)],
              awake=[("2026-10-18T01:02:00+00:00", 6), ("2026-10-18T02:30:00+00:00", 5)])
    m = night(conn, uid, "2026-10-18")
    assert len(m["wakes"]) == 2 and m["final_wake"] is None, m["wakes"]
    assert m["wakes"][0]["watch"] and m["wakes"][0]["minutes"] == 10, m["wakes"]
    assert m["sleep_min"] == 420 - 10 - 5, m
    sleep_feedback.record(conn, uid, "2026-10-19", "bad")

    # Without heart rate: the watch's own reading, said as such.
    add_night("2026-10-17", "2026-10-16T22:40:00+00:00", "2026-10-17T04:00:00+00:00")
    conn.execute("DELETE FROM sleep_samples WHERE ts_utc LIKE '2026-10-16T2%' "
                 "OR ts_utc LIKE '2026-10-17T0%'")
    conn.commit()
    bare = night(conn, uid, "2026-10-17")
    assert not bare["has_hr"] and bare["sleep_min"] == bare["watch_sleep_min"] == 320

    p = period(conn, uid, "2026-10-19")
    assert p["count"] == 3 and p["short_nights"] == 2, p
    assert p["final_wakes"] == ["04:40"], p
    assert p["wakes_per_night"] == 2.0, p
    text = " ".join(p["conclusions"])
    assert "trop peu" in text and "réveil sans te rendormir" in text, text
    assert "surestime" in text and "couche-toi vers" in text, text
    assert "Oxygène la nuit (mesuré sur 1 nuit)" in text, text
    assert "2 réveils probables par nuit" in text and "1 nuit sur 3 se termine " in text, text
    assert "« mal dormi »" in text and "80/100" in text, text
    assert period(conn, other, "2026-10-19") == {"nights": [], "count": 0,
                                                  "conclusions": []}
    assert [k for k, _ in p["keyed"]][:2] == ["average", "watch"], p["keyed"]
    assert p["bed_target"] == "22:39", p["bed_target"]  # 00:10 minus 91 min
    # The morning takes two points: tonight's bedtime, then one other
    # that changes from day to day. Too few nights: nothing.
    assert morning_trend(p, "2026-10-19") is None  # 3 nights
    five = {**p, "count": 5}
    days = {d: morning_trend(five, d) for d in ("2026-10-19", "2026-10-20")}
    for trend in days.values():
        assert trend["points"][0].startswith("Sur les 5 dernières nuits"), trend
        assert "couche-toi vers 22:39 ce soir" in trend["points"][0], trend
        assert len(trend["points"]) == 2 and "Tu dors en moyenne" not in trend["points"][1]
    assert days["2026-10-19"]["points"][1] != days["2026-10-20"]["points"][1]
    print("sleep_analysis.py: all checks passed")
