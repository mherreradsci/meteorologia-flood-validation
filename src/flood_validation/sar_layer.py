"""sar_layer.py — capa de "anegamiento real" a partir de Sentinel-1 RTC, por
unión de todas las escenas dentro de la ventana de validación (no un solo
par antes/después, como el `--change` de flood_monitor.py).

Envuelve `read_vh_db`/`water_threshold`/`detect_flood`/`permanent_water_mask`/
`slope_mask` de flood_monitor.py en vez de reimplementarlos (ver §4/§6 del
plan): cada escena se detecta por separado contra una grilla de referencia
compartida (la primera escena de la ventana; agua permanente y pendiente se
calculan UNA vez contra esa grilla, no por escena), y el resultado final es
la unión (OR lógico) de todas. Si una escena individual falla al leerse
(asset caído, formato raro), se salta con un aviso y se sigue con las demás
— degradación gradual a nivel de escena, coherente con FR3. Si ninguna
escena cae en la ventana, o ninguna se puede leer, el sensor entero se
reporta como no disponible (`None`) en vez de abortar la corrida —
consistente con cómo `permanent_water_mask`/`slope_mask` ya fallan suave.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

import numpy as np

from sensing import (EPOCH, aoi_grid_mask, detect_flood, log,
                     permanent_water_mask, read_vh_db, slope_mask,
                     stac_catalog, water_threshold)


@dataclass
class Acquisition:
    """Metadata reportable de una escena que sí se pudo usar — lo que le
    permite a un manifiesto/reporte decir contra qué evidencia concreta se
    armó la capa, no solo un número final."""
    item_id: str
    datetime_utc: str
    relative_orbit: int | None
    orbit_state: str | None
    threshold_db: float


@dataclass
class SarLayerResult:
    flood: np.ndarray
    template: object  # DataArray de rioxarray; sin type hint fuerte para no forzar el import a nivel de módulo
    acquisitions: list[Acquisition] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)


def search_s1_window(geom: dict, start: datetime, end: datetime) -> list:
    """Todas las escenas Sentinel-1 RTC que intersectan `geom` dentro de
    [start, end], de más antigua a más nueva.

    A diferencia de `search_latest_s1` de flood_monitor.py (una sola
    escena, ventana de N días hacia atrás desde un corte), acá la ventana
    ya viene resuelta por windows.resolve_window y se usan TODAS las
    escenas que caen en ella: esta fase hace unión, no elige una.
    """
    search = stac_catalog().search(
        collections=["sentinel-1-rtc"],
        intersects=geom,
        datetime=f"{start:%Y-%m-%dT%H:%M:%SZ}/{end:%Y-%m-%dT%H:%M:%SZ}",
    )
    return sorted(search.item_collection(),
                 key=lambda it: it.datetime or EPOCH)


def build_real_flood_layer(
        geom: dict, bbox, start: datetime, end: datetime, *,
        threshold: float | None = None, min_area_px: int = 20,
        max_slope: float = 5.0) -> SarLayerResult | None:
    """Arma la capa de anegamiento real de Sentinel-1 para la ventana.

    Devuelve None si el sensor no está disponible para este AOI/ventana
    (sin escenas, o ninguna legible) — no es un error, es la degradación
    esperada que FR3 pide reportar en vez de silenciar.
    """
    items = search_s1_window(geom, start, end)
    if not items:
        log(f"[!] Sentinel-1: sin escenas entre {start:%Y-%m-%d} y "
              f"{end:%Y-%m-%d} para este AOI.")
        return None
    log(f"[+] Sentinel-1: {len(items)} escena(s) en la ventana.")

    template = None
    perm_mask = steep_mask = aoi_mask = None
    union = None
    acquisitions: list[Acquisition] = []
    skipped: list[str] = []

    for item in items:
        props = item.properties
        log(f"[+] Sentinel-1 {item.id}: "
              f"{(item.datetime or EPOCH):%Y-%m-%d %H:%M:%S} UTC "
              f"(órbita relativa {props.get('sat:relative_orbit')}, "
              f"{props.get('sat:orbit_state')})")
        try:
            vh_db = read_vh_db(item, bbox)
            if template is None:
                # Primera escena legible: define la grilla de referencia.
                # Agua permanente y pendiente se calculan una sola vez
                # contra ella, no por escena — son propiedades del
                # terreno, no de la imagen.
                template = vh_db
                aoi_mask = aoi_grid_mask(geom, template)
                log(f"[+] AOI real (polígono sobre la grilla): "
                      f"{aoi_mask.sum():,} px")
                perm_mask = permanent_water_mask(geom, bbox, template)
                steep_mask = slope_mask(geom, bbox, template, max_slope)
            else:
                from rasterio.enums import Resampling

                vh_db = vh_db.rio.reproject_match(
                    template, resampling=Resampling.bilinear)
            thr = water_threshold(vh_db, threshold)
            water = detect_flood(vh_db, thr, perm_mask, steep_mask,
                                 min_area_px, aoi_mask)
        except Exception as e:  # noqa: BLE001
            log(f"[!] Sentinel-1: no pude usar la escena {item.id} "
                  f"({e}). Sigo con las demás.")
            skipped.append(item.id)
            continue

        union = water if union is None else (union | water)
        acquisitions.append(Acquisition(
            item_id=item.id,
            datetime_utc=(item.datetime or EPOCH).isoformat(),
            relative_orbit=props.get("sat:relative_orbit"),
            orbit_state=props.get("sat:orbit_state"),
            threshold_db=thr,
        ))

    if union is None:
        log("[!] Sentinel-1: ninguna escena de la ventana se pudo leer.")
        return None

    pct = 100.0 * union.sum() / max(aoi_mask.sum(), 1)
    log(f"[+] Sentinel-1 (unión de {len(acquisitions)} escena(s), "
          f"{len(skipped)} salteada(s)): {union.sum():,} px anegados "
          f"({pct:.2f}% del AOI)")
    return SarLayerResult(flood=union, template=template,
                          acquisitions=acquisitions, skipped=skipped)
