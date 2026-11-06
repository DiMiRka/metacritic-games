"""Адреса API без версии: постоянное перенаправление на /api/v1 с тем же методом и телом"""

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response

router = APIRouter(include_in_schema=False)


@router.api_route("/api/{path:path}", methods=["GET", "POST"])
async def unversioned(request: Request, path: str) -> Response:
    if path == "v1" or path.startswith("v1/"):
        # Под /api/v1 такого адреса нет: не перенаправляем по кругу
        return JSONResponse({"detail": "Not Found"}, status_code=404)
    query = f"?{request.url.query}" if request.url.query else ""
    response = RedirectResponse(f"/api/v1/{path}{query}", status_code=308)
    response.headers["Deprecation"] = "true"
    return response
