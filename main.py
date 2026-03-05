import fastapi
import fastapi.middleware.cors
from fastapi import Depends
from pydantic import BaseModel, Field
from datetime import datetime, date
from typing import Optional
import uuid
import os
import time
import threading
from motor.motor_asyncio import AsyncIOMotorClient
from contextlib import asynccontextmanager
from dotenv import load_dotenv
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded

# Importar módulo de seguridad
from .security import verify_api_key, get_cors_origins

# Cargar variables de entorno
load_dotenv()

# ---------------------------------------------------------------------------
# Rate Limiting Setup
# ---------------------------------------------------------------------------

limiter = Limiter(key_func=get_remote_address)

# ---------------------------------------------------------------------------
# MongoDB Setup
# ---------------------------------------------------------------------------

mongodb_client: Optional[AsyncIOMotorClient] = None
db = None

@asynccontextmanager
async def lifespan(app: fastapi.FastAPI):
    # Startup
    global mongodb_client, db
    mongodb_uri = os.getenv("MONGODB_URI", "mongodb://localhost:27017")
    mongodb_client = AsyncIOMotorClient(mongodb_uri)
    db = mongodb_client.finanzas
    print(f"✅ Connected to MongoDB at {mongodb_uri}")

    yield  # <-- La app corre aquí

    # Shutdown
    mongodb_client.close()
    print("🔌 MongoDB connection closed")

# ---------------------------------------------------------------------------
# FastAPI App Setup
# ---------------------------------------------------------------------------

app = fastapi.FastAPI(
    title="Investment Portfolio API",
    description="API para gestión de portafolio de inversiones con precios en tiempo real",
    version="1.0.0",
    lifespan=lifespan
)

# Configurar rate limiting
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

# Configurar CORS con orígenes desde variables de entorno
allowed_origins = get_cors_origins()
environment = os.getenv("ENVIRONMENT", "development")

print(f"🌍 CORS Origins: {allowed_origins}")
print(f"🔧 Environment: {environment}")

app.add_middleware(
    fastapi.middleware.cors.CORSMiddleware,
    allow_origins=allowed_origins,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE"],
    allow_headers=["*"],
    expose_headers=["X-API-Key"],
)

# ---------------------------------------------------------------------------
# Data Models
# ---------------------------------------------------------------------------

class TransactionCreate(BaseModel):
    ticker: str
    quantity: float
    buy_price: float
    date: str  # ISO date string
    currency: str = "USD"
    asset_type: str = "Stock"  # Stock / Crypto

class TransactionUpdate(BaseModel):
    ticker: Optional[str] = None
    quantity: Optional[float] = None
    buy_price: Optional[float] = None
    date: Optional[str] = None
    currency: Optional[str] = None
    asset_type: Optional[str] = None

class Transaction(BaseModel):
    id: str
    ticker: str
    quantity: float
    buy_price: float
    date: str
    currency: str
    asset_type: str
    created_at: Optional[str] = None
    updated_at: Optional[str] = None

class PortfolioPosition(BaseModel):
    ticker: str
    asset_type: str
    total_quantity: float
    avg_price: float
    total_invested: float
    current_price: float
    current_value: float
    pnl: float
    pnl_pct: float
    break_even: Optional[float] = None

class PortfolioSummary(BaseModel):
    total_invested: float
    net_worth: float
    total_pnl: float
    total_pnl_pct: float
    best_asset: Optional[str] = None
    best_asset_pnl_pct: Optional[float] = None
    worst_asset: Optional[str] = None
    worst_asset_pnl_pct: Optional[float] = None
    positions: list[PortfolioPosition]
    allocation: list[dict]

# ---------------------------------------------------------------------------
# Price cache (local dict cache with 5 min TTL)
# ---------------------------------------------------------------------------

_price_cache: dict[str, tuple[float, float]] = {}  # ticker -> (price, timestamp)
CACHE_TTL = 300  # 5 minutes

def _get_cached_price(ticker: str) -> Optional[float]:
    if ticker in _price_cache:
        price, ts = _price_cache[ticker]
        if time.time() - ts < CACHE_TTL:
            return price
    return None

def _set_cached_price(ticker: str, price: float):
    _price_cache[ticker] = (price, time.time())


def _fetch_price(ticker: str) -> float:
    """Fetch current price via yfinance with caching."""
    cached = _get_cached_price(ticker)
    if cached is not None:
        print(f"💰 Cached price for {ticker}: ${cached:.2f}")
        return cached

    print(f"🔍 Fetching live price for {ticker}...")
    try:
        import yfinance as yf
        t = yf.Ticker(ticker)
        try:
            price = t.fast_info.last_price
            if price and price > 0:
                _set_cached_price(ticker, float(price))
                print(f"✅ Got price for {ticker}: ${float(price):.2f} (fast_info)")
                return float(price)
        except Exception as e:
            print(f"⚠️  fast_info failed for {ticker}: {e}")
        try:
            hist = t.history(period="5d")
            if hist is not None and not hist.empty:
                price = float(hist["Close"].iloc[-1])
                if price > 0:
                    _set_cached_price(ticker, price)
                    print(f"✅ Got price for {ticker}: ${price:.2f} (history)")
                    return price
        except Exception as e:
            print(f"⚠️  history failed for {ticker}: {e}")
    except ImportError:
        print("❌ yfinance not installed, returning 0.0")
    except Exception as e:
        print(f"❌ Error fetching price for {ticker}: {e}")

    print(f"⚠️  Could not fetch price for {ticker}, returning 0.0")
    return 0.0


# ---------------------------------------------------------------------------
# API Endpoints
# ---------------------------------------------------------------------------

@app.get("/api/health")
@limiter.limit("10/minute")
async def health(request: fastapi.Request):
    """Health check endpoint - No requiere autenticación"""
    return {"status": "ok", "environment": os.getenv("ENVIRONMENT", "development")}


@app.post("/api/transactions", response_model=Transaction)
@limiter.limit("30/minute")
async def create_transaction(
    request: fastapi.Request,
    txn: TransactionCreate,
    api_key: str = Depends(verify_api_key)
):
    txn_id = str(uuid.uuid4())
    now = datetime.utcnow().isoformat()
    record = {
        "_id": txn_id,
        "id": txn_id,
        "ticker": txn.ticker.upper(),
        "quantity": txn.quantity,
        "buy_price": txn.buy_price,
        "date": txn.date,
        "currency": txn.currency,
        "asset_type": txn.asset_type,
        "created_at": now,
        "updated_at": now,
    }
    await db.transactions.insert_one(record)
    return Transaction(**record)


@app.get("/api/transactions", response_model=list[Transaction])
@limiter.limit("60/minute")
async def list_transactions(
    request: fastapi.Request,
    api_key: str = Depends(verify_api_key)
):
    cursor = db.transactions.find({})
    transactions = []
    async for doc in cursor:
        doc.pop("_id", None)
        transactions.append(Transaction(**doc))
    return transactions


@app.get("/api/transactions/{txn_id}", response_model=Transaction)
@limiter.limit("60/minute")
async def get_transaction(
    request: fastapi.Request,
    txn_id: str,
    api_key: str = Depends(verify_api_key)
):
    doc = await db.transactions.find_one({"_id": txn_id})
    if not doc:
        raise fastapi.HTTPException(status_code=404, detail="Transaction not found")
    doc.pop("_id", None)
    return Transaction(**doc)


@app.put("/api/transactions/{txn_id}", response_model=Transaction)
@limiter.limit("30/minute")
async def update_transaction(
    request: fastapi.Request,
    txn_id: str,
    update: TransactionUpdate,
    api_key: str = Depends(verify_api_key)
):
    existing = await db.transactions.find_one({"_id": txn_id})
    if not existing:
        raise fastapi.HTTPException(status_code=404, detail="Transaction not found")
    
    update_data = update.model_dump(exclude_unset=True)
    if "ticker" in update_data:
        update_data["ticker"] = update_data["ticker"].upper()
    update_data["updated_at"] = datetime.utcnow().isoformat()
    
    await db.transactions.update_one(
        {"_id": txn_id},
        {"$set": update_data}
    )
    
    updated_doc = await db.transactions.find_one({"_id": txn_id})
    updated_doc.pop("_id", None)
    return Transaction(**updated_doc)


@app.delete("/api/transactions/{txn_id}")
@limiter.limit("30/minute")
async def delete_transaction(
    request: fastapi.Request,
    txn_id: str,
    api_key: str = Depends(verify_api_key)
):
    result = await db.transactions.delete_one({"_id": txn_id})
    if result.deleted_count == 0:
        raise fastapi.HTTPException(status_code=404, detail="Transaction not found")
    return {"status": "deleted", "id": txn_id}


@app.get("/api/portfolio", response_model=PortfolioSummary)
@limiter.limit("60/minute")
async def get_portfolio(
    request: fastapi.Request,
    api_key: str = Depends(verify_api_key)
):
    cursor = db.transactions.find({})
    transactions = []
    async for doc in cursor:
        transactions.append(doc)
    
    if not transactions:
        return PortfolioSummary(
            total_invested=0,
            net_worth=0,
            total_pnl=0,
            total_pnl_pct=0,
            positions=[],
            allocation=[],
        )

    # Aggregate by ticker
    agg: dict[str, dict] = {}
    for t in transactions:
        ticker = t["ticker"]
        if ticker not in agg:
            agg[ticker] = {
                "ticker": ticker,
                "asset_type": t["asset_type"],
                "total_quantity": 0.0,
                "total_cost": 0.0,
            }
        agg[ticker]["total_quantity"] += t["quantity"]
        agg[ticker]["total_cost"] += t["quantity"] * t["buy_price"]

    # Fetch current prices
    positions: list[PortfolioPosition] = []
    for ticker, data in agg.items():
        current_price = _fetch_price(ticker)
        avg_price = data["total_cost"] / data["total_quantity"] if data["total_quantity"] else 0
        current_value = data["total_quantity"] * current_price
        pnl = current_value - data["total_cost"]
        pnl_pct = (pnl / data["total_cost"] * 100) if data["total_cost"] else 0
        break_even = avg_price if pnl < 0 else None

        positions.append(
            PortfolioPosition(
                ticker=data["ticker"],
                asset_type=data["asset_type"],
                total_quantity=round(data["total_quantity"], 6),
                avg_price=round(avg_price, 4),
                total_invested=round(data["total_cost"], 2),
                current_price=round(current_price, 4),
                current_value=round(current_value, 2),
                pnl=round(pnl, 2),
                pnl_pct=round(pnl_pct, 2),
                break_even=round(break_even, 4) if break_even else None,
            )
        )

    total_invested = sum(p.total_invested for p in positions)
    net_worth = sum(p.current_value for p in positions)
    total_pnl = net_worth - total_invested
    total_pnl_pct = (total_pnl / total_invested * 100) if total_invested else 0

    best = max(positions, key=lambda p: p.pnl_pct) if positions else None
    worst = min(positions, key=lambda p: p.pnl_pct) if positions else None

    allocation = []
    for p in positions:
        pct = (p.current_value / net_worth * 100) if net_worth else 0
        allocation.append({
            "ticker": p.ticker,
            "value": round(p.current_value, 2),
            "percentage": round(pct, 2),
        })

    return PortfolioSummary(
        total_invested=round(total_invested, 2),
        net_worth=round(net_worth, 2),
        total_pnl=round(total_pnl, 2),
        total_pnl_pct=round(total_pnl_pct, 2),
        best_asset=best.ticker if best else None,
        best_asset_pnl_pct=best.pnl_pct if best else None,
        worst_asset=worst.ticker if worst else None,
        worst_asset_pnl_pct=worst.pnl_pct if worst else None,
        positions=positions,
        allocation=allocation,
    )