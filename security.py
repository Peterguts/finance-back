"""
Módulo de seguridad y autenticación para la API
"""
from fastapi import HTTPException, Security, status
from fastapi.security import APIKeyHeader
import os
from typing import Optional

# Header de autenticación
api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)

def get_api_key() -> str:
    """Obtiene la API key desde variables de entorno"""
    api_key = os.getenv("API_KEY")
    if not api_key:
        print("⚠️  WARNING: No API_KEY found in environment variables")
        print("   Using development mode without authentication")
        return "development_mode"
    return api_key

def verify_api_key(api_key: Optional[str] = Security(api_key_header)) -> str:
    """
    Verifica que la API key enviada en el header coincida con la configurada.
    
    En desarrollo (ENVIRONMENT=development), permite requests sin API key.
    En producción (ENVIRONMENT=production), requiere API key obligatoria.
    """
    environment = os.getenv("ENVIRONMENT", "development")
    expected_key = get_api_key()
    
    # Modo desarrollo: permite requests sin autenticación
    if environment == "development":
        if not api_key:
            print("🔓 Development mode: Request without API key (allowed)")
            return "development"
        # Si envían key en desarrollo, validarla igual
        if api_key != expected_key:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid API Key",
            )
        return api_key
    
    # Modo producción: requiere API key obligatoria
    if not api_key:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing API Key. Include X-API-Key header.",
        )
    
    if api_key != expected_key:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid API Key",
        )
    
    return api_key

def get_cors_origins() -> list[str]:
    """Obtiene los orígenes permitidos para CORS desde variables de entorno"""
    origins_str = os.getenv("ALLOWED_ORIGINS", "http://localhost:5173")
    origins = [origin.strip() for origin in origins_str.split(",")]
    return origins
