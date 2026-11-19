"""Reintentar un trabajo por lotes sin repetir lo que ya terminó.

Un lote cancelado o caducado en oCabra dejaba el trabajo muerto: el pipeline
se quedaba esperando un lote que ya no se puede consultar y no había forma de
reintentarlo. Se puede ejecutar con `pytest tests` o directamente con python.
"""

import hashlib
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient  # noqa: E402

import vhs.main as main  # noqa: E402
from vhs.jobs import Job  # noqa: E402


class _Batches:
    """Devuelve el estado que se le diga para cada identificador."""

    def __init__(self, estados):
        self.estados = estados
        self.consultados = []

    def retrieve(self, batch_id):
        self.consultados.append(batch_id)
        if batch_id not in self.estados:
            raise RuntimeError("no existe")
        return SimpleNamespace(id=batch_id, status=self.estados[batch_id])


def _con_cliente(estados):
    """Sustituye el cliente de oCabra y devuelve cómo restaurarlo."""
    lotes = _Batches(estados)
    original = main.create_openai_client
    main.create_openai_client = lambda: SimpleNamespace(batches=lotes)
    return lotes, original


def test_un_lote_cancelado_se_descarta_y_se_rehace():
    params = {
        "stage": "translating",
        "transcription_batch_id": "b_stt",
        "translation_batch_id": "b_trad",
        "live_phase": "running",
        "phase": "translating",
    }
    lotes, original = _con_cliente({"b_stt": "completed", "b_trad": "cancelled"})
    try:
        rehacer = main.reset_batch_job_for_retry(params)
    finally:
        main.create_openai_client = original
    assert rehacer == ["translation_batch_id"], rehacer
    # El de transcripción terminó bien: no se repite trabajo ya pagado.
    assert params["transcription_batch_id"] == "b_stt"
    assert "translation_batch_id" not in params
    assert params["stage"] == "transcribed"
    # El estado vivo del lote viejo no debe quedarse pegado.
    assert "live_phase" not in params
    assert "phase" not in params


def test_un_lote_que_ya_no_existe_tambien_se_descarta():
    """Si el lote no se puede consultar, esperarlo es esperar para siempre."""
    params = {"stage": "transcribing", "transcription_batch_id": "b_perdido"}
    lotes, original = _con_cliente({})
    try:
        rehacer = main.reset_batch_job_for_retry(params)
    finally:
        main.create_openai_client = original
    assert rehacer == ["transcription_batch_id"]
    assert params["stage"] == "prepare"


def test_con_los_dos_lotes_malos_se_vuelve_al_principio():
    params = {
        "stage": "translating",
        "transcription_batch_id": "b_stt",
        "translation_batch_id": "b_trad",
    }
    lotes, original = _con_cliente({"b_stt": "expired", "b_trad": "cancelled"})
    try:
        rehacer = main.reset_batch_job_for_retry(params)
    finally:
        main.create_openai_client = original
    assert len(rehacer) == 2, rehacer
    assert params["stage"] == "prepare"


def test_si_todo_termino_bien_no_se_rehace_nada():
    params = {
        "stage": "translated",
        "transcription_batch_id": "b_stt",
        "translation_batch_id": "b_trad",
    }
    lotes, original = _con_cliente({"b_stt": "completed", "b_trad": "completed"})
    try:
        rehacer = main.reset_batch_job_for_retry(params)
    finally:
        main.create_openai_client = original
    assert rehacer == []
    assert params["stage"] == "translated"


def _sesion(cliente):
    cliente.get("/")
    token = cliente.cookies.get(main.SESSION_COOKIE_NAME)
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _job(owner, status="error", kind="transcription_batch"):
    return Job(
        id="retry001",
        kind=kind,
        owner_id=owner,
        status=status,
        error="El lote batch_x terminó en estado cancelled",
        params={"stage": "translating", "translation_batch_id": "b_trad"},
    )


def test_el_endpoint_reencola_el_trabajo():
    lotes, original = _con_cliente({"b_trad": "cancelled"})
    try:
        with TestClient(main.app) as cliente:
            owner = _sesion(cliente)
            main._job_store.add(_job(owner))
            respuesta = cliente.post("/api/jobs/retry001/retry")
            assert respuesta.status_code == 200, respuesta.text
            cuerpo = respuesta.json()
            assert cuerpo["status"] == "queued"
            assert cuerpo["resend"] == ["translation_batch_id"], cuerpo
            # No se comprueba el estado después: el trabajador lo recoge de
            # inmediato y aquí no hay audio que procesar, así que volvería a
            # fallar. Lo que importa es que quedó encolado y sin el lote
            # inservible.
            trabajo = main._job_store.get("retry001")
            assert trabajo.error is None or trabajo.status != "queued"
            assert "translation_batch_id" not in trabajo.params
            assert trabajo.params["stage"] == "transcribed"
            main._job_store.remove("retry001", lambda ruta: None)
    finally:
        main.create_openai_client = original


def test_no_se_reintenta_un_trabajo_terminado_bien():
    with TestClient(main.app) as cliente:
        owner = _sesion(cliente)
        main._job_store.add(_job(owner, status="done"))
        respuesta = cliente.post("/api/jobs/retry001/retry")
        assert respuesta.status_code == 409, respuesta.text
        main._job_store.remove("retry001", lambda ruta: None)


def test_no_se_reintenta_el_trabajo_de_otra_sesion():
    """La cookie no es una cuenta, pero sí aísla los trabajos."""
    with TestClient(main.app) as cliente:
        _sesion(cliente)
        main._job_store.add(_job("otro-propietario"))
        respuesta = cliente.post("/api/jobs/retry001/retry")
        assert respuesta.status_code == 404, respuesta.text
        main._job_store.remove("retry001", lambda ruta: None)


def test_solo_se_reintentan_los_trabajos_por_lotes():
    with TestClient(main.app) as cliente:
        owner = _sesion(cliente)
        main._job_store.add(_job(owner, kind="upscale"))
        respuesta = cliente.post("/api/jobs/retry001/retry")
        assert respuesta.status_code == 400, respuesta.text
        main._job_store.remove("retry001", lambda ruta: None)


if __name__ == "__main__":
    for nombre, funcion in sorted(globals().items()):
        if nombre.startswith("test_"):
            funcion()
            print(f"OK · {nombre}")
