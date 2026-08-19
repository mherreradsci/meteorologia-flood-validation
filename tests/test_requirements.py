"""test_requirements.py — mantiene requirements.txt como la lista real de
dependencias de producción, separada de requirements-dev.txt (pytest y
compañía). Sin esto, es fácil que una herramienta de dev se cuele en el
requirements.txt de producción (como pasó con pytest, ver el commit que
separó requirements-dev.txt), o que un import nuevo en src/ quede sin
declarar hasta que alguien clona el repo con un venv limpio y explota.

Todo acá es offline: solo lee los .txt/.py del repo y `importlib.metadata`
del venv actual, sin red ni rásters.
"""

import importlib.metadata
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent
REQUIREMENTS = REPO_ROOT / "requirements.txt"
REQUIREMENTS_DEV = REPO_ROOT / "requirements-dev.txt"
SRC = REPO_ROOT / "src"

# Paquetes que son de desarrollo/test, nunca de producción.
DEV_ONLY_PACKAGES = {"pytest", "pytest-cov", "pytest-mock", "ruff", "flake8",
                     "black", "mypy"}

# Nombre de import -> nombre de distribución pip, solo donde difieren.
IMPORT_TO_DIST = {
    "dotenv": "python-dotenv",
    "ee": "earthengine-api",
    "skimage": "scikit-image",
    "yaml": "pyyaml",
}

# Módulos propios del repo, no van en requirements.txt.
LOCAL_MODULES = {"flood_validation", "sensing"}


def _normalize(nombre: str) -> str:
    return nombre.strip().lower().replace("_", "-")


def _parse_requirement_names(path: Path) -> set[str]:
    nombres = set()
    for linea in path.read_text(encoding="utf-8").splitlines():
        linea = linea.split("#", 1)[0].strip()
        if not linea or linea.startswith("-"):
            continue
        nombre = re.split(r"[<>=!~;\[]", linea, maxsplit=1)[0]
        nombres.add(_normalize(nombre))
    return nombres


def _imports_de_src() -> set[str]:
    patron = re.compile(r"^\s*(?:import|from)\s+([A-Za-z_][A-Za-z0-9_]*)")
    encontrados = set()
    for py_file in SRC.rglob("*.py"):
        for linea in py_file.read_text(encoding="utf-8").splitlines():
            m = patron.match(linea)
            if m:
                encontrados.add(m.group(1))
    stdlib = sys.stdlib_module_names
    return {m for m in encontrados
           if m not in stdlib and m not in LOCAL_MODULES and m != "__future__"}


def test_requirements_txt_sin_duplicados():
    lineas = [l.split("#", 1)[0].strip()
             for l in REQUIREMENTS.read_text(encoding="utf-8").splitlines()]
    nombres = [_normalize(re.split(r"[<>=!~;\[]", l, maxsplit=1)[0])
              for l in lineas if l and not l.startswith("-")]
    assert len(nombres) == len(set(nombres)), (
        f"Nombres duplicados en requirements.txt: "
        f"{[n for n in set(nombres) if nombres.count(n) > 1]}")


def test_requirements_txt_no_incluye_paquetes_de_dev():
    nombres = _parse_requirement_names(REQUIREMENTS)
    filtrados = nombres & DEV_ONLY_PACKAGES
    assert not filtrados, (
        f"requirements.txt (producción) incluye paquetes de dev: "
        f"{filtrados} — van en requirements-dev.txt.")


def test_requirements_dev_incluye_a_requirements_txt():
    contenido = REQUIREMENTS_DEV.read_text(encoding="utf-8")
    assert "-r requirements.txt" in contenido, (
        "requirements-dev.txt debería incluir requirements.txt con "
        "'-r requirements.txt' en vez de duplicar las dependencias de "
        "producción a mano.")


def test_requirements_dev_declara_pytest():
    nombres = _parse_requirement_names(REQUIREMENTS_DEV)
    assert "pytest" in nombres


def test_imports_de_src_estan_declarados_en_requirements_txt():
    declarados = _parse_requirement_names(REQUIREMENTS)
    usados = _imports_de_src()
    faltantes = {IMPORT_TO_DIST.get(m, m) for m in usados}
    faltantes = {_normalize(n) for n in faltantes} - declarados
    assert not faltantes, (
        f"src/ importa paquetes que no están en requirements.txt: "
        f"{faltantes}")


def test_cada_dependencia_de_requirements_txt_esta_instalada():
    faltantes = []
    for nombre in _parse_requirement_names(REQUIREMENTS):
        try:
            importlib.metadata.version(nombre)
        except importlib.metadata.PackageNotFoundError:
            faltantes.append(nombre)
    assert not faltantes, (
        f"requirements.txt declara paquetes no instalados en este venv "
        f"(¿typo, o falta `pip install -r requirements.txt`?): {faltantes}")
