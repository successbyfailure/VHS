"""Comprobaciones estáticas del JavaScript del portal.

`node --check` valida la sintaxis, pero no que un método exista: una llamada a
`photoProgress.finish()` pasa el check y revienta en el navegador, que es
exactamente lo que ocurrió. Estos tests leen la plantilla y comprueban lo que
se puede comprobar sin ejecutar nada.
Se puede ejecutar con `pytest tests` o directamente con python.
"""

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

PLANTILLA = Path(__file__).resolve().parents[1] / "templates" / "index.html"
HTML = PLANTILLA.read_text(encoding="utf-8")


def _metodos_del_tracker() -> set:
    """Lo que createProgressTracker() devuelve de verdad."""
    cuerpo = HTML[HTML.index("function createProgressTracker") :]
    retorno = re.search(r"return \{([^}]*)\};", cuerpo)
    assert retorno, "no se encontró el return de createProgressTracker"
    return {nombre.strip() for nombre in retorno.group(1).split(",") if nombre.strip()}


def test_las_barras_de_progreso_solo_usan_metodos_que_existen():
    disponibles = _metodos_del_tracker()
    assert "complete" in disponibles and "reset" in disponibles, disponibles
    llamadas = re.findall(
        r"\b(\w*(?:Progress|tracker))\.(\w+)\(", HTML, flags=re.IGNORECASE
    )
    assert llamadas, "no se encontró ninguna llamada a una barra de progreso"
    malas = {
        f"{objeto}.{metodo}()"
        for objeto, metodo in llamadas
        if metodo not in disponibles
    }
    assert not malas, f"métodos que no existen en el tracker: {sorted(malas)}"


def test_cada_clave_de_traduccion_usada_esta_definida():
    """Un t('...') sin entrada sale como la propia clave en pantalla."""
    usadas = set(re.findall(r"\bt\('([a-z0-9-]+)'\)", HTML))
    definidas = set(re.findall(r"'([a-z0-9-]+)':\s*'", HTML))
    # Las que se construyen con función en vez de cadena.
    definidas |= set(re.findall(r"'([a-z0-9-]+)':\s*\(", HTML))
    faltan = {clave for clave in usadas if clave not in definidas}
    assert not faltan, f"claves usadas sin definir: {sorted(faltan)}"


def test_los_identificadores_que_busca_el_script_existen_en_el_html():
    """getElementById de algo que no está deja el panel muerto en silencio."""
    buscados = set(re.findall(r"getElementById\('([a-z0-9-]+)'\)", HTML))
    presentes = set(re.findall(r'id="([a-z0-9-]+)"', HTML))
    faltan = {identificador for identificador in buscados if identificador not in presentes}
    assert not faltan, f"getElementById sin elemento: {sorted(faltan)}"


def test_el_panel_de_fotos_esta_completo():
    for pieza in (
        'id="photo-form"',
        'id="photo-file"',
        'id="photo-model"',
        'id="photo-format"',
        'id="photo-progress"',
        'id="photo-status"',
        "/api/images/upscale/models",
        "/api/images/upscale",
    ):
        assert pieza in HTML, pieza


def test_los_formularios_del_portal_crean_trabajos():
    """Nada debe esperar ya en la propia petición."""
    assert "'/api/jobs/media'" in HTML
    assert "'/api/jobs/upload'" in HTML
    # Las rutas síncronas siguen existiendo en el servidor, pero el portal ya
    # no las usa para descargar ni convertir.
    assert "downloadFromEndpoint('/api/ffmpeg/upload'" not in HTML
    assert "downloadFromEndpoint(`/api/download`" not in HTML


if __name__ == "__main__":
    for nombre, funcion in sorted(globals().items()):
        if nombre.startswith("test_"):
            funcion()
            print(f"OK · {nombre}")
