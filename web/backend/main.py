import asyncio
import hashlib
import logging
import os
import socket
import time
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Optional

from dotenv import load_dotenv

from common.utils import get_resource_path

load_dotenv(get_resource_path(".env"))

from fastapi import Depends, FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from web.backend.auth import (
    is_local_auth_bypass_enabled,
    is_trading_enabled,
    require_api_key,
    require_ws_token,
)
from web.backend.models import (
    HealthResponse,
    MonitorSnapshotData,
    MonitorSnapshotResponse,
    SuggestSymbolRequest,
    SuggestSymbolResponse,
    SystemStatusResponse,
    TradeActionRequest,
    TradeActionResponse,
)
from web.shared.symbol_helpers import suggest_option_symbol
from web.shared.state_store import read_snapshot
from web.shared.trade_latency import TradeLatencyRecorder, latency_enabled
from web.shared.trade_orders import (
    abort_idempotent_trade,
    begin_idempotent_trade,
    complete_idempotent_trade,
    register_pending_order,
)
from web.shared.trading_actions import execute_market_action, preload_scrip_master

logger = logging.getLogger("neoapp.web.api")
logging.basicConfig(level=os.getenv("WEB_LOG_LEVEL", "INFO"))

SCRIP_MASTER_PRELOAD_TIMEOUT_SECONDS = float(
    os.getenv("SCRIP_MASTER_PRELOAD_TIMEOUT_SECONDS", "60")
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Preload scrip master at startup without blocking the app indefinitely."""
    loop = asyncio.get_running_loop()
    try:
        await asyncio.wait_for(
            loop.run_in_executor(None, preload_scrip_master),
            timeout=SCRIP_MASTER_PRELOAD_TIMEOUT_SECONDS,
        )
    except asyncio.TimeoutError:
        logger.warning(
            "scrip_master_preload_failed reason=timeout timeout_s=%.1f "
            "(lazy-load fallback remains available)",
            SCRIP_MASTER_PRELOAD_TIMEOUT_SECONDS,
        )
    except Exception:
        logger.exception(
            "scrip_master_preload_failed reason=unexpected "
            "(lazy-load fallback remains available)"
        )
    yield


app = FastAPI(title="NeoApp Web API", version="0.1.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(BASE_DIR, "static")
WORKER_HEALTH_WINDOW_SECONDS = int(os.getenv("WORKER_HEALTH_WINDOW_SECONDS", "30"))

app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.middleware("http")
async def trade_action_latency_middleware(request: Request, call_next):
    if request.url.path != "/api/trade/action" or not latency_enabled():
        return await call_next(request)

    recorder = TradeLatencyRecorder()
    request.state.trade_latency = recorder
    t0 = time.perf_counter()
    response = await call_next(request)
    wall_ms = round((time.perf_counter() - t0) * 1000, 1)
    recorder.stages["http_wall_including_auth_ms"] = wall_ms
    logger.info(
        "trade_latency_http_wall request_id=%s wall_ms=%.1f handler_total_ms=%.1f",
        recorder.request_id,
        wall_ms,
        recorder.total_ms(),
    )
    response.headers["X-Trade-Request-Id"] = recorder.request_id
    response.headers["X-Trade-Server-Ms"] = str(recorder.total_ms())
    return response


@app.get("/")
def index():
    return FileResponse(os.path.join(STATIC_DIR, "index.html"))


@app.get("/health", response_model=HealthResponse)
def health():
    return HealthResponse(ok=True, service="neoapp-web-api", timestamp=datetime.now().isoformat())


def _parse_iso(ts: str):
    try:
        return datetime.fromisoformat(ts)
    except Exception:
        return None


def _snapshot_worker_health(snapshot: dict) -> bool:
    if not snapshot:
        return False
    ts_raw = str(snapshot.get("timestamp", "")).strip()
    ts = _parse_iso(ts_raw)
    if not ts:
        return False
    return (datetime.now() - ts).total_seconds() <= WORKER_HEALTH_WINDOW_SECONDS


def _auth_mode() -> str:
    if is_local_auth_bypass_enabled():
        return "local-noauth"
    return "token"


def _detect_server_ip() -> str:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


@app.get("/api/system/status", dependencies=[Depends(require_api_key)], response_model=SystemStatusResponse)
def system_status():
    snapshot = read_snapshot()
    return SystemStatusResponse(
        ok=True,
        service="neoapp-web-api",
        server_ip=_detect_server_ip(),
        trading_enabled=is_trading_enabled(),
        auth_mode=_auth_mode(),
        worker_snapshot_present=bool(snapshot),
        worker_healthy=_snapshot_worker_health(snapshot) if snapshot else None,
        last_worker_update=snapshot.get("timestamp") if snapshot else None,
        timestamp=datetime.now().isoformat(),
    )


@app.get("/api/monitor/snapshot", dependencies=[Depends(require_api_key)], response_model=MonitorSnapshotResponse)
def monitor_snapshot():
    snapshot = read_snapshot()
    if not snapshot:
        return MonitorSnapshotResponse(
            ok=True,
            warming_up=True,
            message="Worker snapshot not available yet",
            timestamp=datetime.now().isoformat(),
        )

    worker_healthy = _snapshot_worker_health(snapshot)
    status = str(snapshot.get("status", "ok"))
    if not worker_healthy and status == "ok":
        status = "stale"

    data = {**snapshot, "status": status, "healthy": worker_healthy}
    return MonitorSnapshotResponse(
        ok=True,
        warming_up=False,
        data=MonitorSnapshotData(**data),
        timestamp=datetime.now().isoformat(),
    )


@app.post("/api/trade/action", dependencies=[Depends(require_api_key)], response_model=TradeActionResponse)
def trade_action(payload: TradeActionRequest, request: Request):
    latency: Optional[TradeLatencyRecorder] = getattr(request.state, "trade_latency", None)
    if latency:
        latency.mark("fastapi_handler_start")

    if not is_trading_enabled():
        raise HTTPException(status_code=403, detail="Trading is disabled. Set TRADING_ENABLED=true to enable live orders.")
    if latency:
        latency.mark("after_trading_enabled_check")

    snapshot = read_snapshot()
    risk = snapshot.get("risk", {}) if isinstance(snapshot, dict) else {}
    if latency:
        latency.mark("after_snapshot_read")
    if payload.action == "BUY" and isinstance(risk, dict):
        if not risk.get("buy_allowed", True):
            reason = risk.get("reason") or "buy_locked"
            cooloff = int(risk.get("cooloff_remaining", 0) or 0)
            if cooloff > 0:
                raise HTTPException(status_code=429, detail=f"Buy blocked: cooling off ({cooloff}s remaining)")
            raise HTTPException(status_code=429, detail=f"Buy blocked by risk controls: {reason}")
    if latency:
        latency.mark("after_risk_checks")

    side = "BUY" if payload.action == "BUY" else "SELL"
    request_id = latency.request_id if latency else None
    client_request_id = (payload.client_request_id or "").strip() or None

    cached_response = None
    if client_request_id:
        try:
            cached_response = begin_idempotent_trade(client_request_id)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        if cached_response:
            if latency:
                latency.add_meta(idempotent_replay=True, client_request_id=client_request_id)
                latency.mark("idempotent_cache_hit")
                latency.flush(action=payload.action, symbol=payload.trading_symbol)
            return TradeActionResponse(**cached_response)

    try:
        resp = execute_market_action(
            payload.trading_symbol,
            payload.lots,
            side,
            latency=latency,
            wait_for_completion=False,
        )
    except ValueError as exc:
        if client_request_id:
            abort_idempotent_trade(client_request_id)
        if latency:
            latency.flush(action=payload.action, symbol=payload.trading_symbol)
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        if client_request_id:
            abort_idempotent_trade(client_request_id)
        if latency:
            latency.flush(action=payload.action, symbol=payload.trading_symbol)
        logger.exception("Trade action failed")
        raise HTTPException(status_code=500, detail=f"Trade action failed: {exc}") from exc

    order_id = str(resp.get("order_id", ""))
    if order_id:
        register_pending_order(
            request_id=request_id or "",
            order_id=order_id,
            symbol=str(resp.get("symbol") or payload.trading_symbol),
            side=str(resp.get("side") or side),
            lots=int(resp.get("lots") or payload.lots),
            client_request_id=client_request_id,
            action=payload.action,
        )

    api_response = TradeActionResponse(
        ok=True,
        action=payload.action,
        trading_symbol=payload.trading_symbol,
        lots=payload.lots,
        request_id=request_id,
        broker_response=resp if isinstance(resp, dict) else {"raw": str(resp)},
    )
    if client_request_id:
        complete_idempotent_trade(client_request_id, api_response.model_dump())

    if latency:
        latency.mark("before_immediate_response")
        latency.flush(action=payload.action, symbol=payload.trading_symbol)

    return api_response


@app.post("/api/symbol/suggest", dependencies=[Depends(require_api_key)], response_model=SuggestSymbolResponse)
def suggest_symbol(payload: SuggestSymbolRequest):
    try:
        symbol = suggest_option_symbol(
            base_symbol=payload.base_symbol,
            index_ltp=payload.index_ltp,
            option_type=payload.option_type,
            offset=payload.offset,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return SuggestSymbolResponse(
        ok=True,
        symbol=symbol,
        base_symbol=payload.base_symbol,
        option_type=payload.option_type,
        offset=payload.offset,
    )


@app.websocket("/ws/monitor")
async def ws_monitor(websocket: WebSocket):
    token = str(websocket.query_params.get("token", ""))
    try:
        require_ws_token(token)
    except HTTPException:
        await websocket.close(code=1008)
        return

    await websocket.accept()
    last_hash = ""

    try:
        while True:
            snapshot = read_snapshot()
            if not snapshot:
                payload = {
                    "status": "warming_up",
                    "warming_up": True,
                    "healthy": False,
                    "timestamp": datetime.now().isoformat(),
                    "message": "Worker snapshot not available yet",
                }
            else:
                status = str(snapshot.get("status", "ok"))
                healthy = _snapshot_worker_health(snapshot)
                if not healthy and status == "ok":
                    status = "stale"
                payload = {**snapshot, "status": status, "healthy": healthy}

            blob = str(payload).encode("utf-8")
            curr_hash = hashlib.sha256(blob).hexdigest()

            if curr_hash != last_hash:
                await websocket.send_json(payload)
                last_hash = curr_hash

            await asyncio.sleep(1)
    except WebSocketDisconnect:
        return
    except Exception:
        logger.exception("WebSocket monitor loop error")
        await websocket.close(code=1011)
