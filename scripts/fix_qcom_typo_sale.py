#!/usr/bin/env python3
"""
Corrige venta QCOM cargada como 0.8 en lugar de 0.0845 (error de tecleo).
Recalcula realized_pnl con el precio medio de compra de todas las compras QCOM.

Uso (desde la carpeta finance-back, con .env y MONGO_URI):
  python scripts/fix_qcom_typo_sale.py
"""
from __future__ import annotations

import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

import os

from pymongo import MongoClient


def normalize_ticker(ticker: str) -> str:
    t = (ticker or "").strip().upper()
    if t in ("LINKUSD", "LINK-USD"):
        return "LINK-USD"
    return t


def main() -> int:
    uri = os.environ.get("MONGO_URI")
    if not uri:
        print("MONGO_URI no está definido en .env", file=sys.stderr)
        return 1

    client = MongoClient(uri)
    db = client.finanzas
    tx_coll = db.transactions
    sales_coll = db.sales

    bought = 0.0
    cost = 0.0
    for d in tx_coll.find({}):
        if normalize_ticker(d.get("ticker") or "") != "QCOM":
            continue
        q = float(d.get("quantity", 0))
        p = float(d.get("buy_price", 0))
        bought += q
        cost += q * p

    if bought <= 0:
        print("No hay compras QCOM; no se hace nada.")
        return 0

    avg_buy = cost / bought

    # Venta con cantidad ~0.8 (typo)
    candidates = list(
        sales_coll.find(
            {
                "ticker": {"$in": ["QCOM"]},
                "quantity": {"$gte": 0.79, "$lte": 0.81},
            }
        )
    )
    if not candidates:
        print("No se encontró venta QCOM con cantidad ~0.8; puede que ya esté corregida.")
        return 0

    new_qty = 0.0845
    fixed = 0
    for doc in candidates:
        sp = float(doc.get("sell_price", 0))
        rpnl = round(new_qty * (sp - avg_buy), 2)
        sales_coll.update_one(
            {"_id": doc["_id"]},
            {
                "$set": {
                    "quantity": new_qty,
                    "realized_pnl": rpnl,
                    "cost_basis": round(avg_buy, 4),
                }
            },
        )
        print(
            f"Actualizado _id={doc['_id']!s}: quantity {doc.get('quantity')} -> {new_qty}, "
            f"realized_pnl -> {rpnl} (avg_buy={avg_buy:.4f}, sell={sp:.2f})"
        )
        fixed += 1

    print(f"Listo. Ventas corregidas: {fixed}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
