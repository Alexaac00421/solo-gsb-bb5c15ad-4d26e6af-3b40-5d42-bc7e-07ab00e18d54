"""跨演练迁移（日志归错演练）HTTP 集成测试。"""

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
    from app import database
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


def _make_drills(client, src="SRC", src_diff=10, dst="DST", dst_diff=30):
    r = client.post(
        "/drills",
        json={"drill_id": src, "allowed_time_diff_seconds": src_diff},
    )
    assert r.status_code == 201, r.text
    r = client.post(
        "/drills",
        json={"drill_id": dst, "allowed_time_diff_seconds": dst_diff},
    )
    assert r.status_code == 201, r.text


def _versions(client, *drills):
    return [client.get(f"/drills/{d}").json()["version"] for d in drills]


# --------------------------------------------------------------------------
# 成功路径
# --------------------------------------------------------------------------
def test_transfer_success_moves_and_recomputes_both(client):
    _make_drills(client)
    # 源：R1/R2 时差 20s 超过源允许 10s（不配对）；R3/R4 时差 5s 配对
    logs = [
        _log("R1", "BH1AAA", "BH2BBB", "2026-10-04T08:00:00+08:00"),
        _log("R2", "BH2BBB", "BH1AAA", "2026-10-04T00:00:20+00:00"),
        _log("R3", "BH3CCC", "BH4DDD", "2026-10-04T00:00:00+00:00"),
        _log("R4", "BH4DDD", "BH3CCC", "2026-10-04T00:00:05+00:00"),
    ]
    r = client.post("/drills/SRC/logs", json={"logs": logs})
    assert r.status_code == 200, r.text

    r = client.post(
        "/drills/SRC/transfer",
        json={"target_drill_id": "DST", "record_nos": ["R1", "R2"]},
    )
    assert r.status_code == 200, r.text
    body = r.json()

    # 两端各只推进一次版本
    assert body["source_drill_id"] == "SRC"
    assert body["target_drill_id"] == "DST"
    assert body["moved_record_nos"] == ["R1", "R2"]
    assert body["source_version"] == 2
    assert body["target_version"] == 1

    # 源端只剩 R3/R4，仍配对
    assert body["source_result"]["version"] == 2
    assert [
        (p["record_a"], p["record_b"])
        for p in body["source_result"]["pairs"]
    ] == [("R3", "R4")]
    assert body["source_result"]["unpaired"] == []

    # 目标端按目标允许时差 30s 重新互证：R1/R2 时差 20s 配对
    assert body["target_result"]["version"] == 1
    assert [
        (p["record_a"], p["record_b"])
        for p in body["target_result"]["pairs"]
    ] == [("R1", "R2")]
    assert body["target_result"]["pairs"][0]["time_diff_seconds"] == 20
    assert body["target_result"]["unpaired"] == []

    # 随后查询的版本与结果一致
    assert client.get("/drills/SRC/result").json()["version"] == 2
    assert client.get("/drills/DST/result").json()["version"] == 1


def test_transfer_preserves_original_log_fields(client):
    _make_drills(client, src_diff=10, dst_diff=10)
    r = client.put(
        "/drills/SRC/logs/R1",
        json=_log("R1", " bh1aaa ", "BH2BBB", "2026-10-04T08:00:00+08:00"),
    )
    assert r.status_code == 200
    before = {l["record_no"]: l for l in client.get("/drills/SRC/logs").json()}

    r = client.post(
        "/drills/SRC/transfer",
        json={"target_drill_id": "DST", "record_nos": ["R1"]},
    )
    assert r.status_code == 200, r.text
    after = {l["record_no"]: l for l in client.get("/drills/DST/logs").json()}
    assert set(after) == {"R1"}
    # 规范化字段、UTC 时间与 updated_at 全部原样保留
    assert after["R1"]["station"] == before["R1"]["station"] == "BH1AAA"
    assert after["R1"]["counterpart"] == "BH2BBB"
    assert after["R1"]["band"] == "40M"
    assert after["R1"]["mode"] == "SSB"
    assert after["R1"]["time"] == "2026-10-04T00:00:00+00:00"
    assert after["R1"]["updated_at"] == before["R1"]["updated_at"]

    # 源端已无该记录
    assert client.get("/drills/SRC/logs").json() == []


def test_transfer_reparticipates_with_target_counterparts(client):
    _make_drills(client, src_diff=5, dst_diff=30)
    # 目标先有一条等待互指的 B->A 记录（单方记录，未配对）
    r = client.put(
        "/drills/DST/logs/T1",
        json=_log("T1", "BH2BBB", "BH1AAA", "2026-10-04T00:00:25+00:00"),
    )
    assert r.status_code == 200
    assert r.json()["unpaired"] == ["T1"]
    # 源端 R1 无互指记录
    r = client.put(
        "/drills/SRC/logs/R1",
        json=_log("R1", "BH1AAA", "BH2BBB", "2026-10-04T08:00:00+08:00"),
    )
    assert r.json()["unpaired"] == ["R1"]

    # 迁入后 R1 与目标 T1 时差 25s：源允许 5s 配不上，目标允许 30s 配上
    r = client.post(
        "/drills/SRC/transfer",
        json={"target_drill_id": "DST", "record_nos": ["R1"]},
    )
    assert r.status_code == 200, r.text
    pairs = r.json()["target_result"]["pairs"]
    assert [(p["record_a"], p["record_b"]) for p in pairs] == [("R1", "T1")]
    assert pairs[0]["time_diff_seconds"] == 25


def test_transfer_empty_drills_advance_each_once_with_snapshots(client):
    _make_drills(client)
    r = client.put(
        "/drills/SRC/logs/R1",
        json=_log("R1", "A", "B", "2026-10-04T00:00:00+00:00"),
    )
    assert r.status_code == 200
    r = client.post(
        "/drills/SRC/transfer",
        json={"target_drill_id": "DST", "record_nos": ["R1"]},
    )
    assert r.status_code == 200, r.text
    assert r.json()["source_version"] == 2
    assert r.json()["target_version"] == 1
    # 新版本快照均已同事务留存（源: 0,1,2；目标: 0,1）
    assert client.get("/drills/SRC/snapshots").json()["versions"] == [0, 1, 2]
    assert client.get("/drills/DST/snapshots").json()["versions"] == [0, 1]

    # 快照内容即迁移后的两端结果
    s2 = client.get("/drills/SRC/snapshots/2").json()
    t1 = client.get("/drills/DST/snapshots/1").json()
    assert s2["pairs"] == [] and s2["unpaired"] == []
    assert t1["unpaired"] == ["R1"]

    # 历史快照不可改写：源 1 版仍含 R1
    s1 = client.get("/drills/SRC/snapshots/1").json()
    assert s1["unpaired"] == ["R1"]


def test_transfer_source_lock_not_involved_still_effective(client):
    _make_drills(client)
    logs = [
        _log("R1", "BH1AAA", "BH2BBB", "2026-10-04T08:00:00+08:00"),
        _log("R2", "BH2BBB", "BH1AAA", "2026-10-04T00:00:05+00:00"),
        _log("R3", "BH3CCC", "BH4DDD", "2026-10-04T00:00:00+00:00"),
        _log("R4", "BH4DDD", "BH3CCC", "2026-10-04T00:00:08+00:00"),
    ]
    assert client.post("/drills/SRC/logs", json={"logs": logs}).status_code == 200
    # 源端锁定 (R1,R2)（推进一版）；迁移与锁定无关的 R3
    r = client.put("/drills/SRC/locks", json={"record_a": "R1", "record_b": "R2"})
    assert r.status_code == 200
    locked_at = r.json()["locks"][0]["locked_at"]

    r = client.post(
        "/drills/SRC/transfer",
        json={"target_drill_id": "DST", "record_nos": ["R3", "R4"]},
    )
    assert r.status_code == 200, r.text
    # 未迁移的锁定照常生效：强制出现在源结果与新版本快照中
    src = r.json()["source_result"]
    assert [(p["record_a"], p["record_b"]) for p in src["pairs"]] == [
        ("R1", "R2")
    ]
    locks = client.get("/drills/SRC/locks").json()["locks"]
    assert [(l["record_a"], l["record_b"]) for l in locks] == [("R1", "R2")]
    assert locks[0]["locked_at"] == locked_at
    snap = client.get(f"/drills/SRC/snapshots/{src['version']}").json()
    assert [(l["record_a"], l["record_b"]) for l in snap["locks"]] == [
        ("R1", "R2")
    ]


def test_transfer_target_existing_lock_remains_and_can_pair_with_moved(client):
    _make_drills(client)
    logs = [
        _log("R1", "BH1AAA", "BH2BBB", "2026-10-04T08:00:00+08:00"),
        _log("R2", "BH2BBB", "BH1AAA", "2026-10-04T00:00:05+00:00"),
    ]
    assert client.post("/drills/SRC/logs", json={"logs": logs}).status_code == 200
    # 目标先放一对互指记录并锁定 (T1,T2)
    tlogs = [
        _log("T1", "BH9XXX", "BH9YYY", "2026-10-04T00:00:00+00:00"),
        _log("T2", "BH9YYY", "BH9XXX", "2026-10-04T00:00:02+00:00"),
    ]
    assert client.post("/drills/DST/logs", json={"logs": tlogs}).status_code == 200
    assert (
        client.put(
            "/drills/DST/locks", json={"record_a": "T1", "record_b": "T2"}
        ).status_code
        == 200
    )
    r = client.post(
        "/drills/SRC/transfer",
        json={"target_drill_id": "DST", "record_nos": ["R1", "R2"]},
    )
    assert r.status_code == 200, r.text
    pairs = [
        (p["record_a"], p["record_b"])
        for p in r.json()["target_result"]["pairs"]
    ]
    # 目标既有锁定保留，迁入记录自由配对
    assert pairs == [("R1", "R2"), ("T1", "T2")]
    target_locks = client.get("/drills/DST/locks").json()["locks"]
    assert [(l["record_a"], l["record_b"]) for l in target_locks] == [
        ("T1", "T2")
    ]


# --------------------------------------------------------------------------
# 拒绝场景：整次拒绝、两端状态不变
# --------------------------------------------------------------------------
def test_transfer_rejects_unknown_source_and_target(client):
    _make_drills(client)
    client.put(
        "/drills/SRC/logs/R1",
        json=_log("R1", "A", "B", "2026-10-04T00:00:00+00:00"),
    )
    r = client.post(
        "/drills/NOPE/transfer",
        json={"target_drill_id": "DST", "record_nos": ["R1"]},
    )
    assert r.status_code == 404 and r.json()["error"] == "drill_not_found"

    r = client.post(
        "/drills/SRC/transfer",
        json={"target_drill_id": "NOPE", "record_nos": ["R1"]},
    )
    assert r.status_code == 404 and r.json()["error"] == "drill_not_found"
    assert _versions(client, "SRC", "DST") == [1, 0]


def test_transfer_rejects_same_drill(client):
    _make_drills(client)
    r = client.post(
        "/drills/SRC/transfer",
        json={"target_drill_id": "SRC", "record_nos": ["R1"]},
    )
    assert r.status_code == 422
    assert r.json()["error"] == "same_drill_transfer"
    assert _versions(client, "SRC") == [0]


def test_transfer_rejects_empty_and_duplicate_record_nos(client):
    _make_drills(client)
    r = client.post(
        "/drills/SRC/transfer",
        json={"target_drill_id": "DST", "record_nos": []},
    )
    assert r.status_code == 422

    r = client.post(
        "/drills/SRC/transfer",
        json={"target_drill_id": "DST", "record_nos": ["R1", " R1 "]},
    )
    assert r.status_code == 409
    assert r.json()["error"] == "duplicate_record_in_transfer"
    assert _versions(client, "SRC", "DST") == [0, 0]


def test_transfer_rejects_missing_source_records_and_keeps_state(client):
    _make_drills(client)
    client.put(
        "/drills/SRC/logs/R1",
        json=_log("R1", "A", "B", "2026-10-04T00:00:00+00:00"),
    )
    r = client.post(
        "/drills/SRC/transfer",
        json={"target_drill_id": "DST", "record_nos": ["R1", "R9"]},
    )
    assert r.status_code == 404
    assert r.json()["error"] == "transfer_record_missing"
    # 整次拒绝：R1 仍在源、目标为空、两端版本不变
    assert {l["record_no"] for l in client.get("/drills/SRC/logs").json()} == {
        "R1"
    }
    assert client.get("/drills/DST/logs").json() == []
    assert _versions(client, "SRC", "DST") == [1, 0]


def test_transfer_rejects_target_same_number_conflict(client):
    _make_drills(client)
    client.put(
        "/drills/SRC/logs/R1",
        json=_log("R1", "A", "B", "2026-10-04T00:00:00+00:00"),
    )
    client.put(
        "/drills/DST/logs/R1",
        json=_log("R1", "C", "D", "2026-10-04T00:00:00+00:00"),
    )
    src_before = _versions(client, "SRC")[0]
    r = client.post(
        "/drills/SRC/transfer",
        json={"target_drill_id": "DST", "record_nos": ["R1"]},
    )
    assert r.status_code == 409
    assert r.json()["error"] == "target_record_exists"
    # 两端日志均未变化（目标 R1 仍是 C/D）
    src_logs = {l["record_no"]: l for l in client.get("/drills/SRC/logs").json()}
    dst_logs = {l["record_no"]: l for l in client.get("/drills/DST/logs").json()}
    assert src_logs["R1"]["station"] == "A"
    assert dst_logs["R1"]["station"] == "C"
    assert _versions(client, "SRC", "DST") == [src_before, 1]


def test_transfer_rejects_when_record_locked_in_source(client):
    _make_drills(client)
    logs = [
        _log("R1", "BH1AAA", "BH2BBB", "2026-10-04T08:00:00+08:00"),
        _log("R2", "BH2BBB", "BH1AAA", "2026-10-04T00:00:05+00:00"),
        _log("R3", "BH3CCC", "BH4DDD", "2026-10-04T00:00:00+00:00"),
    ]
    assert client.post("/drills/SRC/logs", json={"logs": logs}).status_code == 200
    assert (
        client.put(
            "/drills/SRC/locks", json={"record_a": "R1", "record_b": "R2"}
        ).status_code
        == 200
    )
    before_versions = _versions(client, "SRC", "DST")
    # 迁锁定对中的任意一条（record_b）都拒绝
    r = client.post(
        "/drills/SRC/transfer",
        json={"target_drill_id": "DST", "record_nos": ["R3", "R2"]},
    )
    assert r.status_code == 409
    assert r.json()["error"] == "transfer_record_locked"
    # 整次拒绝：R3 也未被迁走；锁定仍在
    assert {l["record_no"] for l in client.get("/drills/SRC/logs").json()} == {
        "R1",
        "R2",
        "R3",
    }
    assert client.get("/drills/DST/logs").json() == []
    assert _versions(client, "SRC", "DST") == before_versions
    assert client.get("/drills/SRC/locks").json()["locks"]


def test_transfer_rejects_when_either_drill_sealed(client):
    _make_drills(client)
    client.put(
        "/drills/SRC/logs/R1",
        json=_log("R1", "A", "B", "2026-10-04T00:00:00+00:00"),
    )
    client.post("/drills/SRC/seal")
    r = client.post(
        "/drills/SRC/transfer",
        json={"target_drill_id": "DST", "record_nos": ["R1"]},
    )
    assert r.status_code == 409 and r.json()["error"] == "drill_sealed"
    assert {l["record_no"] for l in client.get("/drills/SRC/logs").json()} == {
        "R1"
    }
    assert _versions(client, "SRC", "DST") == [1, 0]

    # 目标封存：另建未封存源迁入同样拒绝
    assert (
        client.post(
            "/drills", json={"drill_id": "S2", "allowed_time_diff_seconds": 10}
        ).status_code
        == 201
    )
    client.put(
        "/drills/S2/logs/X1",
        json=_log("X1", "A", "B", "2026-10-04T00:00:00+00:00"),
    )
    client.post("/drills/DST/seal")
    r = client.post(
        "/drills/S2/transfer",
        json={"target_drill_id": "DST", "record_nos": ["X1"]},
    )
    assert r.status_code == 409 and r.json()["error"] == "drill_sealed"
    assert {l["record_no"] for l in client.get("/drills/S2/logs").json()} == {
        "X1"
    }


def test_transfer_is_atomic_under_concurrent_conflict(client):
    """并发迁移：后提交者因源记录缺失/目标冲突而整次回滚，无中间态。"""
    import threading

    _make_drills(client)
    # 第三个演练作为另一迁移目标
    assert (
        client.post(
            "/drills", json={"drill_id": "DST2", "allowed_time_diff_seconds": 30}
        ).status_code
        == 201
    )
    client.put(
        "/drills/SRC/logs/R1",
        json=_log("R1", "BH1AAA", "BH2BBB", "2026-10-04T08:00:00+08:00"),
    )
    results = []

    def move(target):
        r = client.post(
            f"/drills/SRC/transfer",
            json={"target_drill_id": target, "record_nos": ["R1"]},
        )
        results.append((target, r.status_code, r.json() if r.status_code == 200 else r.json().get("error")))

    t1 = threading.Thread(target=move, args=("DST",))
    t2 = threading.Thread(target=move, args=("DST2",))
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    assert len(results) == 2
    assert sorted(s for _t, s, _ in results) == [200, 404]
    # R1 恰好落在成功一侧的目标中；源中已无 R1，另一目标从未出现 R1
    success_target = next(t for t, s, _ in results if s == 200)
    failed_target = next(t for t, s, _ in results if s == 404)
    assert client.get("/drills/SRC/logs").json() == []
    assert {l["record_no"] for l in client.get(f"/drills/{success_target}/logs").json()} == {"R1"}
    assert client.get(f"/drills/{failed_target}/logs").json() == []
