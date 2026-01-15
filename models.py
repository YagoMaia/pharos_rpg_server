from enum import Enum
from typing import List, Optional, Any

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


# class PlayerAction(BaseModel):
#     action: ActionType
#     target_id: Optional[str] = None
#     damage_amount: Optional[int] = 0


class HP(BaseModel):
    current: int
    max: int


class TurnActions(BaseModel):
    standard: bool = True  # True = Disponível
    bonus: bool = True
    reaction: bool = True


class Stance(BaseModel):
    id: str
    name: str
    # ... outros campos da stance


class Skill(BaseModel):
    id: str
    name: str
    # ...


# --- O Modelo Principal (Espelho da sua Interface TS) ---


class Combatant(BaseModel):
    id: str
    name: str
    baseName: str
    initiative: int
    hp: HP
    type: str  # "player" | "npc"

    armorClass: int
    maxFocus: int
    currentFocus: int

    # Usando Dict para representar Record<string, Any> ou modelos específicos
    attributes: Optional[dict[str, Any]] = None
    equipment: Optional[str] = None
    actions: Optional[str] = None  # Descrição textual ou IDs

    stances: List[Stance] = []
    skills: List[Skill] = []

    activeStanceId: Optional[str] = None

    turnActions: TurnActions


# --- Modelo de Ação Atualizado ---


class ActionCost(str, Enum):
    STANDARD = "standard"
    BONUS = "bonus"
    FREE = "free"


class PlayerAction(BaseModel):
    action: str  # "attack", "use_skill", "end_turn", "toggle_stance"
    target_id: Optional[str] = None
    cost: ActionCost = ActionCost.STANDARD  # O front diz quanto custa essa ação
    damage_amount: int = 0
    # ... outros parâmetros (ex: skill_id)
