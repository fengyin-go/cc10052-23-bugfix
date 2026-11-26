"""曝光日志接入领域服务：管理实验曝光日志的批量接入、去重清洗与异常丢弃。

数据链路位置：日志接入层，是实验侧原始数据的第一入口，消费分组分配器的
分组标签，把去重后的曝光数据供给指标计算作为分母。

去重口径：同一（实验编码, 日志批次, 用户标识）只保留实际发生最早的一条曝光；
同批补传的迟到记录与既有已去重记录一起参与去重。每次去重产出批次确认回执，
重跑幂等：保留集合不变时不生成新轮次、确认数不变，迟到重复不再次入账。
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from app.services.base import BaseModuleService
from app.store import store

ACTIVE_STATUS = ("已接收", "已去重")


def parse_moment(value: Any) -> datetime | None:
    """把曝光时间解析成可比较的时点（统一成无时区 UTC），无法解析返回 None。"""
    text = str(value or "").strip()
    if not text:
        return None
    candidates = [text]
    if text.endswith("Z"):
        candidates.append(text[:-1] + "+00:00")
    for candidate in candidates:
        try:
            parsed = datetime.fromisoformat(candidate)
            if parsed.tzinfo is not None:
                parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
            return parsed
        except ValueError:
            continue
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


def _retain_key(row: dict[str, Any]) -> tuple[int, datetime, int]:
    """保留优先级：可解析的曝光时间越早越优先，同时间取先接入（id 小）的。"""
    parsed = parse_moment(row.get("曝光时间"))
    if parsed is None:
        return (1, datetime.max, int(row.get("id", 0)))
    return (0, parsed, int(row.get("id", 0)))


def _row_brief(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": int(row.get("id", 0)),
        "用户标识": str(row.get("用户标识", "")),
        "实验编码": str(row.get("实验编码", "")),
        "曝光时间": str(row.get("曝光时间", "")),
        "计入窗口": str(row.get("计入窗口", "") or ""),
    }


class ExposureLogService(BaseModuleService):
    MODULE = "exposure_log"
    ENTITY = "曝光记录"
    OPERATION_TERM = "接入"
    LIST_FIELDS = ["日志批次", "实验编码", "用户标识", "分组名称", "曝光时间", "计入窗口", "确认轮次"]
    REQUIRED_FIELDS = ["日志批次", "实验编码", "用户标识"]
    STATUS_ORDER = ["已接收", "已去重", "异常丢弃"]
    ACTION_RULES = {"执行去重": "已去重", "丢弃异常": "异常丢弃"}
    NEGATIVE_ACTIONS = []

    def __init__(self) -> None:
        # 批次确认回执：日志批次 -> 轮次列表（最新在最后）。补传员可按原批次复查更正。
        self._receipts: dict[str, list[dict[str, Any]]] = {}

    # ---- 通用动作接线：行级动作转成批次级领域操作 ----

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return None, f"{self.ENTITY} {entry_id} 不存在或已归档"
        if action == "执行去重":
            result = self.dedup_batch(str(entry.get("日志批次", "")))
            return (entry if result.get("ok") else None), str(result.get("message", ""))
        if action == "丢弃异常":
            result = self.drop_abnormal(entry_id, "人工判定异常")
            return (entry if result.get("ok") else None), str(result.get("message", ""))
        return super().run_action(entry_id, action)

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        entry, missing = super().create_entry(values)
        if entry is not None:
            entry["计入窗口"] = str(values.get("计入窗口") or "")
            entry["确认轮次"] = str(values.get("确认轮次") or "")
        return entry, missing

    # ---- 业务扩展：日志去重、批次确认回执与异常清洗 ----

    def dedup_batch(self, batch_id: str, trigger: str | None = None) -> dict[str, Any]:
        """按批次执行曝光去重并登记确认回执。

        同一（实验编码, 日志批次, 用户标识）只保留实际发生最早的一条；另一实验或
        批次的同名用户互不影响。补传记录与既有已去重记录一起参与；重跑幂等：
        保留集合不变时不生成新轮次、确认数不变，仅补充迟到重复明细。
        """
        if not batch_id:
            return {"ok": False, "message": "日志批次为空"}
        rows = [r for r in store.rows(self.MODULE) if str(r.get("日志批次", "")) == batch_id]
        if not rows:
            return {"ok": False, "message": f"批次 {batch_id} 无曝光记录"}
        active = [r for r in rows if r.get("status") in ACTIVE_STATUS]
        has_fresh = any(r.get("status") == "已接收" for r in active)
        groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
        for row in active:
            key = (str(row.get("实验编码", "")), str(row.get("用户标识", "")))
            groups.setdefault(key, []).append(row)
        retained: list[dict[str, Any]] = []
        for members in groups.values():
            members.sort(key=_retain_key)
            winner = members[0]
            retained.append(winner)
            for loser in members[1:]:
                loser["status"] = "异常丢弃"
                loser["pending"] = False
                loser["abnormal"] = True
                loser["丢弃原因"] = f"批次内重复曝光，保留记录 #{winner.get('id')}"
        for row in retained:
            row["status"] = "已去重"
            row["pending"] = False
            row["abnormal"] = False
        retained.sort(key=lambda r: int(r.get("id", 0)))
        retained_ids = [int(r.get("id", 0)) for r in retained]
        dropped_rows = [r for r in rows if r.get("status") == "异常丢弃"]
        rounds = self._receipts.setdefault(batch_id, [])
        prev = rounds[-1] if rounds else None
        prev_ids = [int(e.get("id", 0)) for e in (prev or {}).get("保留记录", [])]
        if prev is not None and sorted(prev_ids) == sorted(retained_ids):
            # 幂等重跑：沿用原确认数，只把新丢弃的迟到重复补进回执明细。
            prev["丢弃重复"] = [self._drop_brief(r) for r in dropped_rows]
            prev["复查时间"] = datetime.now(timezone.utc).isoformat()
            return {
                "ok": True,
                "message": (
                    f"批次 {batch_id} 重跑去重：保留集合未变化，沿用第 {prev['轮次']} 轮确认"
                    f"（确认数 {prev['确认数']}），迟到重复 {len(dropped_rows)} 条未再次入账"
                ),
                "receipt": prev,
            }
        if trigger is None:
            if prev is None:
                trigger = "首次去重"
            elif has_fresh:
                trigger = "补传去重"
            else:
                trigger = "重跑去重"
        round_no = (int(prev["轮次"]) + 1) if prev else 1
        exp_codes = {str(r.get("实验编码", "")) for r in rows}
        receipt = {
            "日志批次": batch_id,
            "轮次": round_no,
            "确认数": len(retained_ids),
            "保留记录": [_row_brief(r) for r in retained],
            "丢弃重复": [self._drop_brief(r) for r in dropped_rows],
            "受影响窗口": [],
            "触发来源": trigger,
            "确认时间": datetime.now(timezone.utc).isoformat(),
        }
        rounds.append(receipt)
        for row in retained:
            row["确认轮次"] = str(round_no)
        # 通知指标计算层：受影响作业标记“已收到新分母”，回执登记受影响窗口。
        affected = self._notify_metric_compute(batch_id, exp_codes, trigger)
        receipt["受影响窗口"] = affected
        return {
            "ok": True,
            "message": (
                f"批次 {batch_id} 第 {round_no} 轮确认完成：保留 {len(retained_ids)} 条，"
                f"丢弃重复 {len(dropped_rows)} 条，受影响窗口 {len(affected)} 个"
            ),
            "receipt": receipt,
        }

    @staticmethod
    def _drop_brief(row: dict[str, Any]) -> dict[str, Any]:
        brief = _row_brief(row)
        brief["丢弃原因"] = str(row.get("丢弃原因", ""))
        return brief

    def _notify_metric_compute(self, batch_id: str, exp_codes: set[str], trigger: str) -> list[str]:
        """把新确认轮次同步给指标计算作业，返回受影响窗口清单。"""
        from app.services import service_for

        service = service_for("metric_compute")
        handler = getattr(service, "on_exposure_confirmation", None)
        if handler is None:
            return []
        return list(handler(batch_id, exp_codes, trigger))

    def batch_receipts(self, batch_id: str) -> dict[str, Any] | None:
        """按原批次复查更正：返回确认回执、轮次历史与当前保留/重复明细。"""
        rows = [r for r in store.rows(self.MODULE) if str(r.get("日志批次", "")) == batch_id]
        rounds = self._receipts.get(batch_id, [])
        if not rows and not rounds:
            return None
        retained = [r for r in rows if r.get("status") == "已去重"]
        dropped = [r for r in rows if r.get("status") == "异常丢弃"]
        return {
            "日志批次": batch_id,
            "最新回执": rounds[-1] if rounds else None,
            "轮次历史": rounds,
            "当前保留": [_row_brief(r) for r in sorted(retained, key=lambda r: int(r.get("id", 0)))],
            "当前重复": [self._drop_brief(r) for r in sorted(dropped, key=lambda r: int(r.get("id", 0)))],
        }

    def latest_round(self, batch_id: str) -> int:
        rounds = self._receipts.get(batch_id, [])
        return int(rounds[-1]["轮次"]) if rounds else 0

    def drop_abnormal(self, entry_id: int, reason: str) -> dict[str, Any]:
        """丢弃异常曝光记录并把质量事件同步给时效监控。"""
        entry = store.find(self.MODULE, entry_id)
        if entry is None or entry.get("status") != "已接收":
            return {"ok": False, "message": "仅已接收记录可丢弃"}
        if not reason.strip():
            return {"ok": False, "message": "丢弃必须填写原因"}
        entry["status"] = "异常丢弃"
        entry["pending"] = False
        entry["abnormal"] = True
        entry["丢弃原因"] = reason
        freshness_rows = store.rows("data_freshness")
        freshness_rows.append({
            "id": max((int(r.get("id", 0)) for r in freshness_rows), default=0) + 1,
            "监控对象": f"exposure_log:{entry_id}",
            "数据域": "日志接入",
            "最新水位": datetime.now(timezone.utc).isoformat(),
            "延迟分钟": "0",
            "时效等级": "新鲜",
            "status": "新鲜",
            "pending": True,
            "abnormal": False,
        })
        return {"ok": True, "message": f"记录已丢弃（{reason}），质量事件已同步"}

    def batch_dedup_all(self) -> dict[str, Any]:
        """批量去重全部批次（含补传后需重确认的去重批次）。"""
        batches = {
            str(r.get("日志批次", ""))
            for r in store.rows(self.MODULE)
            if r.get("status") in ACTIVE_STATUS
        }
        done = sum(1 for b in batches if self.dedup_batch(b).get("ok"))
        return {"ok": True, "message": f"批量去重完成 {done}/{len(batches)} 个批次"}

    def consume_from_variant_assign(self, exp_code: str, user_id: str) -> dict[str, Any]:
        """读取分组分配器的用户分组，为曝光记录补充分组标签。"""
        matched = [
            r for r in store.rows("variant_assign")
            if r.get("实验编码") == exp_code
            and r.get("用户标识") == user_id
            and r.get("status") != "已剔除"
        ]
        if not matched:
            return {"ok": False, "message": f"用户 {user_id} 在实验 {exp_code} 无分组"}
        return {"ok": True, "分组名称": matched[-1].get("分组名称", ""), "分配批次": matched[-1].get("分配批次", "")}

    def produce_to_metric_compute(self, exp_code: str) -> dict[str, Any]:
        """把去重曝光数推送给指标计算层，登记计算输入摘要。"""
        rows = [
            r for r in store.rows(self.MODULE)
            if r.get("实验编码") == exp_code and r.get("status") == "已去重"
        ]
        if not rows:
            return {"ok": False, "message": f"实验 {exp_code} 无去重曝光"}
        summary = {
            "实验编码": exp_code,
            "去重曝光数": str(len(rows)),
            "最新曝光时间": max(str(r.get("曝光时间", "")) for r in rows),
        }
        return {"ok": True, "message": f"去重曝光 {len(rows)} 条已就绪可供计算", "summary": summary}

    def ingest_batch(
        self,
        exp_code: str,
        user_ids: list[str],
        batch_id: str | None = None,
        exposure_time: str | None = None,
    ) -> dict[str, Any]:
        """接入新曝光批次并为每个用户打上分组标签。

        传入已有 batch_id 即同批补传；exposure_time 记录实际发生时间，
        缺省取接入时刻。
        """
        if not user_ids or not exp_code:
            return {"ok": False, "message": "缺少实验编码或用户列表"}
        rows = store.rows(self.MODULE)
        batch_id = batch_id or f"EX-{exp_code}-{datetime.now(timezone.utc).timestamp():.0f}"
        moment = exposure_time or datetime.now(timezone.utc).isoformat()
        for uid in user_ids:
            group_info = self.consume_from_variant_assign(exp_code, uid)
            rows.append({
                "id": max((int(r.get("id", 0)) for r in rows), default=0) + 1,
                "日志批次": batch_id,
                "实验编码": exp_code,
                "用户标识": uid,
                "分组名称": group_info.get("分组名称", "未分组") if group_info.get("ok") else "未分组",
                "曝光时间": moment,
                "计入窗口": "",
                "确认轮次": "",
                "status": "已接收",
                "pending": True,
                "abnormal": False,
            })
        return {"ok": True, "message": f"批次 {batch_id} 接入 {len(user_ids)} 条曝光", "batch_id": batch_id}

    def export_dedup_stats(self, exp_code: str) -> dict[str, Any]:
        """导出实验去重统计，供报告生成器写入数据规模说明。"""
        rows = [r for r in store.rows(self.MODULE) if r.get("实验编码") == exp_code]
        return {
            "实验编码": exp_code,
            "总曝光": len(rows),
            "已去重": sum(1 for r in rows if r.get("status") == "已去重"),
            "异常丢弃": sum(1 for r in rows if r.get("status") == "异常丢弃"),
        }
