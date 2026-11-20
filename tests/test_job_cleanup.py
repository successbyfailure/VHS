"""Un trabajo solo borra lo que es suyo.

Al pasar las descargas a trabajos, el resultado deja de estar siempre en el
directorio del trabajo: una descarga cacheada apunta a CACHE_DIR, que es
compartida. Borrar el trabajo no puede llevarse la caché por delante.
Se puede ejecutar con `pytest tests` o directamente con python.
"""

import shutil
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from vhs.jobs import Job, JobStore, STATUS_DONE  # noqa: E402


def _borrar(ruta: Path) -> None:
    shutil.rmtree(ruta, ignore_errors=True)


def test_no_se_borra_la_cache_al_eliminar_un_trabajo():
    """El caso peligroso: result_path dentro de la caché compartida."""
    with tempfile.TemporaryDirectory() as raiz:
        base = Path(raiz)
        cache = base / "cache"
        cache.mkdir()
        resultado = cache / "video.mp4"
        resultado.write_bytes(b"contenido")
        otro = cache / "otro.mp4"
        otro.write_bytes(b"de otro")
        workdir = base / "jobs" / "j1"
        workdir.mkdir(parents=True)
        (workdir / "temporal.txt").write_text("basura")

        store = JobStore(base / "jobs_meta")
        job = Job(
            id="j1",
            kind="download",
            status=STATUS_DONE,
            result_path=str(resultado),
            result_external=True,
            params={"workdir": str(workdir)},
        )
        store.add(job)
        assert store.remove("j1", _borrar) is True

        assert resultado.exists(), "la descarga cacheada no debe borrarse"
        assert otro.exists(), "y menos aún el resto de la caché"
        assert cache.exists()
        assert not workdir.exists(), "el directorio del trabajo sí se limpia"


def test_un_resultado_propio_si_se_borra():
    with tempfile.TemporaryDirectory() as raiz:
        base = Path(raiz)
        workdir = base / "jobs" / "j2"
        workdir.mkdir(parents=True)
        resultado = workdir / "result.srt"
        resultado.write_text("1\n")

        store = JobStore(base / "jobs_meta")
        store.add(
            Job(
                id="j2",
                kind="transcription_batch",
                status=STATUS_DONE,
                result_path=str(resultado),
                params={"workdir": str(workdir)},
            )
        )
        store.remove("j2", _borrar)
        assert not workdir.exists()


def test_el_purgado_respeta_los_resultados_externos():
    with tempfile.TemporaryDirectory() as raiz:
        base = Path(raiz)
        cache = base / "cache"
        cache.mkdir()
        resultado = cache / "audio.mp3"
        resultado.write_bytes(b"x")
        workdir = base / "jobs" / "j3"
        workdir.mkdir(parents=True)

        store = JobStore(base / "jobs_meta")
        store.add(
            Job(
                id="j3",
                kind="download",
                status=STATUS_DONE,
                finished_at=time.time() - 10 * 24 * 3600,
                result_path=str(resultado),
                result_external=True,
                params={"workdir": str(workdir)},
            )
        )
        assert store.purge_expired(_borrar) == 1
        assert resultado.exists(), "el purgado tampoco puede tocar la caché"
        assert not workdir.exists()


def test_result_external_no_se_publica():
    """Es un detalle interno, no tiene por qué salir en la API."""
    publico = Job(id="j4", kind="download", result_external=True).public()
    assert "result_external" not in publico
    assert "result_path" not in publico


if __name__ == "__main__":
    for nombre, funcion in sorted(globals().items()):
        if nombre.startswith("test_"):
            funcion()
            print(f"OK · {nombre}")
