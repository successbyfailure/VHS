"""Regresión de la limpieza aplicada a la salida del LLM de traducción.

Los subtítulos son texto plano: los modelos pequeños cuelan énfasis Markdown o
una coletilla explicativa aunque el prompt lo prohíba. Se puede ejecutar con
`pytest tests` o directamente con `python tests/test_translation_sanitizer.py`.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from vhs.main import _sanitize_translation  # noqa: E402

CASOS = [
    # Énfasis con asteriscos: se elimina el delimitador, se conserva el texto.
    ("Lijas e imprimación (o *filler primer*, o *aparejo* de alto espesor).",
     "Lijas e imprimación (o filler primer, o aparejo de alto espesor)."),
    ("Hoy te enseño cómo lograr este acabado en **PLA**.",
     "Hoy te enseño cómo lograr este acabado en PLA."),
    ("***Muy importante***.", "Muy importante."),
    # Coletilla final del modelo: se descarta.
    ("Hoy te enseño esto.\n\n(Nota: el texto original contiene un error.)",
     "Hoy te enseño esto."),
    ("Hoy te enseño esto.\n\nNote: this is a comment.", "Hoy te enseño esto."),
    # Texto legítimo: no se toca.
    ("Déjalo secar 15 minutos entre capas.", "Déjalo secar 15 minutos entre capas."),
    ("El precio es 3 * 4 = 12 euros.", "El precio es 3 * 4 = 12 euros."),
    ("Presiona 5*5 en la calculadora.", "Presiona 5*5 en la calculadora."),
    # El guion bajo se respeta siempre: identificadores y nombres de fichero.
    ("Usa el archivo config_final_v2.json.", "Usa el archivo config_final_v2.json."),
    ("La variable __init__ y snake_case_name se mantienen.",
     "La variable __init__ y snake_case_name se mantienen."),
    ("Esto es _muy_ importante.", "Esto es _muy_ importante."),
]


def test_sanitize_translation():
    for entrada, esperado in CASOS:
        obtenido = _sanitize_translation(entrada)
        assert obtenido == esperado, f"{entrada!r} -> {obtenido!r} (esperado {esperado!r})"


if __name__ == "__main__":
    test_sanitize_translation()
    print(f"OK · {len(CASOS)} casos")
