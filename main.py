import asyncio
import base64
import hashlib
import hmac
import json
import os
import secrets
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv

import httpx
import yfinance as yf

# Load .env from the same folder as this file (back/) so MONGO_URI is available
load_dotenv(Path(__file__).resolve().parent / ".env")
from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from motor.motor_asyncio import AsyncIOMotorClient
from pydantic import BaseModel, Field

app = FastAPI(title="Portfolio API")

# Orígenes adicionales permitidos (ej. dominio propio en Vercel), configurables
# sin tocar código: CORS_EXTRA_ORIGINS="https://mi-dominio.com,https://otro.com"
_cors_extra_origins = [
    o.strip() for o in os.environ.get("CORS_EXTRA_ORIGINS", "").split(",") if o.strip()
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000", "http://127.0.0.1:3000", *_cors_extra_origins],
    # Cubre el dominio de producción y todos los previews de Vercel para este
    # proyecto (la URL de preview trae un hash distinto en cada deploy).
    allow_origin_regex=r"https://finance-front.*\.vercel\.app",
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

MONGO_URI = os.environ.get("MONGO_URI")
FINNHUB_API_KEY = os.environ.get("FINNHUB_API_KEY")
USD_TO_GTQ_FALLBACK = float(os.environ.get("USD_TO_GTQ_FALLBACK", "7.75"))
JWT_SECRET = os.environ.get("JWT_SECRET", "")
ADMIN_EMAIL = os.environ.get("ADMIN_EMAIL", "")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "")
TOKEN_TTL_SECONDS = 7 * 24 * 3600

client: Optional[AsyncIOMotorClient] = None
db = None
coll = None  # finanzas.transactions
sales_coll = None  # finanzas.sales
deposits_coll = None  # finanzas.deposits
settings_coll = None  # finanzas.settings (ajustes de efectivo, etc.)
users_coll = None  # finanzas.users

MOCK_PRICES: dict[str, float] = {
    "BTC": 67500.00,
    "ETH": 3450.00,
    "AAPL": 178.50,
    "GOOGL": 141.25,
    "MSFT": 415.80,
    "TSLA": 245.60,
    "AMZN": 178.90,
    "NVDA": 875.30,
}

TICKER_ALIASES: dict[str, str] = {
    "ADOBE": "ADBE",
    "LINKUSD": "LINK-USD",
}


def normalize_ticker(ticker: str) -> str:
    """Un solo símbolo canónico por activo (evita duplicar LINKUSD y LINK-USD)."""
    t = (ticker or "").strip().upper()
    if t in ("LINKUSD", "LINK-USD", "LINK"):
        return "LINK-USD"
    return t


# Por decimales en compras/ventas la neta puede quedar en ~1e-7; no es una posición real.
_POSITION_NET_EPS = 1e-4


def _normalize_net_quantity(net: float) -> float:
    net = max(0.0, float(net))
    if net < _POSITION_NET_EPS:
        return 0.0
    return net


def ticker_raw_variants(ticker: str) -> list[str]:
    """Valores que pueden existir en Mongo para el mismo activo (filtros)."""
    t = normalize_ticker(ticker)
    if t == "LINK-USD":
        return ["LINK-USD", "LINKUSD", "LINK"]
    return [t]


# ---------------------------------------------------------------------------
# Auth: PBKDF2 password hashing + HS256 JWT, solo stdlib
# ---------------------------------------------------------------------------

PBKDF2_ITERATIONS = 200_000


def hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode(), bytes.fromhex(salt), PBKDF2_ITERATIONS
    )
    return f"pbkdf2_sha256${PBKDF2_ITERATIONS}${salt}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        _, iterations, salt, expected = stored.split("$")
        digest = hashlib.pbkdf2_hmac(
            "sha256", password.encode(), bytes.fromhex(salt), int(iterations)
        )
        return hmac.compare_digest(digest.hex(), expected)
    except (ValueError, TypeError):
        return False


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _b64url_decode(data: str) -> bytes:
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))


def create_token(email: str) -> str:
    header = _b64url(json.dumps({"alg": "HS256", "typ": "JWT"}).encode())
    payload = _b64url(
        json.dumps({"sub": email, "exp": int(time.time()) + TOKEN_TTL_SECONDS}).encode()
    )
    signing_input = f"{header}.{payload}".encode()
    signature = _b64url(hmac.new(JWT_SECRET.encode(), signing_input, hashlib.sha256).digest())
    return f"{header}.{payload}.{signature}"


def decode_token(token: str) -> Optional[str]:
    """Devuelve el email del usuario si el token es válido y no expiró."""
    try:
        header, payload, signature = token.split(".")
        signing_input = f"{header}.{payload}".encode()
        expected = _b64url(hmac.new(JWT_SECRET.encode(), signing_input, hashlib.sha256).digest())
        if not hmac.compare_digest(signature, expected):
            return None
        claims = json.loads(_b64url_decode(payload))
        if claims.get("exp", 0) < time.time():
            return None
        return claims.get("sub")
    except (ValueError, TypeError):
        return None


bearer_scheme = HTTPBearer(auto_error=False)


async def require_user(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(bearer_scheme),
) -> str:
    if credentials is None:
        raise HTTPException(status_code=401, detail="No autenticado")
    email = decode_token(credentials.credentials)
    if email is None:
        raise HTTPException(status_code=401, detail="Sesión inválida o expirada")
    return email


def _yfinance_price_sync(symbol: str) -> float:
    """Yahoo Finance via yfinance (no API key). Runs in thread to avoid blocking."""
    try:
        t = yf.Ticker(symbol)
        info = t.fast_info
        price = getattr(info, "last_price", None) or getattr(info, "lastPrice", None)
        if price is not None:
            return float(price)
        hist = t.history(period="1d")
        if hist is not None and not hist.empty and "Close" in hist.columns:
            return float(hist["Close"].iloc[-1])
        return 0.0
    except Exception:
        return 0.0


async def fetch_finnhub_price(ticker: str) -> float:
    if not FINNHUB_API_KEY:
        return 0.0
    symbol = ticker.upper()
    url = f"https://finnhub.io/api/v1/quote?symbol={symbol}&token={FINNHUB_API_KEY}"
    try:
        async with httpx.AsyncClient(timeout=5.0) as client_http:
            r = await client_http.get(url)
            if r.status_code != 200:
                return 0.0
            data = r.json()
            price = data.get("c")
            return float(price) if price is not None else 0.0
    except Exception:
        return 0.0


async def fetch_usd_to_gtq_rate() -> tuple[float, bool, str]:
    """Fetch USD->GTQ rate from public API; fallback to env/default."""
    url = "https://open.er-api.com/v6/latest/USD"
    try:
        async with httpx.AsyncClient(timeout=5.0) as client_http:
            r = await client_http.get(url)
            if r.status_code == 200:
                data = r.json()
                rates = data.get("rates") or {}
                gtq = rates.get("GTQ")
                if gtq is not None:
                    return float(gtq), True, "open.er-api.com"
    except Exception:
        pass
    return USD_TO_GTQ_FALLBACK, False, "fallback"


async def get_current_price(ticker: str) -> float:
    symbol = normalize_ticker(ticker)
    symbol = TICKER_ALIASES.get(symbol, symbol)
    p = await asyncio.to_thread(_yfinance_price_sync, symbol)
    if p > 0:
        return p
    if FINNHUB_API_KEY:
        p = await fetch_finnhub_price(ticker)
        if p > 0:
            return p
    return MOCK_PRICES.get(symbol, 0.0)


@app.on_event("startup")
async def startup_db_client():
    global client, db, coll, sales_coll, deposits_coll, settings_coll, users_coll
    if MONGO_URI:
        client = AsyncIOMotorClient(MONGO_URI)
        db = client.finanzas
        coll = db.transactions
        sales_coll = db.sales
        deposits_coll = db.deposits
        settings_coll = db.settings
        users_coll = db.users
        await coll.create_index("ticker")
        await sales_coll.create_index("ticker")
        await sales_coll.create_index("date")
        await sales_coll.create_index("created_at")
        await deposits_coll.create_index("date")
        await deposits_coll.create_index("created_at")
        # Usuario inicial: solo si la colección está vacía y hay credenciales en .env
        if ADMIN_EMAIL and ADMIN_PASSWORD:
            existing = await users_coll.count_documents({})
            if existing == 0:
                await users_coll.insert_one(
                    {
                        "_id": str(uuid.uuid4()),
                        "email": ADMIN_EMAIL.lower(),
                        "password_hash": hash_password(ADMIN_PASSWORD),
                        "created_at": datetime.now(timezone.utc).isoformat(),
                    }
                )


@app.on_event("shutdown")
async def shutdown_db_client():
    global client
    if client:
        client.close()


class LoginRequest(BaseModel):
    email: str = Field(..., min_length=3)
    password: str = Field(..., min_length=1)


class InvestmentCreate(BaseModel):
    ticker: str = Field(..., min_length=1, max_length=10)
    amount: float = Field(..., gt=0)
    buy_price: float = Field(..., gt=0)


class InvestmentUpdate(BaseModel):
    ticker: Optional[str] = Field(None, min_length=1, max_length=10)
    amount: Optional[float] = Field(None, gt=0)
    buy_price: Optional[float] = Field(None, gt=0)


class Investment(BaseModel):
    id: str
    ticker: str
    amount: float
    buy_price: float
    timestamp: str  # ISO date/datetime from created_at or date
    remaining_quantity: float = 0.0  # FIFO: cuánto de este lote sigue sin vender


class SaleCreate(BaseModel):
    ticker: str = Field(..., min_length=1, max_length=10)
    quantity: float = Field(..., gt=0)
    sell_price: float = Field(..., gt=0)


class Sale(BaseModel):
    id: str
    ticker: str
    quantity: float
    sell_price: float
    date: str
    cost_basis: float
    realized_pnl: float
    created_at: str


class SaleUpdate(BaseModel):
    quantity: Optional[float] = Field(None, gt=0)
    sell_price: Optional[float] = Field(None, gt=0)


class PortfolioPosition(BaseModel):
    ticker: str
    quantity: float
    cost_basis: float
    current_value: float
    unrealized_pnl: float
    realized_pnl: float
    total_bought_quantity: float = 0.0
    total_sold_quantity: float = 0.0


class Movement(BaseModel):
    id: str
    type: str  # "buy" | "sell"
    ticker: str
    quantity: float
    price: float
    amount: float  # quantity * price
    date: str
    created_at: str
    realized_pnl: Optional[float] = None  # only for sells


class PortfolioSummary(BaseModel):
    total_invested: float
    current_value: float
    total_pnl: float
    pnl_percentage: float
    total_realized_pnl: float
    total_unrealized_pnl: float
    total_deposited: float
    total_spent_on_buys: float
    total_received_from_sales: float
    estimated_cash: float
    estimated_cash_before_adjustment: float
    cash_adjustment_usd: float
    estimated_net_worth: float
    investments: list[Investment]
    positions: list[PortfolioPosition]


class DepositCreate(BaseModel):
    date: str = Field(..., min_length=10, max_length=10)
    amount: float = Field(..., gt=0)
    commission_pct: float = Field(..., ge=0, le=100)


class DepositUpdate(BaseModel):
    date: Optional[str] = Field(None, min_length=10, max_length=10)
    amount: Optional[float] = Field(None, gt=0)
    commission_pct: Optional[float] = Field(None, ge=0, le=100)


class Deposit(BaseModel):
    id: str
    date: str
    amount: float
    commission_pct: float
    commission_amount: float
    total: float
    created_at: str
    updated_at: str


class SimulatorAsset(BaseModel):
    ticker: str
    quantity: float
    avg_buy_price: float
    cost_basis: float
    current_price: float
    current_value: float
    unrealized_pnl: float


class SimulatorHistoryPoint(BaseModel):
    date: str
    close: float


class SimulatorScenarioRequest(BaseModel):
    ticker: str = Field(..., min_length=1, max_length=20)
    change_pct: float = Field(..., ge=-50, le=50)


class SimulatorScenarioResult(BaseModel):
    ticker: str
    change_pct: float
    quantity: float
    cost_basis: float
    current_price: float
    projected_price: float
    current_value: float
    projected_value: float
    pnl: float
    roi_pct: float


def _doc_to_investment(doc: dict, remaining_quantity: Optional[float] = None) -> Investment:
    """Map finanzas.transactions document to API Investment model."""
    doc_id = doc.get("id") or doc.get("_id")
    if doc_id is None:
        doc_id = ""
    qty = float(doc.get("quantity", 0))
    return Investment(
        id=str(doc_id),
        ticker=normalize_ticker(doc.get("ticker", "")),
        amount=qty,
        buy_price=float(doc.get("buy_price", 0)),
        timestamp=doc.get("created_at") or doc.get("date") or "",
        remaining_quantity=qty if remaining_quantity is None else remaining_quantity,
    )


def _doc_to_sale(doc: dict) -> Sale:
    """Map finanzas.sales document to API Sale model."""
    doc_id = doc.get("id") or doc.get("_id", "")
    return Sale(
        id=str(doc_id),
        ticker=normalize_ticker(doc.get("ticker", "")),
        quantity=float(doc.get("quantity", 0)),
        sell_price=float(doc.get("sell_price", 0)),
        date=doc.get("date", ""),
        cost_basis=float(doc.get("cost_basis", 0)),
        realized_pnl=float(doc.get("realized_pnl", 0)),
        created_at=doc.get("created_at", ""),
    )


def _parse_doc_datetime(doc: dict) -> Optional[datetime]:
    """Parse created_at/date from mongo docs as timezone-aware datetime."""
    raw = doc.get("created_at") or doc.get("date")
    if not raw:
        return None
    if isinstance(raw, datetime):
        return raw if raw.tzinfo is not None else raw.replace(tzinfo=timezone.utc)
    try:
        s = str(raw)
        if len(s) == 10 and s.count("-") == 2:
            s = f"{s}T00:00:00+00:00"
        s = s.replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)
    except Exception:
        return None


async def _get_position_and_cost_per_ticker() -> dict[str, tuple[float, float, float]]:
    """Returns per ticker: (total_bought_qty, total_sold_qty, avg_buy_price).

    avg_buy_price usa costo promedio MÓVIL: se procesan compras y ventas en
    orden cronológico y cada venta retira unidades al costo promedio vigente
    en ese momento. Así, si un ticker tuvo un ciclo de compra-venta ya
    cerrado en el pasado, ese costo no se mezcla con las compras nuevas que
    abrieron la posición actual (evita inflar/deflactar el costo de lo que
    tienes hoy con precios de lotes que ya no existen).
    """
    events: dict[str, list[tuple[datetime, str, float, float]]] = {}
    async for doc in coll.find():
        t = normalize_ticker(doc.get("ticker") or "")
        if not t:
            continue
        q = float(doc.get("quantity", 0))
        p = float(doc.get("buy_price", 0))
        dt = _parse_doc_datetime(doc) or datetime.min.replace(tzinfo=timezone.utc)
        events.setdefault(t, []).append((dt, "buy", q, p))
    if sales_coll is not None:
        async for doc in sales_coll.find():
            t = normalize_ticker(doc.get("ticker") or "")
            if not t:
                continue
            q = float(doc.get("quantity", 0))
            p = float(doc.get("sell_price", 0))
            dt = _parse_doc_datetime(doc) or datetime.min.replace(tzinfo=timezone.utc)
            events.setdefault(t, []).append((dt, "sell", q, p))

    result: dict[str, tuple[float, float, float]] = {}
    for t, evs in events.items():
        evs.sort(key=lambda e: e[0])
        qty = 0.0
        cost = 0.0
        total_bought = 0.0
        total_sold = 0.0
        for _dt, kind, q, p in evs:
            if kind == "buy":
                qty += q
                cost += q * p
                total_bought += q
            else:
                total_sold += q
                take = min(q, qty) if qty > 1e-12 else 0.0
                if take > 0:
                    cost -= take * (cost / qty)
                    qty -= take
                if qty < 0:
                    qty = 0.0
                if cost < 0:
                    cost = 0.0
        avg_price = (cost / qty) if qty > 1e-9 else 0.0
        result[t] = (total_bought, total_sold, avg_price)
    return result


async def _get_remaining_quantity_by_doc_id() -> dict[str, float]:
    """FIFO: cuánto de cada documento de compra individual sigue sin vender.

    Solo se usa para saber qué documentos de compra siguen "vivos" en un ticker
    (permite habilitar Editar/Eliminar por lote en el frontend incluso si existen
    lotes históricos ya cerrados del mismo ticker). No afecta el costo promedio
    móvil que usa `_get_position_and_cost_per_ticker` para P/L.
    """
    events: dict[str, list[tuple[datetime, str, str, float]]] = {}
    async for doc in coll.find():
        t = normalize_ticker(doc.get("ticker") or "")
        if not t:
            continue
        doc_id = str(doc.get("id") or doc.get("_id"))
        q = float(doc.get("quantity", 0))
        dt = _parse_doc_datetime(doc) or datetime.min.replace(tzinfo=timezone.utc)
        events.setdefault(t, []).append((dt, "buy", doc_id, q))
    if sales_coll is not None:
        async for doc in sales_coll.find():
            t = normalize_ticker(doc.get("ticker") or "")
            if not t:
                continue
            q = float(doc.get("quantity", 0))
            dt = _parse_doc_datetime(doc) or datetime.min.replace(tzinfo=timezone.utc)
            events.setdefault(t, []).append((dt, "sell", "", q))

    remaining: dict[str, float] = {}
    for t, evs in events.items():
        evs.sort(key=lambda e: e[0])
        lots: list[list] = []  # [doc_id, remaining_qty]
        for _dt, kind, doc_id, q in evs:
            if kind == "buy":
                lots.append([doc_id, q])
            else:
                to_sell = q
                for lot in lots:
                    if to_sell <= 1e-12:
                        break
                    take = min(lot[1], to_sell)
                    lot[1] -= take
                    to_sell -= take
        for doc_id, rem in lots:
            remaining[doc_id] = max(0.0, rem)
    return remaining


async def _get_available_quantity(ticker: str) -> float:
    pos = await _get_position_and_cost_per_ticker()
    t = normalize_ticker(ticker)
    if t not in pos:
        return 0
    bought, sold, _ = pos[t]
    return _normalize_net_quantity(max(0, bought - sold))


async def _max_sale_quantity_allowed(ticker: str, current_sale_qty: float) -> float:
    """Máxima cantidad que puede tener esta fila de venta sin vender más de lo comprado."""
    t = normalize_ticker(ticker)
    pos_data = await _get_position_and_cost_per_ticker()
    if t not in pos_data:
        return 0.0
    bought, total_sold, _ = pos_data[t]
    net = bought - total_sold
    return current_sale_qty + net


async def _cash_adjustment_usd() -> float:
    """Ajuste manual al efectivo estimado (comisiones, diferencias con el broker). Ver `settings` portfolio."""
    if settings_coll is None:
        return 0.0
    doc = await settings_coll.find_one({"$or": [{"_id": "portfolio"}, {"id": "portfolio"}]})
    if not doc:
        return 0.0
    return float(doc.get("cash_adjustment_usd", 0))


async def _get_simulator_assets_data() -> dict[str, tuple[float, float, float, float, float]]:
    """
    Returns per ticker:
    (current_qty, avg_buy_price, cost_basis, current_price, current_value)
    """
    pos_data = await _get_position_and_cost_per_ticker()
    out: dict[str, tuple[float, float, float, float, float]] = {}
    for ticker, (bought, sold, avg_buy) in pos_data.items():
        current_qty = _normalize_net_quantity(max(0, bought - sold))
        if current_qty <= 0:
            continue
        cost_basis = round(current_qty * avg_buy, 6)
        current_price = await get_current_price(ticker)
        if current_price <= 0:
            current_price = avg_buy
        current_value = round(current_qty * current_price, 6)
        out[ticker] = (current_qty, avg_buy, cost_basis, current_price, current_value)
    return out


@app.get("/health")
async def health_check():
    return {"status": "healthy", "database": "connected" if db is not None else "not connected"}


@app.post("/auth/login")
async def login(request: LoginRequest):
    if users_coll is None:
        raise HTTPException(status_code=503, detail="Database not connected")
    user = await users_coll.find_one({"email": request.email.strip().lower()})
    if user is None or not verify_password(request.password, user.get("password_hash", "")):
        raise HTTPException(status_code=401, detail="Correo o contraseña incorrectos")
    return {
        "access_token": create_token(user["email"]),
        "token_type": "bearer",
        "email": user["email"],
    }


@app.get("/auth/me")
async def me(email: str = Depends(require_user)):
    return {"email": email}


@app.post("/investments", response_model=Investment)
async def create_investment(investment: InvestmentCreate, _: str = Depends(require_user)):
    if coll is None:
        raise HTTPException(status_code=503, detail="Database not connected")
    now = datetime.now(timezone.utc)
    now_iso = now.isoformat()
    date_str = now.strftime("%Y-%m-%d")
    doc_id = str(uuid.uuid4())
    doc = {
        "_id": doc_id,
        "id": doc_id,
        "ticker": normalize_ticker(investment.ticker),
        "quantity": investment.amount,
        "buy_price": investment.buy_price,
        "date": date_str,
        "currency": "USD",
        "asset_type": "Stock",
        "created_at": now_iso,
        "updated_at": now_iso,
    }
    await coll.insert_one(doc)
    return _doc_to_investment(doc)


@app.get("/investments", response_model=list[Investment])
async def get_investments(ticker: Optional[str] = Query(None), _: str = Depends(require_user)):
    if coll is None:
        raise HTTPException(status_code=503, detail="Database not connected")
    q = {}
    if ticker and ticker.strip():
        q["ticker"] = {"$in": ticker_raw_variants(ticker.strip())}
    remaining_map = await _get_remaining_quantity_by_doc_id()
    investments = []
    cursor = coll.find(q).sort("created_at", -1)
    async for doc in cursor:
        doc_id = str(doc.get("id") or doc.get("_id"))
        investments.append(_doc_to_investment(doc, remaining_map.get(doc_id)))
    return investments


@app.get("/investments/{investment_id}", response_model=Investment)
async def get_investment(investment_id: str, _: str = Depends(require_user)):
    if coll is None:
        raise HTTPException(status_code=503, detail="Database not connected")
    doc = await coll.find_one({"$or": [{"_id": investment_id}, {"id": investment_id}]})
    if doc is None:
        raise HTTPException(status_code=404, detail="Investment not found")
    return _doc_to_investment(doc)


@app.put("/investments/{investment_id}", response_model=Investment)
async def update_investment(
    investment_id: str, update: InvestmentUpdate, _: str = Depends(require_user)
):
    if coll is None:
        raise HTTPException(status_code=503, detail="Database not connected")
    raw = update.model_dump(exclude_none=True)
    set_data = {}
    if "ticker" in raw:
        set_data["ticker"] = normalize_ticker(raw["ticker"])
    if "buy_price" in raw:
        set_data["buy_price"] = raw["buy_price"]
    if "amount" in raw:
        set_data["quantity"] = raw["amount"]
    if not set_data:
        raise HTTPException(status_code=400, detail="No fields to update")
    set_data["updated_at"] = datetime.now(timezone.utc).isoformat()
    result = await coll.update_one(
        {"$or": [{"_id": investment_id}, {"id": investment_id}]},
        {"$set": set_data},
    )
    if result.matched_count == 0:
        raise HTTPException(status_code=404, detail="Investment not found")
    doc = await coll.find_one({"$or": [{"_id": investment_id}, {"id": investment_id}]})
    return _doc_to_investment(doc)


@app.delete("/investments/{investment_id}")
async def delete_investment(investment_id: str, _: str = Depends(require_user)):
    if coll is None:
        raise HTTPException(status_code=503, detail="Database not connected")
    result = await coll.delete_one(
        {"$or": [{"_id": investment_id}, {"id": investment_id}]}
    )
    if result.deleted_count == 0:
        raise HTTPException(status_code=404, detail="Investment not found")
    return {"message": "Investment deleted successfully"}


# --- Sales ---


@app.post("/sales", response_model=Sale)
async def create_sale(sale: SaleCreate, _: str = Depends(require_user)):
    if coll is None or sales_coll is None:
        raise HTTPException(status_code=503, detail="Database not connected")
    ticker = normalize_ticker(sale.ticker)
    available = await _get_available_quantity(ticker)
    if available < sale.quantity:
        raise HTTPException(
            status_code=400,
            detail=f"No hay suficiente cantidad disponible para {ticker}. Disponible: {available:.4f}",
        )
    pos = await _get_position_and_cost_per_ticker()
    if ticker not in pos:
        raise HTTPException(status_code=400, detail=f"Ticker {ticker} no encontrado en posiciones")
    _, _, avg_buy = pos[ticker]
    cost_basis = avg_buy
    realized_pnl = sale.quantity * (sale.sell_price - cost_basis)
    now = datetime.now(timezone.utc)
    now_iso = now.isoformat()
    date_str = now.strftime("%Y-%m-%d")
    doc_id = str(uuid.uuid4())
    doc = {
        "_id": doc_id,
        "id": doc_id,
        "ticker": ticker,
        "quantity": sale.quantity,
        "sell_price": sale.sell_price,
        "date": date_str,
        "cost_basis": round(cost_basis, 4),
        "realized_pnl": round(realized_pnl, 2),
        "created_at": now_iso,
    }
    await sales_coll.insert_one(doc)
    return _doc_to_sale(doc)


@app.get("/sales", response_model=list[Sale])
async def get_sales(
    ticker: Optional[str] = Query(None),
    from_date: Optional[str] = Query(None, alias="from_date"),
    to_date: Optional[str] = Query(None, alias="to_date"),
    limit: int = Query(100, ge=1, le=500),
    _: str = Depends(require_user),
):
    if sales_coll is None:
        raise HTTPException(status_code=503, detail="Database not connected")
    query = {}
    if ticker:
        query["ticker"] = {"$in": ticker_raw_variants(ticker)}
    if from_date or to_date:
        date_q = {}
        if from_date:
            date_q["$gte"] = from_date
        if to_date:
            date_q["$lte"] = to_date
        query["date"] = date_q
    sales_list = []
    async for doc in sales_coll.find(query).sort("created_at", -1).limit(limit):
        sales_list.append(_doc_to_sale(doc))
    return sales_list


@app.get("/sales/{sale_id}", response_model=Sale)
async def get_sale(sale_id: str, _: str = Depends(require_user)):
    if sales_coll is None:
        raise HTTPException(status_code=503, detail="Database not connected")
    doc = await sales_coll.find_one({"$or": [{"_id": sale_id}, {"id": sale_id}]})
    if doc is None:
        raise HTTPException(status_code=404, detail="Sale not found")
    return _doc_to_sale(doc)


@app.patch("/sales/{sale_id}", response_model=Sale)
async def update_sale(sale_id: str, update: SaleUpdate, _: str = Depends(require_user)):
    if sales_coll is None or coll is None:
        raise HTTPException(status_code=503, detail="Database not connected")
    raw = update.model_dump(exclude_none=True)
    if not raw:
        raise HTTPException(status_code=400, detail="No fields to update")
    doc = await sales_coll.find_one({"$or": [{"_id": sale_id}, {"id": sale_id}]})
    if doc is None:
        raise HTTPException(status_code=404, detail="Sale not found")
    ticker = normalize_ticker(doc.get("ticker") or "")
    old_qty = float(doc.get("quantity", 0))
    old_price = float(doc.get("sell_price", 0))
    new_qty = float(raw["quantity"]) if "quantity" in raw else old_qty
    new_price = float(raw["sell_price"]) if "sell_price" in raw else old_price
    max_q = await _max_sale_quantity_allowed(ticker, old_qty)
    if new_qty > max_q + 1e-9:
        raise HTTPException(
            status_code=400,
            detail=f"Cantidad máxima permitida para esta venta: {max_q:.8f}",
        )
    pos_data = await _get_position_and_cost_per_ticker()
    if ticker not in pos_data:
        raise HTTPException(status_code=400, detail="Ticker sin compras registradas")
    _, _, avg_buy = pos_data[ticker]
    realized_pnl = new_qty * (new_price - avg_buy)
    set_data = {
        "quantity": new_qty,
        "sell_price": new_price,
        "cost_basis": round(avg_buy, 4),
        "realized_pnl": round(realized_pnl, 2),
        "realized_pnl_from_broker": False,
    }
    await sales_coll.update_one(
        {"$or": [{"_id": sale_id}, {"id": sale_id}]},
        {"$set": set_data},
    )
    updated = await sales_coll.find_one({"$or": [{"_id": sale_id}, {"id": sale_id}]})
    return _doc_to_sale(updated)


@app.delete("/sales/{sale_id}")
async def delete_sale(sale_id: str, _: str = Depends(require_user)):
    if sales_coll is None:
        raise HTTPException(status_code=503, detail="Database not connected")
    result = await sales_coll.delete_one({"$or": [{"_id": sale_id}, {"id": sale_id}]})
    if result.deleted_count == 0:
        raise HTTPException(status_code=404, detail="Sale not found")
    return {"message": "Sale deleted"}


@app.get("/movements", response_model=list[Movement])
async def get_movements(
    ticker: Optional[str] = Query(None),
    from_date: Optional[str] = Query(None, alias="from_date"),
    to_date: Optional[str] = Query(None, alias="to_date"),
    type_filter: Optional[str] = Query(None, alias="type"),  # "buy" | "sell" | omit = all
    limit: int = Query(200, ge=1, le=1000),
    _: str = Depends(require_user),
):
    """List all movements (buys + sales) with optional filters, sorted by date descending."""
    if coll is None or sales_coll is None:
        raise HTTPException(status_code=503, detail="Database not connected")
    movements: list[Movement] = []
    if type_filter is None or type_filter == "buy":
        q = {}
        if ticker:
            q["ticker"] = {"$in": ticker_raw_variants(ticker)}
        if from_date or to_date:
            date_q = {}
            if from_date:
                date_q["$gte"] = from_date
            if to_date:
                date_q["$lte"] = to_date
            q["date"] = date_q
        async for doc in coll.find(q):
            tid = doc.get("id") or doc.get("_id", "")
            qty = float(doc.get("quantity", 0))
            price = float(doc.get("buy_price", 0))
            movements.append(
                Movement(
                    id=str(tid),
                    type="buy",
                    ticker=normalize_ticker(doc.get("ticker") or ""),
                    quantity=qty,
                    price=price,
                    amount=round(qty * price, 2),
                    date=doc.get("date", ""),
                    created_at=doc.get("created_at", ""),
                    realized_pnl=None,
                )
            )
    if type_filter is None or type_filter == "sell":
        q = {}
        if ticker:
            q["ticker"] = {"$in": ticker_raw_variants(ticker)}
        if from_date or to_date:
            date_q = {}
            if from_date:
                date_q["$gte"] = from_date
            if to_date:
                date_q["$lte"] = to_date
            q["date"] = date_q
        async for doc in sales_coll.find(q):
            sid = doc.get("id") or doc.get("_id", "")
            qty = float(doc.get("quantity", 0))
            price = float(doc.get("sell_price", 0))
            movements.append(
                Movement(
                    id=str(sid),
                    type="sell",
                    ticker=normalize_ticker(doc.get("ticker") or ""),
                    quantity=qty,
                    price=price,
                    amount=round(qty * price, 2),
                    date=doc.get("date", ""),
                    created_at=doc.get("created_at", ""),
                    realized_pnl=round(float(doc.get("realized_pnl", 0)), 2),
                )
            )
    movements.sort(key=lambda m: (m.date or "", m.created_at or ""), reverse=True)
    return movements[:limit]


@app.get("/portfolio/summary", response_model=PortfolioSummary)
async def get_portfolio_summary(_: str = Depends(require_user)):
    if coll is None:
        raise HTTPException(status_code=503, detail="Database not connected")
    investments = []
    remaining_map = await _get_remaining_quantity_by_doc_id()
    async for doc in coll.find().sort("created_at", -1):
        doc_id = str(doc.get("id") or doc.get("_id"))
        investments.append(_doc_to_investment(doc, remaining_map.get(doc_id)))

    ticker_first_buy_dt: dict[str, datetime] = {}
    async for doc in coll.find():
        t = normalize_ticker(doc.get("ticker") or "")
        if not t:
            continue
        dt = _parse_doc_datetime(doc)
        if dt is not None:
            prev = ticker_first_buy_dt.get(t)
            if prev is None or dt < prev:
                ticker_first_buy_dt[t] = dt

    pos_data = await _get_position_and_cost_per_ticker()
    realized_by_ticker: dict[str, float] = {}
    total_received_from_sales = 0.0
    if sales_coll is not None:
        async for doc in sales_coll.find():
            t = normalize_ticker(doc.get("ticker") or "")
            if t:
                realized_by_ticker[t] = realized_by_ticker.get(t, 0) + float(doc.get("realized_pnl", 0))
                first_buy = ticker_first_buy_dt.get(t)
                if first_buy is not None:
                    sale_dt = _parse_doc_datetime(doc)
                    if sale_dt is None or sale_dt >= first_buy:
                        total_received_from_sales += float(doc.get("quantity", 0)) * float(
                            doc.get("sell_price", 0)
                        )

    total_invested = 0.0
    current_value = 0.0
    positions: list[PortfolioPosition] = []
    for ticker, (bought, sold, avg_buy) in pos_data.items():
        current_qty = _normalize_net_quantity(max(0, bought - sold))
        if current_qty <= 0:
            # Siempre incluir el ticker si hubo compras: evita "fantasmas" en la UI cuando
            # la posición neta es 0 pero hay compras registradas o ventas sin P/L calculado.
            positions.append(
                PortfolioPosition(
                    ticker=ticker,
                    quantity=0,
                    cost_basis=0,
                    current_value=0,
                    unrealized_pnl=0,
                    realized_pnl=round(realized_by_ticker.get(ticker, 0), 2),
                    total_bought_quantity=round(bought, 8),
                    total_sold_quantity=round(sold, 8),
                )
            )
            continue
        cost_basis = round(current_qty * avg_buy, 2)
        total_invested += cost_basis
        current_price = await get_current_price(ticker)
        if current_price <= 0:
            current_price = avg_buy
        curr_val = round(current_qty * current_price, 2)
        current_value += curr_val
        unrealized = round(curr_val - cost_basis, 2)
        realized = round(realized_by_ticker.get(ticker, 0), 2)
        positions.append(
            PortfolioPosition(
                ticker=ticker,
                quantity=current_qty,
                cost_basis=cost_basis,
                current_value=curr_val,
                unrealized_pnl=unrealized,
                realized_pnl=realized,
                total_bought_quantity=round(bought, 8),
                total_sold_quantity=round(sold, 8),
            )
        )
    total_realized = sum(realized_by_ticker.values())
    total_unrealized = round(current_value - total_invested, 2)
    total_realized = round(total_realized, 2)
    total_pnl = total_realized + total_unrealized
    pnl_percentage = (total_pnl / total_invested * 100) if total_invested > 0 else 0
    total_invested = round(total_invested, 2)
    current_value = round(current_value, 2)

    total_deposited = 0.0
    if deposits_coll is not None:
        async for doc in deposits_coll.find():
            # Capital depositado: la comisión se registra aparte y no suma al capital invertible.
            total_deposited += float(doc.get("amount", 0))

    total_spent_on_buys = 0.0
    async for doc in coll.find():
        total_spent_on_buys += float(doc.get("quantity", 0)) * float(doc.get("buy_price", 0))
    total_spent_on_buys = round(total_spent_on_buys, 2)

    total_received_from_sales = round(total_received_from_sales, 2)

    total_deposited = round(total_deposited, 2)
    estimated_cash_before_adjustment = round(
        total_deposited - total_spent_on_buys + total_received_from_sales, 2
    )
    cash_adjustment_usd = round(await _cash_adjustment_usd(), 2)
    estimated_cash = round(estimated_cash_before_adjustment + cash_adjustment_usd, 2)
    estimated_net_worth = round(estimated_cash + current_value, 2)

    return PortfolioSummary(
        total_invested=total_invested,
        current_value=current_value,
        total_pnl=round(total_pnl, 2),
        pnl_percentage=round(pnl_percentage, 2),
        total_realized_pnl=total_realized,
        total_unrealized_pnl=total_unrealized,
        total_deposited=total_deposited,
        total_spent_on_buys=total_spent_on_buys,
        total_received_from_sales=total_received_from_sales,
        estimated_cash=estimated_cash,
        estimated_cash_before_adjustment=estimated_cash_before_adjustment,
        cash_adjustment_usd=cash_adjustment_usd,
        estimated_net_worth=estimated_net_worth,
        investments=investments,
        positions=sorted(positions, key=lambda p: (p.quantity == 0, -p.current_value)),
    )


# Rutas más específicas primero: /prices/status y /prices antes que /prices/{ticker}
# (si no, GET /prices/status coincidiría con ticker="status" y devolvería 404)


@app.get("/prices/status")
async def get_prices_status():
    """Check if we can get live market prices (yfinance or Finnhub)."""
    try:
        p = await asyncio.to_thread(_yfinance_price_sync, "AAPL")
        if p > 0:
            return {"live": True}
        if FINNHUB_API_KEY:
            p = await fetch_finnhub_price("AAPL")
            if p > 0:
                return {"live": True}
    except Exception:
        pass
    return {"live": False}


@app.get("/prices")
async def get_all_prices(_: str = Depends(require_user)):
    if coll is None:
        return MOCK_PRICES
    tickers: set[str] = set()
    async for doc in coll.find():
        tickers.add(normalize_ticker(doc.get("ticker") or ""))
    result: dict[str, float] = {}
    for t in tickers:
        if not t:
            continue
        p = await get_current_price(t)
        if p > 0:
            result[t] = p
    return result if result else MOCK_PRICES


@app.get("/prices/{ticker}")
async def get_price(ticker: str, _: str = Depends(require_user)):
    sym = normalize_ticker(ticker)
    price = await get_current_price(sym)
    if price == 0:
        raise HTTPException(status_code=404, detail=f"Price not found for {ticker}")
    return {"ticker": sym, "price": price}


@app.get("/fx/usd-gtq")
async def get_usd_gtq_rate():
    rate, live, source = await fetch_usd_to_gtq_rate()
    return {
        "pair": "USD/GTQ",
        "rate": round(rate, 6),
        "live": live,
        "source": source,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }


@app.get("/simulator/assets", response_model=list[SimulatorAsset])
async def list_simulator_assets(_: str = Depends(require_user)):
    if coll is None:
        raise HTTPException(status_code=503, detail="Database not connected")
    assets = await _get_simulator_assets_data()
    result: list[SimulatorAsset] = []
    for ticker, (qty, avg_buy, cost_basis, current_price, current_value) in assets.items():
        result.append(
            SimulatorAsset(
                ticker=ticker,
                quantity=round(qty, 8),
                avg_buy_price=round(avg_buy, 8),
                cost_basis=round(cost_basis, 6),
                current_price=round(current_price, 6),
                current_value=round(current_value, 6),
                unrealized_pnl=round(current_value - cost_basis, 6),
            )
        )
    return sorted(result, key=lambda a: a.current_value, reverse=True)


@app.get("/simulator/history/{ticker}", response_model=list[SimulatorHistoryPoint])
async def get_simulator_history(
    ticker: str,
    period: str = Query("3mo"),
    interval: str = Query("1d"),
    limit: int = Query(120, ge=10, le=500),
    _: str = Depends(require_user),
):
    symbol = normalize_ticker(ticker)
    symbol = TICKER_ALIASES.get(symbol, symbol)
    try:
        df = await asyncio.to_thread(lambda: yf.Ticker(symbol).history(period=period, interval=interval))
        points: list[SimulatorHistoryPoint] = []
        if df is not None and not df.empty and "Close" in df.columns:
            for idx, row in df.tail(limit).iterrows():
                date_str = idx.strftime("%Y-%m-%d") if hasattr(idx, "strftime") else str(idx)[:10]
                close = float(row.get("Close", 0))
                if close > 0:
                    points.append(SimulatorHistoryPoint(date=date_str, close=round(close, 6)))
        if points:
            return points
    except Exception:
        pass
    # fallback simple synthetic history around current price
    p = await get_current_price(symbol)
    if p <= 0:
        p = 1.0
    out = []
    base = p * 0.97
    for i in range(30):
        d = datetime.now(timezone.utc).date()
        day = (d.fromordinal(d.toordinal() - (29 - i))).isoformat()
        val = round(base + (p - base) * (i / 29), 6)
        out.append(SimulatorHistoryPoint(date=day, close=val))
    return out


@app.post("/simulator/scenario", response_model=SimulatorScenarioResult)
async def simulate_scenario(body: SimulatorScenarioRequest, _: str = Depends(require_user)):
    if coll is None:
        raise HTTPException(status_code=503, detail="Database not connected")
    t = normalize_ticker(body.ticker)
    assets = await _get_simulator_assets_data()
    if t not in assets:
        raise HTTPException(status_code=404, detail=f"No active position for {t}")
    qty, avg_buy, cost_basis, current_price, current_value = assets[t]
    projected_price = current_price * (1 + body.change_pct / 100.0)
    projected_value = qty * projected_price
    pnl = projected_value - cost_basis
    roi_pct = (pnl / cost_basis * 100) if cost_basis > 0 else 0.0
    return SimulatorScenarioResult(
        ticker=t,
        change_pct=round(body.change_pct, 4),
        quantity=round(qty, 8),
        cost_basis=round(cost_basis, 6),
        current_price=round(current_price, 6),
        projected_price=round(projected_price, 6),
        current_value=round(current_value, 6),
        projected_value=round(projected_value, 6),
        pnl=round(pnl, 6),
        roi_pct=round(roi_pct, 4),
    )


def _deposit_amounts(amount: float, commission_pct: float) -> tuple[float, float]:
    commission_amount = round(amount * (commission_pct / 100.0), 2)
    total = round(amount + commission_amount, 2)
    return commission_amount, total


def _doc_to_deposit(doc: dict) -> Deposit:
    doc_id = doc.get("id") or doc.get("_id", "")
    return Deposit(
        id=str(doc_id),
        date=doc.get("date", ""),
        amount=float(doc.get("amount", 0)),
        commission_pct=float(doc.get("commission_pct", 0)),
        commission_amount=float(doc.get("commission_amount", 0)),
        total=float(doc.get("total", 0)),
        created_at=doc.get("created_at", ""),
        updated_at=doc.get("updated_at", ""),
    )


@app.post("/deposits", response_model=Deposit)
async def create_deposit(body: DepositCreate, _: str = Depends(require_user)):
    if deposits_coll is None:
        raise HTTPException(status_code=503, detail="Database not connected")
    commission_amount, total = _deposit_amounts(body.amount, body.commission_pct)
    now = datetime.now(timezone.utc)
    now_iso = now.isoformat()
    doc_id = str(uuid.uuid4())
    doc = {
        "_id": doc_id,
        "id": doc_id,
        "date": body.date,
        "amount": body.amount,
        "commission_pct": body.commission_pct,
        "commission_amount": commission_amount,
        "total": total,
        "created_at": now_iso,
        "updated_at": now_iso,
    }
    await deposits_coll.insert_one(doc)
    return _doc_to_deposit(doc)


@app.get("/deposits", response_model=list[Deposit])
async def list_deposits(_: str = Depends(require_user)):
    if deposits_coll is None:
        raise HTTPException(status_code=503, detail="Database not connected")
    out: list[Deposit] = []
    async for doc in deposits_coll.find().sort("date", -1):
        out.append(_doc_to_deposit(doc))
    return out


@app.get("/deposits/{deposit_id}", response_model=Deposit)
async def get_deposit(deposit_id: str, _: str = Depends(require_user)):
    if deposits_coll is None:
        raise HTTPException(status_code=503, detail="Database not connected")
    doc = await deposits_coll.find_one({"$or": [{"_id": deposit_id}, {"id": deposit_id}]})
    if doc is None:
        raise HTTPException(status_code=404, detail="Deposit not found")
    return _doc_to_deposit(doc)


@app.put("/deposits/{deposit_id}", response_model=Deposit)
async def update_deposit(deposit_id: str, update: DepositUpdate, _: str = Depends(require_user)):
    if deposits_coll is None:
        raise HTTPException(status_code=503, detail="Database not connected")
    doc = await deposits_coll.find_one({"$or": [{"_id": deposit_id}, {"id": deposit_id}]})
    if doc is None:
        raise HTTPException(status_code=404, detail="Deposit not found")
    raw = update.model_dump(exclude_none=True)
    amount = float(doc.get("amount", 0))
    commission_pct = float(doc.get("commission_pct", 0))
    date_str = doc.get("date", "")
    if "amount" in raw:
        amount = raw["amount"]
    if "commission_pct" in raw:
        commission_pct = raw["commission_pct"]
    if "date" in raw:
        date_str = raw["date"]
    commission_amount, total = _deposit_amounts(amount, commission_pct)
    set_data = {
        "date": date_str,
        "amount": amount,
        "commission_pct": commission_pct,
        "commission_amount": commission_amount,
        "total": total,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    await deposits_coll.update_one(
        {"$or": [{"_id": deposit_id}, {"id": deposit_id}]},
        {"$set": set_data},
    )
    doc = await deposits_coll.find_one({"$or": [{"_id": deposit_id}, {"id": deposit_id}]})
    return _doc_to_deposit(doc)


@app.delete("/deposits/{deposit_id}")
async def delete_deposit(deposit_id: str, _: str = Depends(require_user)):
    if deposits_coll is None:
        raise HTTPException(status_code=503, detail="Database not connected")
    result = await deposits_coll.delete_one(
        {"$or": [{"_id": deposit_id}, {"id": deposit_id}]}
    )
    if result.deleted_count == 0:
        raise HTTPException(status_code=404, detail="Deposit not found")
    return {"message": "Deposit deleted successfully"}
