"""PINs de teste para as identidades da mesa.

O servidor recusa o valor legado (`nome.minúsculas`) de propósito, então cada teste
provisiona os PINs que usa — o mesmo que o mestre faz na vida real com
`tools/set_table_pin.py`.
"""

TABLE_PINS = {
    "Gabriel": "pin-de-teste-gabriel",
    "Leandro": "pin-de-teste-leandro",
    "Yago": "pin-de-teste-yago",
    "Luiz": "pin-de-teste-luiz",
    "Amom": "pin-de-teste-amom",
    "Victor": "pin-de-teste-victor",
    "Wayne": "pin-de-teste-wayne",
    "Visitante": "pin-de-teste-visitante",
}


def provision_all(store) -> None:
    """Provisiona a mesa inteira, como o deploy faz com o script de PIN."""
    for name, pin in TABLE_PINS.items():
        store.set_table_pin(name, pin)


def credentials(name: str) -> dict:
    """Corpo de `/auth/table-login` para a identidade dada."""
    return {"name": name, "pin": TABLE_PINS[name]}
