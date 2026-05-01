#!/usr/bin/env python3
"""
Reinserta compras LINK-USD y CRMD que faltan en `transactions` pero sí tienen ventas en `sales`.
Costes inferidos de cada venta: coste ≈ qty * precio_venta - realized_pnl (coherente con el modelo de coste medio).

Idempotente: si ya existen compras para esos lotes (misma qty y precio), no duplica.
"""
from __future__ import annotations

import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

import os

from pymongo import MongoClient


def norm_t(t: str) -> str:
    x = (t or "").strip().upper()
    if x in ("LINKUSD", "LINK-USD"):
        return "LINK-USD"
    return x


def has_buy(tx, ticker: str, qty: float, price: float, tol: float = 1e-4) -> bool:
    t = norm_t(ticker)
    for d in tx.find({}):
        if norm_t(d.get("ticker") or "") != t:
            continue
        if abs(float(d.get("quantity", 0)) - qty) < tol and abs(float(d.get("buy_price", 0)) - price) < 1e-2:
            return True
    return False


def main() -> int:
    uri = os.environ.get("MONGO_URI")
    if not uri:
        print("MONGO_URI no definido", file=sys.stderr)
        return 1

    db = MongoClient(uri).finanzas
    tx = db.transactions
    now = datetime.now(timezone.utc).isoformat()

    # Inferir coste por acción desde venta almacenada: realized = qty * (sell - avg) → usamos como buy_price
    # el coste implícito de ESE lote si fuera único: sell - realized/qty
    def implied_buy(sqty: float, sprice: float, rpnl: float) -> float:
        if sqty <= 0:
            return 0.0
        return sprice - rpnl / sqty

    inserts = []

    # LINK-USD venta 2026-03-17 (histórico)
    s1 = db.sales.find_one(
        {"ticker": {"$in": ["LINK-USD", "LINKUSD"]}, "quantity": {"$gte": 5.163072, "$lte": 5.163074}}
    )
    if s1 and not has_buy(tx, "LINK-USD", 5.163073, implied_buy(5.163073, float(s1["sell_price"]), float(s1.get("realized_pnl", 0)))):
        p = round(implied_buy(5.163073, float(s1["sell_price"]), float(s1.get("realized_pnl", 0))), 6)
        did = str(uuid.uuid4())
        inserts.append(
            {
                "_id": did,
                "id": did,
                "ticker": "LINK-USD",
                "quantity": 5.163073,
                "buy_price": p,
                "date": "2026-03-15",
                "currency": "USD",
                "asset_type": "Stock",
                "created_at": "2026-03-15T12:00:00+00:00",
                "updated_at": now,
                "_restored": True,
            }
        )

    # LINK-USD compra 2026-04-15 (usuario)
    if not has_buy(tx, "LINK-USD", 3.546286, 9.01):
        did = str(uuid.uuid4())
        inserts.append(
            {
                "_id": did,
                "id": did,
                "ticker": "LINK-USD",
                "quantity": 3.546286,
                "buy_price": 9.01,
                "date": "2026-04-15",
                "currency": "USD",
                "asset_type": "Stock",
                "created_at": "2026-04-15T18:00:00+00:00",
                "updated_at": now,
                "_restored": True,
            }
        )

    # CRMD venta 2026-04-06
    s2 = db.sales.find_one({"ticker": "CRMD", "quantity": 7.0})
    if s2 and not has_buy(tx, "CRMD", 7.0, implied_buy(7.0, float(s2["sell_price"]), float(s2.get("realized_pnl", 0)))):
        p = round(implied_buy(7.0, float(s2["sell_price"]), float(s2.get("realized_pnl", 0))), 6)
        did = str(uuid.uuid4())
        inserts.append(
            {
                "_id": did,
                "id": did,
                "ticker": "CRMD",
                "quantity": 7.0,
                "buy_price": p,
                "date": "2026-04-04",
                "currency": "USD",
                "asset_type": "Stock",
                "created_at": "2026-04-04T12:00:00+00:00",
                "updated_at": now,
                "_restored": True,
            }
        )

    # CRMD compra 2026-04-15
    if not has_buy(tx, "CRMD", 4.45595, 6.87):
        did = str(uuid.uuid4())
        inserts.append(
            {
                "_id": did,
                "id": did,
                "ticker": "CRMD",
                "quantity": 4.45595,
                "buy_price": 6.87,
                "date": "2026-04-15",
                "currency": "USD",
                "asset_type": "Stock",
                "created_at": "2026-04-15T18:00:00+00:00",
                "updated_at": now,
                "_restored": True,
            }
        )

    if not inserts:
        print("Nada que restaurar (compras ya presentes o ventas no encontradas).")
        return 0

    for doc in inserts:
        tx.insert_one(doc)
        print(f"Insertada compra {doc['ticker']}: qty={doc['quantity']} @ {doc['buy_price']} id={doc['id']}")

    print(f"Listo: {len(inserts)} documento(s) en transactions.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
