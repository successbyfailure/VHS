"""Regresión de la cola de trabajos.

Lo que se cuida aquí es que un trabajo nunca quede en un estado que engañe al
cliente: ni "en curso" para siempre tras un reinicio, ni "listo" sin fichero.
"""

import asyncio
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from vhs import jobs  # noqa: E402


def _store():
    return jobs.JobStore(Path(tempfile.mkdtemp(prefix="vhs_jobs_test_")))


def test_un_trabajo_se_ejecuta_y_queda_listo():
    store = _store()
    queue = jobs.JobQueue(store)
    salida = Path(tempfile.mkdtemp()) / "r.mp4"
    salida.write_bytes(b"x")

    def runner(params, on_progress, should_cancel):
        on_progress(1, 2)
        on_progress(2, 2)
        return {"path": salida, "name": "r.mp4", "metadata": {"mode": "upscale"}}

    queue.register("upscale", runner)
    job = jobs.new_job("upscale", {}, estimate_seconds=12)

    async def run():
        queue.submit(job)
        for _ in range(100):
            await asyncio.sleep(0.05)
            if job.status in jobs.TERMINAL_STATUSES:
                return

    asyncio.run(run())
    assert job.status == jobs.STATUS_DONE, job.error
    assert job.segments_done == 2 and job.segments_total == 2
    assert job.public()["progress"] == 1.0
    assert job.public()["ready"] is True
    # La vista pública no debe filtrar rutas internas.
    assert "result_path" not in job.public()


def test_el_fallo_del_ejecutor_queda_como_error_legible():
    store = _store()
    queue = jobs.JobQueue(store)

    def runner(params, on_progress, should_cancel):
        raise RuntimeError("la GPU dijo no")

    queue.register("upscale", runner)
    job = jobs.new_job("upscale", {})

    async def run():
        queue.submit(job)
        for _ in range(100):
            await asyncio.sleep(0.05)
            if job.status in jobs.TERMINAL_STATUSES:
                return

    asyncio.run(run())
    assert job.status == jobs.STATUS_ERROR
    assert "la GPU dijo no" in (job.error or "")
    assert job.public()["ready"] is False


def test_cancelar_antes_de_empezar_no_ejecuta_nada():
    store = _store()
    queue = jobs.JobQueue(store)
    llamadas = []

    def runner(params, on_progress, should_cancel):
        llamadas.append(1)
        return {"path": Path("/dev/null"), "name": "x"}

    queue.register("upscale", runner)
    job = jobs.new_job("upscale", {})
    job.cancel_requested = True

    async def run():
        queue.submit(job)
        for _ in range(60):
            await asyncio.sleep(0.05)
            if job.status in jobs.TERMINAL_STATUSES:
                return

    asyncio.run(run())
    assert job.status == jobs.STATUS_CANCELLED
    assert llamadas == [], "no debía ejecutarse"


def test_un_reinicio_no_deja_trabajos_colgados():
    """Un trabajo en curso no es reanudable: debe salir como error, no eterno."""
    store = _store()
    job = jobs.new_job("upscale", {})
    job.status = jobs.STATUS_RUNNING
    store.add(job)

    recargado = jobs.JobStore(store.directory).get(job.id)
    assert recargado is not None
    assert recargado.status == jobs.STATUS_ERROR
    assert "reinició" in (recargado.error or "")


def test_el_ttl_limpia_los_terminados_y_respeta_los_vivos():
    store = _store()
    viejo = jobs.new_job("upscale", {})
    viejo.status = jobs.STATUS_DONE
    viejo.finished_at = time.time() - jobs.JOB_TTL_SECONDS - 10
    store.add(viejo)
    reciente = jobs.new_job("upscale", {})
    reciente.status = jobs.STATUS_DONE
    reciente.finished_at = time.time()
    store.add(reciente)
    en_curso = jobs.new_job("upscale", {})
    en_curso.status = jobs.STATUS_RUNNING
    store.add(en_curso)

    borrados = store.purge_expired(lambda path: None)
    assert borrados == 1
    assert store.get(viejo.id) is None
    assert store.get(reciente.id) is not None
    assert store.get(en_curso.id) is not None


if __name__ == "__main__":
    for nombre, funcion in sorted(globals().items()):
        if nombre.startswith("test_"):
            funcion()
            print(f"OK · {nombre}")
