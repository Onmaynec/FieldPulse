import logging
import os
import time
import uuid
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text

from .db import SessionLocal
from .api import router as api_router
from .mobile import router as mobile_router
from .admin import router as admin_router

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format="%(message)s")
logger = logging.getLogger("fieldpulse")
app = FastAPI(title="FieldPulse", version="0.2.0",
              description="Диспетчеризация инженеров, SLA и офлайн-синхронизация",
              docs_url="/api/docs", redoc_url=None)
app.include_router(api_router)
app.include_router(mobile_router)
app.include_router(admin_router)

@app.middleware("http")
async def trace(request: Request, call_next):
    request_id = request.headers.get("X-Request-ID", "")[:80] or uuid.uuid4().hex
    request.state.request_id = request_id
    started = time.perf_counter()
    try:
        result = await call_next(request)
    except Exception:
        logger.exception('{"request_id":"%s","unhandled":true}', request_id)
        result = JSONResponse(status_code=500, content={
            "code": "INTERNAL_ERROR", "message": "Внутренняя ошибка", "requestId": request_id, "details": None})
    result.headers["X-Request-ID"] = request_id
    result.headers["X-Content-Type-Options"] = "nosniff"
    result.headers["Referrer-Policy"] = "same-origin"
    result.headers["X-Frame-Options"] = "DENY"
    result.headers["Content-Security-Policy"] = (
        "default-src 'self'; img-src 'self' data:; connect-src 'self'; "
        "style-src 'self'; script-src 'self'; object-src 'none'; base-uri 'self'")
    logger.info('{"request_id":"%s","method":"%s","path":"%s","status":%d,"elapsed_ms":%.1f}',
                request_id, request.method, request.url.path, result.status_code,
                (time.perf_counter() - started) * 1000)
    return result

@app.exception_handler(HTTPException)
async def http_error(request: Request, exc: HTTPException):
    detail = exc.detail
    return JSONResponse(status_code=exc.status_code, content={
        "code": detail.get("code", "HTTP_%d" % exc.status_code) if isinstance(detail, dict) else "HTTP_%d" % exc.status_code,
        "message": detail if isinstance(detail, str) else "Операция не выполнена",
        "requestId": getattr(request.state, "request_id", ""),
        "details": detail if isinstance(detail, dict) else None,
    })

@app.exception_handler(RequestValidationError)
async def validation_error(request: Request, exc: RequestValidationError):
    return JSONResponse(status_code=422, content={
        "code": "VALIDATION_ERROR", "message": "Проверьте введённые данные",
        "requestId": getattr(request.state, "request_id", ""),
        "details": [{"loc": list(e["loc"]), "message": e["msg"]} for e in exc.errors()],
    })

@app.get("/health/live")
def live():
    return {"status": "ok"}

@app.get("/health/ready")
def ready():
    try:
        with SessionLocal() as db:
            db.execute(text("SELECT 1"))
        return {"status": "ok"}
    except Exception:
        raise HTTPException(503, detail="База данных недоступна")

PUBLIC = Path(__file__).resolve().parent.parent / "public"
app.mount("/static", StaticFiles(directory=PUBLIC), name="static")

@app.get("/")
def index():
    return FileResponse(PUBLIC / "index.html")

@app.get("/sw.js")
def service_worker():
    return FileResponse(PUBLIC / "sw.js", media_type="application/javascript", headers={"Service-Worker-Allowed": "/"})
