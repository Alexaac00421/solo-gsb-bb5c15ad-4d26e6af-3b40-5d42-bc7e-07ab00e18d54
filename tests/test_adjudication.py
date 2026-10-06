"""复核员批量裁决（锁定/解除 + 隔离/恢复一次提交）HTTP 集成测试。

覆盖：最终状态整批判定与意图顺序无关、同向重复意图去重、相反意图整批
拒绝、引用不存在记录、最终锁定对候选校验、同一记录被多对锁定、锁定
成员最终仍隔离、同批解除旧锁并隔离/恢复并锁定、封存与基准版本过期
拒绝、仅实际变化推进一次版本并同事务留存快照、纯重传不推进、响应
契约（当前版本/锁定与隔离清单/完整互证结果）与既有单项接口兼容。
"""

from __future__ import annotations

import os
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
    return r.json()["version"]


def _pair_keys(pairs):
    return sorted((p["record_a"], p["record_b"]) for p in pairs)


def _lock_keys(body):
    return sorted((l["record_a"], l["record_b"]) for l in body["locks"])


def _quarantine_nos(body):
    return [q["record_no"] for q in body["quarantine"]]


def _version(client, drill="Q"):
    return client.get(f"/drills/{drill}/result").json()["version"]


def _adjudicate(client, drill="Q", base_version=1, locks=(), quarantine=()):
    return client.post(
        f"/drills/{drill}/adjudications",
        json={
            "base_version": base_version,
            "locks": list(locks),
            "quarantine": list(quarantine),
        },
    )


# --------------------------------------------------------------------------
# 基本语义：混合批次按最终状态判定，整批只推进一次版本
# --------------------------------------------------------------------------
def test_mixed_batch_final_state_single_version_advance(client):
    _seed_five(client)  # v1
    client.put("/drills/Q/locks", json={"record_a": "R1", "record_b": "R2"})  # v2
    client.put("/drills/Q/quarantine", json={"record_nos": ["R5"]})  # v3

    # 同批：解除旧锁 (R1,R2) 并隔离 R2、恢复 R5、锁定 (R3,R4)
    r = _adjudicate(
        client,
        base_version=3,
        locks=[
            {"action": "unlock", "record_a": "R2", "record_b": "R1"},
            {"action": "lock", "record_a": "R4", "record_b": "R3"},
        ],
        quarantine=[
            {"action": "quarantine", "record_no": "R2"},
            {"action": "restore", "record_no": "R5"},
        ],
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["drill_id"] == "Q"
    assert body["base_version"] == 3
    assert body["version"] == 4  # 整批只推进一次版本
    assert body["version_advanced"] is True
    # 实际生效回显（记录对内部升序）
    assert body["applied"] == {
        "locked": [{"record_a": "R3", "record_b": "R4"}],
        "unlocked": [{"record_a": "R1", "record_b": "R2"}],
        "quarantined": ["R2"],
        "restored": ["R5"],
    }
    # 当前锁定与隔离清单
    assert _lock_keys(body) == [("R3", "R4")]
    assert _quarantine_nos(body) == ["R2"]
    # 完整互证结果：锁定对强制入选；R2 被隔离 -> R1 落单；R5 恢复后回到未配对
    result = body["result"]
    assert result["version"] == 4
    assert _pair_keys(result["pairs"]) == [("R3", "R4")]
    assert result["unpaired"] == ["R1", "R5"]
    assert result["total_time_diff_seconds"] == 8

    # 结果、锁定、隔离查询端点同步体现；快照同事务留存（含锁定/隔离清单）
    assert client.get("/drills/Q/result").json()["version"] == 4
    assert _lock_keys(client.get("/drills/Q/locks").json()) == [("R3", "R4")]
    assert _quarantine_nos(client.get("/drills/Q/quarantine").json()) == ["R2"]
    assert client.get("/drills/Q/snapshots").json()["versions"] == [0, 1, 2, 3, 4]
    snap = client.get("/drills/Q/snapshots/4").json()
    assert _lock_keys(snap) == [("R3", "R4")]
    assert _quarantine_nos(snap) == ["R2"]
    assert _pair_keys(snap["pairs"]) == [("R3", "R4")]


def test_intent_order_does_not_matter(client):
    intents_locks = [
        {"action": "lock", "record_a": "R1", "record_b": "R2"},
        {"action": "lock", "record_a": "R3", "record_b": "R4"},
    ]
    intents_quar = [
        {"action": "quarantine", "record_no": "R5"},
    ]
    _seed_five(client, drill="A")
    _seed_five(client, drill="B")

    r1 = _adjudicate(client, drill="A", base_version=1,
                     locks=intents_locks, quarantine=intents_quar)
    # 同一批意图颠倒顺序（含记录对内部颠倒）
    r2 = _adjudicate(
        client, drill="B", base_version=1,
        locks=[
            {"action": "lock", "record_a": "R4", "record_b": "R3"},
            {"action": "lock", "record_a": "R2", "record_b": "R1"},
        ],
        quarantine=list(reversed(intents_quar)),
    )
    assert r1.status_code == 200 and r2.status_code == 200
    b1, b2 = r1.json(), r2.json()
    assert b1["version"] == b2["version"] == 2
    assert _lock_keys(b1) == _lock_keys(b2)
    assert _quarantine_nos(b1) == _quarantine_nos(b2)
    assert b1["result"]["pairs"] == b2["result"]["pairs"]
    assert b1["result"]["unpaired"] == b2["result"]["unpaired"]


def test_duplicate_same_direction_intents_deduped(client):
    _seed_five(client)  # v1
    r = _adjudicate(
        client,
        base_version=1,
        locks=[
            {"action": "lock", "record_a": "R1", "record_b": "R2"},
            {"action": "lock", "record_a": "R2", "record_b": "R1"},  # 同向重复
            {"action": "lock", "record_a": "R1", "record_b": "R2"},
        ],
        quarantine=[
            {"action": "quarantine", "record_no": "R5"},
            {"action": "quarantine", "record_no": " R5 "},  # 空白差异也算重复
        ],
    )
    assert r.status_code == 200, r.text
    body = r.json()
    # 重复同向意图只算一次：各只生效一次，整批只推进一次版本
    assert body["applied"]["locked"] == [{"record_a": "R1", "record_b": "R2"}]
    assert body["applied"]["quarantined"] == ["R5"]
    assert body["version"] == 2
    assert client.get("/drills/Q/snapshots").json()["versions"] == [0, 1, 2]


def test_pure_retransmit_no_version_advance(client):
    _seed_five(client)  # v1
    client.put("/drills/Q/locks", json={"record_a": "R1", "record_b": "R2"})  # v2
    client.put("/drills/Q/quarantine", json={"record_nos": ["R5"]})  # v3

    # 全部意图均已是目标状态：重复锁定、解除未锁定的对、重复隔离、恢复未隔离
    r = _adjudicate(
        client,
        base_version=3,
        locks=[
            {"action": "lock", "record_a": "R2", "record_b": "R1"},
            {"action": "unlock", "record_a": "R3", "record_b": "R4"},
        ],
        quarantine=[
            {"action": "quarantine", "record_no": "R5"},
            {"action": "restore", "record_no": "R3"},
        ],
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["version_advanced"] is False
    assert body["version"] == 3  # 纯重传不推进
    assert body["applied"] == {
        "locked": [], "unlocked": [], "quarantined": [], "restored": [],
    }
    assert _lock_keys(body) == [("R1", "R2")]
    assert _quarantine_nos(body) == ["R5"]
    # 无新快照；结果不变
    assert client.get("/drills/Q/snapshots").json()["versions"] == [0, 1, 2, 3]
    assert _pair_keys(body["result"]["pairs"]) == [("R1", "R2"), ("R3", "R4")]


def test_unlock_nonexistent_lock_is_noop_in_batch(client):
    """批量裁决按最终状态判定：解除本就不存在的锁定是幂等空操作（不报错）。"""
    _seed_five(client)
    r = _adjudicate(
        client, base_version=1,
        locks=[{"action": "unlock", "record_a": "R1", "record_b": "R2"}],
    )
    assert r.status_code == 200, r.text
    assert r.json()["version_advanced"] is False
    assert r.json()["version"] == 1


# --------------------------------------------------------------------------
# 整批拒绝：相反意图 / 记录不存在 / 候选失效 / 多对锁定 / 仍隔离 / 封存 / 版本过期
# --------------------------------------------------------------------------
def test_conflicting_intents_rejected_and_state_unchanged(client):
    _seed_five(client)

    # 同一记录对既锁定又解除（顺序无关，记录对内部升序后判定）
    r = _adjudicate(
        client, base_version=1,
        locks=[
            {"action": "lock", "record_a": "R1", "record_b": "R2"},
            {"action": "unlock", "record_a": "R2", "record_b": "R1"},
        ],
    )
    assert r.status_code == 409
    assert r.json()["error"] == "adjudication_intent_conflict"

    # 同一记录既隔离又恢复
    r = _adjudicate(
        client, base_version=1,
        quarantine=[
            {"action": "restore", "record_no": "R5"},
            {"action": "quarantine", "record_no": "R5"},
        ],
    )
    assert r.status_code == 409
    assert r.json()["error"] == "adjudication_intent_conflict"

    # 状态不变：无锁定、无隔离、版本与快照不变
    assert client.get("/drills/Q/locks").json()["locks"] == []
    assert client.get("/drills/Q/quarantine").json()["quarantine"] == []
    assert _version(client) == 1
    assert client.get("/drills/Q/snapshots").json()["versions"] == [0, 1]


def test_missing_record_rejected(client):
    _seed_five(client)
    # 锁定意图引用不存在的记录
    r = _adjudicate(
        client, base_version=1,
        locks=[{"action": "lock", "record_a": "R1", "record_b": "NOPE"}],
    )
    assert r.status_code == 404
    assert r.json()["error"] == "adjudication_record_missing"

    # 解除/隔离/恢复意图引用不存在的记录同样整批拒绝
    r = _adjudicate(
        client, base_version=1,
        locks=[{"action": "unlock", "record_a": "R1", "record_b": "NOPE"}],
    )
    assert r.status_code == 404
    assert r.json()["error"] == "adjudication_record_missing"
    r = _adjudicate(
        client, base_version=1,
        quarantine=[{"action": "quarantine", "record_no": "NOPE"}],
    )
    assert r.status_code == 404
    assert r.json()["error"] == "adjudication_record_missing"
    r = _adjudicate(
        client, base_version=1,
        quarantine=[{"action": "restore", "record_no": "NOPE"}],
    )
    assert r.status_code == 404
    assert r.json()["error"] == "adjudication_record_missing"

    assert _version(client) == 1
    assert client.get("/drills/Q/snapshots").json()["versions"] == [0, 1]


def test_final_lock_not_candidate_rejected(client):
    _seed_five(client)
    # R1 与 R3 未相互指认：最终锁定对不满足互证候选条件
    r = _adjudicate(
        client, base_version=1,
        locks=[{"action": "lock", "record_a": "R1", "record_b": "R3"}],
    )
    assert r.status_code == 409
    assert r.json()["error"] == "lock_not_candidate"
    assert _version(client) == 1
    assert client.get("/drills/Q/locks").json()["locks"] == []


def test_record_locked_by_multiple_pairs_rejected(client):
    _make_drill(client)
    # R1/R3 均与 R2 互指（同波段同模式同时差内）：两对都合法但共用 R2
    client.post("/drills/Q/logs", json={"logs": [
        _log("R1", "BH1AAA", "BH2BBB", "2026-10-04T00:00:00+00:00"),
        _log("R2", "BH2BBB", "BH1AAA", "2026-10-04T00:00:05+00:00"),
        _log("R3", "BH1AAA", "BH2BBB", "2026-10-04T00:00:02+00:00"),
    ]})
    r = _adjudicate(
        client, base_version=1,
        locks=[
            {"action": "lock", "record_a": "R1", "record_b": "R2"},
            {"action": "lock", "record_a": "R2", "record_b": "R3"},
        ],
    )
    assert r.status_code == 409
    assert r.json()["error"] == "record_already_locked"
    assert client.get("/drills/Q/locks").json()["locks"] == []
    assert _version(client) == 1

    # 既有锁定 (R1,R2)，同批再锁 (R2,R3)：最终状态同一记录被多对锁定
    client.put("/drills/Q/locks", json={"record_a": "R1", "record_b": "R2"})
    r = _adjudicate(
        client, base_version=2,
        locks=[{"action": "lock", "record_a": "R2", "record_b": "R3"}],
    )
    assert r.status_code == 409
    assert r.json()["error"] == "record_already_locked"
    assert _lock_keys(client.get("/drills/Q/locks").json()) == [("R1", "R2")]
    assert _version(client) == 2


def test_locked_member_still_quarantined_rejected(client):
    _seed_five(client)
    client.put("/drills/Q/quarantine", json={"record_nos": ["R2"]})  # v2

    # 锁定最终仍被隔离的记录
    r = _adjudicate(
        client, base_version=2,
        locks=[{"action": "lock", "record_a": "R1", "record_b": "R2"}],
    )
    assert r.status_code == 409
    assert r.json()["error"] == "lock_record_quarantined"

    # 同批锁定并隔离同一记录：最终状态仍冲突，整批拒绝
    r = _adjudicate(
        client, base_version=2,
        locks=[{"action": "lock", "record_a": "R3", "record_b": "R4"}],
        quarantine=[{"action": "quarantine", "record_no": "R4"}],
    )
    assert r.status_code == 409
    assert r.json()["error"] == "lock_record_quarantined"

    # 隔离既有锁定成员（未同批解除）：最终状态锁定成员仍被隔离
    client.put("/drills/Q/locks", json={"record_a": "R3", "record_b": "R4"})  # v3
    r = _adjudicate(
        client, base_version=3,
        quarantine=[{"action": "quarantine", "record_no": "R3"}],
    )
    assert r.status_code == 409
    assert r.json()["error"] == "lock_record_quarantined"

    # 状态不变
    assert _quarantine_nos(client.get("/drills/Q/quarantine").json()) == ["R2"]
    assert _lock_keys(client.get("/drills/Q/locks").json()) == [("R3", "R4")]
    assert _version(client) == 3


def test_sealed_and_stale_base_version_rejected(client):
    _seed_five(client)  # v1

    # 基准版本过期（当前 v1，提交 v0）
    r = _adjudicate(
        client, base_version=0,
        locks=[{"action": "lock", "record_a": "R1", "record_b": "R2"}],
    )
    assert r.status_code == 409
    assert r.json()["error"] == "adjudication_base_version_changed"

    # 封存后拒绝一切裁决
    client.post("/drills/Q/seal")
    r = _adjudicate(
        client, base_version=1,
        locks=[{"action": "lock", "record_a": "R1", "record_b": "R2"}],
    )
    assert r.status_code == 409
    assert r.json()["error"] == "drill_sealed"

    # 未知演练
    r = _adjudicate(
        client, drill="NOPE", base_version=0,
        locks=[{"action": "lock", "record_a": "R1", "record_b": "R2"}],
    )
    assert r.status_code == 404
    assert r.json()["error"] == "drill_not_found"

    # 状态不变
    assert _version(client) == 1
    assert client.get("/drills/Q/locks").json()["locks"] == []
    assert client.get("/drills/Q/snapshots").json()["versions"] == [0, 1]


# --------------------------------------------------------------------------
# 同批组合：解除旧锁并隔离、恢复并锁定均可受理
# --------------------------------------------------------------------------
def test_unlock_and_quarantine_same_batch_ok(client):
    _seed_five(client)
    client.put("/drills/Q/locks", json={"record_a": "R1", "record_b": "R2"})  # v2

    # 同批解除旧锁 (R1,R2) 并隔离 R2：最终状态无冲突
    r = _adjudicate(
        client, base_version=2,
        locks=[{"action": "unlock", "record_a": "R1", "record_b": "R2"}],
        quarantine=[{"action": "quarantine", "record_no": "R2"}],
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["version"] == 3
    assert body["applied"]["unlocked"] == [{"record_a": "R1", "record_b": "R2"}]
    assert body["applied"]["quarantined"] == ["R2"]
    assert body["locks"] == []
    assert _quarantine_nos(body) == ["R2"]
    # R2 被隔离后 R1 落单；(R3,R4) 自由配对
    assert _pair_keys(body["result"]["pairs"]) == [("R3", "R4")]
    assert body["result"]["unpaired"] == ["R1", "R5"]


def test_restore_and_lock_same_batch_ok(client):
    _seed_five(client)
    client.put("/drills/Q/quarantine", json={"record_nos": ["R2"]})  # v2

    # 同批恢复 R2 并锁定 (R1,R2)：最终状态无冲突
    r = _adjudicate(
        client, base_version=2,
        locks=[{"action": "lock", "record_a": "R1", "record_b": "R2"}],
        quarantine=[{"action": "restore", "record_no": "R2"}],
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["version"] == 3
    assert body["applied"]["restored"] == ["R2"]
    assert body["applied"]["locked"] == [{"record_a": "R1", "record_b": "R2"}]
    assert _lock_keys(body) == [("R1", "R2")]
    assert body["quarantine"] == []
    # 锁定对强制入选
    assert ("R1", "R2") in _pair_keys(body["result"]["pairs"])


def test_quarantine_only_batch_and_restore_only_batch(client):
    _seed_five(client)
    # 仅隔离意图
    r = _adjudicate(
        client, base_version=1,
        quarantine=[{"action": "quarantine", "record_no": "R3"}],
    )
    assert r.status_code == 200
    assert r.json()["version"] == 2
    assert _quarantine_nos(r.json()) == ["R3"]
    # 仅恢复意图
    r = _adjudicate(
        client, base_version=2,
        quarantine=[{"action": "restore", "record_no": "R3"}],
    )
    assert r.status_code == 200
    assert r.json()["version"] == 3
    assert r.json()["quarantine"] == []


# --------------------------------------------------------------------------
# 请求形状校验
# --------------------------------------------------------------------------
def test_request_shape_validation(client):
    _seed_five(client)
    # 两个列表均为空（含缺省）-> 422
    assert client.post(
        "/drills/Q/adjudications", json={"base_version": 1}
    ).status_code == 422
    assert client.post(
        "/drills/Q/adjudications",
        json={"base_version": 1, "locks": [], "quarantine": []},
    ).status_code == 422
    # 锁定意图两个记录号相同 -> 422 lock_same_record
    r = _adjudicate(
        client, base_version=1,
        locks=[{"action": "lock", "record_a": "R1", "record_b": " R1 "}],
    )
    assert r.status_code == 422
    assert r.json()["error"] == "lock_same_record"
    # 非法 action / 负基准版本 / 空记录号 -> 422
    assert client.post("/drills/Q/adjudications", json={
        "base_version": 1,
        "locks": [{"action": "freeze", "record_a": "R1", "record_b": "R2"}],
    }).status_code == 422
    assert client.post("/drills/Q/adjudications", json={
        "base_version": -1,
        "locks": [{"action": "lock", "record_a": "R1", "record_b": "R2"}],
    }).status_code == 422
    assert client.post("/drills/Q/adjudications", json={
        "base_version": 1,
        "quarantine": [{"action": "quarantine", "record_no": "  "}],
    }).status_code == 422
    # 状态不变
    assert _version(client) == 1


# --------------------------------------------------------------------------
# 与既有单项接口的兼容与联动
# --------------------------------------------------------------------------
def test_existing_single_item_endpoints_still_work_after_batch(client):
    _seed_five(client)
    # 批量裁决锁定 (R1,R2)、隔离 R5
    r = _adjudicate(
        client, base_version=1,
        locks=[{"action": "lock", "record_a": "R1", "record_b": "R2"}],
        quarantine=[{"action": "quarantine", "record_no": "R5"}],
    )
    assert r.status_code == 200 and r.json()["version"] == 2

    # 单项接口照常工作且看到相同状态
    locks = client.get("/drills/Q/locks").json()
    assert _lock_keys(locks) == [("R1", "R2")]
    assert locks["result"]["version"] == 2
    quar = client.get("/drills/Q/quarantine").json()
    assert _quarantine_nos(quar) == ["R5"]

    # 单项解除锁定 -> v3；单项恢复 -> v4；日志写入使锁定失效仍整批拒绝
    r = client.post("/drills/Q/locks/unlock",
                    json={"record_a": "R1", "record_b": "R2"})
    assert r.status_code == 200 and r.json()["result"]["version"] == 3
    r = client.post("/drills/Q/quarantine/restore",
                    json={"record_nos": ["R5"]})
    assert r.status_code == 200 and r.json()["result"]["version"] == 4

    # 批量裁决推进的版本快照同样可回看、可比较
    snap = client.get("/drills/Q/snapshots/2").json()
    assert _lock_keys(snap) == [("R1", "R2")]
    assert _quarantine_nos(snap) == ["R5"]
    d = client.get("/drills/Q/snapshots/2/diff/4").json()
    assert d["locks"]["removed"][0]["record_a"] == "R1"
    assert _quarantine_nos({"quarantine": d["quarantine"]["removed"]}) == ["R5"]


def test_batch_version_feeds_preview_confirm_base_version(client):
    """批量裁决推进的版本即为后续预演/确认的基准版本（契约一致）。"""
    _seed_five(client)
    r = _adjudicate(
        client, base_version=1,
        locks=[{"action": "lock", "record_a": "R1", "record_b": "R2"}],
    )
    assert r.json()["version"] == 2

    pv = client.post("/drills/Q/logs/preview", json={"logs": [
        _log("R5", "BH5EEE", "BH6FFF", "2026-10-04T00:00:00+00:00"),
    ]}).json()
    assert pv["base_version"] == 2
    r = client.post("/drills/Q/logs/confirm", json={
        "logs": [_log("R5", "BH5EEE", "BH6FFF", "2026-10-04T00:00:00+00:00")],
        "base_version": pv["base_version"],
        "token": pv["token"],
    })
    assert r.status_code == 200 and r.json()["version"] == 2  # 同内容重传


# --------------------------------------------------------------------------
# 并发：同一基准版本的批量裁决被串行化，只有一个能推进版本
# --------------------------------------------------------------------------
def test_concurrent_batches_serialized_by_base_version(client):
    _seed_five(client)  # v1
    outcomes = []

    def adjudicate():
        r = _adjudicate(
            client, base_version=1,
            locks=[{"action": "lock", "record_a": "R1", "record_b": "R2"}],
        )
        outcomes.append((r.status_code, r.json().get("error")))

    threads = [threading.Thread(target=adjudicate) for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # 恰好一个批次以基准版本 1 受理并推进到 v2；其余因版本过期整批拒绝
    assert outcomes.count((200, None)) == 1
    assert outcomes.count((409, "adjudication_base_version_changed")) == 4
    assert _version(client) == 2
    assert _lock_keys(client.get("/drills/Q/locks").json()) == [("R1", "R2")]
    assert client.get("/drills/Q/snapshots").json()["versions"] == [0, 1, 2]
