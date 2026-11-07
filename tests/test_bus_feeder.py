from datetime import datetime, timedelta, timezone
from datetime import time as dtime

import pytest
import requests

import bus_feeder as bf

SGT = timezone(timedelta(hours=8))
BASE_ENV = {"CLOCKS": "10.0.0.1=awtrix,10.0.0.2=awtrix_tc002"}


@pytest.fixture(autouse=True)
def config():
    bf.configure(dict(BASE_ENV))
    yield


# ---- config ---------------------------------------------------------------

def test_services_are_stripped():
    cfg, _ = bf.load_config({**BASE_ENV, "SERVICES": " 102, 136 ,"})
    assert cfg["SERVICES"] == ["102", "136"]


def test_parse_clocks_strips_and_validates():
    assert bf.parse_clocks(" 10.0.0.1 = awtrix , clock.lan:8080=ulanzi_tc002") == {
        "10.0.0.1": "awtrix",
        "clock.lan:8080": "ulanzi_tc002",
    }
    with pytest.raises(SystemExit):
        bf.parse_clocks("10.0.0.1=nope")
    with pytest.raises(SystemExit):
        bf.parse_clocks("10.0.0.1/evil?x=awtrix")


@pytest.mark.parametrize(
    "override",
    [
        {"BUS_STOP_CODE": "644/9"},
        {"APP_NAME": "sg bus?"},
        {"SERVICES": "102,1#6"},
        {"SERVICES": " , "},
        {"POLL_SECONDS": "abc"},
        {"POLL_SECONDS": "0"},
        {"CLOCKS": ""},
        {"QUIET_HOURS": "1am-5am"},
    ],
)
def test_bad_config_fails_fast(override):
    with pytest.raises(SystemExit):
        bf.load_config({**BASE_ENV, **override})


def test_quiet_hours():
    window = bf.parse_quiet_hours("23:30-05:30")
    assert window == (dtime(23, 30), dtime(5, 30))
    at = lambda h, m: datetime(2026, 1, 1, h, m)  # noqa: E731
    assert bf.in_quiet_hours(window, at(0, 10))
    assert bf.in_quiet_hours(window, at(23, 30))
    assert not bf.in_quiet_hours(window, at(5, 30))
    assert not bf.in_quiet_hours(window, at(12, 0))
    day = bf.parse_quiet_hours("01:00-05:30")
    assert bf.in_quiet_hours(day, at(3, 0)) and not bf.in_quiet_hours(day, at(6, 0))
    assert bf.parse_quiet_hours("") is None
    assert not bf.in_quiet_hours(None, at(3, 0))


# ---- parsing --------------------------------------------------------------

def test_minutes_until_iso():
    now = datetime(2026, 1, 1, 12, 0, tzinfo=SGT)
    assert bf.minutes_until_iso("2026-01-01T12:05:00+08:00", now) == 5
    assert bf.minutes_until_iso("2026-01-01T11:55:00+08:00", now) == 0
    assert bf.minutes_until_iso("", now) is None
    assert bf.minutes_until_iso("garbage", now) is None
    assert bf.minutes_until_iso("2026-01-01T12:05:00", now) is None  # naive vs aware


def test_parse_lta_skips_bad_rows():
    future = (datetime.now(SGT) + timedelta(minutes=7, seconds=10)).isoformat()
    data = {
        "Services": [
            {"ServiceNo": "102", "NextBus": {"EstimatedArrival": future}},
            {"ServiceNo": "136", "NextBus": None},
            {"NextBus": {"EstimatedArrival": future}},  # no ServiceNo
            "junk",
        ]
    }
    assert bf.parse_lta(data) == {"102": 7, "136": None}


@pytest.mark.parametrize("data", [{}, {"Services": None}, {"Services": "x"}, [], None])
def test_parse_lta_rejects_malformed(data):
    with pytest.raises(bf.FetchError):
        bf.parse_lta(data)


def test_parse_lta_empty_list_is_valid():
    assert bf.parse_lta({"Services": []}) == {}


def test_parse_arrivelah():
    data = {"services": [{"no": "102", "next": {"duration_ms": 125000}}, {"no": "136", "next": None}, {"x": 1}]}
    assert bf.parse_arrivelah(data) == {"102": 2, "136": None}
    with pytest.raises(bf.FetchError):
        bf.parse_arrivelah({"error": "boom"})


# ---- rendering ------------------------------------------------------------

def test_service_label():
    m = {"102": 0, "136": 4}
    assert bf.service_label("102", m) == "102 ARR"
    assert bf.service_label("136", m) == "136 4MIN"
    assert bf.service_label("999", m) == "999 --"


def test_tc002_regions_with_and_without_icon():
    with_icon = bf.tc002_regions(["a", "b"], "AAAA")
    assert with_icon[0]["id"] == "icon" and with_icon[1]["box"] == [18, 0, 34, 8]
    no_icon = bf.tc002_regions(["a", "b"], None)
    assert [r["box"] for r in no_icon] == [[0, 0, 52, 8], [0, 8, 52, 8]]


def test_missing_tc002_icon_does_not_crash(monkeypatch, tmp_path):
    monkeypatch.setattr(bf, "TC002_ICON_PATH", str(tmp_path / "missing.gif"))
    monkeypatch.setattr(bf, "_tc002_icon_cache", {})
    assert bf.tc002_icon_b64() is None


# ---- fetch fallback + run_once --------------------------------------------

def test_malformed_lta_falls_back_to_arrivelah(monkeypatch):
    bf.CONFIG["LTA_ACCOUNT_KEY"] = "k"

    def bad_lta(*a):
        raise bf.FetchError("no Services")

    monkeypatch.setattr(bf, "fetch_lta_minutes", bad_lta)
    monkeypatch.setattr(bf, "fetch_arrivelah_minutes", lambda code: {"102": 3})
    assert bf.fetch_minutes("64409") == ({"102": 3}, "arrivelah")


def test_run_once_survives_total_fetch_failure(monkeypatch):
    def boom(code):
        raise requests.ConnectionError("down")

    monkeypatch.setattr(bf, "fetch_minutes", boom)
    cleared = []
    monkeypatch.setattr(bf, "clear_all", lambda: cleared.append(1))
    bf.run_once()  # must not raise, must not clear on a failed fetch
    assert cleared == []


def test_one_dead_clock_does_not_block_others(monkeypatch):
    seen = []

    def ok(ip, lines):
        seen.append(ip)

    def dead(ip, lines):
        raise requests.ConnectTimeout("timeout")

    monkeypatch.setattr(bf, "PUSHERS", {"awtrix": dead, "awtrix_tc002": ok})
    assert bf.push_all(["102 1MIN"]) == 1
    assert seen == ["10.0.0.2"]
