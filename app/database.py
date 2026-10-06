"""SQLite 持久化层。

并发写入由 SQLite 的写事务串行化保证：每次写入都开启
``BEGIN IMMEDIATE`` 立即获取 RESERVED 锁，第二个并发写入会在获取锁时
阻塞/报忙；配合 busy_timeout 重试，所有写入被串行提交。版本号、
日志与配对结果在同一个事务中更新，保证原子性。

启用 WAL 模式以支持“封存/计算期间仍可并发查询最终结果”。
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import secrets
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Iterable

from . import config
from .matching import (
    GAP_REASON_LABELS,
    InvalidLockError,
    MatchResult,
    Pair,
    Record,
    diagnose_gaps,
    match_records,
    pair_eligibility,
)

MICROS_PER_SECOND = 1_000_000

SCHEMA = """
CREATE TABLE IF NOT EXISTS drills (
    drill_id           TEXT PRIMARY KEY,
    allowed_diff_secs  REAL NOT NULL CHECK (allowed_diff_secs >= 0),
    version            INTEGER NOT NULL DEFAULT 0,
    sealed             INTEGER NOT NULL DEFAULT 0,
    created_at         TEXT NOT NULL,
    sealed_at          TEXT
);

CREATE TABLE IF NOT EXISTS logs (
    drill_id     TEXT NOT NULL,
    record_no    TEXT NOT NULL,
    station      TEXT NOT NULL,
    counterpart  TEXT NOT NULL,
    ts_micros    INTEGER NOT NULL,
    ts_iso       TEXT NOT NULL,
    band         TEXT NOT NULL,
    mode         TEXT NOT NULL,
    updated_at   TEXT NOT NULL,
    PRIMARY KEY (drill_id, record_no),
    FOREIGN KEY (drill_id) REFERENCES drills(drill_id)
);

CREATE TABLE IF NOT EXISTS results (
    drill_id                  TEXT PRIMARY KEY,
    version                   INTEGER NOT NULL,
    pairs_json                TEXT NOT NULL,
    unpaired_json             TEXT NOT NULL,
    total_time_diff_seconds   REAL NOT NULL,
    recomputed_at             TEXT NOT NULL,
    FOREIGN KEY (drill_id) REFERENCES drills(drill_id)
);

-- 复核员人工锁定（互证）：记录号内部升序，一条记录至多被一个锁定占用。
-- 占用冲突由服务层在写事务内校验（SQLite 不支持跨列互斥约束）。
CREATE TABLE IF NOT EXISTS locks (
    drill_id     TEXT NOT NULL,
    record_a     TEXT NOT NULL,
    record_b     TEXT NOT NULL,
    created_at   TEXT NOT NULL,
    PRIMARY KEY (drill_id, record_a, record_b),
    FOREIGN KEY (drill_id) REFERENCES drills(drill_id)
);

-- 复核员争议隔离：只改变互证资格，不删除日志、不改写原字段。
-- 被隔离记录不参与配对、不进入未配对清单/缺口诊断、不计入台站汇总；
-- 与人工锁定互斥（锁定成员不得隔离，隔离记录不得锁定/迁移）。
CREATE TABLE IF NOT EXISTS quarantine (
    drill_id     TEXT NOT NULL,
    record_no    TEXT NOT NULL,
    created_at   TEXT NOT NULL,
    PRIMARY KEY (drill_id, record_no),
    FOREIGN KEY (drill_id) REFERENCES drills(drill_id)
);

-- 版本快照（复核员按版本回看/比较）：版本真正推进时与版本、结果、锁定
-- 同事务留存该版的配对、未配对记录、总时差、锁定清单与隔离清单，
-- 之后不再改变。v0 在创建演练时留存；重传、失败回滚、封存不产生新快照。
CREATE TABLE IF NOT EXISTS snapshots (
    drill_id                  TEXT NOT NULL,
    version                   INTEGER NOT NULL,
    pairs_json                TEXT NOT NULL,
    unpaired_json             TEXT NOT NULL,
    total_time_diff_seconds   REAL NOT NULL,
    locks_json                TEXT NOT NULL,
    quarantine_json           TEXT NOT NULL,
    created_at                 TEXT NOT NULL,
    PRIMARY KEY (drill_id, version),
    FOREIGN KEY (drill_id) REFERENCES drills(drill_id)
);

-- 服务自身的键值元数据：目前存放批量预演校验标识的 HMAC 密钥
-- （预演本身不落任何状态，密钥随库持久化以保证重启后演练仍可确认）。
CREATE TABLE IF NOT EXISTS meta (
    key    TEXT PRIMARY KEY,
    value  TEXT NOT NULL
);
"""


# --------------------------------------------------------------------------
# 领域错误
# --------------------------------------------------------------------------
class ServiceError(Exception):
    """业务错误基类，附带 HTTP 状态码与机器可读错误码。"""

    status_code = 400
    error_code = "bad_request"

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


class DrillNotFoundError(ServiceError):
    status_code = 404
    error_code = "drill_not_found"


class DrillExistsError(ServiceError):
    status_code = 409
    error_code = "drill_exists"


class SealedError(ServiceError):
    status_code = 409
    error_code = "drill_sealed"


class DuplicateRecordInBatchError(ServiceError):
    status_code = 409
    error_code = "duplicate_record_in_batch"


class LockNotFoundError(ServiceError):
    """解除的锁定不存在。"""

    status_code = 404
    error_code = "lock_not_found"


class LockSameRecordError(ServiceError):
    """锁定请求中两个记录号相同。"""

    status_code = 422
    error_code = "lock_same_record"


class LockRecordMissingError(ServiceError):
    """锁定引用了不存在的记录。"""

    status_code = 404
    error_code = "lock_record_missing"


class LockNotCandidateError(ServiceError):
    """两条现存日志不满足互证候选条件（互指/波段模式/时差）。"""

    status_code = 409
    error_code = "lock_not_candidate"


class LockOccupiedError(ServiceError):
    """锁定涉及的记录已被其他锁定占用。"""

    status_code = 409
    error_code = "record_already_locked"


class LockInvalidatedError(ServiceError):
    """写入/更正使既有锁定失效，整次写入被拒绝。"""

    status_code = 409
    error_code = "lock_invalidated"


class SnapshotNotFoundError(ServiceError):
    """指定版本未留存快照（升级前的旧版本不可用现态补造）。"""

    status_code = 404
    error_code = "snapshot_not_found"


class PreviewTokenMismatchError(ServiceError):
    """确认时提交的校验标识与“演练+基准版本+规范化批次”不符，或演练已封存。"""

    status_code = 409
    error_code = "preview_token_mismatch"


class PreviewBaseVersionChangedError(ServiceError):
    """确认时演练当前版本已不同于预演基准版本。"""

    status_code = 409
    error_code = "preview_base_version_changed"


class SameDrillTransferError(ServiceError):
    """迁移的源演练与目标演练相同。"""

    status_code = 422
    error_code = "same_drill_transfer"


class DuplicateRecordInTransferError(ServiceError):
    """迁移请求的记录号列表内出现重复。"""

    status_code = 409
    error_code = "duplicate_record_in_transfer"


class TransferRecordMissingError(ServiceError):
    """待迁移记录在源演练中不存在。"""

    status_code = 404
    error_code = "transfer_record_missing"


class TargetRecordExistsError(ServiceError):
    """目标演练已存在同号记录，迁移会覆盖目标日志。"""

    status_code = 409
    error_code = "target_record_exists"


class TransferRecordLockedError(ServiceError):
    """待迁移记录参与了源演练的人工锁定，不能迁移。"""

    status_code = 409
    error_code = "transfer_record_locked"


class QuarantineRecordMissingError(ServiceError):
    """隔离/恢复批次引用了不存在的记录。"""

    status_code = 404
    error_code = "quarantine_record_missing"


class DuplicateRecordInQuarantineError(ServiceError):
    """隔离/恢复批次的记录号列表内出现重复。"""

    status_code = 409
    error_code = "duplicate_record_in_quarantine"


class QuarantineRecordLockedError(ServiceError):
    """隔离/恢复批次涉及人工锁定中的记录（锁定成员不得隔离）。"""

    status_code = 409
    error_code = "quarantine_record_locked"


class LockRecordQuarantinedError(ServiceError):
    """锁定涉及被隔离的记录（隔离记录不得人工锁定）。"""

    status_code = 409
    error_code = "lock_record_quarantined"


class TransferRecordQuarantinedError(ServiceError):
    """待迁移记录处于隔离状态，不能跨演练迁移。"""

    status_code = 409
    error_code = "transfer_record_quarantined"


class AdjudicationIntentConflictError(ServiceError):
    """批量裁决中同一对象（记录对/记录）出现相反意图，无法判定最终状态。"""

    status_code = 409
    error_code = "adjudication_intent_conflict"


class AdjudicationBaseVersionChangedError(ServiceError):
    """提交批量裁决时演练当前版本已不同于基准版本。"""

    status_code = 409
    error_code = "adjudication_base_version_changed"


class AdjudicationRecordMissingError(ServiceError):
    """批量裁决引用了不存在的记录。"""

    status_code = 404
    error_code = "adjudication_record_missing"


# --------------------------------------------------------------------------
# 连接管理
# --------------------------------------------------------------------------
def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(
        config.database_url(),
        timeout=30.0,
        isolation_level=None,  # 手工控制事务（autocommit 模式）
    )
    conn.row_factory = sqlite3.Row
    conn.execute(f"PRAGMA busy_timeout = 30000")
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db() -> None:
    """创建数据目录、表结构并开启 WAL（幂等）。"""
    import os

    db_path = config.database_url()
    parent = os.path.dirname(db_path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    conn = connect()
    try:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")
        conn.executescript(SCHEMA)
        # 升级既有库：旧版 snapshots 表补隔离清单列（幂等）。
        _migrate_legacy_columns(conn)
        # 批量预演校验标识所需的 HMAC 密钥（不存在即生成，随库持久化）。
        _ensure_preview_secret(conn)
        # 升级既有库：从升级时的当前版本开始留存快照；更早的版本不补造。
        _backfill_legacy_snapshots(conn)
    finally:
        conn.close()


def _migrate_legacy_columns(conn: sqlite3.Connection) -> None:
    """既有库升级：为旧版 snapshots 表补充 quarantine_json 列。

    隔离功能上线前留存的快照本就没有隔离状态，列缺省 '[]' 即其真实
    内容（历史快照保持原貌）；新库由 SCHEMA 直接建出该列，无需迁移。
    """
    cols = {
        row["name"]
        for row in conn.execute("PRAGMA table_info(snapshots)").fetchall()
    }
    if "quarantine_json" not in cols:
        conn.execute(
            "ALTER TABLE snapshots ADD COLUMN quarantine_json "
            "TEXT NOT NULL DEFAULT '[]'"
        )


# --------------------------------------------------------------------------
# 批量预演校验标识：HMAC-SHA256(drill + 基准版本 + 规范化批次)，无状态
# --------------------------------------------------------------------------
PREVIEW_SECRET_KEY = "preview_hmac_secret"
_PREVIEW_TOKEN_PREFIX = "pv1"

# 进程内缓存密钥：预演/确认只读，密钥随库持久化，缺失时补生成。
_preview_secret_cache: str | None = None


def _ensure_preview_secret(conn: sqlite3.Connection) -> str:
    """确保库中存在预演 HMAC 密钥并缓存；不存在则生成 32 字节随机串。"""
    global _preview_secret_cache
    row = conn.execute(
        "SELECT value FROM meta WHERE key = ?", (PREVIEW_SECRET_KEY,)
    ).fetchone()
    if row is not None and row["value"]:
        _preview_secret_cache = row["value"]
        return _preview_secret_cache
    secret = secrets.token_hex(32)
    # INSERT OR IGNORE：并发初始化时以已落库者为准。
    conn.execute(
        "INSERT OR IGNORE INTO meta (key, value) VALUES (?, ?)",
        (PREVIEW_SECRET_KEY, secret),
    )
    row = conn.execute(
        "SELECT value FROM meta WHERE key = ?", (PREVIEW_SECRET_KEY,)
    ).fetchone()
    _preview_secret_cache = row["value"]
    return _preview_secret_cache


def _server_secret() -> str:
    """取预演 HMAC 密钥（缓存优先；缓存为空时落库取/建一个）。"""
    global _preview_secret_cache
    if _preview_secret_cache:
        return _preview_secret_cache
    conn = connect()
    try:
        return _ensure_preview_secret(conn)
    finally:
        conn.close()


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64url_decode(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _preview_signing_payload(
    drill_id: str, base_version: int, items: list[dict]
) -> bytes:
    """构造“演练 + 基准版本 + 规范化批次”的确定性字节串。"""
    canonical = {
        "drill_id": drill_id,
        "base_version": base_version,
        "logs": [
            {
                "record_no": it["record_no"],
                "station": it["station"],
                "counterpart": it["counterpart"],
                "ts_micros": it["ts_micros"],
                "band": it["band"],
                "mode": it["mode"],
            }
            for it in items
        ],
    }
    return json.dumps(
        canonical, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def _make_preview_token(
    drill_id: str, base_version: int, items: list[dict]
) -> str:
    payload = _preview_signing_payload(drill_id, base_version, items)
    mac = hmac.new(
        _server_secret().encode("ascii"), payload, hashlib.sha256
    ).digest()
    return f"{_PREVIEW_TOKEN_PREFIX}.{_b64url(payload)}.{_b64url(mac)}"


def _verify_preview_token(
    token: str, drill_id: str, base_version: int, items: list[dict]
) -> bool:
    """恒定时间比对校验标识是否绑定该演练/基准版本/规范化批次。"""
    try:
        prefix, payload_b64, mac_b64 = token.split(".", 2)
        if prefix != _PREVIEW_TOKEN_PREFIX:
            return False
        payload = _b64url_decode(payload_b64)
        mac = _b64url_decode(mac_b64)
    except (ValueError, binascii.Error):
        return False
    expected = _preview_signing_payload(drill_id, base_version, items)
    expected_mac = hmac.new(
        _server_secret().encode("ascii"), expected, hashlib.sha256
    ).digest()
    return hmac.compare_digest(payload, expected) and hmac.compare_digest(
        mac, expected_mac
    )


def _backfill_legacy_snapshots(conn: sqlite3.Connection) -> None:
    """为升级前已存在、尚无快照的演练留存“当前版本”快照。

    每个这样的演练仅在其当前版本留存一份（内容取自升级时的现态），
    之前版本不可用现态补造。幂等：已有任一快照的演练整体跳过。
    """
    rows = conn.execute(
        """
        SELECT d.drill_id AS drill_id
        FROM drills d
        WHERE NOT EXISTS (
            SELECT 1 FROM snapshots s WHERE s.drill_id = d.drill_id
        )
        """
    ).fetchall()
    if not rows:
        return
    conn.execute("BEGIN IMMEDIATE")
    try:
        now = _now_iso()
        for r in rows:
            drill_id = r["drill_id"]
            res = conn.execute(
                "SELECT * FROM results WHERE drill_id = ?", (drill_id,)
            ).fetchone()
            if res is None:
                # 理论上不会发生（建演练即写空结果）；防御性补一条空结果。
                pairs_json, unpaired_json, total = "[]", "[]", 0.0
                version = conn.execute(
                    "SELECT version FROM drills WHERE drill_id = ?",
                    (drill_id,),
                ).fetchone()["version"]
            else:
                pairs_json = res["pairs_json"]
                unpaired_json = res["unpaired_json"]
                total = res["total_time_diff_seconds"]
                version = res["version"]
            locks = _lock_items(conn, drill_id)
            quarantine = _quarantine_items(conn, drill_id)
            conn.execute(
                "INSERT INTO snapshots (drill_id, version, pairs_json, "
                "unpaired_json, total_time_diff_seconds, locks_json, "
                "quarantine_json, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    drill_id, version, pairs_json, unpaired_json, total,
                    json.dumps(locks, ensure_ascii=False),
                    json.dumps(quarantine, ensure_ascii=False), now,
                ),
            )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise


@contextmanager
def _write_tx(conn: sqlite3.Connection):
    """BEGIN IMMEDIATE 写事务：立即拿写锁，提交/回滚原子。"""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise


@contextmanager
def _read_tx(conn: sqlite3.Connection):
    """延迟读事务：WAL 下整段读取同一一致性快照，不获取写锁、不写库。"""
    conn.execute("BEGIN")
    try:
        yield
    finally:
        conn.execute("ROLLBACK")


# --------------------------------------------------------------------------
# 序列化辅助
# --------------------------------------------------------------------------
def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _micros_to_iso(ts_micros: int) -> str:
    return datetime.fromtimestamp(
        ts_micros / MICROS_PER_SECOND, tz=timezone.utc
    ).isoformat()


def _record_from_row(row: sqlite3.Row) -> Record:
    return Record(
        record_no=row["record_no"],
        station=row["station"],
        counterpart=row["counterpart"],
        ts_micros=row["ts_micros"],
        band=row["band"],
        mode=row["mode"],
    )


def _load_records(conn: sqlite3.Connection, drill_id: str) -> list[Record]:
    rows = conn.execute(
        "SELECT * FROM logs WHERE drill_id = ? ORDER BY rowid", (drill_id,)
    ).fetchall()
    return [_record_from_row(r) for r in rows]


def _record_map(records: Iterable[Record]) -> dict[str, Record]:
    return {r.record_no: r for r in records}


def _load_lock_keys(conn: sqlite3.Connection, drill_id: str) -> list[tuple[str, str]]:
    rows = conn.execute(
        "SELECT record_a, record_b FROM locks WHERE drill_id = ? "
        "ORDER BY record_a, record_b",
        (drill_id,),
    ).fetchall()
    return [(r["record_a"], r["record_b"]) for r in rows]


def _load_quarantine_nos(conn: sqlite3.Connection, drill_id: str) -> set[str]:
    """当前被隔离的记录号集合。"""
    rows = conn.execute(
        "SELECT record_no FROM quarantine WHERE drill_id = ?", (drill_id,)
    ).fetchall()
    return {r["record_no"] for r in rows}


def _quarantine_items(conn: sqlite3.Connection, drill_id: str) -> list[dict]:
    """组装隔离清单（按记录号升序）。"""
    rows = conn.execute(
        "SELECT record_no, created_at FROM quarantine WHERE drill_id = ? "
        "ORDER BY record_no",
        (drill_id,),
    ).fetchall()
    return [
        {"record_no": r["record_no"], "quarantined_at": r["created_at"]}
        for r in rows
    ]


def _check_lock_candidate(
    record_map: dict[str, Record], allowed_micros: int, a: str, b: str
) -> None:
    """把算法层原因码翻译为对应业务错误抛出；合法时静默返回。"""
    problem = pair_eligibility(record_map, allowed_micros, a, b)
    if problem is None:
        return
    code, message = problem
    if code == "same_record":
        raise LockSameRecordError(message)
    if code == "record_missing":
        raise LockRecordMissingError(message)
    raise LockNotCandidateError(message)


def _persist_result(
    conn: sqlite3.Connection, drill_id: str, version: int, result: MatchResult
) -> None:
    pairs_payload = [
        {
            "record_a": p.record_a,
            "record_b": p.record_b,
            "time_diff_seconds": p.time_diff_seconds,
        }
        for p in result.pairs
    ]
    conn.execute(
        """
        INSERT INTO results (drill_id, version, pairs_json, unpaired_json,
                             total_time_diff_seconds, recomputed_at)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(drill_id) DO UPDATE SET
            version = excluded.version,
            pairs_json = excluded.pairs_json,
            unpaired_json = excluded.unpaired_json,
            total_time_diff_seconds = excluded.total_time_diff_seconds,
            recomputed_at = excluded.recomputed_at
        """,
        (
            drill_id,
            version,
            json.dumps(pairs_payload, ensure_ascii=False),
            json.dumps(list(result.unpaired), ensure_ascii=False),
            result.total_time_diff_seconds,
            _now_iso(),
        ),
    )


def _persist_snapshot(
    conn: sqlite3.Connection,
    drill_id: str,
    version: int,
    result: MatchResult,
    locks: list[dict],
    quarantine: list[dict],
) -> None:
    """在写事务内留存版本快照（配对/未配对/总时差/锁定清单/隔离清单）。

    与版本推进、结果重算在同一事务提交；快照一旦留存不再改变。
    """
    pairs_payload = [
        {
            "record_a": p.record_a,
            "record_b": p.record_b,
            "time_diff_seconds": p.time_diff_seconds,
        }
        for p in result.pairs
    ]
    conn.execute(
        """
        INSERT INTO snapshots (drill_id, version, pairs_json, unpaired_json,
                               total_time_diff_seconds, locks_json,
                               quarantine_json, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            drill_id,
            version,
            json.dumps(pairs_payload, ensure_ascii=False),
            json.dumps(list(result.unpaired), ensure_ascii=False),
            result.total_time_diff_seconds,
            json.dumps(locks, ensure_ascii=False),
            json.dumps(quarantine, ensure_ascii=False),
            _now_iso(),
        ),
    )


def _recompute(conn: sqlite3.Connection, drill_id: str) -> None:
    """重算全场配对：版本 +1（调用方负责），结果与版本同事务落库。

    被隔离记录不参与配对、不进入未配对清单；先保留全部锁定对，再对
    剩余未隔离日志按“配对数最多、总时差最小、记录号字典序最小”求解；
    锁定即使降低全场配对数也不替换。
    """
    drill = _get_drill_row(conn, drill_id)
    version = drill["version"] + 1
    records = _load_records(conn, drill_id)
    quarantined = _load_quarantine_nos(conn, drill_id)
    active = [r for r in records if r.record_no not in quarantined]
    lock_keys = _load_lock_keys(conn, drill_id)
    try:
        result = match_records(
            active, drill["allowed_diff_secs"], locked_keys=lock_keys
        )
    except InvalidLockError as exc:
        raise LockInvalidatedError(str(exc))
    conn.execute(
        "UPDATE drills SET version = ? WHERE drill_id = ?",
        (version, drill_id),
    )
    _persist_result(conn, drill_id, version, result)
    # 版本真正推进：同事务留存该版快照（含当时锁定清单与隔离清单）。
    rmap = _record_map(records)
    _persist_snapshot(
        conn,
        drill_id,
        version,
        result,
        _lock_items(conn, drill_id, rmap),
        _quarantine_items(conn, drill_id),
    )


def _get_drill_row(
    conn: sqlite3.Connection, drill_id: str
) -> sqlite3.Row:
    row = conn.execute(
        "SELECT * FROM drills WHERE drill_id = ?", (drill_id,)
    ).fetchone()
    if row is None:
        raise DrillNotFoundError(f"演练号不存在: {drill_id}")
    return row


def _require_unsealed(row: sqlite3.Row) -> None:
    if row["sealed"]:
        raise SealedError(f"演练 {row['drill_id']} 已封存，拒绝写入")


# --------------------------------------------------------------------------
# 对外服务操作
# --------------------------------------------------------------------------
def create_drill(drill_id: str, allowed_diff_secs: float) -> dict:
    conn = connect()
    try:
        with _write_tx(conn):
            exists = conn.execute(
                "SELECT 1 FROM drills WHERE drill_id = ?", (drill_id,)
            ).fetchone()
            if exists is not None:
                raise DrillExistsError(f"演练号已存在: {drill_id}")
            conn.execute(
                "INSERT INTO drills (drill_id, allowed_diff_secs, version, "
                "sealed, created_at) VALUES (?, ?, 0, 0, ?)",
                (drill_id, allowed_diff_secs, _now_iso()),
            )
            # 空场结果：版本 0，无配对。
            empty = MatchResult((), (), 0.0)
            _persist_result(conn, drill_id, 0, empty)
            # 创建演练的 0 版即可回看：同事务留存空场快照。
            _persist_snapshot(conn, drill_id, 0, empty, [], [])
        return get_drill(drill_id)
    finally:
        conn.close()


def upsert_log(
    drill_id: str,
    record_no: str,
    station: str,
    counterpart: str,
    ts_micros: int,
    band: str,
    mode: str,
) -> dict:
    """写入/更正单条日志；同内容重传不推进版本，异内容更正后重算全场。"""
    conn = connect()
    try:
        changed = False
        with _write_tx(conn):
            drill = _get_drill_row(conn, drill_id)
            _require_unsealed(drill)
            row = conn.execute(
                "SELECT * FROM logs WHERE drill_id = ? AND record_no = ?",
                (drill_id, record_no),
            ).fetchone()
            ts_iso = _micros_to_iso(ts_micros)
            if row is None:
                conn.execute(
                    "INSERT INTO logs (drill_id, record_no, station, "
                    "counterpart, ts_micros, ts_iso, band, mode, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        drill_id, record_no, station, counterpart,
                        ts_micros, ts_iso, band, mode, _now_iso(),
                    ),
                )
                changed = True
            elif (
                row["station"], row["counterpart"], row["ts_micros"],
                row["band"], row["mode"],
            ) != (station, counterpart, ts_micros, band, mode):
                conn.execute(
                    "UPDATE logs SET station = ?, counterpart = ?, "
                    "ts_micros = ?, ts_iso = ?, band = ?, mode = ?, "
                    "updated_at = ? WHERE drill_id = ? AND record_no = ?",
                    (
                        station, counterpart, ts_micros, ts_iso,
                        band, mode, _now_iso(), drill_id, record_no,
                    ),
                )
                changed = True
            # 同内容重传：什么都不改，版本不推进。
            if changed:
                _recompute(conn, drill_id)
        return get_result(drill_id)
    finally:
        conn.close()


def _check_batch_unique(items: list[dict]) -> None:
    """批次内同一记录号出现多次视为非法（无法判定终态）。"""
    seen: set[str] = set()
    for it in items:
        if it["record_no"] in seen:
            raise DuplicateRecordInBatchError(
                f"批次内记录号重复: {it['record_no']}"
            )
        seen.add(it["record_no"])


def _apply_batch_items(
    conn: sqlite3.Connection, drill_id: str, items: list[dict]
) -> bool:
    """在写事务内按批量写入语义 upsert 整批日志，返回是否有任一记录变化。

    同内容（规范化后一致）不写；新记录插入、异内容更正。不负责重算，
    由调用方在有变化时触发全场重算（重算内含锁定有效性校验，
    失效抛 ``lock_invalidated``，事务随异常整批回滚）。
    """
    changed = False
    for it in items:
        record_no = it["record_no"]
        ts_micros = it["ts_micros"]
        ts_iso = _micros_to_iso(ts_micros)
        row = conn.execute(
            "SELECT * FROM logs WHERE drill_id = ? AND record_no = ?",
            (drill_id, record_no),
        ).fetchone()
        if row is None:
            conn.execute(
                "INSERT INTO logs (drill_id, record_no, station, "
                "counterpart, ts_micros, ts_iso, band, mode, "
                "updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    drill_id, record_no, it["station"],
                    it["counterpart"], ts_micros, ts_iso,
                    it["band"], it["mode"], _now_iso(),
                ),
            )
            changed = True
        elif (
            row["station"], row["counterpart"], row["ts_micros"],
            row["band"], row["mode"],
        ) != (
            it["station"], it["counterpart"], ts_micros,
            it["band"], it["mode"],
        ):
            conn.execute(
                "UPDATE logs SET station = ?, counterpart = ?, "
                "ts_micros = ?, ts_iso = ?, band = ?, mode = ?, "
                "updated_at = ? WHERE drill_id = ? AND record_no = ?",
                (
                    it["station"], it["counterpart"], ts_micros,
                    ts_iso, it["band"], it["mode"], _now_iso(),
                    drill_id, record_no,
                ),
            )
            changed = True
    return changed


def batch_upsert_logs(
    drill_id: str, items: Iterable[dict]
) -> dict:
    """批量写入/更正：整体一个事务，任一非法则整批拒绝。

    批次内同一记录号出现多次视为非法（无法判定终态）。
    """
    items = list(items)
    _check_batch_unique(items)

    conn = connect()
    try:
        with _write_tx(conn):
            drill = _get_drill_row(conn, drill_id)
            _require_unsealed(drill)
            changed = _apply_batch_items(conn, drill_id, items)
            if changed:
                _recompute(conn, drill_id)
        return get_result(drill_id)
    finally:
        conn.close()


# --------------------------------------------------------------------------
# 批量写入预演与确认（复核员提交前预演）
#
# 预演是只读操作：在 WAL 一致性读快照上把整批 upsert 在内存中模拟一遍，
# 沿用与正式写入完全相同的规范化、批内重复校验、锁定有效性校验与全场
# 最优配对规则，输出当前结果与拟写入结果；不改日志、锁定、结果、版本、
# 快照。返回的校验标识（无状态 HMAC）绑定“演练 + 基准版本 + 规范化批次”。
# 确认在写事务内核验封存/基准版本/标识后，完全走现有批量写入语义。
# --------------------------------------------------------------------------
def _result_payload_from_match(
    drill_id: str,
    version: int,
    sealed: bool,
    result: MatchResult,
    recomputed_at: str | None = None,
) -> dict:
    return {
        "drill_id": drill_id,
        "version": version,
        "sealed": sealed,
        "pairs": [
            {
                "record_a": p.record_a,
                "record_b": p.record_b,
                "time_diff_seconds": p.time_diff_seconds,
            }
            for p in result.pairs
        ],
        "unpaired": list(result.unpaired),
        "total_time_diff_seconds": result.total_time_diff_seconds,
        "recomputed_at": recomputed_at,
    }


def _simulate_batch(
    records: list[Record], items: list[dict]
) -> list[Record]:
    """在内存中按批量 upsert 语义模拟整批写入（新记录追加、异内容替换）。"""
    by_no = {r.record_no: i for i, r in enumerate(records)}
    simulated = list(records)
    for it in items:
        candidate = Record(
            record_no=it["record_no"],
            station=it["station"],
            counterpart=it["counterpart"],
            ts_micros=it["ts_micros"],
            band=it["band"],
            mode=it["mode"],
        )
        idx = by_no.get(it["record_no"])
        if idx is None:
            by_no[it["record_no"]] = len(simulated)
            simulated.append(candidate)
        else:
            simulated[idx] = candidate
    return simulated


def _batch_changes_records(
    record_map: dict[str, Record], items: list[dict]
) -> bool:
    """按批量 upsert 语义判断整批是否含任一“新增或异内容更正”记录。"""
    for it in items:
        existing = record_map.get(it["record_no"])
        if existing is None:
            return True
        if (
            existing.station, existing.counterpart, existing.ts_micros,
            existing.band, existing.mode,
        ) != (
            it["station"], it["counterpart"], it["ts_micros"],
            it["band"], it["mode"],
        ):
            return True
    return False


def preview_batch_logs(drill_id: str, items: Iterable[dict]) -> dict:
    """只读预演：返回当前版本、拟写入完整结果、配对/未配对差异与校验标识。

    非法记录（由请求模型拦截）、批内记录号重复、演练已封存、批次使任一
    既有锁定失效均按现有批量写入的规则报错；预演不改变任何状态。
    """
    items = list(items)
    _check_batch_unique(items)

    conn = connect()
    try:
        with _read_tx(conn):
            drill = _get_drill_row(conn, drill_id)
            _require_unsealed(drill)
            base_version = drill["version"]
            records = _load_records(conn, drill_id)
            lock_keys = _load_lock_keys(conn, drill_id)
            quarantined = _load_quarantine_nos(conn, drill_id)

            # 预演遵守隔离状态：被隔离记录在拟写入结果中仍不参与配对
            # （隔离期间更正日志不改变其隔离状态）。
            simulated = _simulate_batch(records, items)
            active = [
                r for r in simulated if r.record_no not in quarantined
            ]
            try:
                proposed = match_records(
                    active, drill["allowed_diff_secs"],
                    locked_keys=lock_keys,
                )
            except InvalidLockError as exc:
                raise LockInvalidatedError(str(exc))

            current = _result_from_conn(conn, drill_id)
            changed = _batch_changes_records(_record_map(records), items)
        proposed_version = base_version + 1 if changed else base_version
        proposed_payload = _result_payload_from_match(
            drill_id, proposed_version, False, proposed
        )
        token = _make_preview_token(drill_id, base_version, items)
        return {
            "drill_id": drill_id,
            "base_version": base_version,
            "current_result": current,
            "proposed_version": proposed_version,
            "would_advance_version": changed,
            "proposed_result": proposed_payload,
            "diff": {
                "pairs": _pairs_diff(current["pairs"], proposed_payload["pairs"]),
                "unpaired": _unpaired_diff(
                    current["unpaired"], proposed_payload["unpaired"]
                ),
                "total_time_diff_seconds": {
                    "from": current["total_time_diff_seconds"],
                    "to": proposed_payload["total_time_diff_seconds"],
                    "delta": (
                        proposed_payload["total_time_diff_seconds"]
                        - current["total_time_diff_seconds"]
                    ),
                },
            },
            "normalized_batch": [
                {
                    "record_no": it["record_no"],
                    "station": it["station"],
                    "counterpart": it["counterpart"],
                    "time": _micros_to_iso(it["ts_micros"]),
                    "band": it["band"],
                    "mode": it["mode"],
                }
                for it in items
            ],
            "token": token,
        }
    finally:
        conn.close()


def confirm_batch_logs(
    drill_id: str, items: Iterable[dict], base_version: int, token: str
) -> dict:
    """按预演校验标识确认提交：标识不符/版本已变化/已封存均拒绝且不改状态。

    校验通过后完全沿用批量写入语义（整批 upsert、有变化才重算推进版本、
    同内容重传不推进版本）在一个原子事务内落库。
    """
    items = list(items)
    _check_batch_unique(items)

    conn = connect()
    try:
        with _write_tx(conn):
            drill = _get_drill_row(conn, drill_id)
            _require_unsealed(drill)
            if drill["version"] != base_version:
                raise PreviewBaseVersionChangedError(
                    f"演练 {drill_id} 当前版本 {drill['version']} "
                    f"与预演基准版本 {base_version} 不一致，请重新预演"
                )
            if not _verify_preview_token(
                token, drill_id, base_version, items
            ):
                raise PreviewTokenMismatchError(
                    "校验标识与演练、基准版本或规范化批次不匹配，"
                    "可能批次已改动或演练已封存，请重新预演"
                )
            changed = _apply_batch_items(conn, drill_id, items)
            if changed:
                _recompute(conn, drill_id)
        return get_result(drill_id)
    finally:
        conn.close()


def seal_drill(drill_id: str) -> dict:
    conn = connect()
    try:
        with _write_tx(conn):
            drill = _get_drill_row(conn, drill_id)
            _require_unsealed(drill)
            conn.execute(
                "UPDATE drills SET sealed = 1, sealed_at = ? WHERE drill_id = ?",
                (_now_iso(), drill_id),
            )
        return get_drill(drill_id)
    finally:
        conn.close()


# --------------------------------------------------------------------------
# 日志归错演练：跨演练迁移（复核员把记录从源演练移至目标演练）
#
# 单次写事务（BEGIN IMMEDIATE）内完成全部校验与改动：
#   * 两演练均存在且未封存、源/目标不同；
#   * 记录号列表非空、无重复；源记录全部存在；目标无同号记录；
#   * 被移记录不参与源演练的任何人工锁定（未迁移的锁定照常生效）。
# 任一条件不满足整次拒绝、回滚，两端状态不变。成功时日志原字段（含
# 规范化后的呼号/波段/模式、UTC 时间与 updated_at）原样转存，两端各
# 重算一次并各只推进一次版本，同事务留存各自的新版本快照；重算时源
# 沿用源允许时差、目标沿用目标允许时差。双方日志、版本、结果与快照
# 在同一事务原子提交。
# --------------------------------------------------------------------------
def transfer_logs(
    source_drill_id: str,
    target_drill_id: str,
    record_nos: Iterable[str],
) -> dict:
    """把源演练中的指定记录原子迁移到目标演练。"""
    record_nos = list(record_nos)
    # 无重复：保留请求顺序用于回显（服务层已保证非空）。
    seen: set[str] = set()
    ordered: list[str] = []
    for no in record_nos:
        if no in seen:
            raise DuplicateRecordInTransferError(f"迁移记录号重复: {no}")
        seen.add(no)
        ordered.append(no)

    conn = connect()
    try:
        with _write_tx(conn):
            source = _get_drill_row(conn, source_drill_id)
            # 目标未知同样报 drill_not_found（先确认两端演练都存在）。
            target = _get_drill_row(conn, target_drill_id)
            if source_drill_id == target_drill_id:
                raise SameDrillTransferError(
                    "源演练与目标演练不能相同，无需迁移"
                )
            _require_unsealed(source)
            _require_unsealed(target)

            placeholders = ",".join("?" for _ in ordered)
            rows = conn.execute(
                f"SELECT * FROM logs WHERE drill_id = ? AND record_no IN "
                f"({placeholders})",
                (source_drill_id, *ordered),
            ).fetchall()
            found = {r["record_no"] for r in rows}
            missing = [no for no in ordered if no not in found]
            if missing:
                raise TransferRecordMissingError(
                    f"源演练 {source_drill_id} 中记录不存在: "
                    + ", ".join(missing)
                )

            target_rows = conn.execute(
                f"SELECT record_no FROM logs WHERE drill_id = ? AND record_no IN "
                f"({placeholders})",
                (target_drill_id, *ordered),
            ).fetchall()
            if target_rows:
                clash = sorted(r["record_no"] for r in target_rows)
                raise TargetRecordExistsError(
                    f"目标演练 {target_drill_id} 已存在同号记录: "
                    + ", ".join(clash)
                )

            # 被移记录不得参与源演练的任何人工锁定（未涉及的锁定照常生效）。
            lock_params = tuple(v for no in ordered for v in (no, no))
            locked_rows = conn.execute(
                "SELECT record_a, record_b FROM locks WHERE drill_id = ? "
                "AND ("
                + " OR ".join(
                    ["record_a = ? OR record_b = ?"] * len(ordered)
                )
                + ")",
                (source_drill_id, *lock_params),
            ).fetchall()
            if locked_rows:
                pair = locked_rows[0]
                raise TransferRecordLockedError(
                    f"记录参与了源演练的人工锁定 "
                    f"({pair['record_a']}, {pair['record_b']})，"
                    "请先解除锁定再迁移"
                )

            # 被移记录不得处于隔离状态（隔离记录不得跨演练迁移）。
            quarantined_rows = conn.execute(
                f"SELECT record_no FROM quarantine WHERE drill_id = ? "
                f"AND record_no IN ({placeholders})",
                (source_drill_id, *ordered),
            ).fetchall()
            if quarantined_rows:
                raise TransferRecordQuarantinedError(
                    "记录处于隔离状态，不能跨演练迁移: "
                    + ", ".join(
                        sorted(r["record_no"] for r in quarantined_rows)
                    )
                )

            # 原字段原样转存（station/counterpart/band/mode/时间均保留入库时
            # 的规范化结果；updated_at 不刷新，保留日志原字段）。
            by_no = {r["record_no"]: r for r in rows}
            for no in ordered:
                r = by_no[no]
                conn.execute(
                    "INSERT INTO logs (drill_id, record_no, station, "
                    "counterpart, ts_micros, ts_iso, band, mode, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        target_drill_id, no, r["station"], r["counterpart"],
                        r["ts_micros"], r["ts_iso"], r["band"], r["mode"],
                        r["updated_at"],
                    ),
                )
                conn.execute(
                    "DELETE FROM logs WHERE drill_id = ? AND record_no = ?",
                    (source_drill_id, no),
                )

            # 两端各只重算一次：各自版本 +1、结果与新版本快照同事务留存。
            _recompute(conn, source_drill_id)
            _recompute(conn, target_drill_id)

            payload = {
                "source_drill_id": source_drill_id,
                "target_drill_id": target_drill_id,
                "moved_record_nos": ordered,
                "source_result": _result_from_conn(conn, source_drill_id),
                "target_result": _result_from_conn(conn, target_drill_id),
            }
            payload["source_version"] = payload["source_result"]["version"]
            payload["target_version"] = payload["target_result"]["version"]
            return payload
    finally:
        conn.close()


# --------------------------------------------------------------------------
# 锁定（复核员人工互证）
# --------------------------------------------------------------------------
def _normalize_pair(a: str, b: str) -> tuple[str, str]:
    return (a, b) if a <= b else (b, a)


def _lock_items(
    conn: sqlite3.Connection,
    drill_id: str,
    record_map: dict[str, Record] | None = None,
) -> list[dict]:
    """组装锁定清单（含实时计算的时差），按记录对升序。"""
    rows = conn.execute(
        "SELECT record_a, record_b, created_at FROM locks "
        "WHERE drill_id = ? ORDER BY record_a, record_b",
        (drill_id,),
    ).fetchall()
    if record_map is None:
        record_map = _record_map(_load_records(conn, drill_id))
    items = []
    for row in rows:
        a, b = row["record_a"], row["record_b"]
        diff = None
        if a in record_map and b in record_map:
            diff = abs(
                record_map[a].ts_micros - record_map[b].ts_micros
            ) / MICROS_PER_SECOND
        items.append(
            {
                "record_a": a,
                "record_b": b,
                "time_diff_seconds": diff,
                "locked_at": row["created_at"],
            }
        )
    return items


def _result_from_conn(conn: sqlite3.Connection, drill_id: str) -> dict:
    """在指定连接（可能是未提交的写事务内）读取当前互证结果。"""
    drill = _get_drill_row(conn, drill_id)
    row = conn.execute(
        "SELECT * FROM results WHERE drill_id = ?", (drill_id,)
    ).fetchone()
    if row is None:  # 理论上不会发生（建演练即写空结果）
        return {
            "drill_id": drill_id,
            "version": drill["version"],
            "sealed": bool(drill["sealed"]),
            "pairs": [],
            "unpaired": [],
            "total_time_diff_seconds": 0.0,
        }
    return {
        "drill_id": drill_id,
        "version": row["version"],
        "sealed": bool(drill["sealed"]),
        "pairs": json.loads(row["pairs_json"]),
        "unpaired": json.loads(row["unpaired_json"]),
        "total_time_diff_seconds": row["total_time_diff_seconds"],
        "recomputed_at": row["recomputed_at"],
    }


def _lock_action_response(
    conn: sqlite3.Connection,
    drill_id: str,
    record_map: dict[str, Record] | None = None,
) -> dict:
    locks = _lock_items(conn, drill_id, record_map)
    payload = _result_from_conn(conn, drill_id)
    return {
        "lock": locks[-1] if locks else None,
        "locks": locks,
        "result": payload,
    }


def lock_pair(drill_id: str, record_a: str, record_b: str) -> dict:
    """锁定一对互证候选；重复锁定同一对视为重传（幂等、不推进版本）。"""
    a, b = _normalize_pair(record_a, record_b)
    conn = connect()
    try:
        with _write_tx(conn):
            drill = _get_drill_row(conn, drill_id)
            _require_unsealed(drill)
            if a == b:
                raise LockSameRecordError(f"两条记录号不能相同: {a}")

            existing = conn.execute(
                "SELECT 1 FROM locks WHERE drill_id = ? AND record_a = ? "
                "AND record_b = ?",
                (drill_id, a, b),
            ).fetchone()
            if existing is not None:
                # 重复锁定同一对：幂等重传，状态不变、版本不推进。
                return _lock_action_response(conn, drill_id)

            # 隔离记录不得人工锁定（隔离与锁定互斥）。
            quarantined = _load_quarantine_nos(conn, drill_id)
            if a in quarantined or b in quarantined:
                no = a if a in quarantined else b
                raise LockRecordQuarantinedError(
                    f"记录 {no} 处于隔离状态，不能人工锁定，"
                    "请先恢复再锁定"
                )

            records = _load_records(conn, drill_id)
            rmap = _record_map(records)
            allowed_micros = int(
                round(drill["allowed_diff_secs"] * MICROS_PER_SECOND)
            )
            _check_lock_candidate(rmap, allowed_micros, a, b)

            # 两条记录均不得已被其他锁定占用。
            occupied = conn.execute(
                "SELECT record_a, record_b FROM locks WHERE drill_id = ? "
                "AND (record_a = ? OR record_b = ? OR record_a = ? OR record_b = ?)",
                (drill_id, a, a, b, b),
            ).fetchall()
            if occupied:
                other = occupied[0]
                raise LockOccupiedError(
                    f"记录已被锁定 ({other['record_a']}, {other['record_b']}) "
                    f"占用，不能重复锁定"
                )

            conn.execute(
                "INSERT INTO locks (drill_id, record_a, record_b, created_at) "
                "VALUES (?, ?, ?, ?)",
                (drill_id, a, b, _now_iso()),
            )
            # 锁定状态变化与结果、版本原子提交。
            _recompute(conn, drill_id)
            return _lock_action_response(conn, drill_id, rmap)
    finally:
        conn.close()


def unlock_pair(drill_id: str, record_a: str, record_b: str) -> dict:
    """解除已有锁定；锁定不存在时明确报错（404 lock_not_found）。"""
    a, b = _normalize_pair(record_a, record_b)
    conn = connect()
    try:
        with _write_tx(conn):
            drill = _get_drill_row(conn, drill_id)
            _require_unsealed(drill)
            cur = conn.execute(
                "DELETE FROM locks WHERE drill_id = ? AND record_a = ? "
                "AND record_b = ?",
                (drill_id, a, b),
            )
            if cur.rowcount == 0:
                raise LockNotFoundError(f"锁定不存在: ({a}, {b})")
            # 解除锁定后重算：锁定状态变化与结果、版本原子提交。
            _recompute(conn, drill_id)
            return _lock_action_response(conn, drill_id)
    finally:
        conn.close()


def list_locks(drill_id: str) -> dict:
    """查询锁定清单及最新互证结果。"""
    conn = connect()
    try:
        _get_drill_row(conn, drill_id)  # 未知演练 -> 404
        return {
            "drill_id": drill_id,
            "locks": _lock_items(conn, drill_id),
            "result": get_result(drill_id),
        }
    finally:
        conn.close()


# --------------------------------------------------------------------------
# 争议日志隔离（复核员批量隔离/恢复）
#
# 隔离只改变互证资格：不删除日志、不改写原字段。被隔离记录不参与配对、
# 不进入未配对清单与缺口诊断（诊断候选也仅限未隔离日志），也不计入台站
# 汇总的日志总数/已配对数/未配对数；隔离期间仍可按原接口更正日志，隔离
# 状态保留；恢复后按当前日志重新求解。隔离与人工锁定互斥：隔离记录不得
# 锁定或跨演练迁移，已有锁定成员不得隔离。批次中记录不存在、编号重复、
# 涉及锁定或演练已封存时整批拒绝、状态不变。仅实际状态变化推进一次版本，
# 结果与隔离清单同事务写入新版本快照；重复操作不推进版本，历史快照保持
# 原貌。
# --------------------------------------------------------------------------
def _prepare_quarantine_batch(record_nos: Iterable[str]) -> list[str]:
    """校验批次记录号无重复，返回去重后的请求顺序列表（服务层已 strip）。"""
    seen: set[str] = set()
    ordered: list[str] = []
    for no in record_nos:
        if no in seen:
            raise DuplicateRecordInQuarantineError(
                f"隔离/恢复批次内记录号重复: {no}"
            )
        seen.add(no)
        ordered.append(no)
    return ordered


def _check_quarantine_batch(
    conn: sqlite3.Connection, drill_id: str, ordered: list[str]
) -> None:
    """批次记录须全部现存且不涉及人工锁定，否则整批拒绝（状态不变）。"""
    placeholders = ",".join("?" for _ in ordered)
    rows = conn.execute(
        f"SELECT record_no FROM logs WHERE drill_id = ? AND record_no IN "
        f"({placeholders})",
        (drill_id, *ordered),
    ).fetchall()
    found = {r["record_no"] for r in rows}
    missing = [no for no in ordered if no not in found]
    if missing:
        raise QuarantineRecordMissingError(
            f"演练 {drill_id} 中记录不存在: " + ", ".join(missing)
        )
    lock_params = tuple(v for no in ordered for v in (no, no))
    locked_rows = conn.execute(
        "SELECT record_a, record_b FROM locks WHERE drill_id = ? AND ("
        + " OR ".join(["record_a = ? OR record_b = ?"] * len(ordered))
        + ")",
        (drill_id, *lock_params),
    ).fetchall()
    if locked_rows:
        pair = locked_rows[0]
        raise QuarantineRecordLockedError(
            f"批次涉及人工锁定中的记录 "
            f"({pair['record_a']}, {pair['record_b']})，"
            "请先解除锁定再隔离/恢复"
        )


def _quarantine_action_response(
    conn: sqlite3.Connection, drill_id: str, affected: list[str]
) -> dict:
    """隔离/恢复操作响应：本次实际变化的记录、当前隔离清单与最新结果。"""
    return {
        "drill_id": drill_id,
        "affected_record_nos": affected,
        "quarantine": _quarantine_items(conn, drill_id),
        "result": _result_from_conn(conn, drill_id),
    }


def quarantine_logs(drill_id: str, record_nos: Iterable[str]) -> dict:
    """批量隔离争议日志；批次中已隔离的记录自动跳过（重复操作不推进版本）。

    有实际隔离发生时版本 +1，全场按未隔离日志重算，结果与隔离清单在
    同一事务写入新版本快照。
    """
    ordered = _prepare_quarantine_batch(record_nos)
    conn = connect()
    try:
        with _write_tx(conn):
            drill = _get_drill_row(conn, drill_id)
            _require_unsealed(drill)
            _check_quarantine_batch(conn, drill_id, ordered)
            current = _load_quarantine_nos(conn, drill_id)
            affected = [no for no in ordered if no not in current]
            now = _now_iso()
            for no in affected:
                conn.execute(
                    "INSERT INTO quarantine (drill_id, record_no, created_at) "
                    "VALUES (?, ?, ?)",
                    (drill_id, no, now),
                )
            if affected:
                _recompute(conn, drill_id)
            return _quarantine_action_response(conn, drill_id, affected)
    finally:
        conn.close()


def restore_logs(drill_id: str, record_nos: Iterable[str]) -> dict:
    """批量恢复被隔离日志；批次中未隔离的记录自动跳过（重复操作不推进版本）。

    有实际恢复发生时版本 +1，按当前日志重新求解全场互证，结果与隔离
    清单在同一事务写入新版本快照。
    """
    ordered = _prepare_quarantine_batch(record_nos)
    conn = connect()
    try:
        with _write_tx(conn):
            drill = _get_drill_row(conn, drill_id)
            _require_unsealed(drill)
            _check_quarantine_batch(conn, drill_id, ordered)
            current = _load_quarantine_nos(conn, drill_id)
            affected = [no for no in ordered if no in current]
            if affected:
                placeholders = ",".join("?" for _ in affected)
                conn.execute(
                    f"DELETE FROM quarantine WHERE drill_id = ? AND "
                    f"record_no IN ({placeholders})",
                    (drill_id, *affected),
                )
                _recompute(conn, drill_id)
            return _quarantine_action_response(conn, drill_id, affected)
    finally:
        conn.close()


def list_quarantine(drill_id: str) -> dict:
    """查询隔离清单（按记录号升序）及最新互证结果（只读，封存后仍可查）。"""
    conn = connect()
    try:
        with _read_tx(conn):
            _get_drill_row(conn, drill_id)  # 未知演练 -> 404
            return {
                "drill_id": drill_id,
                "quarantine": _quarantine_items(conn, drill_id),
                "result": _result_from_conn(conn, drill_id),
            }
    finally:
        conn.close()


# --------------------------------------------------------------------------
# 复核员批量裁决（一次提交锁定/解除与隔离/恢复意图）
#
# 整批按最终状态判定：意图顺序不影响结果；重复同向意图只算一次；同一对象
# 出现相反意图整批拒绝。引用不存在的记录、最终锁定对不满足既有互证候选
# 条件、同一记录被多对锁定或最终仍处于隔离状态、演练已封存、基准版本过期
# 均整批拒绝且状态不变。同批解除旧锁并隔离、恢复并锁定均可（按最终状态
# 合成，天然兼容）。仅实际状态变化时在单个事务内重算互证、推进一次版本并
# 留存对应快照；纯重传不推进版本、不留存快照。
# --------------------------------------------------------------------------
def _prepare_adjudication_intents(
    lock_intents: Iterable[dict],
    quarantine_intents: Iterable[dict],
) -> tuple[
    list[tuple[str, str]], list[tuple[str, str]], list[str], list[str]
]:
    """规范化裁决意图并做请求形状校验（先于演练状态检查）。

    记录对内部升序；同向重复意图去重（保留请求顺序）；同一对象出现相反
    意图（同一记录对既锁定又解除、同一记录既隔离又恢复）整批拒绝。
    返回 (锁定对, 解除对, 隔离记录, 恢复记录) 四个去重后的请求顺序列表。
    """
    lock_keys: list[tuple[str, str]] = []
    unlock_keys: list[tuple[str, str]] = []
    for it in lock_intents:
        a, b = _normalize_pair(it["record_a"], it["record_b"])
        if a == b:
            raise LockSameRecordError(f"两条记录号不能相同: {a}")
        target = lock_keys if it["action"] == "lock" else unlock_keys
        if (a, b) not in target:
            target.append((a, b))
    pair_conflict = sorted(set(lock_keys) & set(unlock_keys))
    if pair_conflict:
        a, b = pair_conflict[0]
        raise AdjudicationIntentConflictError(
            f"同一记录对出现相反意图: ({a}, {b}) 既锁定又解除，"
            "无法判定最终状态"
        )

    quar_nos: list[str] = []
    restore_nos: list[str] = []
    for it in quarantine_intents:
        no = it["record_no"]
        target = quar_nos if it["action"] == "quarantine" else restore_nos
        if no not in target:
            target.append(no)
    quar_conflict = sorted(set(quar_nos) & set(restore_nos))
    if quar_conflict:
        raise AdjudicationIntentConflictError(
            f"同一记录出现相反意图: {quar_conflict[0]} 既隔离又恢复，"
            "无法判定最终状态"
        )
    return lock_keys, unlock_keys, quar_nos, restore_nos


def submit_adjudication(
    drill_id: str,
    base_version: int,
    lock_intents: Iterable[dict],
    quarantine_intents: Iterable[dict],
) -> dict:
    """复核员批量裁决：锁定/解除与隔离/恢复意图一次提交，按最终状态整批判定。

    全部校验在单个写事务内完成；任一不满足即整批回滚、状态不变。
    有实际状态变化时整批只推进一次版本，重算结果与锁定/隔离清单在同一
    事务写入新版本快照；纯重传（全部意图均已是目标状态）不推进版本。
    """
    lock_keys, unlock_keys, quar_nos, restore_nos = (
        _prepare_adjudication_intents(lock_intents, quarantine_intents)
    )

    conn = connect()
    try:
        with _write_tx(conn):
            drill = _get_drill_row(conn, drill_id)
            _require_unsealed(drill)
            if drill["version"] != base_version:
                raise AdjudicationBaseVersionChangedError(
                    f"演练 {drill_id} 当前版本 {drill['version']} 与基准版本 "
                    f"{base_version} 不一致，请重新获取当前状态后再提交裁决"
                )

            # 任一意图引用不存在的记录 -> 整批拒绝
            records = _load_records(conn, drill_id)
            rmap = _record_map(records)
            referenced: list[str] = []
            for a, b in lock_keys + unlock_keys:
                referenced.extend((a, b))
            referenced.extend(quar_nos + restore_nos)
            missing = sorted({no for no in referenced if no not in rmap})
            if missing:
                raise AdjudicationRecordMissingError(
                    f"演练 {drill_id} 中记录不存在: " + ", ".join(missing)
                )

            # 最终状态按集合合成：意图顺序不影响结果。已是目标状态的意图
            # （重复锁定/解除未锁定/重复隔离/恢复未隔离）自动跳过。
            current_locks = set(_load_lock_keys(conn, drill_id))
            current_quar = _load_quarantine_nos(conn, drill_id)
            final_locks = (current_locks - set(unlock_keys)) | set(lock_keys)
            final_quar = (current_quar - set(restore_nos)) | set(quar_nos)

            # 最终锁定对须全部满足既有互证候选条件（现存/互指/波段模式/时差）。
            allowed_micros = int(
                round(drill["allowed_diff_secs"] * MICROS_PER_SECOND)
            )
            for a, b in sorted(final_locks):
                _check_lock_candidate(rmap, allowed_micros, a, b)

            # 同一记录不得被多对锁定占用。
            used: dict[str, tuple[str, str]] = {}
            for a, b in sorted(final_locks):
                for no in (a, b):
                    if no in used:
                        oa, ob = used[no]
                        raise LockOccupiedError(
                            f"记录 {no} 被多个锁定对占用: "
                            f"({oa}, {ob}) 与 ({a}, {b})"
                        )
                    used[no] = (a, b)

            # 最终状态下锁定与隔离互斥（同批解除旧锁并隔离、恢复并锁定
            # 在最终状态中不冲突，均可受理）。
            still_quarantined = sorted(set(used) & final_quar)
            if still_quarantined:
                raise LockRecordQuarantinedError(
                    "最终状态下记录仍处于隔离状态，不能作为锁定对成员: "
                    + ", ".join(still_quarantined)
                )

            # 仅实际状态变化落库；纯重传不推进版本、不留存快照。
            to_lock = [k for k in lock_keys if k not in current_locks]
            to_unlock = [k for k in unlock_keys if k in current_locks]
            to_quar = [no for no in quar_nos if no not in current_quar]
            to_restore = [no for no in restore_nos if no in current_quar]
            changed = bool(to_lock or to_unlock or to_quar or to_restore)
            if changed:
                now = _now_iso()
                for a, b in to_unlock:
                    conn.execute(
                        "DELETE FROM locks WHERE drill_id = ? AND "
                        "record_a = ? AND record_b = ?",
                        (drill_id, a, b),
                    )
                for a, b in to_lock:
                    conn.execute(
                        "INSERT INTO locks (drill_id, record_a, record_b, "
                        "created_at) VALUES (?, ?, ?, ?)",
                        (drill_id, a, b, now),
                    )
                if to_restore:
                    placeholders = ",".join("?" for _ in to_restore)
                    conn.execute(
                        f"DELETE FROM quarantine WHERE drill_id = ? AND "
                        f"record_no IN ({placeholders})",
                        (drill_id, *to_restore),
                    )
                for no in to_quar:
                    conn.execute(
                        "INSERT INTO quarantine (drill_id, record_no, "
                        "created_at) VALUES (?, ?, ?)",
                        (drill_id, no, now),
                    )
                # 整批只推进一次版本：重算结果与锁定/隔离清单同事务留存快照。
                _recompute(conn, drill_id)

            result = _result_from_conn(conn, drill_id)
            return {
                "drill_id": drill_id,
                "base_version": base_version,
                "version": result["version"],
                "version_advanced": changed,
                "applied": {
                    "locked": [
                        {"record_a": a, "record_b": b} for a, b in to_lock
                    ],
                    "unlocked": [
                        {"record_a": a, "record_b": b} for a, b in to_unlock
                    ],
                    "quarantined": to_quar,
                    "restored": to_restore,
                },
                "locks": _lock_items(conn, drill_id, rmap),
                "quarantine": _quarantine_items(conn, drill_id),
                "result": result,
            }
    finally:
        conn.close()


def get_drill(drill_id: str) -> dict:
    conn = connect()
    try:
        row = _get_drill_row(conn, drill_id)
        return {
            "drill_id": row["drill_id"],
            "allowed_time_diff_seconds": row["allowed_diff_secs"],
            "version": row["version"],
            "sealed": bool(row["sealed"]),
            "created_at": row["created_at"],
            "sealed_at": row["sealed_at"],
        }
    finally:
        conn.close()


def list_drills() -> list[dict]:
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT drill_id FROM drills ORDER BY drill_id"
        ).fetchall()
        return [get_drill(r["drill_id"]) for r in rows]
    finally:
        conn.close()


def list_logs(drill_id: str) -> list[dict]:
    conn = connect()
    try:
        _get_drill_row(conn, drill_id)  # 未知演练 -> 404
        rows = conn.execute(
            "SELECT * FROM logs WHERE drill_id = ? ORDER BY rowid",
            (drill_id,),
        ).fetchall()
        quarantined = _load_quarantine_nos(conn, drill_id)
        return [
            {
                "record_no": r["record_no"],
                "station": r["station"],
                "counterpart": r["counterpart"],
                "time": r["ts_iso"],
                "band": r["band"],
                "mode": r["mode"],
                "updated_at": r["updated_at"],
                # 隔离只改变互证资格，日志本身保留（附加展示字段）。
                "quarantined": r["record_no"] in quarantined,
            }
            for r in rows
        ]
    finally:
        conn.close()


def get_result(drill_id: str) -> dict:
    """查询当前（封存后即为最终）互证结果。"""
    conn = connect()
    try:
        drill = _get_drill_row(conn, drill_id)
        row = conn.execute(
            "SELECT * FROM results WHERE drill_id = ?", (drill_id,)
        ).fetchone()
        if row is None:  # 理论上不会发生（建演练即写空结果）
            return {
                "drill_id": drill_id,
                "version": drill["version"],
                "sealed": bool(drill["sealed"]),
                "pairs": [],
                "unpaired": [],
                "total_time_diff_seconds": 0.0,
            }
        return {
            "drill_id": drill_id,
            "version": row["version"],
            "sealed": bool(drill["sealed"]),
            "pairs": json.loads(row["pairs_json"]),
            "unpaired": json.loads(row["unpaired_json"]),
            "total_time_diff_seconds": row["total_time_diff_seconds"],
            "recomputed_at": row["recomputed_at"],
        }
    finally:
        conn.close()


# --------------------------------------------------------------------------
# 未互证日志缺口诊断（复核员按演练查看未配对缺口）：只读，不改任何状态
# --------------------------------------------------------------------------
def get_gap_diagnostics(drill_id: str) -> dict:
    """按当前日志为每条未配对记录归因缺口原因（只读）。

    归因与当前版本、当前日志/配对结果严格一致；不改变日志、结果、版本
    与快照。封存后仍可查询；未知演练抛 ``drill_not_found``。
    """
    conn = connect()
    try:
        with _read_tx(conn):
            drill = _get_drill_row(conn, drill_id)
            row = conn.execute(
                "SELECT * FROM results WHERE drill_id = ?", (drill_id,)
            ).fetchone()
            unpaired = (
                json.loads(row["unpaired_json"]) if row is not None else []
            )
            # 缺口诊断的候选仅限未隔离日志（被隔离记录也不在未配对清单中）。
            quarantined = _load_quarantine_nos(conn, drill_id)
            records = [
                r
                for r in _load_records(conn, drill_id)
                if r.record_no not in quarantined
            ]
            gaps = diagnose_gaps(
                records, drill["allowed_diff_secs"], unpaired
            )
        return {
            "drill_id": drill_id,
            "version": drill["version"],
            "sealed": bool(drill["sealed"]),
            "allowed_time_diff_seconds": drill["allowed_diff_secs"],
            "gaps": [
                {
                    "record_no": g.record_no,
                    "reason": g.reason,
                    "reason_label": GAP_REASON_LABELS[g.reason],
                    "counterpart_record_no": g.counterpart_record_no,
                    "time_diff_seconds": g.time_diff_seconds,
                }
                for g in gaps
            ],
        }
    finally:
        conn.close()


# --------------------------------------------------------------------------
# 按台站汇总（演练结果按台站统计）：只读，不改任何状态
# --------------------------------------------------------------------------
def get_station_summary(drill_id: str) -> dict:
    """按台站呼号汇总当前日志与互证结果（只读）。

    以记录的本台呼号归组：一对互证的两条记录分别计入两端台站的已配对
    数；未配对记录只计入其本台。仅实际上报过日志的台站出现（仅在对方
    呼号中出现的台站不产生汇总项）。台站按呼号升序；配对覆盖率 =
    已配对数 / 日志总数（保留 6 位小数）。不改变日志、结果、版本与
    快照；封存后仍可查询；未知演练抛 ``drill_not_found``。
    """
    conn = connect()
    try:
        with _read_tx(conn):
            drill = _get_drill_row(conn, drill_id)
            row = conn.execute(
                "SELECT pairs_json, unpaired_json FROM results "
                "WHERE drill_id = ?",
                (drill_id,),
            ).fetchone()
            paired_nos: set[str] = set()
            unpaired_nos: set[str] = set()
            if row is not None:
                for p in json.loads(row["pairs_json"]):
                    paired_nos.add(p["record_a"])
                    paired_nos.add(p["record_b"])
                unpaired_nos = set(json.loads(row["unpaired_json"]))
            records = _load_records(conn, drill_id)
            quarantined = _load_quarantine_nos(conn, drill_id)

        # 结果与日志同事务落库：每条记录必属于已配对/未配对之一。
        # 被隔离记录不计入台站的日志总数、已配对数与未配对数。
        buckets: dict[str, list[int]] = {}  # 呼号 -> [总数, 已配对, 未配对]
        for r in records:
            if r.record_no in quarantined:
                continue
            bucket = buckets.setdefault(r.station, [0, 0, 0])
            bucket[0] += 1
            if r.record_no in paired_nos:
                bucket[1] += 1
            elif r.record_no in unpaired_nos:
                bucket[2] += 1
        stations = [
            {
                "station": station,
                "total_logs": total,
                "paired_logs": paired,
                "unpaired_logs": unpaired,
                "paired_coverage": (
                    round(paired / total, 6) if total else 0.0
                ),
            }
            for station, (total, paired, unpaired) in sorted(buckets.items())
        ]
        return {
            "drill_id": drill_id,
            "version": drill["version"],
            "sealed": bool(drill["sealed"]),
            "stations": stations,
        }
    finally:
        conn.close()


# --------------------------------------------------------------------------
# 版本快照（复核员按版本回看 / 比较）：只读，不改变当前状态
# --------------------------------------------------------------------------
def _snapshot_payload(row: sqlite3.Row, sealed: bool) -> dict:
    return {
        "drill_id": row["drill_id"],
        "version": row["version"],
        "sealed": sealed,
        "pairs": json.loads(row["pairs_json"]),
        "unpaired": json.loads(row["unpaired_json"]),
        "total_time_diff_seconds": row["total_time_diff_seconds"],
        "locks": json.loads(row["locks_json"]),
        "quarantine": json.loads(row["quarantine_json"]),
        "snapshot_at": row["created_at"],
    }


def _get_snapshot_row(
    conn: sqlite3.Connection, drill_id: str, version: int
) -> sqlite3.Row:
    """取已留存快照；未知演练抛 drill_not_found，未留存版本抛
    snapshot_not_found。"""
    _get_drill_row(conn, drill_id)
    row = conn.execute(
        "SELECT * FROM snapshots WHERE drill_id = ? AND version = ?",
        (drill_id, version),
    ).fetchone()
    if row is None:
        raise SnapshotNotFoundError(
            f"演练 {drill_id} 的版本 {version} 未留存快照"
            "（升级前的旧版本不可用现态补造）"
        )
    return row


def get_snapshot(drill_id: str, version: int) -> dict:
    """取回指定演练、指定版本已留存的快照（只读，不改变当前状态）。"""
    conn = connect()
    try:
        drill = _get_drill_row(conn, drill_id)
        row = _get_snapshot_row(conn, drill_id, version)
        return _snapshot_payload(row, bool(drill["sealed"]))
    finally:
        conn.close()


def list_snapshots(drill_id: str) -> dict:
    """列出演练已留存快照的版本号（升序）。"""
    conn = connect()
    try:
        _get_drill_row(conn, drill_id)  # 未知演练 -> 404
        rows = conn.execute(
            "SELECT version FROM snapshots WHERE drill_id = ? ORDER BY version",
            (drill_id,),
        ).fetchall()
        return {
            "drill_id": drill_id,
            "versions": [r["version"] for r in rows],
        }
    finally:
        conn.close()


def _pairs_diff(pairs_old: list[dict], pairs_new: list[dict]) -> dict:
    """按记录对身份比较两份配对清单：新增/移除/同对时差变化。

    ``added`` 以新清单内容为准，``removed`` 以旧清单内容为准。
    """
    pairs_old = sorted(pairs_old, key=lambda p: (p["record_a"], p["record_b"]))
    pairs_new = sorted(pairs_new, key=lambda p: (p["record_a"], p["record_b"]))
    old_map = {(p["record_a"], p["record_b"]): p for p in pairs_old}
    new_map = {(p["record_a"], p["record_b"]): p for p in pairs_new}

    added_keys = sorted(set(new_map) - set(old_map))
    removed_keys = sorted(set(old_map) - set(new_map))
    pair_time_diff_changed = [
        {
            "record_a": key[0],
            "record_b": key[1],
            "from_time_diff_seconds": old_map[key]["time_diff_seconds"],
            "to_time_diff_seconds": new_map[key]["time_diff_seconds"],
        }
        for key in sorted(set(old_map) & set(new_map))
        if old_map[key]["time_diff_seconds"]
        != new_map[key]["time_diff_seconds"]
    ]
    return {
        "added": [new_map[k] for k in added_keys],
        "removed": [old_map[k] for k in removed_keys],
        "time_diff_changed": pair_time_diff_changed,
    }


def _unpaired_diff(unpaired_old: list[str], unpaired_new: list[str]) -> dict:
    """未配对记录号集合差异（按记录号升序）。"""
    return {
        "added": sorted(set(unpaired_new) - set(unpaired_old)),
        "removed": sorted(set(unpaired_old) - set(unpaired_new)),
    }


def _diff_snapshots(
    from_snap: dict, to_snap: dict
) -> dict:
    """比较两个已留存快照：按记录号稳定排序列出配对、未配对、锁定与隔离
    清单的新增/移除；同一记录对的时差变化单列。

    ``added`` 以 to（新）版本内容为准，``removed`` 以 from（旧）版本为准。
    """
    locks_old = sorted(
        from_snap["locks"], key=lambda l: (l["record_a"], l["record_b"])
    )
    locks_new = sorted(
        to_snap["locks"], key=lambda l: (l["record_a"], l["record_b"])
    )
    old_lock = {(l["record_a"], l["record_b"]): l for l in locks_old}
    new_lock = {(l["record_a"], l["record_b"]): l for l in locks_new}

    # 隔离清单按记录号身份比较（恢复后再隔离视为同一记录，不重复列入）。
    quar_old = {q["record_no"]: q for q in from_snap["quarantine"]}
    quar_new = {q["record_no"]: q for q in to_snap["quarantine"]}

    return {
        "drill_id": to_snap["drill_id"],
        "from_version": from_snap["version"],
        "to_version": to_snap["version"],
        "pairs": _pairs_diff(from_snap["pairs"], to_snap["pairs"]),
        "unpaired": _unpaired_diff(
            from_snap["unpaired"], to_snap["unpaired"]
        ),
        "locks": {
            "added": [new_lock[k] for k in sorted(set(new_lock) - set(old_lock))],
            "removed": [old_lock[k] for k in sorted(set(old_lock) - set(new_lock))],
        },
        "quarantine": {
            "added": [quar_new[k] for k in sorted(set(quar_new) - set(quar_old))],
            "removed": [quar_old[k] for k in sorted(set(quar_old) - set(quar_new))],
        },
        "total_time_diff_seconds": {
            "from": from_snap["total_time_diff_seconds"],
            "to": to_snap["total_time_diff_seconds"],
            "delta": (
                to_snap["total_time_diff_seconds"]
                - from_snap["total_time_diff_seconds"]
            ),
        },
    }


def compare_snapshots(
    drill_id: str, from_version: int, to_version: int
) -> dict:
    """比较两个已留存版本（只读）。

    相同版本所有差异为空；两个版本都必须已留存，否则明确报错。
    """
    conn = connect()
    try:
        drill = _get_drill_row(conn, drill_id)
        sealed = bool(drill["sealed"])
        if from_version == to_version:
            # 版本也必须已留存：未知/未留存明确报错（不能用现态补造）。
            row = _get_snapshot_row(conn, drill_id, from_version)
            snap = _snapshot_payload(row, sealed)
            return _diff_snapshots(snap, snap)
        from_row = _get_snapshot_row(conn, drill_id, from_version)
        to_row = _get_snapshot_row(conn, drill_id, to_version)
        return _diff_snapshots(
            _snapshot_payload(from_row, sealed),
            _snapshot_payload(to_row, sealed),
        )
    finally:
        conn.close()
