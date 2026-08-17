"""
Entry point for running the ERP server.

    python run.py

Serves on HOST:PORT from .env (defaults to 0.0.0.0:8000 so other PCs
on the office LAN can reach it at http://<this-pc-ip>:8000).
"""
import uvicorn

from app.config import settings

if __name__ == "__main__":
    uvicorn.run("app.main:app", host=settings.HOST, port=settings.PORT, reload=False)
