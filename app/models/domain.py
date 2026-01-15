from typing import Dict, List, Literal, Optional

from pydantic import BaseModel

# --- Tipos Primitivos e Enums ---

AttributeName = Literal[
    "Constituição", "Força", "Carisma", "Sabedoria", "Inteligência", "Destreza"
]

CharacterClass = Literal[
    "Guerreiro", "Corsário", "Vanguarda", "Mago", "Apóstata", "Atirador", "Orador"
]

ActionType = Literal["Padrão", "Movimento", "Reação", "Completa", "Livre", "Ação Bônus"]

# --- Sub-modelos ---


class Attribute(BaseModel):
    name: AttributeName
    value: int
    modifier: int


class StatsPair(BaseModel):
    current: int
    max: int


class TurnActions(BaseModel):
    standard: bool = True
    bonus: bool = True
    reaction: bool = True


class Skill(BaseModel):
    id: str
    name: str
    cost: int
    actionType: ActionType
    description: str
    level: int


class Stance(BaseModel):
    id: str
    name: str
    benefit: str
    restriction: str
    maneuver: str
    recovery: Optional[str] = None
    acBonus: Optional[int] = 0


# --- Entidade Principal de Combate ---


class Combatant(BaseModel):
    id: str
    name: str
    baseName: str
    initiative: int
    hp: StatsPair
    type: Literal["player", "npc"]

    armorClass: int
    maxFocus: int
    currentFocus: int

    # OptionalDict para lidar com Record<AttributeName, Attribute>
    attributes: Dict[str, Attribute] = {}

    equipment: Optional[str] = None  # String simplificada para combate
    actions: Optional[str] = None

    stances: List[Stance] = []
    skills: List[Skill] = []

    activeStanceId: Optional[str] = None

    turnActions: TurnActions


# --- Estado da Sessão (O que vai para o Redis) ---


class CombatSession(BaseModel):
    session_id: str
    turn_index: int = 0
    # A ordem dos IDs define a iniciativa
    turn_order: List[str]
    # Mapa de ID -> Combatant
    combatants: Dict[str, Combatant]
    logs: List[str] = []

# class CombatState(BaseModel):
#     session_id: str
#     turn_index: int = 0  # Índice do array de turn_order
#     turn_order: List[str]  # Lista de IDs dos personagens ordenados
#     entities: dict[str, Character]  # Mapa de ID -> Personagem
#     active: bool = True
#     logs: List[str] = []
