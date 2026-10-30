"""Erro de domínio compartilhado entre stores, rotas e testes."""


class CampaignError(Exception):
    """Falha esperada do domínio; as rotas a traduzem para a resposta HTTP."""

    def __init__(self, status: int, message: str):
        self.status = status
        super().__init__(message)
