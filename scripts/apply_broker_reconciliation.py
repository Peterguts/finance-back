#!/usr/bin/env python3
"""
1) G/P realizado de la venta QCOM del broker (~41.60) en el documento de venta.
2) Ajuste de efectivo para alinear efectivo estimado con el saldo real (~145.15 vs fórmula ~116.11 → +29.04).
"""
from __future__ import annotations

import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

import os

from pymongo import MongoClient


def main() -> int:
    uri = os.environ.get("MONGO_URI")
    if not uri:
        print("MONGO_URI no definido", file=sys.stderr)
        return 1

    db = MongoClient(uri).finanzas
    sales = db.sales
    settings = db.settings

    # Venta QCOM 29/4 ~0.0845 @ ~181.4
    r = sales.update_one(
        {
            "ticker": "QCOM",
            "quantity": {"$gte": 0.084, "$lte": 0.085},
        },
        {
            "$set": {
                "realized_pnl": 41.60,
                "sell_price": 181.44,
                "realized_pnl_from_broker": True,
            }
        },
    )
    print("QCOM sale update: matched", r.matched_count, "modified", r.modified_count)

    settings.update_one(
        {"_id": "portfolio"},
        {
            "$set": {
                "id": "portfolio",
                "cash_adjustment_usd": 29.04,
                "note": "Reconciliación: comisiones / diferencias vs modelo de flujo",
            }
        },
        upsert=True,
    )
    print("settings.portfolio: cash_adjustment_usd = 29.04 (upsert)")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
