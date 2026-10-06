"""未互证日志缺口诊断测试。

覆盖四类归因（候选已占用 / 时差超限 / 波段或模式不符 / 缺少对方日志）、
“时差最小再按记录号最小”的对方记录选择、记录号排序、锁定占用、
以及 HTTP 只读语义（不改日志/结果/版本/快照、封存后可查、未知演练 404）。
"""

from __future__ import annotations

import os
import tempfile

import pytest
from fastapi.testclient import TestClient

from app.matching import (
    MICROS_PER_SECOND,
    Record,
    diagnose_gaps,
    match_records,
)


# --------------------------------------------------------------------------
# 算法层纯函数测试
# --------------------------------------------------------------------------
def rec(no, station, counterpart, t, band="40m", mode="SSB"):
    return Record(
        record_no=no,
        station=station,
        counterpart=counterpart,
        ts_micros=int(t * MICROS_PER_SECOND),
        band=band.upper(),
        mode=mode.upper(),
    )


def _diagnose(records, allowed, unpaired):
    return diagnose_gaps(records, allowed, unpaired)


def test_missing_counterpart_when_no_mutual_record():
    records = [
        rec("U1", "A", "B", 0),
        rec("X1", "Z", "Y", 0),  # 与 U1 不互指
    ]
    gaps = _diagnose(records, 10, ["U1"])
    assert len(gaps) == 1
    g = gaps[0]
    assert g.record_no == "U1"
    assert g.reason == "missing_counterpart_log"
    assert g.counterpart_record_no is None
    assert g.time_diff_seconds is None


def test_band_or_mode_mismatch_picks_min_diff_then_min_no():
    records = [
        rec("U1", "A", "B", 0, "40m", "SSB"),
        rec("V1", "B", "A", 3, "20m", "SSB"),  # 互指、波段不同
        rec("V2", "B", "A", 2, "40m", "CW"),   # 互指、模式不同（时差更小）
    ]
    gaps = _diagnose(records, 10, ["U1"])
    assert gaps[0].reason == "band_or_mode_mismatch"
    # 时差最小（2s < 3s）优先于记录号。
    assert gaps[0].counterpart_record_no == "V2"
    assert gaps[0].time_diff_seconds == 2

    # 时差相同时取记录号最小。
    records2 = [
        rec("U1", "A", "B", 0),
        rec("V2", "B", "A", 0, "40m", "CW"),
        rec("V1", "B", "A", 0, "20m", "SSB"),
    ]
    gaps2 = _diagnose(records2, 10, ["U1"])
    assert gaps2[0].reason == "band_or_mode_mismatch"
    assert gaps2[0].counterpart_record_no == "V1"
    assert gaps2[0].time_diff_seconds == 0


def test_time_diff_exceeded_picks_smallest_diff():
    records = [
        rec("U1", "A", "B", 0),
        rec("V1", "B", "A", 20),  # 互指、同波段同模式但时差 20s > 10s
        rec("V2", "B", "A", 15),
    ]
    gaps = _diagnose(records, 10, ["U1"])
    assert gaps[0].reason == "time_diff_exceeded"
    assert gaps[0].counterpart_record_no == "V2"
    assert gaps[0].time_diff_seconds == 15


def test_candidate_occupied_prefers_min_diff_over_record_no():
    # U1 在当前结果中未配对（由调用方传入），存在两个完全合格候选：
    # R2 记录号更大但时差更小，应取 R2。
    records = [
        rec("U1", "A", "B", 0),
        rec("R1", "B", "A", 5),
        rec("R2", "B", "A", 2),
    ]
    gaps = _diagnose(records, 10, ["U1"])
    assert gaps[0].reason == "candidate_occupied"
    assert gaps[0].counterpart_record_no == "R2"
    assert gaps[0].time_diff_seconds == 2

    # 时差并列时取记录号最小。
    records2 = [
        rec("U1", "A", "B", 0),
        rec("R2", "B", "A", 0),
        rec("R1", "B", "A", 0),
    ]
    gaps2 = _diagnose(records2, 10, ["U1"])
    assert gaps2[0].reason == "candidate_occupied"
    assert gaps2[0].counterpart_record_no == "R1"
    assert gaps2[0].time_diff_seconds == 0


def test_attribution_priority_from_actual_match_result():
    """四类场景混合：unpaired 直接取自 match_records 的全场最优结果。"""
    allowed = 10
    records = [
        # 候选已占用：X2 的合格候选 Y1 被 X1 占用（最优解配 X1-Y1）
        rec("X1", "A", "B", 0),
        rec("X2", "A", "B", 1),
        rec("Y1", "B", "A", 0),
        # 时差超限：T1/T2 互指同波段同模式，时差 15s > 10s
        rec("T1", "C", "D", 0),
        rec("T2", "D", "C", 15),
        # 波段或模式不符：M1(40m SSB) vs M2(20m SSB)
        rec("M1", "E", "F", 0, "40m", "SSB"),
        rec("M2", "F", "E", 0, "20m", "SSB"),
        # 缺少对方日志
        rec("Z1", "G", "H", 0),
    ]
    result = match_records(records, allowed)
    assert [(p.record_a, p.record_b) for p in result.pairs] == [("X1", "Y1")]

    gaps = diagnose_gaps(records, allowed, result.unpaired)
    # 按记录号排序
    assert [g.record_no for g in gaps] == ["M1", "M2", "T1", "T2", "X2", "Z1"]

    by_no = {g.record_no: g for g in gaps}
    assert by_no["X2"].reason == "candidate_occupied"
    assert by_no["X2"].counterpart_record_no == "Y1"
    assert by_no["X2"].time_diff_seconds == 1

    assert by_no["T1"].reason == "time_diff_exceeded"
    assert by_no["T1"].counterpart_record_no == "T2"
    assert by_no["T1"].time_diff_seconds == 15
    assert by_no["T2"].reason == "time_diff_exceeded"
    assert by_no["T2"].counterpart_record_no == "T1"

    assert by_no["M1"].reason == "band_or_mode_mismatch"
    assert by_no["M1"].counterpart_record_no == "M2"
    assert by_no["M2"].reason == "band_or_mode_mismatch"
    assert by_no["M2"].counterpart_record_no == "M1"

    assert by_no["Z1"].reason == "missing_counterpart_log"
    assert by_no["Z1"].counterpart_record_no is None
    assert by_no["Z1"].time_diff_seconds is None


def test_forced_locks_can_make_eligible_candidates_occupied():
    # L1/R2 锁定后，L2 与 R1 均未配对，但其合格候选分别是
    # 被占用的 R2（锁定对）与 L1（锁定对）-> 候选已占用。
    allowed = 50
    records = [
        rec("L1", "A", "B", 0),
        rec("L2", "A", "B", 100),
        rec("R1", "B", "A", 0),
        rec("R2", "B", "A", 50),
    ]
    result = match_records(records, allowed, locked_keys=[("L1", "R2")])
    assert [(p.record_a, p.record_b) for p in result.pairs] == [("L1", "R2")]
    gaps = {g.record_no: g for g in diagnose_gaps(records, allowed, result.unpaired)}
    assert gaps["L2"].reason == "candidate_occupied"
    assert gaps["L2"].counterpart_record_no == "R2"
    assert gaps["L2"].time_diff_seconds == 50
    assert gaps["R1"].reason == "candidate_occupied"
    assert gaps["R1"].counterpart_record_no == "L1"
    assert gaps["R1"].time_diff_seconds == 0


def test_gaps_sorted_by_record_no():
    records = [rec("Z9", "A", "B", 0), rec("A0", "C", "D", 0)]
    gaps = _diagnose(records, 10, ["Z9", "A0"])
    assert [g.record_no for g in gaps] == ["A0", "Z9"]


# --------------------------------------------------------------------------
# HTTP 集成测试
# --------------------------------------------------------------------------
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


_MIXED_LOGS = [
    _log("X1", "BH1AAA", "BH2BBB", "2026-10-04T00:00:00+00:00"),
    _log("X2", "BH1AAA", "BH2BBB", "2026-10-04T00:00:01+00:00"),
    _log("Y1", "BH2BBB", "BH1AAA", "2026-10-04T00:00:00+00:00"),
    _log("T1", "BH3CCC", "BH4DDD", "2026-10-04T00:00:00+00:00"),
    _log("T2", "BH4DDD", "BH3CCC", "2026-10-04T00:00:15+00:00"),
    _log("M1", "BH5EEE", "BH6FFF", "2026-10-04T00:00:00+00:00", "40m", "SSB"),
    _log("M2", "BH6FFF", "BH5EEE", "2026-10-04T00:00:00+00:00", "20m", "SSB"),
    _log("Z1", "BH7GGG", "BH8HHH", "2026-10-04T00:00:00+00:00"),
]


def test_gap_diagnostics_endpoint_full_classification(client):
    client.post(
        "/drills",
        json={"drill_id": "GAP", "allowed_time_diff_seconds": 10},
    )
    r = client.post("/drills/GAP/logs", json={"logs": _MIXED_LOGS})
    assert r.status_code == 200, r.text
    version = r.json()["version"]

    r = client.get("/drills/GAP/gap-diagnostics")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["drill_id"] == "GAP"
    assert body["version"] == version
    assert body["sealed"] is False
    assert body["allowed_time_diff_seconds"] == 10
    assert [g["record_no"] for g in body["gaps"]] == [
        "M1", "M2", "T1", "T2", "X2", "Z1"
    ]

    by_no = {g["record_no"]: g for g in body["gaps"]}
    assert by_no["X2"] == {
        "record_no": "X2",
        "reason": "candidate_occupied",
        "reason_label": "候选已占用",
        "counterpart_record_no": "Y1",
        "time_diff_seconds": 1,
    }
    assert by_no["T1"]["reason"] == "time_diff_exceeded"
    assert by_no["T1"]["counterpart_record_no"] == "T2"
    assert by_no["T1"]["time_diff_seconds"] == 15
    assert by_no["M1"]["reason"] == "band_or_mode_mismatch"
    assert by_no["M1"]["counterpart_record_no"] == "M2"
    assert by_no["Z1"] == {
        "record_no": "Z1",
        "reason": "missing_counterpart_log",
        "reason_label": "缺少对方日志",
        "counterpart_record_no": None,
        "time_diff_seconds": None,
    }


def test_gap_diagnostics_is_read_only(client):
    client.post(
        "/drills",
        json={"drill_id": "GAP", "allowed_time_diff_seconds": 10},
    )
    client.post("/drills/GAP/logs", json={"logs": _MIXED_LOGS})
    before = client.get("/drills/GAP/result").json()
    snapshots_before = client.get("/drills/GAP/snapshots").json()

    for _ in range(3):
        r = client.get("/drills/GAP/gap-diagnostics")
        assert r.status_code == 200

    after = client.get("/drills/GAP/result").json()
    assert after["version"] == before["version"]
    assert after["pairs"] == before["pairs"]
    assert after["unpaired"] == before["unpaired"]
    assert client.get("/drills/GAP/snapshots").json() == snapshots_before


def test_gap_diagnostics_empty_drill(client):
    client.post(
        "/drills",
        json={"drill_id": "EMPTY", "allowed_time_diff_seconds": 10},
    )
    r = client.get("/drills/EMPTY/gap-diagnostics")
    assert r.status_code == 200
    body = r.json()
    assert body["version"] == 0
    assert body["gaps"] == []


def test_gap_diagnostics_reflects_corrections_and_locks(client):
    client.post(
        "/drills",
        json={"drill_id": "LK", "allowed_time_diff_seconds": 50},
    )
    client.post("/drills/LK/logs", json={"logs": [
        _log("L1", "A", "B", "2026-10-04T00:00:00+00:00"),
        _log("L2", "A", "B", "2026-10-04T00:01:40+00:00"),
        _log("R1", "B", "A", "2026-10-04T00:00:00+00:00"),
        _log("R2", "B", "A", "2026-10-04T00:00:50+00:00"),
    ]})
    # 自由最优两对全部配对 -> 无缺口。
    assert client.get("/drills/LK/gap-diagnostics").json()["gaps"] == []

    # 锁定 L1-R2：L2/R1 落单，但合格候选被锁定占用。
    r = client.put("/drills/LK/locks", json={"record_a": "L1", "record_b": "R2"})
    assert r.status_code == 200, r.text
    body = client.get("/drills/LK/gap-diagnostics").json()
    assert body["version"] == r.json()["result"]["version"]
    gaps = {g["record_no"]: g for g in body["gaps"]}
    assert set(gaps) == {"L2", "R1"}
    assert gaps["L2"]["reason"] == "candidate_occupied"
    assert gaps["L2"]["counterpart_record_no"] == "R2"
    assert gaps["R1"]["counterpart_record_no"] == "L1"

    # 解锁后恢复全配对 -> 缺口清空（诊断反映现态）。
    assert client.post(
        "/drills/LK/locks/unlock", json={"record_a": "L1", "record_b": "R2"}
    ).status_code == 200
    assert client.get("/drills/LK/gap-diagnostics").json()["gaps"] == []


def test_gap_diagnostics_available_after_seal(client):
    client.post(
        "/drills",
        json={"drill_id": "S", "allowed_time_diff_seconds": 10},
    )
    client.post("/drills/S/logs", json={"logs": [
        _log("Z1", "A", "B", "2026-10-04T00:00:00+00:00"),
    ]})
    assert client.post("/drills/S/seal").status_code == 200

    r = client.get("/drills/S/gap-diagnostics")
    assert r.status_code == 200
    body = r.json()
    assert body["sealed"] is True
    assert len(body["gaps"]) == 1
    assert body["gaps"][0]["reason"] == "missing_counterpart_log"


def test_gap_diagnostics_unknown_drill_is_404(client):
    r = client.get("/drills/NOPE/gap-diagnostics")
    assert r.status_code == 404
    assert r.json()["error"] == "drill_not_found"
