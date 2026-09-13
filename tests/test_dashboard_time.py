"""Every timestamp on the console is one ``<time datetime='<ISO 8601 UTC>' data-local>`` element from ``render.when``: the
fallback text is UTC ``YYYY-MM-DD HH:MM Z`` with no seconds, and app.js re-formats ``time[data-local]`` in the viewer's zone."""
from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta, timezone

from swesim.dashboard import render
from test_dashboard import site as site  # noqa: F401 - fixture

TIME_RE = re.compile(r"<time datetime='(?P<iso>[^']+)'(?P<local> data-local(?:='clock')?) title='(?P<title>[^']+)'>(?P<text>[^<]*)</time>")
FALLBACK_RE = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2} Z$")
CLOCK_RE = re.compile(r"^\d{2}:\d{2}$")


def _parse(html: str) -> dict:
    m = TIME_RE.fullmatch(html)
    assert m, html
    return m.groupdict()


def test_when_emits_a_time_element_with_the_utc_iso_value_and_no_seconds():
    t = _parse(render.when("2026-09-13T12:15:42+00:00"))
    assert t["iso"] == "2026-09-13T12:15:42+00:00" and t["local"] == " data-local" and t["text"] == "2026-09-13 12:15 Z"
    assert t["title"].startswith(t["iso"]) and "12:15:42" not in t["text"] and FALLBACK_RE.match(t["text"])
    # other zones are converted to UTC; naive strings, epochs and datetimes are taken as UTC
    assert _parse(render.when("2026-09-13T14:15:42+02:00"))["iso"] == "2026-09-13T12:15:42+00:00"
    assert _parse(render.when("2026-09-13T14:15:42+02:00"))["text"] == "2026-09-13 12:15 Z"
    assert _parse(render.when("2026-09-13T12:15:42"))["iso"] == "2026-09-13T12:15:42+00:00"
    assert _parse(render.when(datetime(2026, 9, 13, 12, 15, 42, tzinfo=timezone(timedelta(hours=-7)))))["text"] == "2026-09-13 19:15 Z"
    assert _parse(render.when(datetime(2026, 9, 13, 12, 15, 42, tzinfo=UTC).timestamp()))["text"] == "2026-09-13 12:15 Z"
    assert _parse(render.when("2026-09-13T12:15:42Z"))["iso"] == "2026-09-13T12:15:42+00:00"
    # the timeline clock keeps HH:MM and asks the client for the same shape
    c = _parse(render.when("2026-09-13T12:15:42+00:00", clock_only=True))
    assert c["local"] == " data-local='clock'" and c["text"] == "12:15" and CLOCK_RE.match(c["text"])
    # missing and unparseable values never raise and never invent a time
    assert render.when(None) == "—" and render.when("") == "—" and render.when("not a date") == "not a date" and "<time" not in render.when("<b>x</b>")
    assert render.clock("2026-09-13T12:15:42+00:00") == "12:15" and render.day_clock("2026-09-13T12:15:42+00:00") == "2026-09-13 12:15"


def _times(html: str) -> list[dict]:
    return [m.groupdict() for m in TIME_RE.finditer(html)]


def test_rendered_pages_carry_local_time_elements_without_seconds(site):
    c = site["client"]
    for route in ("/", "/trials", "/scenarios", "/spend", f"/scenario/{site['spec'].id}", "/db/trials", "/db/costs"):
        times = _times(c.get(route).text)
        assert times, route
        for t in times:
            assert FALLBACK_RE.match(t["text"]), (route, t)
            assert t["iso"].endswith("+00:00") and t["title"].startswith(t["iso"]), (route, t)
    # the episode timeline's per-message clock is HH:MM (no seconds) and the lineage started/finished are full local stamps
    ep = c.get(f"/trial/{site['done']}").text
    clocks = [t for t in _times(ep) if t["local"] == " data-local='clock'"]
    stamps = [t for t in _times(ep) if t["local"] == " data-local"]
    assert clocks and all(CLOCK_RE.match(t["text"]) for t in clocks)
    assert stamps and all(FALLBACK_RE.match(t["text"]) for t in stamps)
    assert not re.search(r"<time[^>]*>\d{2}:\d{2}:\d{2}</time>", ep)
    # candidates: the scan-time column is a "time" column, so client re-renders format it the same way
    cand = c.get("/candidates").text
    assert '"key": "discovered_at", "label": "last scan", "kind": "time"' in cand.replace("&quot;", '"')


def test_client_formats_time_elements_in_the_viewers_zone(site):
    js = site["client"].get("/static/app.js").text
    assert "Intl.DateTimeFormat" in js and 'querySelectorAll("time[data-local]")' in js and "localizeTimes(document)" in js
    assert 'hourCycle: "h23"' in js and "getUTCMonth" not in js  # 24 h local time, never a hand-rolled UTC clock
    assert js.count("localizeTimes(") >= 3  # on load, after chart/fragment patches, and via timeEl for re-rendered rows
    assert "timeEl(r.ts_iso, true)" in js and "ts_iso.slice(11, 19)" not in js  # the live timeline clock has no seconds either
