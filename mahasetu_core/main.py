"""ASGI entry point: uvicorn mahasetu_core.main:app"""
from .api import create_app

app = create_app()
