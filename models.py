from enum import Enum
from typing import List, Optional

from pydantic import BaseModel


class EntityType(str, Enum):
    PLAYER = "player"
    NPC = "npc"


class Character(BaseModel):
    id: str
    name: str
    type: EntityType
    hp: int
    max_hp: int
    initiative: int  # Para ordenar os turnos


class CombatState(BaseModel):
    session_id: str
    turn_index: int = 0  # Índice do array de turn_order
    turn_order: List[str]  # Lista de IDs dos personagens ordenados
    entities: dict[str, Character]  # Mapa de ID -> Personagem
    active: bool = True
    logs: List[str] = []


class ActionType(str, Enum):
    ATTACK = "attack"
    END_TURN = "end_turn"


class PlayerAction(BaseModel):
    action: ActionType
    target_id: Optional[str] = None
    damage_amount: Optional[int] = 0
