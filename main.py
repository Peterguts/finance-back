import asyncio
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv

import httpx
import yfinance as yf

# Load .env from the same folder as this file (back/) so MONGO_URI is available
load_dotenv(Path(__file__).resolve().parent / ".env")
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from motor.motor_asyncio import AsyncIOMotorClient
from pydantic import BaseModel, Field

app = FastAPI(title="Portfolio API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

MONGO_URI = os.environ.get("MONGO_URI")
FINNHUB_API_KEY = os.environ.get("FINNHUB_API_KEY")
USD_TO_GTQ_FALLBACK = float(os.environ.get("USD_TO_GTQ_FALLBACK", "7.75"))
client: Optional[AsyncIOMotorClient] = None
db = None
coll = None  # finanzas.transactions
sales_coll = None  # finanzas.sales
deposits_coll = None  # finanzas.deposits

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
    if t in ("LINKUSD", "LINK-USD"):
        return "LINK-USD"
    return t


def ticker_raw_variants(ticker: str) -> list[str]:
    """Valores que pueden existir en Mongo para el mismo activo (filtros)."""
    t = normalize_ticker(ticker)
    if t == "LINK-USD":
        return ["LINK-USD", "LINKUSD"]
    return [t]


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
    global client, db, coll, sales_coll, deposits_coll
    if MONGO_URI:
        client = AsyncIOMotorClient(MONGO_URI)
        db = client.finanzas
        coll = db.transactions
        sales_coll = db.sales
        deposits_coll = db.deposits
        await coll.create_index("ticker")
        await sales_coll.create_index("ticker")
        await sales_coll.create_index("date")
        await sales_coll.create_index("created_at")
        await deposits_coll.create_index("date")
        await deposits_coll.create_index("created_at")


@app.on_event("shutdown")
async def shutdown_db_client():
    global client
    if client:
        client.close()


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


class PortfolioPosition(BaseModel):
    ticker: str
    quantity: float
    cost_basis: float
    current_value: float
    unrealized_pnl: float
    realized_pnl: float


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


def _doc_to_investment(doc: dict) -> Investment:
    """Map finanzas.transactions document to API Investment model."""
    doc_id = doc.get("id") or doc.get("_id")
    if doc_id is None:
        doc_id = ""
    return Investment(
        id=str(doc_id),
        ticker=normalize_ticker(doc.get("ticker", "")),
        amount=float(doc.get("quantity", 0)),
        buy_price=float(doc.get("buy_price", 0)),
        timestamp=doc.get("created_at") or doc.get("date") or "",
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
    """Returns per ticker: (total_bought_qty, total_sold_qty, avg_buy_price)."""
    ticker_bought: dict[str, list[tuple[float, float]]] = {}  # ticker -> [(qty, price), ...]
    ticker_first_buy_dt: dict[str, datetime] = {}
    async for doc in coll.find():
        t = normalize_ticker(doc.get("ticker") or "")
        if not t:
            continue
        q = float(doc.get("quantity", 0))
        p = float(doc.get("buy_price", 0))
        if t not in ticker_bought:
            ticker_bought[t] = []
        ticker_bought[t].append((q, p))
        dt = _parse_doc_datetime(doc)
        if dt is not None:
            prev = ticker_first_buy_dt.get(t)
            if prev is None or dt < prev:
                ticker_first_buy_dt[t] = dt
    ticker_sold: dict[str, float] = {}
    if sales_coll is not None:
        async for doc in sales_coll.find():
            t = normalize_ticker(doc.get("ticker") or "")
            if not t:
                continue
            first_buy = ticker_first_buy_dt.get(t)
            if first_buy is not None:
                sale_dt = _parse_doc_datetime(doc)
                # Preserve historical sales records without descontar posiciones
                # when they happened before the current portfolio baseline.
                if sale_dt is not None and sale_dt < first_buy:
                    continue
            ticker_sold[t] = ticker_sold.get(t, 0) + float(doc.get("quantity", 0))
    result: dict[str, tuple[float, float, float]] = {}
    for t, buys in ticker_bought.items():
        total_bought = sum(q for q, _ in buys)
        total_cost = sum(q * p for q, p in buys)
        total_sold = ticker_sold.get(t, 0)
        avg_price = total_cost / total_bought if total_bought > 0 else 0
        result[t] = (total_bought, total_sold, avg_price)
    return result


async def _get_available_quantity(ticker: str) -> float:
    pos = await _get_position_and_cost_per_ticker()
    t = normalize_ticker(ticker)
    if t not in pos:
        return 0
    bought, sold, _ = pos[t]
    return max(0, bought - sold)


@app.get("/health")
async def health_check():
    return {"status": "healthy", "database": "connected" if db is not None else "not connected"}


@app.post("/investments", response_model=Investment)
async def create_investment(investment: InvestmentCreate):
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
async def get_investments(ticker: Optional[str] = Query(None)):
    if coll is None:
        raise HTTPException(status_code=503, detail="Database not connected")
    q = {}
    if ticker and ticker.strip():
        q["ticker"] = {"$in": ticker_raw_variants(ticker.strip())}
    investments = []
    cursor = coll.find(q).sort("created_at", -1)
    async for doc in cursor:
        investments.append(_doc_to_investment(doc))
    return investments


@app.get("/investments/{investment_id}", response_model=Investment)
async def get_investment(investment_id: str):
    if coll is None:
        raise HTTPException(status_code=503, detail="Database not connected")
    doc = await coll.find_one({"$or": [{"_id": investment_id}, {"id": investment_id}]})
    if doc is None:
        raise HTTPException(status_code=404, detail="Investment not found")
    return _doc_to_investment(doc)


@app.put("/investments/{investment_id}", response_model=Investment)
async def update_investment(investment_id: str, update: InvestmentUpdate):
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
async def delete_investment(investment_id: str):
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
async def create_sale(sale: SaleCreate):
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
async def get_sale(sale_id: str):
    if sales_coll is None:
        raise HTTPException(status_code=503, detail="Database not connected")
    doc = await sales_coll.find_one({"$or": [{"_id": sale_id}, {"id": sale_id}]})
    if doc is None:
        raise HTTPException(status_code=404, detail="Sale not found")
    return _doc_to_sale(doc)


@app.get("/movements", response_model=list[Movement])
async def get_movements(
    ticker: Optional[str] = Query(None),
    from_date: Optional[str] = Query(None, alias="from_date"),
    to_date: Optional[str] = Query(None, alias="to_date"),
    type_filter: Optional[str] = Query(None, alias="type"),  # "buy" | "sell" | omit = all
    limit: int = Query(200, ge=1, le=1000),
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
async def get_portfolio_summary():
    if coll is None:
        raise HTTPException(status_code=503, detail="Database not connected")
    investments = []
    async for doc in coll.find().sort("created_at", -1):
        investments.append(_doc_to_investment(doc))

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
        current_qty = max(0, bought - sold)
        if current_qty <= 0:
            if ticker in realized_by_ticker and realized_by_ticker[ticker] != 0:
                positions.append(
                    PortfolioPosition(
                        ticker=ticker,
                        quantity=0,
                        cost_basis=0,
                        current_value=0,
                        unrealized_pnl=0,
                        realized_pnl=round(realized_by_ticker[ticker], 2),
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
    estimated_cash = round(
        total_deposited - total_spent_on_buys + total_received_from_sales, 2
    )
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
async def get_all_prices():
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
async def get_price(ticker: str):
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
async def create_deposit(body: DepositCreate):
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
async def list_deposits():
    if deposits_coll is None:
        raise HTTPException(status_code=503, detail="Database not connected")
    out: list[Deposit] = []
    async for doc in deposits_coll.find().sort("date", -1):
        out.append(_doc_to_deposit(doc))
    return out


@app.get("/deposits/{deposit_id}", response_model=Deposit)
async def get_deposit(deposit_id: str):
    if deposits_coll is None:
        raise HTTPException(status_code=503, detail="Database not connected")
    doc = await deposits_coll.find_one({"$or": [{"_id": deposit_id}, {"id": deposit_id}]})
    if doc is None:
        raise HTTPException(status_code=404, detail="Deposit not found")
    return _doc_to_deposit(doc)


@app.put("/deposits/{deposit_id}", response_model=Deposit)
async def update_deposit(deposit_id: str, update: DepositUpdate):
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
async def delete_deposit(deposit_id: str):
    if deposits_coll is None:
        raise HTTPException(status_code=503, detail="Database not connected")
    result = await deposits_coll.delete_one(
        {"$or": [{"_id": deposit_id}, {"id": deposit_id}]}
    )
    if result.deleted_count == 0:
        raise HTTPException(status_code=404, detail="Deposit not found")
    return {"message": "Deposit deleted successfully"}
