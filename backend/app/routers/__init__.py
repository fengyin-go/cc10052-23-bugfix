"""业务路由汇总：领域查询路由在前，所有模块共用一套资源路由，由领域服务完成分派。"""
from __future__ import annotations

from app.routers import domain, resource

ROUTERS = [domain, resource]
