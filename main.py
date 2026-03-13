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
from fastapi import FastAPI, HTTPException
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
client: Optional[AsyncIOMotorClient] = None
db = None
coll = None  # finanzas.transactions

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


async def get_current_price(ticker: str) -> float:
    symbol = ticker.upper()
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
    global client, db, coll
    if MONGO_URI:
        client = AsyncIOMotorClient(MONGO_URI)
        db = client.finanzas
        coll = db.transactions
        await coll.create_index("ticker")


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


class PortfolioSummary(BaseModel):
    total_invested: float
    current_value: float
    total_pnl: float
    pnl_percentage: float
    investments: list[Investment]


def _doc_to_investment(doc: dict) -> Investment:
    """Map finanzas.transactions document to API Investment model."""
    doc_id = doc.get("id") or doc.get("_id")
    if doc_id is None:
        doc_id = ""
    return Investment(
        id=str(doc_id),
        ticker=doc.get("ticker", ""),
        amount=float(doc.get("quantity", 0)),
        buy_price=float(doc.get("buy_price", 0)),
        timestamp=doc.get("created_at") or doc.get("date") or "",
    )


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
        "ticker": investment.ticker.upper(),
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
async def get_investments():
    if coll is None:
        raise HTTPException(status_code=503, detail="Database not connected")
    investments = []
    async for doc in coll.find().sort("created_at", -1):
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
        set_data["ticker"] = raw["ticker"].upper()
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


@app.get("/portfolio/summary", response_model=PortfolioSummary)
async def get_portfolio_summary():
    if coll is None:
        raise HTTPException(status_code=503, detail="Database not connected")
    investments = []
    total_invested = 0.0
    current_value = 0.0
    async for doc in coll.find():
        inv = _doc_to_investment(doc)
        investments.append(inv)
        invested_amount = inv.amount * inv.buy_price
        total_invested += invested_amount
        current_price = await get_current_price(inv.ticker)
        if current_price > 0:
            current_value += inv.amount * current_price
        else:
            current_value += invested_amount
    total_pnl = current_value - total_invested
    pnl_percentage = (total_pnl / total_invested * 100) if total_invested > 0 else 0
    return PortfolioSummary(
        total_invested=round(total_invested, 2),
        current_value=round(current_value, 2),
        total_pnl=round(total_pnl, 2),
        pnl_percentage=round(pnl_percentage, 2),
        investments=investments,
    )


@app.get("/prices/{ticker}")
async def get_price(ticker: str):
    price = await get_current_price(ticker.upper())
    if price == 0:
        raise HTTPException(status_code=404, detail=f"Price not found for {ticker}")
    return {"ticker": ticker.upper(), "price": price}


@app.get("/prices")
async def get_all_prices():
    if coll is None:
        return MOCK_PRICES
    tickers: set[str] = set()
    async for doc in coll.find():
        tickers.add((doc.get("ticker") or "").upper())
    result: dict[str, float] = {}
    for t in tickers:
        p = await get_current_price(t)
        if p > 0:
            result[t] = p
    return result if result else MOCK_PRICES


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
