"""按台站汇总（GET /drills/{drill_id}/station-summary）集成测试。

覆盖：一对互证分别计入两端台站、未配对只计入本台、对方呼号不产生
台站项、呼号升序、覆盖率计算（含非整数值）、当前版本与封存状态、
只读语义（不改日志/结果/版本/快照）、空演练空汇总、未知演练 404。
"""

from __future__ import annotations

import os
import tempfile

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(monkeypatch):
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    monkeypatch.setenv("QSO_DB_PATH", tmp.name)
    from app import database  # noqa: F401
    from app.main import app

    with TestClient(app) as c:
        yield c
    for suffix in ("", "-wal", "-shm"):
        try:
            os.unlink(tmp.name + suffix)
        except OSError:
            pass


def _log(no, station, counterpart, t, band="40m", mode="SSB"):
    return {
        "record_no": no,
        "station": station,
        "counterpart": counterpart,
        "time": t,
        "band": band,
        "mode": mode,
    }


def _seed_mixed(client, drill="SUM"):
    """R1-R2 配对；R3 模式不同落单；R4 缺少对方日志落单（BH4DDD 无日志）。"""
    client.post(
        "/drills",
        json={"drill_id": drill, "allowed_time_diff_seconds": 10},
    )
    r = client.post(f"/drills/{drill}/logs", json={"logs": [
        _log("R1", "BH1AAA", "BH2BBB", "2026-10-04T00:00:00+00:00"),
        _log("R2", "BH2BBB", "BH1AAA", "2026-10-04T00:00:05+00:00"),
        _log("R3", "BH1AAA", "BH2BBB", "2026-10-04T00:00:01+00:00",
             mode="CW"),
        _log("R4", "BH3CCC", "BH4DDD", "2026-10-04T00:00:00+00:00"),
    ]})
    assert r.status_code == 200, r.text
    return r.json()["version"]


def test_station_summary_counts_pair_into_both_stations(client):
    version = _seed_mixed(client)

    r = client.get("/drills/SUM/station-summary")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["drill_id"] == "SUM"
    assert body["version"] == version
    assert body["sealed"] is False

    # 台站按呼号升序；仅在对方呼号中出现的 BH4DDD 不产生汇总项。
    assert [s["station"] for s in body["stations"]] == [
        "BH1AAA", "BH2BBB", "BH3CCC"
    ]
    by_station = {s["station"]: s for s in body["stations"]}

    # R1（配对）与 R3（未配对）都计入本台 BH1AAA。
    assert by_station["BH1AAA"] == {
        "station": "BH1AAA",
        "total_logs": 2,
        "paired_logs": 1,
        "unpaired_logs": 1,
        "paired_coverage": 0.5,
    }
    # 一对互证的另一端 R2 计入 BH2BBB。
    assert by_station["BH2BBB"] == {
        "station": "BH2BBB",
        "total_logs": 1,
        "paired_logs": 1,
        "unpaired_logs": 0,
        "paired_coverage": 1.0,
    }
    # 未配对的 R4 只计入其本台 BH3CCC。
    assert by_station["BH3CCC"] == {
        "station": "BH3CCC",
        "total_logs": 1,
        "paired_logs": 0,
        "unpaired_logs": 1,
        "paired_coverage": 0.0,
    }


def test_station_summary_fractional_coverage_rounded(client):
    client.post("/drills", json={"drill_id": "FRAC",
                                 "allowed_time_diff_seconds": 10})
    client.post("/drills/FRAC/logs", json={"logs": [
        _log("P1", "BH9III", "BH8HHH", "2026-10-04T00:00:00+00:00"),
        _log("Q1", "BH8HHH", "BH9III", "2026-10-04T00:00:03+00:00"),
        _log("P2", "BH9III", "BH8HHH", "2026-10-04T00:00:00+00:00",
             mode="CW"),
        _log("P3", "BH9III", "BH7GGG", "2026-10-04T00:00:00+00:00"),
    ]})
    body = client.get("/drills/FRAC/station-summary").json()
    by_station = {s["station"]: s for s in body["stations"]}
    # 1/3 保留 6 位小数。
    assert by_station["BH9III"]["total_logs"] == 3
    assert by_station["BH9III"]["paired_logs"] == 1
    assert by_station["BH9III"]["unpaired_logs"] == 2
    assert by_station["BH9III"]["paired_coverage"] == 0.333333


def test_station_summary_empty_drill_returns_empty(client):
    client.post("/drills", json={"drill_id": "EMPTY",
                                 "allowed_time_diff_seconds": 10})
    r = client.get("/drills/EMPTY/station-summary")
    assert r.status_code == 200
    body = r.json()
    assert body["version"] == 0
    assert body["sealed"] is False
    assert body["stations"] == []


def test_station_summary_unknown_drill_is_404(client):
    r = client.get("/drills/NOPE/station-summary")
    assert r.status_code == 404
    assert r.json()["error"] == "drill_not_found"


def test_station_summary_is_read_only(client):
    _seed_mixed(client)
    before = client.get("/drills/SUM/result").json()
    snapshots_before = client.get("/drills/SUM/snapshots").json()

    for _ in range(3):
        r = client.get("/drills/SUM/station-summary")
        assert r.status_code == 200

    after = client.get("/drills/SUM/result").json()
    assert after["version"] == before["version"]
    assert after["pairs"] == before["pairs"]
    assert after["unpaired"] == before["unpaired"]
    assert client.get("/drills/SUM/snapshots").json() == snapshots_before


def test_station_summary_reflects_writes_and_locks(client):
    client.post("/drills", json={"drill_id": "LK",
                                 "allowed_time_diff_seconds": 50})
    client.post("/drills/LK/logs", json={"logs": [
        _log("L1", "A", "B", "2026-10-04T00:00:00+00:00"),
        _log("L2", "A", "B", "2026-10-04T00:01:40+00:00"),
        _log("R1", "B", "A", "2026-10-04T00:00:00+00:00"),
        _log("R2", "B", "A", "2026-10-04T00:00:50+00:00"),
    ]})
    # 自由最优两对全部配对：两台站覆盖率均为 1。
    body = client.get("/drills/LK/station-summary").json()
    by_station = {s["station"]: s for s in body["stations"]}
    assert by_station["A"]["paired_logs"] == 2
    assert by_station["A"]["paired_coverage"] == 1.0
    assert by_station["B"]["paired_logs"] == 2

    # 锁定 L1-R2 后 L2/R1 落单：汇总随当前结果变化，版本同步。
    r = client.put("/drills/LK/locks", json={"record_a": "L1",
                                             "record_b": "R2"})
    assert r.status_code == 200, r.text
    body = client.get("/drills/LK/station-summary").json()
    assert body["version"] == r.json()["result"]["version"]
    by_station = {s["station"]: s for s in body["stations"]}
    assert by_station["A"] == {
        "station": "A", "total_logs": 2, "paired_logs": 1,
        "unpaired_logs": 1, "paired_coverage": 0.5,
    }
    assert by_station["B"] == {
        "station": "B", "total_logs": 2, "paired_logs": 1,
        "unpaired_logs": 1, "paired_coverage": 0.5,
    }


def test_station_summary_available_after_seal(client):
    _seed_mixed(client, drill="S")
    version = client.get("/drills/S/result").json()["version"]
    assert client.post("/drills/S/seal").status_code == 200

    r = client.get("/drills/S/station-summary")
    assert r.status_code == 200
    body = r.json()
    assert body["sealed"] is True
    assert body["version"] == version  # 封存不推进版本
    assert len(body["stations"]) == 3
