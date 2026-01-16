from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware  # <--- IMPORTANTE

from app.routers import websocket

app = FastAPI(title="RPG Server")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # "*" libera para qualquer IP (Celular, PC, etc)
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(websocket.router)
