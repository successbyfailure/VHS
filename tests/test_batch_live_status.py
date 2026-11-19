"""Estado vivo del lote: por qué un trabajo lleva un minuto sin avanzar.

oCabra sirve `BatchLiveStatus` en el campo `ocabra` mientras el lote no ha
terminado. Sin él, el portal se quedaba en 0% durante la carga del modelo sin
poder explicarlo. Se puede ejecutar con `pytest tests` o directamente con python.
"""

import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from vhs import main  # noqa: E402
from vhs.jobs import Job  # noqa: E402


def _lote(status="in_progress", completed=0, total=36, ocabra=None):
    return SimpleNamespace(
        id="batch_1",
        status=status,
        request_counts=SimpleNamespace(
            **{"total": total, "completed": completed, "failed": 0}
        ),
        ocabra=ocabra,
    )


def _publico(params):
    return Job(id="j1", kind="transcription_batch", params=params).public()


def test_la_carga_del_modelo_se_explica():
    """El caso que motivó esto: 65 s en 0 completadas sin decir por qué."""
    params = {"progress_done": 1, "progress_total": 4}
    cambio = main._apply_batch_live_status(
        params,
        _lote(
            ocabra={
                "phase": "loading_model",
                "detail": "Loading the model.",
                "queue_position": None,
                "in_flight": 0,
                "waiting": 4,
                "models": [
                    {"model": "gemma4:26b", "status": "loading", "expected_wait_seconds": 48}
                ],
            }
        ),
    )
    assert cambio is True
    publico = _publico(params)
    assert publico["live_phase"] == "loading_model"
    assert publico["expected_wait_seconds"] == 48
    assert publico["detail"] == "Loading the model."
    assert publico["batch_completed"] == 0
    assert publico["batch_total"] == 36


def test_la_cola_dice_cuantos_van_delante():
    params = {}
    main._apply_batch_live_status(
        params,
        _lote(ocabra={"phase": "queued", "detail": "Queued.", "queue_position": 2}),
    )
    publico = _publico(params)
    assert publico["live_phase"] == "queued"
    assert publico["queue_position"] == 2


def test_el_contador_de_peticiones_avanza():
    params = {}
    main._apply_batch_live_status(params, _lote(completed=22))
    assert _publico(params)["batch_completed"] == 22


def test_al_terminar_el_estado_vivo_se_limpia():
    """oCabra deja de servirlo, y el portal no debe seguir mostrándolo."""
    params = {}
    main._apply_batch_live_status(
        params, _lote(ocabra={"phase": "running", "detail": "Serving 4 line(s)."})
    )
    assert _publico(params)["live_phase"] == "running"
    main._apply_batch_live_status(
        params, _lote(status="completed", completed=36, ocabra=None)
    )
    publico = _publico(params)
    assert publico["live_phase"] == ""
    assert publico["detail"] == ""
    assert publico["batch_completed"] == 36


def test_solo_se_persiste_cuando_algo_cambia():
    """El sondeo es cada 2 s: reescribir el job en cada vuelta es gratuito."""
    params = {}
    lote = _lote(ocabra={"phase": "running", "detail": "Serving 4 line(s)."})
    assert main._apply_batch_live_status(params, lote) is True
    assert main._apply_batch_live_status(params, lote) is False


def test_un_lote_sin_extension_no_rompe_nada():
    """Contra api.openai.com no hay campo `ocabra`."""
    params = {}
    main._apply_batch_live_status(params, _lote(ocabra=None))
    publico = _publico(params)
    assert publico["live_phase"] == ""
    assert publico["batch_total"] == 36


def test_el_checkpoint_recuerda_el_progreso():
    """Hace falta para republicar el estado vivo sin perder la fase local."""
    params = {}
    vistos = []
    main._batch_checkpoint(params, lambda d, t: vistos.append((d, t)), "translating", 3, 4)
    assert params["phase"] == "translating"
    assert (params["progress_done"], params["progress_total"]) == (3, 4)
    assert vistos == [(3, 4)]


def test_wait_for_batch_publica_en_cada_sondeo():
    class _Lotes:
        def __init__(self):
            self.estados = ["in_progress", "completed"]

        def retrieve(self, _batch_id):
            estado = self.estados.pop(0)
            return _lote(
                status=estado,
                completed=0 if estado == "in_progress" else 36,
                ocabra={"phase": "running", "detail": "Serving 4 line(s)."}
                if estado == "in_progress"
                else None,
            )

    cliente = SimpleNamespace(batches=_Lotes())
    vistos = []
    main._wait_for_batch(cliente, "batch_1", lambda: False, vistos.append)
    assert len(vistos) == 2, vistos


if __name__ == "__main__":
    for nombre, funcion in sorted(globals().items()):
        if nombre.startswith("test_"):
            funcion()
            print(f"OK · {nombre}")
