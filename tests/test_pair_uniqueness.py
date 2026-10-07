"""配对唯一性核查测试。

覆盖算法层（SCC 等优环判定、唯一/可替换、删边后字典序最小替代、增删
与未配对清单、锁定固定与隔离排除、空场）与 HTTP 只读语义（不改日志/
结果/版本/快照、封存后可查、未知演练 404、响应与原结果兼容）。
"""

from __future__ import annotations

import os
import tempfile

import pytest
from fastapi.testclient import TestClient

from app.matching import (
    MICROS_PER_SECOND,
    Record,
    match_records,
    review_pair_uniqueness,
)
import random


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


def _by_key(review):
    return {rv.pair.as_tuple(): rv for rv in review.pairs}


def test_empty_drill():
    review = review_pair_uniqueness([], 10)
    assert review.pairs == ()
    assert review.locked_pairs == ()
    assert review.optimal_pair_count == 0
    assert review.total_time_diff_seconds == 0.0


def test_single_unique_pair():
    records = [rec("a", "A", "B", 0), rec("b", "B", "A", 3)]
    review = review_pair_uniqueness(records, 10)
    assert review.optimal_pair_count == 1
    assert len(review.pairs) == 1
    rv = review.pairs[0]
    assert rv.pair.as_tuple() == ("a", "b")
    assert rv.universal is True
    assert rv.alternative is None


def test_symmetric_4_cycle_two_equal_cost_matchings():
    # 两条完美匹配总时差均为 0：(1,3)+(2,4) 与 (1,4)+(2,3)。
    records = [
        rec("1", "A", "B", 0),
        rec("2", "A", "B", 0),
        rec("3", "B", "A", 0),
        rec("4", "B", "A", 0),
    ]
    review = review_pair_uniqueness(records, 10)
    assert review.optimal_pair_count == 2
    assert review.total_time_diff_seconds == 0
    by_key = _by_key(review)
    assert set(by_key) == {("1", "3"), ("2", "4")}
    for key, rv in by_key.items():
        assert rv.universal is False
        alt = rv.alternative
        assert alt is not None
        assert key not in {p.as_tuple() for p in alt.pairs}
        assert len(alt.pairs) == 2
        assert alt.total_time_diff_seconds == 0
    # 当前结果按字典序取 (1,3)+(2,4)。
    current = match_records(records, 10)
    assert [p.as_tuple() for p in current.pairs] == [("1", "3"), ("2", "4")]
    # 不含 (1,3) 的字典序最小替代为 (1,4)+(2,3)。
    alt13 = by_key[("1", "3")].alternative
    assert [p.as_tuple() for p in alt13.pairs] == [("1", "4"), ("2", "3")]
    assert {p.as_tuple() for p in alt13.added_pairs} == {("1", "4"), ("2", "3")}
    assert {p.as_tuple() for p in alt13.removed_pairs} == {
        ("1", "3"), ("2", "4")
    }
    assert alt13.unpaired == ()
    # 不含 (2,4) 的替代同一份（当前结果的另一套完美匹配）。
    alt24 = by_key[("2", "4")].alternative
    assert [p.as_tuple() for p in alt24.pairs] == [("1", "4"), ("2", "3")]
    assert {p.as_tuple() for p in alt24.removed_pairs} == {
        ("1", "3"), ("2", "4")
    }


def test_different_cost_matchings_current_unique():
    # 两种完美匹配：(1,3)+(2,4) 总时差 0；(1,4)+(2,3) 总时差 20。
    records = [
        rec("1", "A", "B", 0),
        rec("2", "A", "B", 10),
        rec("3", "B", "A", 0),
        rec("4", "B", "A", 10),
    ]
    review = review_pair_uniqueness(records, 10)
    by_key = _by_key(review)
    assert set(by_key) == {("1", "3"), ("2", "4")}
    assert all(rv.universal for rv in review.pairs)
    assert all(rv.alternative is None for rv in review.pairs)


def test_cardinality_tie_but_no_min_cost_tie():
    # L1 可配 R1(0)/R2(50)；L2 只能配 R2(50)。最大配对数 2 的方案唯一：
    # (L1,R1)+(L2,R2)，总时差 50。
    records = [
        rec("L1", "A", "B", 0),
        rec("L2", "A", "B", 100),
        rec("R1", "B", "A", 0),
        rec("R2", "B", "A", 50),
    ]
    review = review_pair_uniqueness(records, 50)
    assert review.optimal_pair_count == 2
    assert review.total_time_diff_seconds == 50
    assert all(rv.universal for rv in review.pairs)


def test_equal_cost_alternating_path_with_unmatched_ends():
    # 最大配对数 1：L1 可配 R1/R2（时差都为 0），L2 无合格边。
    # (L1,R1) 与 (L1,R2) 都是最优（1 对、时差 0），当前取字典序 (L1,R1)。
    records = [
        rec("L1", "A", "B", 0),
        rec("L2", "A", "B", 200),
        rec("R1", "B", "A", 0),
        rec("R2", "B", "A", 0),
    ]
    review = review_pair_uniqueness(records, 10)
    by_key = _by_key(review)
    assert list(by_key) == [("L1", "R1")]
    rv = by_key[("L1", "R1")]
    assert rv.universal is False
    alt = rv.alternative
    assert [p.as_tuple() for p in alt.pairs] == [("L1", "R2")]
    assert [p.as_tuple() for p in alt.added_pairs] == [("L1", "R2")]
    assert [p.as_tuple() for p in alt.removed_pairs] == [("L1", "R1")]
    # R1 在替代方案中落单；L2 两个方案都未配对。
    assert tuple(sorted(alt.unpaired)) == ("L2", "R1")


def test_six_cycle_lex_smallest_alternative():
    # 完全二分 3x3 同时间：6 个等优完美匹配；当前为字典序最小
    # (l1,r1)+(l2,r2)+(l3,r3)。
    records = [
        rec("l1", "A", "B", 0),
        rec("l2", "A", "B", 0),
        rec("l3", "A", "B", 0),
        rec("r1", "B", "A", 0),
        rec("r2", "B", "A", 0),
        rec("r3", "B", "A", 0),
    ]
    review = review_pair_uniqueness(records, 10)
    by_key = _by_key(review)
    assert set(by_key) == {("l1", "r1"), ("l2", "r2"), ("l3", "r3")}
    # 不含 (l1,r1) 的字典序最小完美匹配：
    # (l1,r2)+(l2,r1)+(l3,r3)
    alt = by_key[("l1", "r1")].alternative
    assert [p.as_tuple() for p in alt.pairs] == [
        ("l1", "r2"), ("l2", "r1"), ("l3", "r3")
    ]
    # 不含 (l3,r3) 的字典序最小替代：最早变化的可换对是 (l1,r1)
    # -> (l1,r2),(l2,r1)，l3-r3 已在另一种轮换中消失：
    # (l1,r2)+(l2,r3)+(l3,r1) 与 (l1,r3)... 比较取最小。
    alt3 = by_key[("l3", "r3")].alternative
    assert ("l3", "r3") not in {p.as_tuple() for p in alt3.pairs}
    # 字典序最小且不含 l3-r3：l1 取最小可行 r1 不行（会把 r2/r3 留给
    # l2/l3；若 l1-r1，l2/l3 配 r2/r3 的两种中 l3 必配 r2 或 r3；
    # 取 (l1,r1)+(l2,r3)+(l3,r2) 字典序最小）。
    assert [p.as_tuple() for p in alt3.pairs] == [
        ("l1", "r1"), ("l2", "r3"), ("l3", "r2")
    ]
    assert alt3.unpaired == ()


def test_locked_pairs_are_forced_and_not_reviewed():
    # 自由最优 2 对；锁定 (L1,R2) 后全场只剩 1 对（锁定不替换）。
    records = [
        rec("L1", "A", "B", 0),
        rec("L2", "A", "B", 100),
        rec("R1", "B", "A", 0),
        rec("R2", "B", "A", 50),
    ]
    review = review_pair_uniqueness(records, 50, locked_keys=[("L1", "R2")])
    assert review.optimal_pair_count == 1
    assert [p.as_tuple() for p in review.locked_pairs] == [("L1", "R2")]
    # 锁定对不进入非锁定核查条目；自由池无配对 -> 条目为空。
    assert review.pairs == ()

    # 锁定固定、自由部分仍有等优替代：锁定 (L1,R1)，L2 可配 R2/R3。
    records2 = [
        rec("L1", "A", "B", 0),
        rec("L2", "A", "B", 0),
        rec("R1", "B", "A", 0),
        rec("R2", "B", "A", 0),
        rec("R3", "B", "A", 0),
    ]
    review2 = review_pair_uniqueness(
        records2, 10, locked_keys=[("L1", "R1")]
    )
    assert review2.optimal_pair_count == 2
    assert [p.as_tuple() for p in review2.locked_pairs] == [("L1", "R1")]
    assert len(review2.pairs) == 1
    rv = review2.pairs[0]
    assert rv.pair.as_tuple() == ("L2", "R2")
    assert rv.universal is False
    alt = rv.alternative
    # 替代方案必须保留锁定对；锁定对同时存在于两份方案，不列入增删。
    assert ("L1", "R1") in {p.as_tuple() for p in alt.pairs}
    assert [p.as_tuple() for p in alt.pairs] == [("L1", "R1"), ("L2", "R3")]
    assert [p.as_tuple() for p in alt.added_pairs] == [("L2", "R3")]
    assert [p.as_tuple() for p in alt.removed_pairs] == [("L2", "R2")]


def test_review_matches_current_result_pairs():
    records = [
        rec("1", "A", "B", 0),
        rec("2", "A", "B", 0),
        rec("3", "B", "A", 0),
        rec("4", "B", "A", 0),
    ]
    result = match_records(records, 10)
    review = review_pair_uniqueness(records, 10)
    assert [rv.pair.as_tuple() for rv in review.pairs] == [
        p.as_tuple() for p in result.pairs
    ]
    assert review.optimal_pair_count == len(result.pairs)
    assert (
        review.total_time_diff_seconds == result.total_time_diff_seconds
    )


# --------------------------------------------------------------------------
# 小规模随机穷举对照（判定与替代方案必须与全枚举一致）
# --------------------------------------------------------------------------
def _brute_optima(records, allowed_diff_seconds, locked_keys):
    allowed = int(allowed_diff_seconds * MICROS_PER_SECOND)
    locked = set()
    locked_ids = set()
    locked_total = 0
    rmap = {r.record_no: r for r in records}
    for a, b in locked_keys:
        a, b = sorted((a, b))
        locked.add((a, b))
        locked_ids |= {a, b}
        locked_total += abs(rmap[a].ts_micros - rmap[b].ts_micros)
    free = [r for r in records if r.record_no not in locked_ids]

    candidates = []
    for i in range(len(free)):
        for j in range(i + 1, len(free)):
            a, b = free[i], free[j]
            if (
                a.station == b.counterpart
                and a.counterpart == b.station
                and a.band == b.band
                and a.mode == b.mode
                and abs(a.ts_micros - b.ts_micros) <= allowed
            ):
                candidates.append(
                    (
                        tuple(sorted((a.record_no, b.record_no))),
                        frozenset((i, j)),
                        abs(a.ts_micros - b.ts_micros),
                    )
                )

    feasible = []
    for mask in range(1 << len(candidates)):
        used = set()
        keys, total = [], locked_total
        ok = True
        for idx in range(len(candidates)):
            if mask >> idx & 1:
                key, verts, weight = candidates[idx]
                if used & verts:
                    ok = False
                    break
                used |= verts
                keys.append(key)
                total += weight
        if ok:
            feasible.append(
                (
                    len(keys) + len(locked),
                    total,
                    tuple(sorted(keys + list(locked))),
                )
            )
    max_count = max(c for c, _t, _k in feasible)
    min_total = min(t for c, t, _k in feasible if c == max_count)
    optima = {k for c, t, k in feasible if c == max_count and t == min_total}
    return optima, max_count, min_total


def test_randomized_against_bruteforce():
    rng = random.Random(20261006)
    stations = ["A", "B", "C", "D"]
    for _trial in range(200):
        n = rng.randint(0, 7)
        records = []
        for k in range(n):
            s = rng.choice(stations)
            c = rng.choice([x for x in stations if x != s])
            records.append(
                rec(
                    f"r{k}", s, c, rng.randint(0, 60),
                    band=rng.choice(["40m", "20m"]),
                    mode=rng.choice(["SSB", "CW"]),
                )
            )
        allowed = rng.choice([0, 5, 30, 100])
        locks = []
        valid_pairs = []
        for i in range(n):
            for j in range(i + 1, n):
                a, b = records[i], records[j]
                if (
                    a.station == b.counterpart
                    and a.counterpart == b.station
                    and a.band == b.band
                    and a.mode == b.mode
                    and abs(a.ts_micros - b.ts_micros)
                    <= allowed * MICROS_PER_SECOND
                ):
                    valid_pairs.append((a.record_no, b.record_no))
        if valid_pairs and rng.random() < 0.5:
            locks = [rng.choice(valid_pairs)]

        review = review_pair_uniqueness(records, allowed, locks)
        optima, max_count, min_total = _brute_optima(
            records, allowed, locks
        )
        assert review.optimal_pair_count == max_count
        assert abs(
            review.total_time_diff_seconds * MICROS_PER_SECOND - min_total
        ) < 1e-6

        result = match_records(records, allowed, locked_keys=locks)
        locked_set = {tuple(sorted(k)) for k in locks}
        assert {rv.pair.as_tuple() for rv in review.pairs} == {
            p.as_tuple() for p in result.pairs
            if p.as_tuple() not in locked_set
        }
        cur_keys = tuple(p.as_tuple() for p in result.pairs)
        for rv in review.pairs:
            key = rv.pair.as_tuple()
            in_all = all(key in m for m in optima)
            assert rv.universal == in_all
            if not in_all:
                expected_alt = min(m for m in optima if key not in m)
                got = tuple(p.as_tuple() for p in rv.alternative.pairs)
                assert got == expected_alt
                assert {
                    p.as_tuple() for p in rv.alternative.removed_pairs
                } == set(cur_keys) - set(expected_alt)
                assert {
                    p.as_tuple() for p in rv.alternative.added_pairs
                } == set(expected_alt) - set(cur_keys)
                alt_matched = {x for pair in expected_alt for x in pair}
                assert tuple(sorted(rv.alternative.unpaired)) == tuple(
                    sorted(
                        r.record_no for r in records
                        if r.record_no not in alt_matched
                    )
                )


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


_SYMMETRIC_LOGS = [
    _log("1", "A", "B", "2026-10-04T00:00:00+00:00"),
    _log("2", "A", "B", "2026-10-04T00:00:00+00:00"),
    _log("3", "B", "A", "2026-10-04T00:00:00+00:00"),
    _log("4", "B", "A", "2026-10-04T00:00:00+00:00"),
]


def test_endpoint_replaceable_pairs(client):
    client.post(
        "/drills",
        json={"drill_id": "UNI", "allowed_time_diff_seconds": 10},
    )
    r = client.post("/drills/UNI/logs", json={"logs": _SYMMETRIC_LOGS})
    assert r.status_code == 200, r.text
    version = r.json()["version"]

    r = client.get("/drills/UNI/pair-uniqueness")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["drill_id"] == "UNI"
    assert body["version"] == version
    assert body["sealed"] is False
    assert body["allowed_time_diff_seconds"] == 10
    assert body["optimal_pair_count"] == 2
    assert body["total_time_diff_seconds"] == 0
    assert body["locked_pairs"] == []
    assert [
        (p["record_a"], p["record_b"]) for p in body["pairs"]
    ] == [("1", "3"), ("2", "4")]
    for item in body["pairs"]:
        assert item["universal"] is False
        alt = item["alternative"]
        assert alt is not None
        assert len(alt["pairs"]) == 2
        assert alt["total_time_diff_seconds"] == 0
        assert (
            (item["record_a"], item["record_b"])
            not in {(p["record_a"], p["record_b"]) for p in alt["pairs"]}
        )
    first = body["pairs"][0]
    assert first == {
        "record_a": "1",
        "record_b": "3",
        "time_diff_seconds": 0.0,
        "universal": False,
        "alternative": {
            "pairs": [
                {"record_a": "1", "record_b": "4", "time_diff_seconds": 0.0},
                {"record_a": "2", "record_b": "3", "time_diff_seconds": 0.0},
            ],
            "unpaired": [],
            "total_time_diff_seconds": 0.0,
            "added_pairs": [
                {"record_a": "1", "record_b": "4", "time_diff_seconds": 0.0},
                {"record_a": "2", "record_b": "3", "time_diff_seconds": 0.0},
            ],
            "removed_pairs": [
                {"record_a": "1", "record_b": "3", "time_diff_seconds": 0.0},
                {"record_a": "2", "record_b": "4", "time_diff_seconds": 0.0},
            ],
        },
    }


def test_endpoint_unique_pairs_marked_universal(client):
    client.post(
        "/drills",
        json={"drill_id": "U2", "allowed_time_diff_seconds": 10},
    )
    client.post("/drills/U2/logs", json={"logs": [
        _log("1", "A", "B", "2026-10-04T00:00:00+00:00"),
        _log("2", "A", "B", "2026-10-04T00:00:10+00:00"),
        _log("3", "B", "A", "2026-10-04T00:00:00+00:00"),
        _log("4", "B", "A", "2026-10-04T00:00:10+00:00"),
    ]})
    body = client.get("/drills/U2/pair-uniqueness").json()
    assert body["optimal_pair_count"] == 2
    assert body["total_time_diff_seconds"] == 0
    assert {
        (p["record_a"], p["record_b"]) for p in body["pairs"]
    } == {("1", "3"), ("2", "4")}
    assert all(p["universal"] for p in body["pairs"])
    assert all(p["alternative"] is None for p in body["pairs"])


def test_endpoint_locked_pair_not_reviewed_and_forced(client):
    client.post(
        "/drills",
        json={"drill_id": "U3", "allowed_time_diff_seconds": 50},
    )
    client.post("/drills/U3/logs", json={"logs": [
        _log("L1", "A", "B", "2026-10-04T00:00:00+00:00"),
        _log("L2", "A", "B", "2026-10-04T00:01:40+00:00"),
        _log("R1", "B", "A", "2026-10-04T00:00:00+00:00"),
        _log("R2", "B", "A", "2026-10-04T00:00:50+00:00"),
    ]})
    r = client.put("/drills/U3/locks", json={"record_a": "L1", "record_b": "R2"})
    assert r.status_code == 200
    body = client.get("/drills/U3/pair-uniqueness").json()
    assert body["version"] == r.json()["result"]["version"]
    assert body["optimal_pair_count"] == 1
    assert [(p["record_a"], p["record_b"]) for p in body["locked_pairs"]] == [
        ("L1", "R2")
    ]
    # 锁定后自由池无配对 -> 非锁定核查条目为空。
    assert body["pairs"] == []


def test_endpoint_excludes_quarantined_logs(client):
    # 1/2/3/4 对称等优；隔离 3、4 后，1、2 落单 -> 空核查清单。
    client.post(
        "/drills",
        json={"drill_id": "U4", "allowed_time_diff_seconds": 10},
    )
    client.post("/drills/U4/logs", json={"logs": _SYMMETRIC_LOGS})
    r = client.put("/drills/U4/quarantine", json={"record_nos": ["3", "4"]})
    assert r.status_code == 200, r.text
    body = client.get("/drills/U4/pair-uniqueness").json()
    assert body["version"] == r.json()["result"]["version"]
    assert body["optimal_pair_count"] == 0
    assert body["pairs"] == []
    assert body["locked_pairs"] == []

    # 恢复后核查反映现态（两对均可替换）。
    client.post("/drills/U4/quarantine/restore", json={"record_nos": ["3", "4"]})
    body = client.get("/drills/U4/pair-uniqueness").json()
    assert body["optimal_pair_count"] == 2
    assert all(p["universal"] is False for p in body["pairs"])


def test_endpoint_empty_drill(client):
    client.post(
        "/drills",
        json={"drill_id": "U5", "allowed_time_diff_seconds": 10},
    )
    body = client.get("/drills/U5/pair-uniqueness").json()
    assert body["version"] == 0
    assert body["optimal_pair_count"] == 0
    assert body["total_time_diff_seconds"] == 0
    assert body["pairs"] == []
    assert body["locked_pairs"] == []


def test_endpoint_is_read_only(client):
    client.post(
        "/drills",
        json={"drill_id": "U6", "allowed_time_diff_seconds": 10},
    )
    client.post("/drills/U6/logs", json={"logs": _SYMMETRIC_LOGS})
    before = client.get("/drills/U6/result").json()
    snapshots_before = client.get("/drills/U6/snapshots").json()
    for _ in range(3):
        r = client.get("/drills/U6/pair-uniqueness")
        assert r.status_code == 200
    after = client.get("/drills/U6/result").json()
    assert after["version"] == before["version"]
    assert after["pairs"] == before["pairs"]
    assert client.get("/drills/U6/snapshots").json() == snapshots_before


def test_endpoint_available_after_seal(client):
    client.post(
        "/drills",
        json={"drill_id": "U7", "allowed_time_diff_seconds": 10},
    )
    client.post("/drills/U7/logs", json={"logs": _SYMMETRIC_LOGS})
    assert client.post("/drills/U7/seal").status_code == 200
    r = client.get("/drills/U7/pair-uniqueness")
    assert r.status_code == 200
    body = r.json()
    assert body["sealed"] is True
    assert body["optimal_pair_count"] == 2
    assert all(p["universal"] is False for p in body["pairs"])


def test_endpoint_unknown_drill_is_404(client):
    r = client.get("/drills/NOPE/pair-uniqueness")
    assert r.status_code == 404
    assert r.json()["error"] == "drill_not_found"


def test_result_endpoint_unchanged(client):
    # 原结果接口仍按既有三级优先级选定配对，请求/响应兼容。
    client.post(
        "/drills",
        json={"drill_id": "U8", "allowed_time_diff_seconds": 10},
    )
    r = client.post("/drills/U8/logs", json={"logs": _SYMMETRIC_LOGS})
    result = r.json()
    assert [(p["record_a"], p["record_b"]) for p in result["pairs"]] == [
        ("1", "3"), ("2", "4")
    ]
    assert set(result) == {
        "drill_id", "version", "sealed", "pairs", "unpaired",
        "total_time_diff_seconds", "recomputed_at",
    }
