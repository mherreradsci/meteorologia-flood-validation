#!/usr/bin/env python3
"""cli.py — argparse de flood_validation.

Sigue la convención de flood_monitor.py/list_s1_items.py: --aoi/--bbox/
--place mutuamente excluyentes con los mismos nombres y defaults, más los
flags propios de validación (ventana con inicio y fin, --susceptibility,
--output-dir/--config-dir resueltos por ruta absoluta y no por cwd, y
--dry-run). --sensors/--awei-variant/--hand-threshold/--confidence-threshold/
--cache-dir quedan afuera hasta las fases que realmente los usan (2-4): no
tiene sentido un flag sin comportamiento detrás.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from sensing import DEFAULT_REGION

# Resueltos contra la ubicación del paquete, no contra el cwd: a diferencia
# de OUTPUT_DIR en flood_monitor.py (relativo al directorio desde el que se
# invoca python), estos defaults no cambian según desde dónde se corra el
# comando.
REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG_DIR = REPO_ROOT / "config"
DEFAULT_OUTPUT_DIR = REPO_ROOT / "output" / "validation"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Valida el producto de susceptibilidad de anegamiento "
                     "contra una capa de anegamiento real estimada con "
                     "sensores remotos públicos (Sentinel-1, Sentinel-2, "
                     "Dynamic World opcional).")
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--aoi", type=Path, help="GeoJSON con el área de interés")
    g.add_argument("--bbox", nargs=4, type=float,
                   metavar=("XMIN", "YMIN", "XMAX", "YMAX"),
                   help="Bounding box en lon/lat (EPSG:4326)")
    g.add_argument("--place", type=str,
                   help="Punto de interés por nombre (ej: 'Punitaqui'), "
                        "geocodificado con Nominatim/OSM dentro de --region")
    p.add_argument("--region", type=str, default=DEFAULT_REGION,
                   help=f"Contexto geográfico para --place, y clave de "
                        f"config/regions.yaml (default: '{DEFAULT_REGION}')")
    p.add_argument("--buffer-km", type=float, default=5.0,
                   help="Margen en km alrededor del lugar geocodificado "
                        "(default: 5)")
    p.add_argument("--start-date-utc", type=str, default=None,
                   help="Inicio de la ventana de validación (YYYY-MM-DD o "
                        "ISO 8601), interpretado en UTC salvo --local-time. "
                        "Si se omite, se calcula como --end-date-utc menos "
                        "--days.")
    p.add_argument("--end-date-utc", type=str, default=None,
                   help="Fin de la ventana de validación (YYYY-MM-DD o "
                        "ISO 8601), interpretado en UTC salvo --local-time. "
                        "Default: ahora.")
    p.add_argument("--local-time", action="store_true",
                   help="Interpreta --start-date-utc/--end-date-utc en la "
                        "zona horaria local de esta máquina en vez de UTC. "
                        "Un offset explícito en la fecha tiene prioridad.")
    p.add_argument("--days", type=int, default=10,
                   help="Si se omite --start-date-utc, largo de la ventana "
                        "en días hacia atrás desde --end-date-utc "
                        "(default: 10, igual que flood_monitor.py)")
    p.add_argument("--susceptibility", type=Path, default=None,
                   help="Ruta explícita a un raster de susceptibilidad "
                        "(.tif) de meteorologia-flood-projections. Si se "
                        "omite, se resuelve automáticamente desde "
                        "regions.yaml según el ciclo de pronóstico que "
                        "cubre la ventana (Fase 5).")
    p.add_argument("--config-dir", type=Path, default=DEFAULT_CONFIG_DIR,
                   help=f"Directorio con regions.yaml y validation.yaml "
                        f"(default: {DEFAULT_CONFIG_DIR})")
    p.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR,
                   help=f"Directorio de salida (default: "
                        f"{DEFAULT_OUTPUT_DIR})")
    # Mismos nombres y defaults que flood_monitor.py: sar_layer.py envuelve
    # water_threshold/detect_flood/slope_mask directamente, así que estos
    # tres valores tienen que viajar con la misma semántica.
    p.add_argument("--threshold", type=float, default=None,
                   help="Umbral fijo en dB para VH (ej: -18), aplicado a "
                        "cada escena de la ventana. Si se omite, cada "
                        "escena usa su propio umbral de Otsu.")
    p.add_argument("--min-area-px", type=int, default=20,
                   help="Área mínima (píxeles) para conservar un parche de "
                        "agua, por escena antes de la unión (default: 20)")
    p.add_argument("--max-slope", type=float, default=5.0,
                   help="Pendiente máxima en grados (Copernicus DEM): "
                        "píxeles más empinados se descartan como falsos "
                        "positivos. 0 desactiva. Default: 5")
    p.add_argument("--awei-variant", choices=["nsh", "sh", "both"],
                   default=None,
                   help="Variante de AWEI para la capa Sentinel-2: 'sh' "
                        "trae término de sombra (relevante en el terreno "
                        "montañoso de Coquimbo), 'nsh' no, 'both' cuenta "
                        "cualquiera de las dos como evidencia. Si se "
                        "omite, usa el default de la región en "
                        "regions.yaml.")
    p.add_argument("--dry-run", action="store_true",
                   help="Resuelve AOI/ventana/config y escribe el "
                        "manifiesto de la corrida, sin procesar rásters. "
                        "Única acción implementada por ahora (Fase 1 — "
                        "fundamentos).")
    return p.parse_args(argv)
