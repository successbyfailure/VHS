#!/usr/bin/env python3
from __future__ import annotations

import os
from pathlib import Path


EXAMPLE_ENV_PATH = Path(os.environ.get("EXAMPLE_ENV_PATH", "/app/example.env"))
TARGET_ENV_PATH = Path(os.environ.get("ENV_TARGET_PATH", "/app/.env"))


def load_lines(path: Path) -> list[str]:
    if not path.exists():
        return []
    return path.read_text().splitlines()


def extract_keys(lines: list[str]) -> set[str]:
    keys: set[str] = set()
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if "=" in line:
            key = line.split("=", 1)[0].strip()
            if key:
                keys.add(key)
    return keys


def main() -> int:
    if not EXAMPLE_ENV_PATH.exists():
        print(f"example.env no encontrado en {EXAMPLE_ENV_PATH}")
        return 1

    existing_lines = load_lines(TARGET_ENV_PATH)
    existing_keys = extract_keys(existing_lines)

    new_entries: list[str] = []
    for line in load_lines(EXAMPLE_ENV_PATH):
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in line:
            continue
        key = line.split("=", 1)[0].strip()
        if key and key not in existing_keys:
            new_entries.append(line)
            existing_keys.add(key)

    if not new_entries:
        print("No hay variables nuevas para añadir.")
        return 0

    if existing_lines and existing_lines[-1].strip():
        existing_lines.append("")
    existing_lines.append("# Añadidas automáticamente desde example.env")
    existing_lines.extend(new_entries)

    TARGET_ENV_PATH.parent.mkdir(parents=True, exist_ok=True)
    TARGET_ENV_PATH.write_text("\n".join(existing_lines) + "\n")

    added_keys = ", ".join(entry.split("=", 1)[0].strip() for entry in new_entries)
    print(f"Añadidas {len(new_entries)} variable(s): {added_keys}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
