"""告警 API."""
from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from ...common.db import get_session
from ...schemas.events import AlertOut
from ..crud import list_alerts

router = APIRouter(prefix="/api/alerts", tags=["alerts"])


@router.get("", response_model=list[AlertOut])
async def list_(
    limit: int = Query(100, ge=1, le=500),
    session: AsyncSession = Depends(get_session),
):
    """告警列表 (按时间倒序)."""
    rows = await list_alerts(session, limit=limit)
    return [AlertOut.model_validate(a, from_attributes=True) for a in rows]
