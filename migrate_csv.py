#!/usr/bin/env python3
"""
Script de migración para importar datos del CSV de inversiones a MongoDB
"""
import csv
import os
import sys
import uuid
from datetime import datetime, timedelta
from motor.motor_asyncio import AsyncIOMotorClient
import asyncio
from dotenv import load_dotenv

# Cargar variables de entorno
load_dotenv()


async def migrate_csv_to_mongo(csv_path: str):
    # Conectar a MongoDB
    mongodb_uri = os.getenv("MONGODB_URI", "mongodb://localhost:27017")
    client = AsyncIOMotorClient(mongodb_uri)
    db = client.finanzas
    
    print(f"🔗 Conectado a MongoDB: {mongodb_uri}")
    print(f"📂 Leyendo CSV: {csv_path}")
    
    # Leer el CSV
    transactions = []
    with open(csv_path, 'r', encoding='utf-8') as f:
        reader = csv.DictReader(f)
        for idx, row in enumerate(reader):
            # Saltar filas vacías o de totales
            if not row.get('Ticker') or not row['Ticker'].strip():
                continue
            
            ticker = row['Ticker'].strip()
            invertido = float(row['Invertido'].replace(',', '.'))
            cantidad = float(row['Cantidad de Títulos'].replace(',', '.'))
            
            # Calcular precio de compra correcto: Invertido / Cantidad
            precio_compra = invertido / cantidad if cantidad > 0 else 0
            
            # Determinar tipo de asset (crypto si termina en -USD, sino stock)
            asset_type = "Crypto" if ticker.endswith("-USD") else "Stock"
            
            # Usar fecha escalonada en los últimos 6 meses (para tener historial)
            # La más reciente (primer row) es más nueva
            dias_atras = idx * 30  # 30 días entre cada transacción
            fecha_compra = datetime.now() - timedelta(days=dias_atras)
            
            txn_id = str(uuid.uuid4())
            now = datetime.utcnow().isoformat()
            
            transaction = {
                "_id": txn_id,
                "id": txn_id,
                "ticker": ticker.upper(),
                "quantity": cantidad,
                "buy_price": precio_compra,
                "date": fecha_compra.strftime("%Y-%m-%d"),
                "currency": "USD",
                "asset_type": asset_type,
                "created_at": now,
                "updated_at": now,
            }
            transactions.append(transaction)
            print(f"  ✓ {ticker}: {cantidad} @ ${precio_compra:.2f} (Total: ${invertido:.2f}) - {fecha_compra.strftime('%Y-%m-%d')}")
    
    # Verificar si ya hay datos
    existing_count = await db.transactions.count_documents({})
    if existing_count > 0:
        print(f"\n⚠️  Ya existen {existing_count} transacciones en la base de datos.")
        response = input("¿Deseas borrar todo e importar de nuevo? (s/n): ")
        if response.lower() == 's':
            await db.transactions.delete_many({})
            print("🗑️  Base de datos limpiada")
        else:
            print("❌ Migración cancelada")
            client.close()
            return
    
    # Insertar transacciones
    if transactions:
        result = await db.transactions.insert_many(transactions)
        print(f"\n✅ {len(result.inserted_ids)} transacciones migradas exitosamente!")
    else:
        print("⚠️  No se encontraron transacciones válidas en el CSV")
    
    client.close()
    print("🔌 Conexión cerrada")


if __name__ == "__main__":
    csv_file = sys.argv[1] if len(sys.argv) > 1 else "../Inversión.csv"
    
    if not os.path.exists(csv_file):
        print(f"❌ Archivo no encontrado: {csv_file}")
        print(f"💡 Uso: python migrate_csv.py [ruta_al_csv]")
        sys.exit(1)
    
    asyncio.run(migrate_csv_to_mongo(csv_file))
