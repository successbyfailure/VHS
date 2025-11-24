# Instrucciones para colaboradores

Estas indicaciones aplican a todo el repositorio.

## Versionado
- La versión visible del servicio se define en `versions.json` (clave `"vhs"`) y se repite en el campo **Versión** del `README.md`.
- Cada cambio funcional o de configuración debe incrementar automáticamente el número de versión (parche) antes de fusionar.
- Para acelerar el proceso, ejecuta este fragmento que sincroniza `versions.json` y el `README.md` aumentando el último dígito:
  ```bash
  python - <<'PY'
  import json
  from pathlib import Path

  version_file = Path("versions.json")
  readme = Path("README.md")

  current = json.loads(version_file.read_text(encoding="utf-8"))
  major, minor, patch = map(int, current["vhs"].split("."))
  new_version = f"{major}.{minor}.{patch + 1}"
  current["vhs"] = new_version
  version_file.write_text(json.dumps(current, indent=2) + "\n", encoding="utf-8")

  readme.write_text(
      readme.read_text(encoding="utf-8").replace(
          f"**Versión**: {major}.{minor}.{patch}", f"**Versión**: {new_version}", 1
      ),
      encoding="utf-8",
  )
  print(f"Versión actualizada a {new_version}")
  PY
  ```

## Estilo y documentación
- Describe cambios y decisiones relevantes directamente en los commits y en la documentación correspondiente.
- Usa comentarios breves y claros: el idioma preferente es el español.
