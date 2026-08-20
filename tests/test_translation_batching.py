"""Regresión del troceado por lotes de la traducción.

Un lote mal alineado desplazaría los subtítulos, así que ante cualquier
respuesta que no respete la numeración hay que caer al modo uno-a-uno.
Se puede ejecutar con `pytest tests` o directamente con python.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from vhs import main  # noqa: E402


class _FakeClient:
    """Sustituye al cliente OpenAI: devuelve respuestas fijas y cuenta llamadas."""

    def __init__(self, respuestas):
        self.respuestas = list(respuestas)
        self.peticiones = []

    def _responder(self, *_args, **kwargs):
        self.peticiones.append(kwargs)
        return self.respuestas.pop(0)


def _parchear(monkey_client):
    original = main._chat_translate
    main._chat_translate = lambda client, model, system, user: client._responder(
        system=system, user=user
    )
    return original


def test_lote_bien_formado():
    cliente = _FakeClient(["1. uno\n2. dos\n3. tres"])
    original = _parchear(cliente)
    try:
        salida = main._translate_batch(cliente, "m", ["one", "two", "three"])
    finally:
        main._chat_translate = original
    assert salida == ["uno", "dos", "tres"], salida
    assert len(cliente.peticiones) == 1, "debe ser una sola petición"


def test_lote_desalineado_se_rechaza():
    # Faltan líneas: aceptarlo desplazaría los subtítulos.
    for respuesta in (
        "1. uno\n2. dos",                 # falta la tercera
        "uno\ndos\ntres",                 # sin numeración
        "1. uno\n2. dos\n3. tres\n4. x",  # una línea de más
        "1. uno\n2. \n3. tres",           # una traducción vacía
    ):
        cliente = _FakeClient([respuesta])
        original = _parchear(cliente)
        try:
            assert main._translate_batch(cliente, "m", ["a", "b", "c"]) is None, respuesta
        finally:
            main._chat_translate = original


def test_fallback_uno_a_uno_conserva_el_orden():
    # El lote falla y se reintenta segmento a segmento, sin perder el orden.
    cliente = _FakeClient(["respuesta basura", "uno", "dos", "tres"])
    original = _parchear(cliente)
    original_openai = main.OpenAI
    original_batch = main.TRANSLATION_BATCH_SIZE
    original_conc = main.TRANSLATION_CONCURRENCY
    main.OpenAI = lambda **_kwargs: cliente
    main.TRANSLATION_BATCH_SIZE = 3
    main.TRANSLATION_CONCURRENCY = 1
    try:
        salida = main._translate_texts_to_spanish(["one", "two", "three"])
    finally:
        main._chat_translate = original
        main.OpenAI = original_openai
        main.TRANSLATION_BATCH_SIZE = original_batch
        main.TRANSLATION_CONCURRENCY = original_conc
    assert salida == ["uno", "dos", "tres"], salida
    assert len(cliente.peticiones) == 4, cliente.peticiones


def test_numeros_con_separadores_distintos():
    cliente = _FakeClient(["1) uno\n2 - dos\n3: tres"])
    original = _parchear(cliente)
    try:
        salida = main._translate_batch(cliente, "m", ["a", "b", "c"])
    finally:
        main._chat_translate = original
    assert salida == ["uno", "dos", "tres"], salida


if __name__ == "__main__":
    for nombre, funcion in sorted(globals().items()):
        if nombre.startswith("test_"):
            funcion()
            print(f"OK · {nombre}")
