#!/usr/bin/env python3
"""
Alinea compras LINK-USD y CRMD con el historial del usuario (fechas y precios).
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

    tx = MongoClient(uri).finanzas.transactions

    updates = [
        # (filtro por qty único, campos)
        (
            {"ticker": "LINK-USD", "quantity": {"$gte": 5.163072, "$lte": 5.163074}},
            {
                "date": "2026-03-14",
                "buy_price": 8.75,
                "created_at": "2026-03-14T12:00:00+00:00",
                "_restored": False,
            },
        ),
        (
            {"ticker": "LINK-USD", "quantity": {"$gte": 3.546285, "$lte": 3.546287}},
            {
                "date": "2026-04-14",
                "buy_price": 9.01,
                "created_at": "2026-04-14T12:00:00+00:00",
                "_restored": False,
            },
        ),
        (
            {"ticker": "CRMD", "quantity": {"$gte": 6.999, "$lte": 7.001}},
            {
                "date": "2026-04-03",
                "buy_price": 6.87,
                "created_at": "2026-04-03T12:00:00+00:00",
                "_restored": False,
            },
        ),
        (
            {"ticker": "CRMD", "quantity": {"$gte": 4.455949, "$lte": 4.455951}},
            {
                "date": "2026-04-14",
                "buy_price": 6.87,
                "created_at": "2026-04-14T12:00:00+00:00",
                "_restored": False,
            },
        ),
    ]

    for filt, patch in updates:
        r = tx.update_one(filt, {"$set": patch})
        print(filt, "matched", r.matched_count, "modified", r.modified_count)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
