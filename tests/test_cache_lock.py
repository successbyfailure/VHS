"""Regresión del lock por clave de caché.

Dos peticiones idénticas simultáneas generaban la misma entrada a la vez: una
moría con "Unable to rename file ... .part" y otra podía servir una respuesta
truncada. El lock debe serializarlas por clave sin bloquear claves distintas.
"""

import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from vhs.main import cache_key_lock, _CACHE_LOCKS, write_cache_file_atomic  # noqa: E402


def test_misma_clave_se_serializa():
    activos = []
    max_simultaneos = []

    def trabajo():
        with cache_key_lock("clave-a"):
            activos.append(1)
            max_simultaneos.append(len(activos))
            time.sleep(0.05)
            activos.pop()

    hilos = [threading.Thread(target=trabajo) for _ in range(5)]
    for h in hilos:
        h.start()
    for h in hilos:
        h.join()
    assert max(max_simultaneos) == 1, max_simultaneos


def test_claves_distintas_no_se_bloquean():
    barrera = threading.Barrier(3, timeout=5)

    def trabajo(clave):
        with cache_key_lock(clave):
            barrera.wait()  # revienta si las claves se serializaran entre sí

    hilos = [threading.Thread(target=trabajo, args=(f"clave-{i}",)) for i in range(3)]
    for h in hilos:
        h.start()
    for h in hilos:
        h.join()


def test_el_diccionario_de_locks_no_crece():
    for i in range(50):
        with cache_key_lock(f"efimera-{i}"):
            pass
    assert _CACHE_LOCKS == {}, _CACHE_LOCKS


def test_escritura_atomica_no_deja_temporales(tmp_path=None):
    destino = Path(tmp_path or "/tmp") / "vhs_test_atomico.txt"
    write_cache_file_atomic(destino, "contenido completo")
    assert destino.read_text(encoding="utf-8") == "contenido completo"
    temporal = destino.with_name(f".{destino.name}.tmp")
    assert not temporal.exists(), "el temporal debe desaparecer"
    destino.unlink()


if __name__ == "__main__":
    for nombre, funcion in sorted(globals().items()):
        if nombre.startswith("test_"):
            funcion()
            print(f"OK · {nombre}")
