"""susceptibility.py — localiza y carga el producto de susceptibilidad de
`meteorologia-flood-projections` (repo hermano) para compararlo contra la
capa de anegamiento real fusionada.

El producto no es un archivo único: cada corrida del pronóstico (GFS cada
6h, IFS aparte) produce su propio raster binario bajo
`outputs/<region>/<sufijo>/`, nombrado
`mapa_anegamientos_<sufijo>_extension_<AAAAMMDD>_<HH>utc_<timestamp-local>.tif`
— confirmado contra archivos reales del repo hermano en la Fase 0 (§16.1
del plan), no adivinado. No vive en este repo: `source_root` en
`regions.yaml` apunta a esa carpeta por ruta relativa, asumiendo que los
dos repos están clonados como hermanos (igual supuesto que ya documenta
`config/regions.yaml`).

Formato confirmado en Fase 0: GeoTIFF de una banda, uint8, EPSG:4326,
nodata=255, valores binarios {0,1} — no hay nada continuo que threshold-
sweepear (ver §7 del plan, revisado). Cada raster proyecta anegamiento
desde lluvia acumulada de 72 h *a partir* de la hora del ciclo
(`PROJECTION_HORIZON`), así que "el ciclo que corresponde" a una ventana de
validación es cualquiera cuya ventana de 72h se solape con ella, no una
fecha única — `find_cycles` devuelve todos los que califican, ordenados
del más reciente al más antiguo; `resolve_susceptibility` usa el más
reciente por default (el de lead time más corto, en general el más
preciso), dejando la comparación multi-ciclo por lead time (la idea de
"sweep" del §7 revisado) para cuando el reporte de la Fase 6/7 la necesite
— el motor de métricas de esta fase compara un ciclo por vez, no exige
elegir uno solo para siempre.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np

PROJECTION_HORIZON = timedelta(hours=72)
# mapa_anegamientos_gfs_extension_20260730_18utc_20260730-190146.tif
_NAME_RE = re.compile(
    r"mapa_anegamientos_(?P<sufijo>\w+)_extension_"
    r"(?P<fecha>\d{8})_(?P<hora>\d{2})utc_(?P<local>\d{8}-\d{6})\.tif$")
# Margen del clip, igual convención que sar_layer/optical_layer/terrain.
CLIP_PAD_DEG = 0.02


@dataclass
class SusceptibilityCycle:
    path: Path
    sufijo: str
    cycle_utc: datetime | None  # None si vino de --susceptibility (ruta explícita)

    @property
    def window_end_utc(self) -> datetime | None:
        return None if self.cycle_utc is None else self.cycle_utc + PROJECTION_HORIZON


def _parse_cycle(path: Path) -> SusceptibilityCycle | None:
    m = _NAME_RE.search(path.name)
    if not m:
        return None
    cycle = datetime.strptime(f"{m['fecha']}{m['hora']}", "%Y%m%d%H").replace(
        tzinfo=timezone.utc)
    return SusceptibilityCycle(path=path, sufijo=m["sufijo"], cycle_utc=cycle)


def find_cycles(source_root: Path, sufijo: str, start: datetime,
                end: datetime) -> list[SusceptibilityCycle]:
    """Todos los ciclos de `sufijo` bajo `source_root/<sufijo>/` cuya
    ventana de proyección (ciclo a ciclo+72h) se solapa con [start, end],
    del más reciente al más antiguo. Lista vacía si el directorio no
    existe o no hay ninguno que califique — no es un error, es la
    resolución normal de "no hay pronóstico para esta fecha todavía"."""
    dir_ = Path(source_root) / sufijo
    if not dir_.is_dir():
        return []
    ciclos = []
    for p in dir_.glob(f"mapa_anegamientos_{sufijo}_extension_*.tif"):
        c = _parse_cycle(p)
        if c is None or c.cycle_utc is None:
            continue
        if c.cycle_utc <= end and c.window_end_utc >= start:
            ciclos.append(c)
    return sorted(ciclos, key=lambda c: c.cycle_utc, reverse=True)


def resolve_susceptibility(
        *, explicit_path: Path | None, source_root: str | None,
        sufijo_preferido: str, start: datetime, end: datetime,
        base_dir: Path | None = None) -> SusceptibilityCycle | None:
    """`explicit_path` (--susceptibility) gana siempre, sin importar
    `source_root`. Si no se dio, resuelve el ciclo más reciente de
    `sufijo_preferido` bajo `source_root` cuya ventana se solape con
    [start, end]. `base_dir` resuelve `source_root` si es relativo (por
    default, el directorio desde el que se invoca el proceso — mismo
    quirk documentado para OUTPUT_DIR en flood_monitor.py/CLAUDE.md).
    None si no hay ruta explícita ni ningún ciclo que califique."""
    if explicit_path is not None:
        return SusceptibilityCycle(path=Path(explicit_path), sufijo="explicit",
                                   cycle_utc=None)
    if not source_root:
        return None
    root = Path(source_root)
    if not root.is_absolute():
        root = (base_dir or Path.cwd()) / root
    ciclos = find_cycles(root, sufijo_preferido, start, end)
    return ciclos[0] if ciclos else None


def load_susceptibility(path: Path, template, bbox) -> np.ndarray | None:
    """Lee el raster binario y lo rasteriza (nearest — es categórico, no
    continuo) sobre la grilla de `template`. None si el archivo no existe
    o no se puede leer — mismo fallo suave que las capas de sensor y las
    máscaras de plausibilidad."""
    if not Path(path).exists():
        print(f"[!] Susceptibilidad: no existe {path}.")
        return None
    try:
        import rioxarray
        import xarray as xr
        from rasterio.enums import Resampling

        raw = rioxarray.open_rasterio(path, masked=True)
        assert isinstance(raw, xr.DataArray)
        da = raw.squeeze("band", drop=True) if "band" in raw.dims else raw
        pbbox = (bbox[0] - CLIP_PAD_DEG, bbox[1] - CLIP_PAD_DEG,
                 bbox[2] + CLIP_PAD_DEG, bbox[3] + CLIP_PAD_DEG)
        da = da.rio.clip_box(*pbbox, crs="EPSG:4326")
        reproj = da.rio.reproject_match(template, resampling=Resampling.nearest)
        mask = np.isfinite(reproj.values) & (reproj.values > 0.5)
        print(f"[+] Susceptibilidad cargada desde {path}: "
              f"{mask.sum():,} px susceptibles ({100 * mask.mean():.2f}% "
              f"del AOI)")
        return mask
    except Exception as e:  # noqa: BLE001
        print(f"[!] No pude cargar la susceptibilidad desde {path} ({e}).")
        return None
