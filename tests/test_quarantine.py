"""争议日志隔离（批量隔离/恢复/隔离清单）HTTP 集成测试。

覆盖：隔离排除配对/未配对/缺口诊断/台站汇总、恢复后按当前日志重解、
隔离期间更正保留状态、隔离与锁定/迁移互斥、整批拒绝（记录缺失/编号
重复/涉及锁定/已封存）、仅实际变化推进一次版本并同事务写入快照、
重复操作不推进版本、历史快照保持原貌、预演与确认遵守隔离状态、
既有库升级迁移。
"""

from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import threading

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


def _make_drill(client, drill="Q", allowed=10):
    r = client.post(
        "/drills", json={"drill_id": drill, "allowed_time_diff_seconds": allowed}
    )
    assert r.status_code == 201, r.text


def _seed_five(client, drill="Q", allowed=10):
    """R1-R2 配对（5s）；R3-R4 配对（8s）；R5 单边落单。批量写入后 v1。"""
    _make_drill(client, drill, allowed)
    r = client.post(f"/drills/{drill}/logs", json={"logs": [
        _log("R1", "BH1AAA", "BH2BBB", "2026-10-04T00:00:00+00:00"),
        _log("R2", "BH2BBB", "BH1AAA", "2026-10-04T00:00:05+00:00"),
        _log("R3", "BH3CCC", "BH4DDD", "2026-10-04T00:00:00+00:00"),
        _log("R4", "BH4DDD", "BH3CCC", "2026-10-04T00:00:08+00:00"),
        _log("R5", "BH5EEE", "BH6FFF", "2026-10-04T00:00:00+00:00"),
    ]})
    assert r.status_code == 200, r.text
    assert [tuple(p) for p in sorted(
        (p["record_a"], p["record_b"]) for p in r.json()["pairs"]
    )] == [("R1", "R2"), ("R3", "R4")]
    assert r.json()["unpaired"] == ["R5"]
    return r.json()["version"]


def _pair_keys(pairs):
    return sorted((p["record_a"], p["record_b"]) for p in pairs)


def _quarantine_nos(body):
    return [q["record_no"] for q in body["quarantine"]]


# --------------------------------------------------------------------------
# 隔离/恢复基本语义
# --------------------------------------------------------------------------
def test_quarantine_excludes_from_result_and_advances_once(client):
    _seed_five(client)

    r = client.put("/drills/Q/quarantine",
                   json={"record_nos": ["R3", "R5"]})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["drill_id"] == "Q"
    assert body["affected_record_nos"] == ["R3", "R5"]
    # 隔离清单按记录号升序，带隔离时间
    assert _quarantine_nos(body) == ["R3", "R5"]
    assert all(q["quarantined_at"] for q in body["quarantine"])
    # 整批只推进一次版本；被隔离记录不配对、不进入未配对清单
    # （R3 被隔离后其对方 R4 落单，R5 被隔离不进入未配对清单）
    result = body["result"]
    assert result["version"] == 2
    assert _pair_keys(result["pairs"]) == [("R1", "R2")]
    assert result["unpaired"] == ["R4"]
    assert result["total_time_diff_seconds"] == 5

    # 结果查询端点同步体现；日志本身保留（不删除、不改写原字段）
    got = client.get("/drills/Q/result").json()
    assert got["version"] == 2 and got["unpaired"] == ["R4"]
    logs = {l["record_no"]: l for l in client.get("/drills/Q/logs").json()}
    assert set(logs) == {"R1", "R2", "R3", "R4", "R5"}
    assert logs["R3"]["quarantined"] is True
    assert logs["R5"]["quarantined"] is True
    assert logs["R1"]["quarantined"] is False
    assert logs["R3"]["station"] == "BH3CCC"  # 原字段未改写

    # 隔离清单查询：按记录号升序 + 最新结果
    lst = client.get("/drills/Q/quarantine")
    assert lst.status_code == 200
    assert _quarantine_nos(lst.json()) == ["R3", "R5"]
    assert lst.json()["result"]["version"] == 2


def test_quarantine_idempotent_retransmit_no_version_advance(client):
    _seed_five(client)
    b1 = client.put("/drills/Q/quarantine", json={"record_nos": ["R3"]}).json()
    assert b1["result"]["version"] == 2

    # 重复隔离同一记录：无实际状态变化，版本不推进、无新快照
    b2 = client.put("/drills/Q/quarantine", json={"record_nos": ["R3"]}).json()
    assert b2["affected_record_nos"] == []
    assert b2["result"]["version"] == 2
    assert _quarantine_nos(b2) == ["R3"]
    assert client.get("/drills/Q/snapshots").json()["versions"] == [0, 1, 2]

    # 混合批次：已隔离的跳过、新记录隔离，仍只推进一次版本
    b3 = client.put(
        "/drills/Q/quarantine", json={"record_nos": ["R3", "R4", "R5"]}
    ).json()
    assert b3["affected_record_nos"] == ["R4", "R5"]
    assert b3["result"]["version"] == 3
    assert _quarantine_nos(b3) == ["R3", "R4", "R5"]


def test_restore_recomputes_with_current_logs(client):
    _seed_five(client)
    client.put("/drills/Q/quarantine", json={"record_nos": ["R3", "R5"]})

    r = client.post("/drills/Q/quarantine/restore",
                    json={"record_nos": ["R3"]})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["affected_record_nos"] == ["R3"]
    assert _quarantine_nos(body) == ["R5"]
    # 恢复后按当前日志重新求解：R3-R4 重新配对，版本 +1
    assert body["result"]["version"] == 3
    assert _pair_keys(body["result"]["pairs"]) == [("R1", "R2"), ("R3", "R4")]
    assert body["result"]["unpaired"] == []

    # 重复恢复（含从未隔离的记录）：无实际变化，不推进版本
    r = client.post("/drills/Q/quarantine/restore",
                    json={"record_nos": ["R3", "R1"]})
    assert r.status_code == 200
    assert r.json()["affected_record_nos"] == []
    assert r.json()["result"]["version"] == 3
    assert client.get("/drills/Q/snapshots").json()["versions"] == [0, 1, 2, 3]

    # 恢复最后一条：清单清空
    r = client.post("/drills/Q/quarantine/restore",
                    json={"record_nos": ["R5"]})
    assert r.json()["affected_record_nos"] == ["R5"]
    assert r.json()["quarantine"] == []
    assert r.json()["result"]["version"] == 4
    assert r.json()["result"]["unpaired"] == ["R5"]


def test_quarantine_all_records_yields_empty_result(client):
    _seed_five(client)
    r = client.put(
        "/drills/Q/quarantine",
        json={"record_nos": ["R1", "R2", "R3", "R4", "R5"]},
    )
    assert r.status_code == 200
    result = r.json()["result"]
    assert result["pairs"] == [] and result["unpaired"] == []
    assert result["total_time_diff_seconds"] == 0
    # 台站汇总与缺口诊断同样为空
    assert client.get("/drills/Q/station-summary").json()["stations"] == []
    assert client.get("/drills/Q/gap-diagnostics").json()["gaps"] == []


# --------------------------------------------------------------------------
# 整批拒绝：记录不存在 / 编号重复 / 涉及锁定 / 已封存
# --------------------------------------------------------------------------
def test_quarantine_batch_rejections_keep_state(client):
    _seed_five(client)

    # 记录不存在 -> 404，整批拒绝
    r = client.put("/drills/Q/quarantine",
                   json={"record_nos": ["R1", "NOPE"]})
    assert r.status_code == 404
    assert r.json()["error"] == "quarantine_record_missing"

    # 批次内编号重复（含空白差异）-> 409
    r = client.put("/drills/Q/quarantine",
                   json={"record_nos": ["R1", " R1 "]})
    assert r.status_code == 409
    assert r.json()["error"] == "duplicate_record_in_quarantine"

    # 空列表 -> 422；空白记录号 -> 422
    assert client.put(
        "/drills/Q/quarantine", json={"record_nos": []}
    ).status_code == 422
    assert client.put(
        "/drills/Q/quarantine", json={"record_nos": ["  "]}
    ).status_code == 422

    # 恢复批次同样适用上述整批校验
    r = client.post("/drills/Q/quarantine/restore",
                    json={"record_nos": ["R9"]})
    assert r.status_code == 404
    assert r.json()["error"] == "quarantine_record_missing"
    r = client.post("/drills/Q/quarantine/restore",
                    json={"record_nos": ["R1", "R1"]})
    assert r.status_code == 409
    assert r.json()["error"] == "duplicate_record_in_quarantine"

    # 未知演练 -> 404
    r = client.put("/drills/NOPE/quarantine", json={"record_nos": ["R1"]})
    assert r.status_code == 404 and r.json()["error"] == "drill_not_found"
    r = client.post("/drills/NOPE/quarantine/restore",
                    json={"record_nos": ["R1"]})
    assert r.status_code == 404 and r.json()["error"] == "drill_not_found"
    r = client.get("/drills/NOPE/quarantine")
    assert r.status_code == 404 and r.json()["error"] == "drill_not_found"

    # 全部拒绝后状态不变：无隔离、版本仍为 1、无新快照
    assert client.get("/drills/Q/quarantine").json()["quarantine"] == []
    assert client.get("/drills/Q/result").json()["version"] == 1
    assert client.get("/drills/Q/snapshots").json()["versions"] == [0, 1]


def test_quarantine_sealed_drill_rejected_but_list_queryable(client):
    _seed_five(client)
    client.put("/drills/Q/quarantine", json={"record_nos": ["R3"]})
    v = client.get("/drills/Q/result").json()["version"]
    assert client.post("/drills/Q/seal").status_code == 200

    r = client.put("/drills/Q/quarantine", json={"record_nos": ["R4"]})
    assert r.status_code == 409 and r.json()["error"] == "drill_sealed"
    r = client.post("/drills/Q/quarantine/restore",
                    json={"record_nos": ["R3"]})
    assert r.status_code == 409 and r.json()["error"] == "drill_sealed"

    # 封存后仍可查询隔离清单；状态不变
    lst = client.get("/drills/Q/quarantine")
    assert lst.status_code == 200
    assert _quarantine_nos(lst.json()) == ["R3"]
    assert lst.json()["result"]["sealed"] is True
    assert client.get("/drills/Q/result").json()["version"] == v


# --------------------------------------------------------------------------
# 隔离与锁定互斥
# --------------------------------------------------------------------------
def test_locked_member_cannot_be_quarantined(client):
    _seed_five(client)
    client.put("/drills/Q/locks", json={"record_a": "R1", "record_b": "R2"})
    v = client.get("/drills/Q/result").json()["version"]

    # 已有锁定成员不得隔离：整批拒绝
    r = client.put("/drills/Q/quarantine",
                   json={"record_nos": ["R3", "R2"]})
    assert r.status_code == 409
    assert r.json()["error"] == "quarantine_record_locked"
    # 恢复批次涉及锁定成员同样整批拒绝
    r = client.post("/drills/Q/quarantine/restore",
                    json={"record_nos": ["R1"]})
    assert r.status_code == 409
    assert r.json()["error"] == "quarantine_record_locked"

    # 状态不变：无隔离、锁定仍在、版本不变
    assert client.get("/drills/Q/quarantine").json()["quarantine"] == []
    assert client.get("/drills/Q/locks").json()["locks"]
    assert client.get("/drills/Q/result").json()["version"] == v

    # 解除锁定后即可隔离
    client.post("/drills/Q/locks/unlock",
                json={"record_a": "R1", "record_b": "R2"})
    r = client.put("/drills/Q/quarantine", json={"record_nos": ["R2"]})
    assert r.status_code == 200
    assert _quarantine_nos(r.json()) == ["R2"]


def test_quarantined_record_cannot_be_locked(client):
    _seed_five(client)
    client.put("/drills/Q/quarantine", json={"record_nos": ["R2"]})
    v = client.get("/drills/Q/result").json()["version"]

    r = client.put("/drills/Q/locks",
                   json={"record_a": "R1", "record_b": "R2"})
    assert r.status_code == 409
    assert r.json()["error"] == "lock_record_quarantined"
    assert client.get("/drills/Q/locks").json()["locks"] == []
    assert client.get("/drills/Q/result").json()["version"] == v

    # 恢复后即可锁定
    client.post("/drills/Q/quarantine/restore", json={"record_nos": ["R2"]})
    r = client.put("/drills/Q/locks",
                   json={"record_a": "R1", "record_b": "R2"})
    assert r.status_code == 200
    assert _pair_keys(r.json()["result"]["pairs"])[0] == ("R1", "R2")


def test_quarantined_record_cannot_be_transferred(client):
    _seed_five(client, drill="SRC")
    _make_drill(client, drill="DST")
    client.put("/drills/SRC/quarantine", json={"record_nos": ["R5"]})
    v_src = client.get("/drills/SRC/result").json()["version"]

    r = client.post("/drills/SRC/transfer",
                    json={"target_drill_id": "DST", "record_nos": ["R5"]})
    assert r.status_code == 409
    assert r.json()["error"] == "transfer_record_quarantined"
    # 两端状态不变
    assert {l["record_no"] for l in client.get("/drills/SRC/logs").json()} == {
        "R1", "R2", "R3", "R4", "R5"
    }
    assert client.get("/drills/DST/logs").json() == []
    assert client.get("/drills/SRC/result").json()["version"] == v_src
    assert client.get("/drills/DST/result").json()["version"] == 0

    # 恢复后可正常迁移
    client.post("/drills/SRC/quarantine/restore",
                json={"record_nos": ["R5"]})
    r = client.post("/drills/SRC/transfer",
                    json={"target_drill_id": "DST", "record_nos": ["R5"]})
    assert r.status_code == 200
    assert r.json()["moved_record_nos"] == ["R5"]


# --------------------------------------------------------------------------
# 隔离期间更正日志：状态保留；恢复后按当前日志求解
# --------------------------------------------------------------------------
def test_correction_during_quarantine_preserves_status(client):
    _seed_five(client)
    # 隔离 R2（R1 的互证对象）：R1 落单
    client.put("/drills/Q/quarantine", json={"record_nos": ["R2"]})
    got = client.get("/drills/Q/result").json()
    assert _pair_keys(got["pairs"]) == [("R3", "R4")]
    assert got["unpaired"] == ["R1", "R5"]

    # 隔离期间按原接口更正 R2（5s -> 3s）：写入受理、版本推进，
    # 但 R2 仍被隔离，不参与配对
    r = client.put(
        "/drills/Q/logs/R2",
        json=_log("R2", "BH2BBB", "BH1AAA", "2026-10-04T00:00:03+00:00"),
    )
    assert r.status_code == 200
    assert r.json()["version"] == 3
    assert _pair_keys(r.json()["pairs"]) == [("R3", "R4")]
    logs = {l["record_no"]: l for l in client.get("/drills/Q/logs").json()}
    assert logs["R2"]["quarantined"] is True
    assert logs["R2"]["time"] == "2026-10-04T00:00:03+00:00"

    # 批量写入更正被隔离记录同样受理且保留隔离状态
    r = client.post("/drills/Q/logs", json={"logs": [
        _log("R2", "BH2BBB", "BH1AAA", "2026-10-04T00:00:02+00:00"),
    ]})
    assert r.status_code == 200
    assert _pair_keys(r.json()["pairs"]) == [("R3", "R4")]

    # 恢复后按当前日志重新求解：以更正后的 2s 时差配对
    r = client.post("/drills/Q/quarantine/restore",
                    json={"record_nos": ["R2"]})
    assert r.status_code == 200
    pairs = {p["record_a"] + p["record_b"]: p for p in r.json()["result"]["pairs"]}
    assert pairs["R1R2"]["time_diff_seconds"] == 2
    assert _quarantine_nos(r.json()) == []


# --------------------------------------------------------------------------
# 缺口诊断与台站汇总排除隔离记录
# --------------------------------------------------------------------------
def test_gap_diagnostics_ignores_quarantined(client):
    _make_drill(client, drill="G", allowed=10)
    # X1/X2 互指但时差 100s 超限；X3 无任何互指
    client.post("/drills/G/logs", json={"logs": [
        _log("X1", "A", "B", "2026-10-04T00:00:00+00:00"),
        _log("X2", "B", "A", "2026-10-04T00:01:40+00:00"),
        _log("X3", "C", "D", "2026-10-04T00:00:00+00:00"),
    ]})
    gaps = {g["record_no"]: g for g in
            client.get("/drills/G/gap-diagnostics").json()["gaps"]}
    assert gaps["X1"]["reason"] == "time_diff_exceeded"
    assert gaps["X1"]["counterpart_record_no"] == "X2"

    # 隔离 X2 后：X2 不出现在诊断中；X1 的候选仅限未隔离日志 -> 缺少对方日志
    client.put("/drills/G/quarantine", json={"record_nos": ["X2"]})
    body = client.get("/drills/G/gap-diagnostics").json()
    gaps = {g["record_no"]: g for g in body["gaps"]}
    assert "X2" not in gaps
    assert gaps["X1"]["reason"] == "missing_counterpart_log"
    assert gaps["X1"]["counterpart_record_no"] is None
    assert gaps["X1"]["time_diff_seconds"] is None
    assert gaps["X3"]["reason"] == "missing_counterpart_log"


def test_station_summary_excludes_quarantined(client):
    _seed_five(client)
    # 隔离 R3（BH3CCC 唯一日志）与 R5（BH5EEE 唯一日志）
    client.put("/drills/Q/quarantine", json={"record_nos": ["R3", "R5"]})
    body = client.get("/drills/Q/station-summary").json()
    by_station = {s["station"]: s for s in body["stations"]}
    # 全部被隔离的台站不产生汇总项
    assert set(by_station) == {"BH1AAA", "BH2BBB", "BH4DDD"}
    assert by_station["BH1AAA"] == {
        "station": "BH1AAA", "total_logs": 1, "paired_logs": 1,
        "unpaired_logs": 0, "paired_coverage": 1.0,
    }
    # R4 因 R3 被隔离而落单，只计入其本台
    assert by_station["BH4DDD"] == {
        "station": "BH4DDD", "total_logs": 1, "paired_logs": 0,
        "unpaired_logs": 1, "paired_coverage": 0.0,
    }


# --------------------------------------------------------------------------
# 版本快照：隔离清单同事务写入；历史快照保持原貌；比较含隔离差异
# --------------------------------------------------------------------------
def test_quarantine_snapshots_and_diff(client):
    _seed_five(client)  # v1
    client.put("/drills/Q/quarantine",
               json={"record_nos": ["R3", "R5"]})  # v2
    client.post("/drills/Q/quarantine/restore",
                json={"record_nos": ["R5"]})  # v3

    assert client.get("/drills/Q/snapshots").json()["versions"] == [0, 1, 2, 3]
    # 各版快照留存当时的隔离清单
    assert client.get("/drills/Q/snapshots/0").json()["quarantine"] == []
    assert client.get("/drills/Q/snapshots/1").json()["quarantine"] == []
    v2 = client.get("/drills/Q/snapshots/2").json()
    assert _quarantine_nos(v2) == ["R3", "R5"]
    assert _pair_keys(v2["pairs"]) == [("R1", "R2")]
    assert v2["unpaired"] == ["R4"]  # R3 被隔离后其对方 R4 落单
    v3 = client.get("/drills/Q/snapshots/3").json()
    assert _quarantine_nos(v3) == ["R3"]
    # R3 仍被隔离：R4 继续落单，恢复的 R5 回到未配对清单
    assert _pair_keys(v3["pairs"]) == [("R1", "R2")]
    assert v3["unpaired"] == ["R4", "R5"]

    # 版本比较：隔离新增/移除按记录号升序单列
    d = client.get("/drills/Q/snapshots/1/diff/2").json()
    assert _quarantine_nos({"quarantine": d["quarantine"]["added"]}) == [
        "R3", "R5"
    ]
    assert d["quarantine"]["removed"] == []
    d = client.get("/drills/Q/snapshots/2/diff/3").json()
    assert d["quarantine"]["added"] == []
    assert _quarantine_nos({"quarantine": d["quarantine"]["removed"]}) == ["R5"]
    # 相同版本比较：隔离差异为空
    d = client.get("/drills/Q/snapshots/3/diff/3").json()
    assert d["quarantine"] == {"added": [], "removed": []}

    # 历史快照保持原貌：后续操作不改写已留存内容
    before = client.get("/drills/Q/snapshots/2").json()
    client.put("/drills/Q/quarantine", json={"record_nos": ["R1"]})
    client.post("/drills/Q/quarantine/restore",
                json={"record_nos": ["R1", "R3"]})
    assert client.get("/drills/Q/snapshots/2").json() == before


def test_failed_quarantine_persists_no_snapshot(client):
    _seed_five(client)
    client.put("/drills/Q/quarantine", json={"record_nos": ["R3"]})  # v2
    # 各类整批拒绝均不产生新版本/新快照
    client.put("/drills/Q/quarantine", json={"record_nos": ["NOPE"]})
    client.put("/drills/Q/quarantine", json={"record_nos": ["R1", "R1"]})
    client.put("/drills/Q/locks", json={"record_a": "R1", "record_b": "R2"})
    client.put("/drills/Q/quarantine", json={"record_nos": ["R1"]})
    assert client.get("/drills/Q/snapshots").json()["versions"] == [0, 1, 2, 3]
    # v3 是锁定推进的版本；其隔离清单仍为 [R3]
    assert _quarantine_nos(client.get("/drills/Q/snapshots/3").json()) == ["R3"]


# --------------------------------------------------------------------------
# 批量预演与确认遵守隔离状态
# --------------------------------------------------------------------------
def test_preview_and_confirm_respect_quarantine(client):
    _seed_five(client)
    client.put("/drills/Q/quarantine", json={"record_nos": ["R2"]})  # v2

    # 预演更正被隔离的 R2：拟写入结果中 R2 仍不参与配对
    corrected = [_log("R2", "BH2BBB", "BH1AAA", "2026-10-04T00:00:03+00:00")]
    r = client.post("/drills/Q/logs/preview", json={"logs": corrected})
    assert r.status_code == 200, r.text
    pv = r.json()
    assert pv["base_version"] == 2
    assert pv["would_advance_version"] is True
    assert pv["proposed_version"] == 3
    assert _pair_keys(pv["current_result"]["pairs"]) == [("R3", "R4")]
    assert _pair_keys(pv["proposed_result"]["pairs"]) == [("R3", "R4")]
    assert pv["proposed_result"]["unpaired"] == ["R1", "R5"]

    # 确认后原子落库：R2 更正生效但仍被隔离
    r = client.post("/drills/Q/logs/confirm", json={
        "logs": corrected,
        "base_version": pv["base_version"],
        "token": pv["token"],
    })
    assert r.status_code == 200, r.text
    assert r.json()["version"] == 3
    assert _pair_keys(r.json()["pairs"]) == [("R3", "R4")]
    logs = {l["record_no"]: l for l in client.get("/drills/Q/logs").json()}
    assert logs["R2"]["quarantined"] is True
    assert logs["R2"]["time"] == "2026-10-04T00:00:03+00:00"

    # 恢复后按更正后的当前日志求解：R1-R2 以 3s 时差配对
    r = client.post("/drills/Q/quarantine/restore",
                    json={"record_nos": ["R2"]})
    pairs = {p["record_a"] + p["record_b"]: p for p in r.json()["result"]["pairs"]}
    assert pairs["R1R2"]["time_diff_seconds"] == 3


def test_preview_new_log_for_quarantined_record_counterpart(client):
    """预演新增与被隔离记录互指的日志：候选仍仅限未隔离日志。"""
    _make_drill(client, drill="P", allowed=10)
    client.put("/drills/P/logs/A1",
               json=_log("A1", "A", "B", "2026-10-04T00:00:00+00:00"))
    client.put("/drills/P/quarantine", json={"record_nos": ["A1"]})

    # 预演新增 B1（与 A1 互指）：A1 被隔离，B1 仍落单
    r = client.post("/drills/P/logs/preview", json={"logs": [
        _log("B1", "B", "A", "2026-10-04T00:00:01+00:00"),
    ]})
    assert r.status_code == 200
    assert r.json()["proposed_result"]["pairs"] == []
    assert r.json()["proposed_result"]["unpaired"] == ["B1"]


# --------------------------------------------------------------------------
# 并发：重复隔离被串行化，版本只推进一次
# --------------------------------------------------------------------------
def test_concurrent_identical_quarantine_advances_once(client):
    _seed_five(client)
    results = []

    def quar():
        r = client.put("/drills/Q/quarantine", json={"record_nos": ["R3"]})
        results.append(r.status_code)

    threads = [threading.Thread(target=quar) for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert results == [200] * 5
    # 5 个相同批次并发：仅第一个实际隔离，版本只推进一次
    assert client.get("/drills/Q/result").json()["version"] == 2
    assert _quarantine_nos(client.get("/drills/Q/quarantine").json()) == ["R3"]
    assert client.get("/drills/Q/snapshots").json()["versions"] == [0, 1, 2]


# --------------------------------------------------------------------------
# 既有库升级：补 quarantine 表与 snapshots.quarantine_json 列
# --------------------------------------------------------------------------
def test_legacy_database_migration_adds_quarantine_support(monkeypatch):
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    monkeypatch.setenv("QSO_DB_PATH", tmp.name)

    # 旧版 schema：无 quarantine 表，snapshots 无 quarantine_json 列
    conn = sqlite3.connect(tmp.name)
    conn.executescript(
        """
        CREATE TABLE drills (
            drill_id TEXT PRIMARY KEY, allowed_diff_secs REAL NOT NULL
                CHECK (allowed_diff_secs >= 0),
            version INTEGER NOT NULL DEFAULT 0, sealed INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL, sealed_at TEXT);
        CREATE TABLE logs (
            drill_id TEXT NOT NULL, record_no TEXT NOT NULL, station TEXT NOT NULL,
            counterpart TEXT NOT NULL, ts_micros INTEGER NOT NULL,
            ts_iso TEXT NOT NULL, band TEXT NOT NULL, mode TEXT NOT NULL,
            updated_at TEXT NOT NULL, PRIMARY KEY (drill_id, record_no));
        CREATE TABLE results (
            drill_id TEXT PRIMARY KEY, version INTEGER NOT NULL,
            pairs_json TEXT NOT NULL, unpaired_json TEXT NOT NULL,
            total_time_diff_seconds REAL NOT NULL, recomputed_at TEXT NOT NULL);
        CREATE TABLE locks (
            drill_id TEXT NOT NULL, record_a TEXT NOT NULL, record_b TEXT NOT NULL,
            created_at TEXT NOT NULL,
            PRIMARY KEY (drill_id, record_a, record_b));
        CREATE TABLE snapshots (
            drill_id TEXT NOT NULL, version INTEGER NOT NULL,
            pairs_json TEXT NOT NULL, unpaired_json TEXT NOT NULL,
            total_time_diff_seconds REAL NOT NULL, locks_json TEXT NOT NULL,
            created_at TEXT NOT NULL, PRIMARY KEY (drill_id, version));
        """
    )
    import datetime as dt
    base = int(dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc).timestamp() * 1_000_000)
    conn.execute("INSERT INTO drills VALUES ('OLD', 50, 1, 0, 't0', NULL)")
    conn.execute(
        "INSERT INTO logs VALUES ('OLD', 'L1', 'A', 'B', ?, "
        "'2026-01-01T00:00:00+00:00', '40M', 'SSB', 't')",
        (base,),
    )
    conn.execute(
        "INSERT INTO results VALUES ('OLD', 1, '[]', '[\"L1\"]', 0.0, 't')"
    )
    conn.execute(
        "INSERT INTO snapshots VALUES ('OLD', 1, '[]', '[\"L1\"]', 0.0, "
        "'[]', 'tsnap')"
    )
    conn.commit()
    conn.close()

    from app import database
    from app.main import app

    database.init_db()  # 升级路径：补表、补列（幂等）
    database.init_db()  # 再次执行不报错
    with TestClient(app) as c:
        # 旧快照可取回，隔离清单按空清单呈现（历史内容保持原貌）
        snap = c.get("/drills/OLD/snapshots/1").json()
        assert snap["quarantine"] == []
        assert snap["unpaired"] == ["L1"]

        # 隔离功能立即可用：推进版本并写入含隔离清单的新快照
        r = c.put("/drills/OLD/quarantine", json={"record_nos": ["L1"]})
        assert r.status_code == 200, r.text
        assert r.json()["result"]["version"] == 2
        assert r.json()["result"]["unpaired"] == []
        v2 = c.get("/drills/OLD/snapshots/2").json()
        assert _quarantine_nos(v2) == ["L1"]

    for suffix in ("", "-wal", "-shm"):
        try:
            os.unlink(tmp.name + suffix)
        except OSError:
            pass
