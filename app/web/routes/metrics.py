import ipaddress
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from app.auth.service import AuthContext
from app.monitoring.metrics import refresh
from app.web.deps import Session, load_auth

router = APIRouter()


def internal_client(request: Request) -> bool:
    """Запрос из внутренней сети Docker, например от Prometheus.
    Снаружи всё идёт через Caddy, а он подставляет настоящий адрес клиента в X-Forwarded-For,
    поэтому из интернета внутренний адрес не подделать
    """
    host = request.client.host if request.client else ""
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    return address.is_private or address.is_loopback


@router.get("/metrics", include_in_schema=False)
async def metrics(
    request: Request,
    session: Session,
    auth: Annotated[AuthContext | None, Depends(load_auth)],
) -> Response:
    if not internal_client(request) and not (auth and auth.is_admin):
        raise HTTPException(status_code=404)
    await refresh(session)
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)
