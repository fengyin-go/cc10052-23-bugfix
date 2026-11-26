"""领域查询接口：曝光批次确认回执与指标作业计算依据的只读查询。

路径均为三段以上，与通用资源路由 /api/{module}/{entry_id} 不冲突。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException

from app.services import service_for

router = APIRouter(prefix="/api", tags=["领域查询"])


@router.get("/exposure_log/batches/{batch_id}/receipts", response_model=dict)
def exposure_batch_receipts(batch_id: str) -> dict[str, Any]:
    """按原批次复查更正：确认回执、轮次历史与当前保留/重复明细。"""
    service = service_for("exposure_log")
    data = service.batch_receipts(batch_id) if service else None
    if data is None:
        raise HTTPException(status_code=404, detail=f"批次 {batch_id} 无曝光记录或确认回执")
    return data


@router.get("/metric_compute/jobs/{entry_id}/basis", response_model=dict)
def metric_job_basis(entry_id: int) -> dict[str, Any]:
    """作业计算依据：采用记录、确认轮次、修订理由与逐批次分母对账。"""
    service = service_for("metric_compute")
    data = service.job_basis(entry_id) if service else None
    if data is None:
        raise HTTPException(status_code=404, detail=f"计算作业 {entry_id} 不存在或已归档")
    return data
