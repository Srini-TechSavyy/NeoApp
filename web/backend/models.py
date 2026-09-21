from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field


class TradeActionRequest(BaseModel):
    trading_symbol: str = Field(min_length=5, max_length=40)
    lots: int = Field(default=1, ge=1, le=500)
    action: Literal["BUY", "EXIT"]
    client_request_id: Optional[str] = Field(default=None, min_length=8, max_length=64)


class TradeActionResponse(BaseModel):
    ok: bool
    action: str
    trading_symbol: str
    lots: int
    request_id: Optional[str] = None
    broker_response: Dict[str, Any]


class HealthResponse(BaseModel):
    ok: bool
    service: str
    timestamp: str


class MonitorPeriod(BaseModel):
    type: str
    week_start: str
    week_end: str


class MonitorTradeStats(BaseModel):
    no_of_trades: int
    wins: int
    losses: int
    win_rate: float
    recent: List[int]


class MonitorPnL(BaseModel):
    gross: float
    net: float
    day: float
    charges: float


class MonitorSnapshotData(BaseModel):
    model_config = ConfigDict(extra="allow")

    status: str
    timestamp: str
    period: Optional[MonitorPeriod] = None
    trade_stats: Optional[MonitorTradeStats] = None
    pnl: Optional[MonitorPnL] = None
    source: Optional[str] = None
    healthy: Optional[bool] = None
    message: Optional[str] = None
    error: Optional[str] = None
    last_success_at: Optional[str] = None
    consecutive_failures: Optional[int] = None


class MonitorSnapshotResponse(BaseModel):
    ok: bool
    warming_up: bool
    data: Optional[MonitorSnapshotData] = None
    message: Optional[str] = None
    timestamp: str


class SystemStatusResponse(BaseModel):
    ok: bool
    service: str
    server_ip: Optional[str] = None
    trading_enabled: bool
    auth_mode: str
    worker_snapshot_present: bool
    worker_healthy: Optional[bool] = None
    last_worker_update: Optional[str] = None
    timestamp: str


class SuggestSymbolRequest(BaseModel):
    base_symbol: str = Field(min_length=3, max_length=20)
    index_ltp: float = Field(gt=0)
    option_type: Literal["CE", "PE"]
    offset: int = Field(default=0, ge=-10, le=10)


class SuggestSymbolResponse(BaseModel):
    ok: bool
    symbol: str
    base_symbol: str
    option_type: str
    offset: int
