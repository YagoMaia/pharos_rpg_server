# Instruções para Agents — modelo_reset

> Projeto pessoal. Regras gerais de desenvolvimento ficam na skill `dev-standards`; este arquivo registra apenas contexto, runtime e regras específicas deste repositório.

---

## Contexto do projeto

- Projeto: `pharos_rpg_server`
- Tipo: projeto pessoal Python
- Branch base padrão: `main`

---

<!-- BEGIN RUNTIME-AUDIT -->
## Ambiente e runtime

- Linguagem principal: Python
- Runtime: Python >=3.11
- Gerenciador de pacotes: uv
- Arquivo de versão: `.python-version` / `pyproject.toml requires-python (>=3.11)`
- Sistema operacional local esperado: Windows/PowerShell

### Antes de rodar comandos

```powershell
uv sync
```

### Comandos de validação

```powershell
# Instalação
uv sync

# Testes — exige Redis de integração; ver observação abaixo
uv run pytest

# Testes com Redis em container
docker run -d --name pharos-test-redis -p 6379:6379 redis:7-alpine
$env:PHAROS_TEST_REDIS_URL = "redis://127.0.0.1:6379/0"
uv run pytest

# Lint
uv run ruff check .

# Formatação
uv run ruff format .

# Build
não identificado
```

### Observações para agents

- `tests/test_campaign_live_redis.py` **falha** sem Redis de integração, por
  escolha: ele cobre pub/sub real e um `skipTest()` silencioso deixava a suíte
  verde sem exercitar o transporte. Precedência: `PHAROS_TEST_REDIS_URL` →
  `redis-server` efêmero no PATH → falha com mensagem explícita.
- `ruff` não está declarado em `pyproject.toml`; rodar com
  `uv run --with ruff ruff check .` até que entre no grupo `dev`.
- Não assumir Python global; usar `uv` e a versão definida pelo projeto.
- Não acessar `.env*`, secrets ou diretórios de dependências (`.venv`, `dist`, `build`).
- Não alterar/remover `uv.lock` sem confirmação humana.
- Se `pytest`/`ruff` falhar por ambiente ausente, executar `uv sync` antes de nova tentativa.
<!-- END RUNTIME-AUDIT -->

---

## Padrões de desenvolvimento

Aplicar a skill `dev-standards` para:

- Conventional Commits em inglês;
- TDD obrigatório para lógica de negócio;
- Clean Code e estrutura de código;
- testes/validações antes de considerar a tarefa concluída;
- segurança básica e proteção contra secrets;
- Definition of Done.

Não duplicar essas regras aqui; se houver dúvida, use `dev-standards` como fonte.

---

## Skills úteis

| Skill | Quando usar |
|-------|-------------|
| `dev-standards` | Conventional Commits, TDD, Clean Code e Definition of Done |
| `dev-workflow` | Implementar/finalizar tarefa com validação e fechamento |
| `agents-runtime-audit` | Atualizar runtime/comandos deste arquivo |
| `pr-description-github` | Gerar PR GitHub pessoal |
| `pr-validation` | Validar criticamente PR/MR |
| `docs-organization` | Criar documentação técnica em `docs/` |

---

## O que NÃO fazer

- Não hardcodar API keys, tokens ou senhas.
- Não ler nem expor `.env*`, chaves privadas ou arquivos de credenciais.
- Não commitar `.venv`, builds, caches ou dados locais.
- Não fazer force push em `main`.
- Não commitar lógica de negócio sem teste novo ou atualizado cobrindo a mudança, salvo justificativa explícita.
