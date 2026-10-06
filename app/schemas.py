"""请求/响应 Pydantic 模型与输入校验。

明确报错的非法输入：
  * 允许时差为负数；
  * 时间缺时区信息（naive datetime）；
  * 本台呼号与对方呼号相同；
  * 呼号/记录号/波段/模式为空或全空白。
未知演练、封存写入等状态错误在路由/服务层报错。
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

MICROS_PER_SECOND = 1_000_000


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="ignore")


def _clean_code(value: str, field_name: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field_name} 必须是字符串")
    cleaned = value.strip().upper()
    if not cleaned:
        raise ValueError(f"{field_name} 不能为空")
    return cleaned


def parse_allowed_diff(value: float) -> float:
    if value < 0:
        raise ValueError("允许时差必须为非负数（秒）")
    return float(value)


def aware_time_to_micros(value: datetime) -> int:
    """要求带时区时间，归一化为 UTC 微秒时间戳。"""
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("时间必须携带时区信息，例如 2026-10-04T08:00:00+08:00")
    return int(value.astimezone(timezone.utc).timestamp() * MICROS_PER_SECOND)


_DRILL_ID_RE = re.compile(r"^[A-Za-z0-9_.\-]+$")


def _clean_record_nos(values: list[str]) -> list[str]:
    """记录号列表统一校验：逐条 strip，拒绝非字符串与空串。"""
    cleaned: list[str] = []
    for v in values:
        if not isinstance(v, str):
            raise ValueError("记录号必须是字符串")
        no = v.strip()
        if not no:
            raise ValueError("记录号不能为空")
        cleaned.append(no)
    return cleaned


class CreateDrillRequest(StrictModel):
    drill_id: str = Field(..., description="演练号（唯一）")
    allowed_time_diff_seconds: float = Field(
        ..., description="允许时差（秒），非负"
    )

    @field_validator("drill_id")
    @classmethod
    def _drill_id(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("演练号不能为空")
        if not _DRILL_ID_RE.match(v):
            raise ValueError("演练号只能包含字母、数字、下划线、连字符与点")
        return v

    @field_validator("allowed_time_diff_seconds")
    @classmethod
    def _non_negative(cls, v: float) -> float:
        # 显式校验以返回明确的中文报错（不使用 ge 的内置英文信息）。
        return parse_allowed_diff(v)


class LogEntry(StrictModel):
    record_no: str = Field(..., description="唯一记录号（演练内唯一）")
    station: str = Field(..., description="本台呼号")
    counterpart: str = Field(..., description="对方呼号")
    time: datetime = Field(..., description="带时区的通联时间（ISO 8601）")
    band: str = Field(..., description="波段，如 40m")
    mode: str = Field(..., description="模式，如 SSB")

    @field_validator("record_no")
    @classmethod
    def _record_no(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("记录号不能为空")
        return v

    @field_validator("station")
    @classmethod
    def _station(cls, v: str) -> str:
        return _clean_code(v, "本台呼号")

    @field_validator("counterpart")
    @classmethod
    def _counterpart(cls, v: str) -> str:
        return _clean_code(v, "对方呼号")

    @field_validator("band", "mode")
    @classmethod
    def _band_mode(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("波段与模式不能为空")
        return v

    @field_validator("time")
    @classmethod
    def _time_aware(cls, v: datetime) -> datetime:
        if v.tzinfo is None or v.utcoffset() is None:
            raise ValueError(
                "时间必须携带时区信息，例如 2026-10-04T08:00:00+08:00"
            )
        return v

    @model_validator(mode="after")
    def _stations_differ(self) -> "LogEntry":
        if self.station == self.counterpart:
            raise ValueError("本台呼号与对方呼号不能相同")
        return self

    def to_service(self) -> dict:
        return {
            "record_no": self.record_no,
            "station": self.station,
            "counterpart": self.counterpart,
            "ts_micros": aware_time_to_micros(self.time),
            "band": self.band.upper(),
            "mode": self.mode.upper(),
        }


class BatchLogsRequest(StrictModel):
    logs: list[LogEntry] = Field(..., min_length=1)


# --------------------------------------------------------------------------
# 日志归错演练：跨演练迁移（复核员一次把记录从源演练移至目标演练）
# --------------------------------------------------------------------------
class TransferLogsRequest(StrictModel):
    """跨演练迁移请求体：目标演练号与非空、无重复的记录号列表。"""

    target_drill_id: str = Field(..., description="目标演练号（须已存在且未封存）")
    record_nos: list[str] = Field(
        ...,
        min_length=1,
        description="待迁移的源演练记录号列表（非空、不可重复）",
    )

    @field_validator("target_drill_id")
    @classmethod
    def _target_drill_id(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("目标演练号不能为空")
        if not _DRILL_ID_RE.match(v):
            raise ValueError("演练号只能包含字母、数字、下划线、连字符与点")
        return v

    @field_validator("record_nos")
    @classmethod
    def _record_nos(cls, values: list[str]) -> list[str]:
        return _clean_record_nos(values)


class TransferLogsResponse(StrictModel):
    """跨演练迁移结果：两端版本各推进一次，并回传迁移记录与两端互证结果。"""

    source_drill_id: str
    target_drill_id: str
    moved_record_nos: list[str]
    source_version: int
    target_version: int
    source_result: ResultResponse
    target_result: ResultResponse


# --------------------------------------------------------------------------
# 批量写入预演（复核员提交前预演）与确认
# --------------------------------------------------------------------------
class NormalizedLogItem(StrictModel):
    """预演回传的规范化批次条目（确认时据此与校验标识核对）。"""

    record_no: str
    station: str
    counterpart: str
    time: str
    band: str
    mode: str


class ConfirmBatchRequest(BatchLogsRequest):
    """确认预演：提交批次、基准版本与预演返回的校验标识。"""

    base_version: int = Field(
        ..., description="预演返回的基准版本（演练当前版本须仍为该版本）"
    )
    token: str = Field(
        ..., min_length=1, description="预演返回的校验标识（不透明字符串）"
    )


class PreviewTotalDiffSection(StrictModel):
    from_: float = Field(..., alias="from")
    to: float
    delta: float

    model_config = ConfigDict(extra="ignore", populate_by_name=True)


class PreviewDiff(StrictModel):
    """当前 -> 拟写入：配对增删/同对时差变化、未配对增删、总时差变化。"""

    pairs: PairDiffSection
    unpaired: UnpairedDiffSection
    total_time_diff_seconds: PreviewTotalDiffSection


class BatchPreviewResponse(StrictModel):
    """批量预演结果：不改变任何状态；确认时回传 base_version 与 token。"""

    drill_id: str
    base_version: int
    current_result: ResultResponse
    proposed_version: int
    would_advance_version: bool
    proposed_result: ResultResponse
    diff: PreviewDiff
    normalized_batch: list[NormalizedLogItem]
    token: str


class PairOut(BaseModel):
    record_a: str
    record_b: str
    time_diff_seconds: float


class ResultResponse(StrictModel):
    drill_id: str
    version: int
    sealed: bool
    pairs: list[PairOut]
    unpaired: list[str]
    total_time_diff_seconds: float
    recomputed_at: str | None = None


# --------------------------------------------------------------------------
# 未互证日志缺口诊断（复核员按演练查看未配对缺口；只读）
# --------------------------------------------------------------------------
class GapItemOut(StrictModel):
    """单条未配对记录的缺口诊断：归因 + 该类中时差最小的对方记录。"""

    record_no: str
    reason: str = Field(
        ...,
        description=(
            "缺口原因：candidate_occupied（候选已占用）/ "
            "time_diff_exceeded（时差超限）/ "
            "band_or_mode_mismatch（波段或模式不符）/ "
            "missing_counterpart_log（缺少对方日志）"
        ),
    )
    reason_label: str = Field(..., description="原因中文说明")
    counterpart_record_no: str | None = Field(
        None, description="该类中时差最小（再按记录号最小）的对方记录；末类为 null"
    )
    time_diff_seconds: float | None = Field(
        None, description="与该对方记录的实际时差（秒）；末类为 null"
    )


class GapDiagnosticsResponse(StrictModel):
    """按演练查看的未配对日志缺口诊断（当前版本；只读）。"""

    drill_id: str
    version: int
    sealed: bool
    allowed_time_diff_seconds: float
    gaps: list[GapItemOut]


# --------------------------------------------------------------------------
# 按台站汇总（演练结果按台站统计；只读）
# --------------------------------------------------------------------------
class StationSummaryItem(StrictModel):
    """单个台站的汇总：日志总数、已配对数、未配对数与配对覆盖率。"""

    station: str = Field(..., description="台站呼号（已规范化大写）")
    total_logs: int = Field(..., description="该台站上报的日志总数")
    paired_logs: int = Field(
        ..., description="已配对日志数（一对互证的两条记录分别计入两端台站）"
    )
    unpaired_logs: int = Field(
        ..., description="未配对日志数（只计入其本台）"
    )
    paired_coverage: float = Field(
        ..., description="配对覆盖率 = 已配对数 / 日志总数（0..1，保留 6 位小数）"
    )


class StationSummaryResponse(StrictModel):
    """按台站汇总的互证结果（当前版本；只读；台站按呼号升序）。"""

    drill_id: str
    version: int
    sealed: bool
    stations: list[StationSummaryItem]


class LockRequest(StrictModel):
    """锁定/解除锁定请求体：一对记录号（顺序任意，服务端内部升序）。"""

    record_a: str = Field(..., description="第一条记录号（演练内现存）")
    record_b: str = Field(..., description="第二条记录号（演练内现存）")

    @field_validator("record_a", "record_b")
    @classmethod
    def _record_no(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("记录号不能为空")
        return v


class LockItem(BaseModel):
    record_a: str
    record_b: str
    time_diff_seconds: float | None = None
    locked_at: str


class LockActionResponse(StrictModel):
    """锁定/解除操作响应：当前操作锁定、锁定清单与最新互证结果。"""

    lock: LockItem | None = None
    locks: list[LockItem]
    result: ResultResponse


class LockListResponse(StrictModel):
    """锁定清单查询响应。"""

    drill_id: str
    locks: list[LockItem]
    result: ResultResponse


# --------------------------------------------------------------------------
# 争议日志隔离（复核员批量隔离/恢复；只改变互证资格，不删除日志）
# --------------------------------------------------------------------------
class QuarantineRequest(StrictModel):
    """批量隔离/恢复请求体：非空、无重复的记录号列表。"""

    record_nos: list[str] = Field(
        ...,
        min_length=1,
        description="待隔离/恢复的记录号列表（非空、不可重复）",
    )

    @field_validator("record_nos")
    @classmethod
    def _record_nos(cls, values: list[str]) -> list[str]:
        return _clean_record_nos(values)


class QuarantineItem(StrictModel):
    """一条隔离记录：记录号与隔离时间。"""

    record_no: str
    quarantined_at: str


class QuarantineActionResponse(StrictModel):
    """隔离/恢复操作响应：本次实际变化的记录、当前隔离清单与最新结果。"""

    drill_id: str
    affected_record_nos: list[str] = Field(
        ...,
        description="本次实际隔离/恢复的记录号（请求顺序；重复操作自动跳过）",
    )
    quarantine: list[QuarantineItem] = Field(
        ..., description="当前完整隔离清单（按记录号升序）"
    )
    result: ResultResponse


class QuarantineListResponse(StrictModel):
    """隔离清单查询响应（按记录号升序）。"""

    drill_id: str
    quarantine: list[QuarantineItem]
    result: ResultResponse


# --------------------------------------------------------------------------
# 复核员批量裁决（一次提交锁定/解除与隔离/恢复意图，按最终状态整批判定）
# --------------------------------------------------------------------------
class AdjudicationLockIntent(StrictModel):
    """锁定/解除意图：一对记录号（顺序任意，服务端内部升序）。"""

    action: Literal["lock", "unlock"] = Field(
        ..., description="lock=锁定该记录对；unlock=解除该记录对的锁定"
    )
    record_a: str = Field(..., description="第一条记录号（演练内现存）")
    record_b: str = Field(..., description="第二条记录号（演练内现存）")

    @field_validator("record_a", "record_b")
    @classmethod
    def _record_no(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("记录号不能为空")
        return v


class AdjudicationQuarantineIntent(StrictModel):
    """隔离/恢复意图：单条记录号。"""

    action: Literal["quarantine", "restore"] = Field(
        ..., description="quarantine=隔离该记录；restore=恢复该记录"
    )
    record_no: str = Field(..., description="记录号（演练内现存）")

    @field_validator("record_no")
    @classmethod
    def _record_no(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("记录号不能为空")
        return v


class AdjudicationRequest(StrictModel):
    """批量裁决请求：基准版本 + 锁定/解除意图列表 + 隔离/恢复意图列表。

    整批按最终状态判定：意图顺序无关；重复同向意图只算一次；同一对象
    出现相反意图整批拒绝。两个列表至少提交一条意图。
    """

    base_version: int = Field(
        ..., ge=0, description="基准版本（演练当前版本须仍为该版本）"
    )
    locks: list[AdjudicationLockIntent] = Field(
        default_factory=list, description="锁定/解除记录对意图列表"
    )
    quarantine: list[AdjudicationQuarantineIntent] = Field(
        default_factory=list, description="隔离/恢复记录意图列表"
    )

    @model_validator(mode="after")
    def _at_least_one_intent(self) -> "AdjudicationRequest":
        if not self.locks and not self.quarantine:
            raise ValueError("锁定/解除与隔离/恢复意图至少提交一条")
        return self


class PairRef(StrictModel):
    """一对记录号（内部升序）。"""

    record_a: str
    record_b: str


class AdjudicationApplied(StrictModel):
    """本批实际生效的状态变化（请求顺序；纯重传时各项为空）。"""

    locked: list[PairRef] = Field(..., description="本批实际锁定的记录对")
    unlocked: list[PairRef] = Field(..., description="本批实际解除的记录对")
    quarantined: list[str] = Field(..., description="本批实际隔离的记录号")
    restored: list[str] = Field(..., description="本批实际恢复的记录号")


class AdjudicationResponse(StrictModel):
    """批量裁决响应：当前版本、锁定与隔离清单及完整互证结果。"""

    drill_id: str
    base_version: int
    version: int = Field(
        ..., description="当前版本（有实际变化时 = 基准版本 + 1，纯重传不变）"
    )
    version_advanced: bool = Field(
        ..., description="本批是否有实际状态变化并推进了一次版本"
    )
    applied: AdjudicationApplied
    locks: list[LockItem] = Field(..., description="当前完整锁定清单（按记录对升序）")
    quarantine: list[QuarantineItem] = Field(
        ..., description="当前完整隔离清单（按记录号升序）"
    )
    result: ResultResponse


# --------------------------------------------------------------------------
# 版本快照（复核员按版本回看 / 比较）
# --------------------------------------------------------------------------
class SnapshotResponse(StrictModel):
    """指定版本的留存快照：该版配对、未配对、总时差、锁定清单与隔离清单。"""

    drill_id: str
    version: int
    sealed: bool
    pairs: list[PairOut]
    unpaired: list[str]
    total_time_diff_seconds: float
    locks: list[LockItem]
    quarantine: list[QuarantineItem]
    snapshot_at: str


class SnapshotListResponse(StrictModel):
    """已留存快照的版本号清单。"""

    drill_id: str
    versions: list[int]


class PairDiff(StrictModel):
    """同一记录对在两个版本间的时差变化。"""

    record_a: str
    record_b: str
    from_time_diff_seconds: float
    to_time_diff_seconds: float


class PairDiffSection(StrictModel):
    added: list[PairOut]
    removed: list[PairOut]
    time_diff_changed: list[PairDiff]


class UnpairedDiffSection(StrictModel):
    added: list[str]
    removed: list[str]


class LockDiffSection(StrictModel):
    added: list[LockItem]
    removed: list[LockItem]


class QuarantineDiffSection(StrictModel):
    """两版间隔离清单的新增/移除（按记录号身份比较，升序）。"""

    added: list[QuarantineItem]
    removed: list[QuarantineItem]


class TotalDiffSection(StrictModel):
    from_: float = Field(..., alias="from")
    to: float
    delta: float

    model_config = ConfigDict(extra="ignore", populate_by_name=True)


class SnapshotDiffResponse(StrictModel):
    """两个已留存版本的差异（相同版本各节均为空，delta 为 0）。"""

    drill_id: str
    from_version: int
    to_version: int
    pairs: PairDiffSection
    unpaired: UnpairedDiffSection
    locks: LockDiffSection
    quarantine: QuarantineDiffSection
    total_time_diff_seconds: TotalDiffSection


class DrillResponse(StrictModel):
    drill_id: str
    allowed_time_diff_seconds: float
    version: int
    sealed: bool
    created_at: str
    sealed_at: str | None = None
