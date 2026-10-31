"""Guard de teste: qualquer toque no transporte de combate falha alto.

Por que não `patch("app.managers.connection.manager")`: módulos que fizeram
`from app.managers.connection import manager` (é o caso de `app/routers/campaigns.py`)
guardaram a referência ao objeto original, então substituir o atributo do módulo
não troca nada para eles — o teste fica verde com um broadcast acidental.

Aqui o próprio objeto é alterado: todo alias existente passa a ver o guard, e a
chamada levanta `AssertionError` em vez de silenciosamente publicar no Redis.
"""

from collections.abc import Iterator
from contextlib import contextmanager

from app.managers.connection import manager

TRANSPORT_METHODS = ("get_state", "broadcast_state", "broadcast_event")


@contextmanager
def no_combat_transport() -> Iterator[None]:
    """Falha o teste se a preparação tocar estado ou broadcast de combate."""

    def forbidden(name: str):
        def _fail(*args, **kwargs):
            raise AssertionError(
                f"preparação não pode tocar o transporte de combate: {name}() foi chamado"
            )

        return _fail

    originals = {name: getattr(manager, name) for name in TRANSPORT_METHODS}
    for name in TRANSPORT_METHODS:
        setattr(manager, name, forbidden(name))
    try:
        yield
    finally:
        for name, original in originals.items():
            setattr(manager, name, original)
