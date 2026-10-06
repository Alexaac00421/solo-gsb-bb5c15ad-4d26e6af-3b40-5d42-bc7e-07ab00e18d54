#!/usr/bin/env python3
"""一键演示脚本：创建演练 -> 写入日志 -> 查询互证 -> 更正 -> 封存。

用法：
    python scripts/demo.py [BASE_URL]

默认 BASE_URL=http://127.0.0.1:8080。
仅使用标准库，无需安装额外依赖。
"""

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request


def call(method: str, url: str, payload: dict | None = None) -> tuple[int, dict]:
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        url, data=data, method=method,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read().decode() or "{}")
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode())


def show(title: str, status: int, body: dict) -> None:
    print(f"\n=== {title} [{status}] ===")
    print(json.dumps(body, ensure_ascii=False, indent=2))


def main(base_url: str) -> None:
    # 带时间戳，保证演示可重复运行
    from datetime import datetime, timezone
    drill = "DEMO-" + datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")

    show("创建演练（允许时差 10 秒）", *call(
        "POST", f"{base_url}/drills",
        {"drill_id": drill, "allowed_time_diff_seconds": 10},
    ))

    logs = [
        # BH1AAA 与 BH2BBB 互指，时差 5 秒，同波段同模式 -> 可配对
        {"record_no": "R1", "station": "BH1AAA", "counterpart": "BH2BBB",
         "time": "2026-10-04T08:00:00+08:00", "band": "40m", "mode": "SSB"},
        {"record_no": "R2", "station": "BH2BBB", "counterpart": "BH1AAA",
         "time": "2026-10-04T00:00:05+00:00", "band": "40m", "mode": "SSB"},
        # 单边记录，无人互指 -> 未配对
        {"record_no": "R3", "station": "BH3CCC", "counterpart": "BH4DDD",
         "time": "2026-10-04T00:00:00+00:00", "band": "40m", "mode": "SSB"},
        # 与 R1/R2 模式不同 -> 不可配对
        {"record_no": "R4", "station": "BH1AAA", "counterpart": "BH2BBB",
         "time": "2026-10-04T00:00:01+00:00", "band": "40m", "mode": "CW"},
        {"record_no": "R5", "station": "BH2BBB", "counterpart": "BH1AAA",
         "time": "2026-10-04T00:00:01+00:00", "band": "40m", "mode": "CW"},
    ]
    show("批量写入 5 条日志", *call(
        "POST", f"{base_url}/drills/{drill}/logs", {"logs": logs}
    ))

    # --- 复核员提交前预演：只读，不改任何状态；确认须回传标识与基准版本 ---
    preview_logs = [
        {"record_no": "R7", "station": "BH7GGG", "counterpart": "BH8HHH",
         "time": "2026-10-04T00:00:00+00:00", "band": "40m", "mode": "SSB"},
        {"record_no": "R8", "station": "BH8HHH", "counterpart": "BH7GGG",
         "time": "2026-10-04T00:00:06+00:00", "band": "40m", "mode": "SSB"},
    ]
    _st, pv = call(
        "POST", f"{base_url}/drills/{drill}/logs/preview",
        {"logs": preview_logs},
    )
    show("批量预演 R7/R8（只读，状态不变）", _st, pv)

    # 篡改批次后拿原标识确认 -> 409 preview_token_mismatch，状态不变
    tampered = [{**preview_logs[0], "time": "2026-10-04T00:00:01+00:00"},
                preview_logs[1]]
    show("用原标识确认被改动的批次（应 409 preview_token_mismatch）", *call(
        "POST", f"{base_url}/drills/{drill}/logs/confirm",
        {"logs": tampered, "base_version": pv["base_version"],
         "token": pv["token"]},
    ))

    # 原批次 + 原标识 + 基准版本确认 -> 原子落库，结果等于预演 proposed
    show("按预演确认提交 R7/R8", *call(
        "POST", f"{base_url}/drills/{drill}/logs/confirm",
        {"logs": preview_logs, "base_version": pv["base_version"],
         "token": pv["token"]},
    ))

    # 同一标识重放：版本已变化 -> 409 preview_base_version_changed
    show("重放旧标识（应 409 preview_base_version_changed）", *call(
        "POST", f"{base_url}/drills/{drill}/logs/confirm",
        {"logs": preview_logs, "base_version": pv["base_version"],
         "token": pv["token"]},
    ))

    # 同内容重传预演：would_advance_version=false，差异全空
    show("同内容重传预演（不应推进版本）", *call(
        "POST", f"{base_url}/drills/{drill}/logs/preview",
        {"logs": [{**preview_logs[0], "station": " bh7ggg ", "band": "40M"}]},
    ))

    # 同内容重传（规范化后一致）-> 版本不变
    show("R1 同内容重传（版本应不变）", *call(
        "PUT", f"{base_url}/drills/{drill}/logs/R1",
        {**logs[0], "station": " bh1aaa "},
    ))

    # 异内容更正：把 R3 的对方记录 R6 补上，但时差 60 秒超过 10 秒 -> 仍不配对
    show("补一条超出时差的互指记录 R6", *call(
        "PUT", f"{base_url}/drills/{drill}/logs/R6",
        {"record_no": "R6", "station": "BH4DDD", "counterpart": "BH3CCC",
         "time": "2026-10-04T00:01:00+00:00", "band": "40m", "mode": "SSB"},
    ))

    # 更正 R6 到 8 秒差 -> R3-R6 配对，全场重算
    show("更正 R6（时差变为 8 秒，全场重算）", *call(
        "PUT", f"{base_url}/drills/{drill}/logs/R6",
        {"record_no": "R6", "station": "BH4DDD", "counterpart": "BH3CCC",
         "time": "2026-10-04T00:00:08+00:00", "band": "40m", "mode": "SSB"},
    ))

    # --- 复核员人工锁定（互证） ---
    # 锁定 R1-R2：现存、互指、同波段同模式、时差 5 秒 <= 10 秒
    show("锁定 (R1, R2)", *call(
        "PUT", f"{base_url}/drills/{drill}/locks",
        {"record_a": "R1", "record_b": "R2"},
    ))

    # 重复锁定同一对（颠倒顺序）视为重传：版本不推进
    show("重复锁定 (R2, R1)（重传，版本应不变）", *call(
        "PUT", f"{base_url}/drills/{drill}/locks",
        {"record_a": "R2", "record_b": "R1"},
    ))

    # 锁定非互证候选（R1 与 R3 不相互指认）-> 409 lock_not_candidate
    show("锁定非候选 (R1, R3)（应被拒绝 409）", *call(
        "PUT", f"{base_url}/drills/{drill}/locks",
        {"record_a": "R1", "record_b": "R3"},
    ))

    # 更正 R2 使锁定时差超限（60 秒 > 10 秒）-> 409 lock_invalidated，
    # 整次写入被拒绝，日志、锁定、版本均不变
    show("更正 R2 致锁定失效（整次写入应被拒绝 409）", *call(
        "PUT", f"{base_url}/drills/{drill}/logs/R2",
        {"record_no": "R2", "station": "BH2BBB", "counterpart": "BH1AAA",
         "time": "2026-10-04T00:01:00+00:00", "band": "40m", "mode": "SSB"},
    ))

    # 查询锁定清单及最新结果
    show("查询锁定清单", *call("GET", f"{base_url}/drills/{drill}/locks"))

    # 解除锁定（不存在的锁定会 404 lock_not_found）
    show("解除不存在的锁定（应 404）", *call(
        "POST", f"{base_url}/drills/{drill}/locks/unlock",
        {"record_a": "R3", "record_b": "R6"},
    ))
    show("解除锁定 (R1, R2)（顺序任意，全场重算）", *call(
        "POST", f"{base_url}/drills/{drill}/locks/unlock",
        {"record_a": "R2", "record_b": "R1"},
    ))

    show("查询最终互证结果", *call("GET", f"{base_url}/drills/{drill}/result"))

    # --- 复核员缺口诊断：为每条未配对记录按当前日志归因（只读） ---
    show("未配对日志缺口诊断（只读，带当前版本）", *call(
        "GET", f"{base_url}/drills/{drill}/gap-diagnostics"
    ))

    # --- 按台站汇总：逐台站日志总数/已配对/未配对/配对覆盖率（只读） ---
    show("按台站汇总互证结果（只读，台站按呼号升序）", *call(
        "GET", f"{base_url}/drills/{drill}/station-summary"
    ))

    # --- 版本快照：复核员按版本回看、比较两版差异（只读） ---
    # 列出已留存版本号（创建即有 0；每次真正推进版本同事务留存一版）
    show("已留存快照版本号", *call(
        "GET", f"{base_url}/drills/{drill}/snapshots"
    ))

    # 取回 0 版（创建演练时的空场快照）
    show("回看 0 版快照（空场）", *call(
        "GET", f"{base_url}/drills/{drill}/snapshots/0"
    ))

    # 比较解锁前后两版：配对/未配对/锁定的新增移除、同一对时差变化单列。
    # 当前版本为锁定解除后的版本；取其与“锁定中”的上一版比较。
    status, result = call("GET", f"{base_url}/drills/{drill}/result")
    cur_v = result["version"]
    show(f"比较 {cur_v - 1} -> {cur_v} 版（锁定解除前后）", *call(
        "GET", f"{base_url}/drills/{drill}/snapshots/{cur_v - 1}/diff/{cur_v}"
    ))

    # 相同版本差异为空（该版本须已留存）
    show(f"比较相同版本 {cur_v} -> {cur_v}（差异应全空）", *call(
        "GET", f"{base_url}/drills/{drill}/snapshots/{cur_v}/diff/{cur_v}"
    ))

    # 未留存版本明确报错（重传/失败回滚/封存均不产生快照）
    show("取回未留存版本（应 404 snapshot_not_found）", *call(
        "GET", f"{base_url}/drills/{drill}/snapshots/{cur_v + 100}"
    ))

    # --- 复核员争议日志隔离：批量隔离/恢复（只改变互证资格，不删日志） ---
    show("批量隔离争议日志 R4/R5", *call(
        "PUT", f"{base_url}/drills/{drill}/quarantine",
        {"record_nos": ["R4", "R5"]},
    ))

    # 重复隔离同一批：无实际状态变化，不推进版本
    show("重复隔离同一批（重传，版本应不变）", *call(
        "PUT", f"{base_url}/drills/{drill}/quarantine",
        {"record_nos": ["R4", "R5"]},
    ))

    show("查询隔离清单（按记录号升序）", *call(
        "GET", f"{base_url}/drills/{drill}/quarantine"
    ))

    # 隔离期间仍可按原接口更正日志，隔离状态保留（R5 仍不参与配对）
    show("隔离期间更正 R5（隔离状态保留）", *call(
        "PUT", f"{base_url}/drills/{drill}/logs/R5",
        {"record_no": "R5", "station": "BH2BBB", "counterpart": "BH1AAA",
         "time": "2026-10-04T00:00:03+00:00", "band": "40m", "mode": "CW"},
    ))

    # 隔离记录不得人工锁定
    show("锁定被隔离的 R4/R5（应 409 lock_record_quarantined）", *call(
        "PUT", f"{base_url}/drills/{drill}/locks",
        {"record_a": "R4", "record_b": "R5"},
    ))

    # 批次含不存在的记录：整批拒绝、状态不变
    show("隔离批次含不存在记录（应 404 quarantine_record_missing）", *call(
        "PUT", f"{base_url}/drills/{drill}/quarantine",
        {"record_nos": ["R4", "NOPE"]},
    ))

    # 恢复后按当前日志重新求解：R4/R5 以更正后的内容重新参与配对
    show("批量恢复 R4/R5（按当前日志重新求解）", *call(
        "POST", f"{base_url}/drills/{drill}/quarantine/restore",
        {"record_nos": ["R4", "R5"]},
    ))

    # --- 复核员批量裁决：锁定/解除与隔离/恢复一次提交，按最终状态整批判定 ---
    status, cur = call("GET", f"{base_url}/drills/{drill}/result")
    base_v = cur["version"]

    # 一次提交相互牵连的裁决：锁定 (R3,R6) + 隔离 R5，
    # 一个事务内整批判定，有实际变化时只推进一次版本并留存快照
    show("批量裁决：锁定 (R3,R6) + 隔离 R5（整批只推进一次版本）", *call(
        "POST", f"{base_url}/drills/{drill}/adjudications",
        {"base_version": base_v,
         "locks": [{"action": "lock", "record_a": "R3", "record_b": "R6"}],
         "quarantine": [{"action": "quarantine", "record_no": "R5"}]},
    ))

    # 纯重传：重复锁定已锁定的 (R3,R6)、重复隔离 R5 -> 版本不推进
    show("纯重传批量裁决（版本应不变）", *call(
        "POST", f"{base_url}/drills/{drill}/adjudications",
        {"base_version": base_v + 1,
         "locks": [{"action": "lock", "record_a": "R6", "record_b": "R3"}],
         "quarantine": [{"action": "quarantine", "record_no": "R5"}]},
    ))

    # 同一对象出现相反意图 -> 409 adjudication_intent_conflict，状态不变
    show("同一记录对既锁定又解除（应 409 adjudication_intent_conflict）",
         *call(
        "POST", f"{base_url}/drills/{drill}/adjudications",
        {"base_version": base_v + 1,
         "locks": [{"action": "lock", "record_a": "R1", "record_b": "R2"},
                   {"action": "unlock", "record_a": "R2", "record_b": "R1"}]},
    ))

    # 基准版本过期 -> 409 adjudication_base_version_changed，状态不变
    show("基准版本过期（应 409 adjudication_base_version_changed）", *call(
        "POST", f"{base_url}/drills/{drill}/adjudications",
        {"base_version": base_v,
         "locks": [{"action": "lock", "record_a": "R1", "record_b": "R2"}]},
    ))

    # 同批解除旧锁并隔离：解除 (R3,R6) + 隔离 R6（最终状态无冲突，可受理）
    show("同批解除旧锁 (R3,R6) 并隔离 R6", *call(
        "POST", f"{base_url}/drills/{drill}/adjudications",
        {"base_version": base_v + 1,
         "locks": [{"action": "unlock", "record_a": "R3", "record_b": "R6"}],
         "quarantine": [{"action": "quarantine", "record_no": "R6"}]},
    ))

    # 同批恢复并锁定：恢复 R6 + 重新锁定 (R3,R6)（最终状态无冲突，可受理）
    show("同批恢复 R6 并重新锁定 (R3,R6)", *call(
        "POST", f"{base_url}/drills/{drill}/adjudications",
        {"base_version": base_v + 2,
         "locks": [{"action": "lock", "record_a": "R3", "record_b": "R6"}],
         "quarantine": [{"action": "restore", "record_no": "R6"}]},
    ))

    show("封存演练", *call("POST", f"{base_url}/drills/{drill}/seal"))

    # 封存不产生新快照；封存状态从现态读取（旧快照 sealed 也为 true）
    show("封存后回看 0 版快照（sealed 应为 true，内容仍为空场）", *call(
        "GET", f"{base_url}/drills/{drill}/snapshots/0"
    ))

    status, body = call(
        "PUT", f"{base_url}/drills/{drill}/logs/R7",
        {"record_no": "R7", "station": "BH5EEE", "counterpart": "BH6FFF",
         "time": "2026-10-04T00:00:00+00:00", "band": "40m", "mode": "SSB"},
    )
    show("封存后写入（应被拒绝 409）", status, body)

    show("封存后仍可查询结果", *call("GET", f"{base_url}/drills/{drill}/result"))

    # 封存后仍可进行缺口诊断（只读；版本、sealed 与最终现态一致）
    show("封存后仍可查询缺口诊断", *call(
        "GET", f"{base_url}/drills/{drill}/gap-diagnostics"
    ))

    # 封存后仍可按台站汇总（只读；sealed 为 true）
    show("封存后仍可查询台站汇总", *call(
        "GET", f"{base_url}/drills/{drill}/station-summary"
    ))

    # 未知演练明确报错：404 drill_not_found
    show("未知演练的缺口诊断（应 404 drill_not_found）", *call(
        "GET", f"{base_url}/drills/NOPE/gap-diagnostics"
    ))


if __name__ == "__main__":
    base = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8080"
    main(base.rstrip("/"))
