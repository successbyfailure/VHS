"""Cola de trabajos para operaciones que no caben en una petición HTTP.

El nivel rápido de escalado va a ~0,65x el tiempo real y encaja síncrono, pero
el de difusión va a ~21x: un vídeo de 10 minutos dejaría la petición abierta
más de tres horas. Esto lo convierte en un trabajo con estado consultable.

Decisiones que conviene no revertir sin pensarlo:

* **Un trabajo a la vez.** El cuello es la GPU; lanzar dos en paralelo solo
  hace que se pisen la VRAM y que ambos vayan más lentos. La cola es serie.
* **Los metadatos se persisten en disco.** El resultado ya vive en disco, así
  que perder el índice en un reinicio sería tirar trabajo hecho por nada.
* **No se cancela a mitad del segmento en curso.** Cancelar deja el trabajo
  marcado y se para al terminar el segmento, en vez de matar un proceso de GPU
  y arriesgar dejar la tarjeta en mal estado.
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, Optional

from fastapi.concurrency import run_in_threadpool

# Los trabajos terminados se conservan este tiempo para que el cliente pueda
# recoger el resultado aunque cierre la pestaña y vuelva más tarde.
JOB_TTL_SECONDS = 24 * 3600

STATUS_QUEUED = "queued"
STATUS_RUNNING = "running"
STATUS_DONE = "done"
STATUS_ERROR = "error"
STATUS_CANCELLED = "cancelled"
TERMINAL_STATUSES = {STATUS_DONE, STATUS_ERROR, STATUS_CANCELLED}


@dataclass
class Job:
    id: str
    kind: str
    status: str = STATUS_QUEUED
    created_at: float = field(default_factory=time.time)
    started_at: Optional[float] = None
    finished_at: Optional[float] = None
    segments_done: int = 0
    segments_total: int = 0
    estimate_seconds: float = 0.0
    params: Dict[str, Any] = field(default_factory=dict)
    metadata: Dict[str, Any] = field(default_factory=dict)
    error: Optional[str] = None
    result_path: Optional[str] = None
    result_name: Optional[str] = None
    cancel_requested: bool = False

    def public(self) -> Dict[str, Any]:
        """Vista para el cliente: sin rutas internas del sistema de ficheros."""
        data = asdict(self)
        data.pop("result_path", None)
        data.pop("cancel_requested", None)
        elapsed = (self.finished_at or time.time()) - (self.started_at or self.created_at)
        data["elapsed_seconds"] = round(max(0.0, elapsed), 1)
        if self.segments_total:
            data["progress"] = round(self.segments_done / self.segments_total, 3)
        else:
            data["progress"] = 0.0
        data["ready"] = self.status == STATUS_DONE
        return data


class JobStore:
    """Índice de trabajos en memoria, respaldado en disco."""

    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self.directory.mkdir(parents=True, exist_ok=True)
        self._jobs: Dict[str, Job] = {}
        self._load()

    def _path(self, job_id: str) -> Path:
        return self.directory / f"{job_id}.json"

    def _load(self) -> None:
        for entry in self.directory.glob("*.json"):
            try:
                data = json.loads(entry.read_text(encoding="utf-8"))
                job = Job(**data)
            except (OSError, TypeError, ValueError, json.JSONDecodeError):
                continue
            # Un trabajo que estaba en marcha cuando el proceso murió no puede
            # reanudarse: se marca como error en vez de dejarlo colgado.
            if job.status in {STATUS_QUEUED, STATUS_RUNNING}:
                job.status = STATUS_ERROR
                job.error = "El servicio se reinició mientras el trabajo estaba en curso"
                job.finished_at = time.time()
            self._jobs[job.id] = job

    def persist(self, job: Job) -> None:
        tmp = self._path(job.id).with_suffix(".json.tmp")
        try:
            tmp.write_text(json.dumps(asdict(job), ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(self._path(job.id))
        except OSError:
            tmp.unlink(missing_ok=True)

    def add(self, job: Job) -> None:
        self._jobs[job.id] = job
        self.persist(job)

    def get(self, job_id: str) -> Optional[Job]:
        return self._jobs.get(job_id)

    def list(self, limit: int = 50) -> list[Job]:
        return sorted(self._jobs.values(), key=lambda j: j.created_at, reverse=True)[:limit]

    def purge_expired(self, cleanup: Callable[[Path], None]) -> int:
        removed = 0
        now = time.time()
        for job in list(self._jobs.values()):
            if job.status not in TERMINAL_STATUSES:
                continue
            if now - (job.finished_at or job.created_at) < JOB_TTL_SECONDS:
                continue
            if job.result_path:
                cleanup(Path(job.result_path).parent)
            self._path(job.id).unlink(missing_ok=True)
            self._jobs.pop(job.id, None)
            removed += 1
        return removed


class JobQueue:
    """Ejecuta los trabajos de uno en uno, con el trabajador arrancado en caliente."""

    def __init__(self, store: JobStore) -> None:
        self.store = store
        self._queue: asyncio.Queue[str] = asyncio.Queue()
        self._task: Optional[asyncio.Task] = None
        self._runners: Dict[str, Callable[..., Any]] = {}

    def register(self, kind: str, runner: Callable[..., Any]) -> None:
        self._runners[kind] = runner

    def submit(self, job: Job) -> None:
        self.store.add(job)
        self._queue.put_nowait(job.id)
        self._ensure_worker()

    def _ensure_worker(self) -> None:
        # Arranque perezoso: VHS no tiene hooks de arranque y así se evita
        # tocar la construcción de la app solo por esto.
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._worker(), name="vhs-job-worker")

    def pending(self) -> int:
        return self._queue.qsize()

    async def _worker(self) -> None:
        while True:
            try:
                job_id = await asyncio.wait_for(self._queue.get(), timeout=300)
            except asyncio.TimeoutError:
                # Sin trabajo pendiente el trabajador se retira; volverá a
                # arrancar solo en el siguiente envío.
                return
            job = self.store.get(job_id)
            if job is None:
                continue
            if job.cancel_requested:
                job.status = STATUS_CANCELLED
                job.finished_at = time.time()
                self.store.persist(job)
                continue
            await self._run(job)

    async def _run(self, job: Job) -> None:
        runner = self._runners.get(job.kind)
        if runner is None:
            job.status = STATUS_ERROR
            job.error = f"No hay ejecutor registrado para '{job.kind}'"
            job.finished_at = time.time()
            self.store.persist(job)
            return

        job.status = STATUS_RUNNING
        job.started_at = time.time()
        self.store.persist(job)

        def on_progress(done: int, total: int) -> None:
            job.segments_done, job.segments_total = done, total
            self.store.persist(job)

        def should_cancel() -> bool:
            return job.cancel_requested

        try:
            result = await run_in_threadpool(
                runner, job.params, on_progress, should_cancel
            )
        except Exception as exc:  # el ejecutor decide qué es recuperable
            job.status = STATUS_ERROR
            job.error = str(exc)
        else:
            if job.cancel_requested:
                job.status = STATUS_CANCELLED
            else:
                job.status = STATUS_DONE
                job.result_path = str(result["path"])
                job.result_name = result.get("name")
                job.metadata = result.get("metadata", {})
        job.finished_at = time.time()
        self.store.persist(job)


def new_job(kind: str, params: Dict[str, Any], *, estimate_seconds: float = 0.0) -> Job:
    return Job(
        id=uuid.uuid4().hex[:16],
        kind=kind,
        params=params,
        estimate_seconds=round(estimate_seconds, 1),
    )
