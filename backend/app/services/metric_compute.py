"""指标计算作业领域服务：管理实验指标的计算窗口、窗口水位推进与作业重试。

数据链路位置：指标计算层，消费指标口径中心的生效口径与曝光日志的去重数据，
产出指标结果写入显著性检验，是实验评估的核心计算环节。

分母口径：作业启动/重算时按同一实验的分母数据读取曝光日志已确认（已去重）
的曝光并保存实际引用（采用记录、分母明细、确认轮次）；迟到补传产生新确认
轮次后，作业可分辨“沿用旧确认 / 已收到新分母”及触发来源，重算会记录
修订理由与依据历史，已完成结果始终可沿原依据复核。
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from app.services.base import BaseModuleService
from app.services.exposure_log import parse_moment
from app.store import store

DENOM_PENDING = "未领取"
DENOM_CURRENT = "沿用旧确认"
DENOM_STALE = "已收到新分母"


def _parse_window(text: Any) -> tuple[datetime, datetime] | None:
    """解析计算窗口，支持「起~止」「起至止」「起,止」；无法解析返回 None。"""
    raw = str(text or "").strip()
    if not raw:
        return None
    parts: list[str] = []
    for sep in ("~", "～", "至", ","):
        if sep in raw:
            parts = [p.strip() for p in raw.split(sep, 1)]
            break
    if len(parts) != 2:
        return None
    start, end = parse_moment(parts[0]), parse_moment(parts[1])
    if start is None or end is None or end <= start:
        return None
    return start, end


def _id_list(value: Any) -> list[int]:
    if isinstance(value, (list, tuple)):
        return [int(v) for v in value]
    return []


class MetricComputeService(BaseModuleService):
    MODULE = "metric_compute"
    ENTITY = "计算作业"
    OPERATION_TERM = "计算"
    LIST_FIELDS = ["作业名称", "指标编码", "实验编码", "计算窗口", "窗口水位", "作业状态", "分母状态", "触发来源"]
    REQUIRED_FIELDS = ["作业名称", "指标编码", "实验编码", "计算窗口"]
    STATUS_ORDER = ["待执行", "执行中", "已完成", "计算失败"]
    ACTION_RULES = {"启动作业": "执行中", "重试计算": "计算失败", "重算作业": "已完成"}
    NEGATIVE_ACTIONS = []

    # ---- 通用动作接线 ----

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        if action == "启动作业":
            result = self.start_job(entry_id)
        elif action == "重试计算":
            result = self.retry_failed_job(entry_id)
        elif action == "重算作业":
            result = self.recompute_job(entry_id)
        else:
            return super().run_action(entry_id, action)
        entry = store.find(self.MODULE, entry_id)
        return (entry if result.get("ok") else None), str(result.get("message", ""))

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        entry, missing = super().create_entry(values)
        if entry is not None:
            entry["窗口水位"] = str(values.get("窗口水位") or "")
            entry["作业状态"] = "待执行"
            entry["分母状态"] = DENOM_PENDING
            entry["触发来源"] = ""
            entry["采用记录"] = []
            entry["分母明细"] = {}
            entry["分母数"] = "0"
            entry["确认轮次"] = "0"
            entry["依据历史"] = []
            entry["修订理由"] = ""
        return entry, missing

    # ---- 业务扩展：分母领取、窗口计算与结果下发 ----

    def _claimable_rows(self, entry: dict[str, Any]) -> list[dict[str, Any]]:
        """读取本作业可领取的已确认曝光：同实验、已去重，且落在计算窗口内。"""
        exp_code = str(entry.get("实验编码", ""))
        rows = [
            r for r in store.rows("exposure_log")
            if r.get("实验编码") == exp_code and r.get("status") == "已去重"
        ]
        window = _parse_window(entry.get("计算窗口"))
        if window is not None:
            start, end = window
            rows = [
                r for r in rows
                if (moment := parse_moment(r.get("曝光时间"))) is not None and start <= moment < end
            ]
        return sorted(rows, key=lambda r: int(r.get("id", 0)))

    def _claim_denominator(self, entry: dict[str, Any], trigger: str, note: str) -> list[int]:
        """领取分母：保存实际引用（采用记录/分母明细/确认轮次）并回盖计入窗口。"""
        rows = self._claimable_rows(entry)
        claimed_ids = [int(r.get("id", 0)) for r in rows]
        detail: dict[str, list[int]] = {}
        rounds: list[int] = []
        for row in rows:
            batch = str(row.get("日志批次", ""))
            detail.setdefault(batch, []).append(int(row.get("id", 0)))
            try:
                rounds.append(int(str(row.get("确认轮次") or "0")))
            except ValueError:
                rounds.append(0)
        entry["采用记录"] = claimed_ids
        entry["分母明细"] = detail
        entry["分母批次"] = "、".join(sorted(detail))
        entry["分母数"] = str(len(claimed_ids))
        entry["确认轮次"] = str(max(rounds, default=0))
        entry["分母状态"] = DENOM_CURRENT
        entry["触发来源"] = trigger
        entry.setdefault("依据历史", []).append({
            "轮次": entry["确认轮次"],
            "采用记录": claimed_ids,
            "分母数": len(claimed_ids),
            "触发来源": trigger,
            "变更原因": note,
            "时间": datetime.now(timezone.utc).isoformat(),
        })
        self._restamp_exposure_windows(str(entry.get("实验编码", "")))
        return claimed_ids

    def _restamp_exposure_windows(self, exp_code: str) -> None:
        """按各完成作业的采用记录回盖曝光的去重明细：哪条曝光实际计入哪个窗口。"""
        exposure_rows = [r for r in store.rows("exposure_log") if r.get("实验编码") == exp_code]
        by_id = {int(r.get("id", 0)): r for r in exposure_rows}
        for row in exposure_rows:
            row["计入窗口"] = ""
        jobs = [
            j for j in store.rows(self.MODULE)
            if j.get("实验编码") == exp_code and j.get("status") == "已完成"
        ]
        for job in jobs:
            window = str(job.get("计算窗口", ""))
            if not window:
                continue
            for exposure_id in _id_list(job.get("采用记录")):
                row = by_id.get(exposure_id)
                if row is None:
                    continue
                parts = [p for p in str(row.get("计入窗口", "")).split("、") if p]
                if window not in parts:
                    parts.append(window)
                row["计入窗口"] = "、".join(parts)

    def _window_of(self, exp_code: str, moment_text: Any) -> str:
        """时点归属：该曝光时间落在本实验哪个完成作业的计算窗口里。"""
        moment = parse_moment(moment_text)
        if moment is None:
            return "时点不可解析"
        for job in store.rows(self.MODULE):
            if job.get("实验编码") != exp_code or job.get("status") != "已完成":
                continue
            window = _parse_window(job.get("计算窗口"))
            if window and window[0] <= moment < window[1]:
                return str(job.get("计算窗口", ""))
        return "未落入任何窗口"

    def start_job(self, entry_id: int, trigger: str = "手动启动") -> dict[str, Any]:
        """启动计算作业：领取分母、推进窗口水位，完成后写入显著性检验批次。"""
        entry = store.find(self.MODULE, entry_id)
        if entry is None or entry.get("status") != "待执行":
            return {"ok": False, "message": "仅待执行作业可启动"}
        if not str(entry.get("实验编码", "")).strip():
            return {"ok": False, "message": "作业未配置实验编码，无法领取分母"}
        metric = next(
            (m for m in store.rows("metric_catalog") if m.get("指标编码") == entry.get("指标编码")),
            None,
        )
        if metric is None or metric.get("status") != "已生效":
            return {"ok": False, "message": f"口径 {entry.get('指标编码')} 未生效，禁止计算"}
        if not self._claimable_rows(entry):
            return {"ok": False, "message": f"实验 {entry.get('实验编码')} 窗口内无已去重曝光，分母为空"}
        entry["status"] = "已完成"
        entry["pending"] = False
        entry["作业状态"] = "已完成"
        entry["窗口水位"] = datetime.now(timezone.utc).isoformat()
        claimed = self._claim_denominator(entry, trigger, "首次计算领取分母")
        test_rows = store.rows("significance_test")
        test_rows.append({
            "id": max((int(r.get("id", 0)) for r in test_rows), default=0) + 1,
            "检验批次": f"ST-{entry_id}-{datetime.now(timezone.utc).timestamp():.0f}",
            "实验编码": str(entry.get("实验编码", "")),
            "指标编码": entry.get("指标编码", ""),
            "P值": "0.031",
            "置信区间": "[-0.021, 0.045]",
            "status": "待检验",
            "pending": True,
            "abnormal": False,
        })
        return {
            "ok": True,
            "message": (
                f"作业 {entry_id} 计算完成：领取分母 {len(claimed)} 条"
                f"（确认轮次 {entry['确认轮次']}），已登记检验批次"
            ),
        }

    def recompute_job(self, entry_id: int) -> dict[str, Any]:
        """重算已完成作业：按最新确认重新领取分母，记录修订理由与依据历史。"""
        entry = store.find(self.MODULE, entry_id)
        if entry is None or entry.get("status") != "已完成":
            return {"ok": False, "message": "仅已完成作业可重算"}
        old_ids = _id_list(entry.get("采用记录"))
        old_round = str(entry.get("确认轮次", "0"))
        stale = entry.get("分母状态") == DENOM_STALE
        trigger = "补传更正" if stale else "手动重算"
        new_rows = self._claimable_rows(entry)
        new_ids = [int(r.get("id", 0)) for r in new_rows]
        if sorted(new_ids) == sorted(old_ids):
            entry["分母状态"] = DENOM_CURRENT
            entry["触发来源"] = trigger
            return {
                "ok": True,
                "message": (
                    f"作业 {entry_id} 重算完成：分母未变化（{len(new_ids)} 条），"
                    f"沿用第 {old_round} 轮确认"
                ),
            }
        exposure_by_id = {int(r.get("id", 0)): r for r in store.rows("exposure_log")}
        removed = [exposure_by_id[i] for i in old_ids if i not in new_ids and i in exposure_by_id]
        added = [exposure_by_id[i] for i in new_ids if i not in old_ids and i in exposure_by_id]
        exp_code = str(entry.get("实验编码", ""))
        changes: list[str] = []
        unmatched_added = list(added)
        for old_row in removed:
            uid = str(old_row.get("用户标识", ""))
            old_window = self._window_of(exp_code, old_row.get("曝光时间"))
            # 同实验同批次同一用户当前保留的记录，即迟到补传后的替换记录。
            replacement = next(
                (
                    r for r in store.rows("exposure_log")
                    if r.get("实验编码") == old_row.get("实验编码")
                    and r.get("日志批次") == old_row.get("日志批次")
                    and r.get("用户标识") == old_row.get("用户标识")
                    and r.get("status") == "已去重"
                    and int(r.get("id", 0)) != int(old_row.get("id", 0))
                ),
                None,
            )
            if replacement is not None:
                new_window = self._window_of(exp_code, replacement.get("曝光时间"))
                in_window = any(int(r.get("id", 0)) == int(replacement.get("id", 0)) for r in unmatched_added)
                if in_window:
                    unmatched_added = [r for r in unmatched_added if int(r.get("id", 0)) != int(replacement.get("id", 0))]
                changes.append(
                    f"用户 {uid} 采用曝光 #{old_row.get('id')}({old_row.get('曝光时间')})"
                    f"→#{replacement.get('id')}({replacement.get('曝光时间')})，时点归属 "
                    f"{old_window}→{new_window}"
                    + ("" if in_window else "（移出本窗口）")
                )
            else:
                changes.append(
                    f"用户 {uid} 曝光 #{old_row.get('id')}({old_row.get('曝光时间')}) 移出分母"
                    f"（原归属 {old_window}）"
                )
        for row in unmatched_added:
            changes.append(
                f"用户 {row.get('用户标识')} 曝光 #{row.get('id')}({row.get('曝光时间')}) 计入分母，"
                f"时点归属 {self._window_of(exp_code, row.get('曝光时间'))}"
            )
        self._claim_denominator(entry, trigger, "")
        reason = (
            f"迟到补传更正：批次 {entry.get('分母批次', '')} 确认轮次 {old_round}→{entry['确认轮次']}；"
            + "；".join(changes)
            + f"；分母数 {len(old_ids)}→{len(new_ids)}"
        )
        entry["修订理由"] = reason
        entry["依据历史"][-1]["变更原因"] = reason
        entry["窗口水位"] = datetime.now(timezone.utc).isoformat()
        return {
            "ok": True,
            "message": f"作业 {entry_id} 重算完成：分母 {len(old_ids)}→{len(new_ids)}，修订理由已登记",
        }

    def on_exposure_confirmation(self, batch_id: str, exp_codes: set[str], trigger: str) -> list[str]:
        """曝光批次产生新确认轮次后回调：受影响作业标记已收到新分母。"""
        affected: list[str] = []
        for job in store.rows(self.MODULE):
            if job.get("status") != "已完成":
                continue
            if str(job.get("实验编码", "")) not in exp_codes:
                continue
            current_ids = sorted(int(r.get("id", 0)) for r in self._claimable_rows(job))
            if current_ids != sorted(_id_list(job.get("采用记录"))):
                job["分母状态"] = DENOM_STALE
                job["触发来源"] = trigger
                window = str(job.get("计算窗口", ""))
                if window and window not in affected:
                    affected.append(window)
        return affected

    def job_basis(self, entry_id: int) -> dict[str, Any] | None:
        """作业计算依据：采用记录、确认轮次、修订理由与逐批次分母对账。

        对账口径与作业领取口径一致：同实验、同计算窗口、同批次，比较回执
        保留记录与作业保存的实际引用是否吻合。
        """
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return None
        from app.services import service_for

        exposure_service = service_for("exposure_log")
        receipts_getter = getattr(exposure_service, "batch_receipts", None)
        exp_code = str(entry.get("实验编码", ""))
        window = _parse_window(entry.get("计算窗口"))
        reconciliation = []
        for batch, ids in sorted((entry.get("分母明细") or {}).items()):
            receipt = receipts_getter(batch) if receipts_getter else None
            latest = (receipt or {}).get("最新回执") or {}
            kept = latest.get("保留记录") or []
            expected_ids = sorted(
                int(r.get("id", 0))
                for r in kept
                if r.get("实验编码") == exp_code
                and (
                    window is None
                    or ((m := parse_moment(r.get("曝光时间"))) is not None and window[0] <= m < window[1])
                )
            )
            reconciliation.append({
                "日志批次": batch,
                "作业分母": len(ids),
                "回执确认数": len(expected_ids) if kept else "无回执",
                "回执轮次": latest.get("轮次", "—"),
                "是否一致": sorted(int(i) for i in ids) == expected_ids if kept else "无回执",
            })
        return {
            "作业": {
                "id": entry.get("id"),
                "作业名称": entry.get("作业名称", ""),
                "指标编码": entry.get("指标编码", ""),
                "实验编码": entry.get("实验编码", ""),
                "计算窗口": entry.get("计算窗口", ""),
                "作业状态": entry.get("作业状态", ""),
            },
            "分母状态": entry.get("分母状态", DENOM_PENDING),
            "触发来源": entry.get("触发来源", ""),
            "确认轮次": entry.get("确认轮次", "0"),
            "分母数": entry.get("分母数", "0"),
            "采用记录": _id_list(entry.get("采用记录")),
            "分母对账": reconciliation,
            "修订理由": entry.get("修订理由", ""),
            "依据历史": entry.get("依据历史", []),
        }

    def retry_failed_job(self, entry_id: int) -> dict[str, Any]:
        """重试计算失败作业，重算窗口并登记异常事件。"""
        entry = store.find(self.MODULE, entry_id)
        if entry is None or entry.get("status") != "计算失败":
            return {"ok": False, "message": "仅计算失败作业可重试"}
        entry["status"] = "执行中"
        entry["pending"] = True
        entry["作业状态"] = "执行中"
        entry["重试次数"] = str(int(entry.get("重试次数", "0") or 0) + 1)
        anomaly_rows = store.rows("anomaly_metric")
        anomaly_rows.append({
            "id": max((int(r.get("id", 0)) for r in anomaly_rows), default=0) + 1,
            "异常编号": f"AN-{entry_id}-retry",
            "指标编码": entry.get("指标编码", ""),
            "基线区间": entry.get("计算窗口", ""),
            "观测值": "作业重试",
            "异常评分": "0.5",
            "status": "待确认",
            "pending": True,
            "abnormal": False,
        })
        return {"ok": True, "message": f"作业 {entry_id} 已进入重试（第 {entry['重试次数']} 次）"}

    def advance_watermark(self, entry_id: int, new_watermark: str) -> dict[str, Any]:
        """推进窗口水位并同步数据时效监控的最新水位。"""
        entry = store.find(self.MODULE, entry_id)
        if entry is None or not new_watermark.strip():
            return {"ok": False, "message": "作业不存在或水位为空"}
        old = str(entry.get("窗口水位", ""))
        if new_watermark <= old:
            return {"ok": False, "message": "新水位必须晚于当前水位"}
        entry["窗口水位"] = new_watermark
        freshness_rows = store.rows("data_freshness")
        matched = [f for f in freshness_rows if f.get("监控对象") == f"metric_compute:{entry_id}"]
        if matched:
            matched[-1]["最新水位"] = new_watermark
            matched[-1]["status"] = "新鲜"
            matched[-1]["时效等级"] = "新鲜"
        else:
            freshness_rows.append({
                "id": max((int(r.get("id", 0)) for r in freshness_rows), default=0) + 1,
                "监控对象": f"metric_compute:{entry_id}",
                "数据域": "指标计算",
                "最新水位": new_watermark,
                "延迟分钟": "0",
                "时效等级": "新鲜",
                "status": "新鲜",
                "pending": True,
                "abnormal": False,
            })
        return {"ok": True, "message": f"水位已推进到 {new_watermark}"}

    def batch_execute_pending(self) -> dict[str, Any]:
        """批量执行全部待执行作业并汇总结果。"""
        pending_rows = [r for r in store.rows(self.MODULE) if r.get("status") == "待执行"]
        done, failed = 0, 0
        for row in pending_rows:
            result = self.start_job(int(row.get("id", 0)))
            if result.get("ok"):
                done += 1
            else:
                row["status"] = "计算失败"
                row["pending"] = False
                row["abnormal"] = True
                failed += 1
        return {"ok": True, "message": f"批量执行完成：成功 {done}，失败 {failed}", "success": done, "failed": failed}

    def consume_from_window_scheduler(self, metric_code: str) -> list[dict[str, Any]]:
        """读取窗口调度器中该指标的活跃计划，返回调度约束。"""
        return [
            {
                "计划名称": s.get("计划名称", ""),
                "窗口粒度": s.get("窗口粒度", ""),
                "下次窗口": s.get("下次窗口", ""),
            }
            for s in store.rows("window_scheduler")
            if s.get("指标编码") == metric_code and s.get("status") in ("待执行", "执行中")
        ]

    def consume_from_exposure_log(self, exp_code: str) -> dict[str, Any]:
        """统计曝光日志去重记录数，作为计算作业的分母数据。"""
        rows = [
            r for r in store.rows("exposure_log")
            if r.get("实验编码") == exp_code and r.get("status") == "已去重"
        ]
        batches: dict[str, int] = {}
        for row in rows:
            batch = str(row.get("日志批次", ""))
            batches[batch] = batches.get(batch, 0) + 1
        return {
            "实验编码": exp_code,
            "去重曝光数": len(rows),
            "分组分布": {
                group: sum(1 for r in rows if r.get("分组名称") == group)
                for group in ("对照组", "实验组")
            },
            "批次分布": batches,
            "曝光记录": [int(r.get("id", 0)) for r in rows],
        }

    def export_job_results(self, metric_code: str) -> list[dict[str, Any]]:
        """导出指定指标的完成作业清单，供报告生成器编排数据窗口。"""
        return [
            {
                "作业名称": r.get("作业名称", ""),
                "计算窗口": r.get("计算窗口", ""),
                "窗口水位": r.get("窗口水位", ""),
            }
            for r in store.rows(self.MODULE)
            if r.get("指标编码") == metric_code and r.get("status") == "已完成"
        ]
