import json

from fastapi import FastAPI, WebSocket, WebSocketDisconnect

from app.managers.connection import manager
from game_server import GameLogic

app = FastAPI()
game_logic = GameLogic(manager)


