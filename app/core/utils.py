import functools
import logging
import time

# Configuração básica do log
logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s"
)


def static_method_log(func):
    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        nome_funcao = func.__name__
        logging.info(f"Executando método estático: {nome_funcao}")
        logging.info(f"Argumentos: args={args}, kwargs={kwargs}")

        inicio = time.time()
        try:
            resultado = func(*args, **kwargs)
            duracao = time.time() - inicio
            logging.info(
                f"Método {nome_funcao} finalizado com sucesso em {duracao:.4f}s"
            )
            return resultado
        except Exception as e:
            logging.error(f"Erro no método {nome_funcao}: {str(e)}")
            raise e

    return wrapper
