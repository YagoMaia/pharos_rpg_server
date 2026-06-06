import functools
import logging
import time

# Configuração básica do log
logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s"
)


def static_method_log(func):
    @functools.wraps(func)
    async def wrapper(*args, **kwargs):
        nome_funcao = func.__name__
        logging.info(f"Executando: {nome_funcao}")

        inicio = time.time()
        try:
            resultado = await func(*args, **kwargs)
            duracao = time.time() - inicio
            logging.info(f"{nome_funcao} finalizado em {duracao:.4f}s")
            return resultado
        except Exception as e:
            logging.error(f"Erro em {nome_funcao}: {e}")
            raise

    return wrapper
