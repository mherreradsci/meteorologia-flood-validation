"""dynamic_world.py — capa de "anegamiento real" opcional a partir de
Dynamic World V1 (Google Earth Engine), corroborando Sentinel-1/Sentinel-2
con una probabilidad de agua casi en tiempo real (10 m). Ver §5/§16.4 del
plan: se dejó deliberadamente sin construir mientras el entorno no tenía
credenciales GEE; ese import queda guardado (`ee = None` si falta la
librería) y `build_dynamic_world_layer` devuelve `None` ante cualquier
problema de auth/red/cuota — mismo contrato "sensor no disponible, no
error" que `sar_layer.py`/`optical_layer.py`.

Diferencia deliberada de diseño frente a esos dos módulos: GEE resuelve
reducciones sobre la colección del lado del servidor, así que en vez de
descargar cada imagen de la ventana y unirlas localmente (necesario para
STAC, donde cada asset es un archivo aparte), acá se arma **una sola
composición server-side** —`.max()` de la banda "water" sobre la ventana,
mismo espíritu que el OR-de-escenas de SAR/óptico: "anegado si en algún
punto de la ventana se vio agua"— y se descarga una sola imagen ya
binarizada. Hacer un loop por imagen y descargar cada una por separado acá
sería un antipatrón de uso de GEE (multiplica llamadas a una API con cuota,
para replicar un patrón pensado para STAC) y no un fallback más "fiel" al
resto del pipeline.
"""

from __future__ import annotations

import io
from dataclasses import dataclass, field
from datetime import datetime

import numpy as np

from sensing import aoi_grid_mask, log

try:
    import ee
except ImportError:  # earthengine-api no instalado: sensor no disponible.
    ee = None

DYNAMIC_WORLD_COLLECTION = "GOOGLE/DYNAMICWORLD/V1"


@dataclass
class Acquisition:
    """Metadata de una imagen Dynamic World que contribuyó a la
    composición — no hay try/except por imagen (la reducción es
    server-side), pero igual se listan una por una para que el reporte
    pueda mostrar contra qué evidencia concreta se armó la capa."""
    item_id: str
    datetime_utc: str


@dataclass
class DynamicWorldResult:
    flood: np.ndarray
    template: object  # DataArray de rioxarray
    acquisitions: list[Acquisition] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)


def _ee_initialize(gee_project: str | None) -> bool:
    if ee is None:
        log("[i] dynamic_world: earthengine-api no está instalado — "
              "sensor no disponible.")
        return False
    if not gee_project:
        log("[i] dynamic_world: falta gee_project en validation.yaml — "
              "sensor no disponible.")
        return False
    try:
        ee.Initialize(project=gee_project)
    except Exception as e:  # noqa: BLE001
        log(f"[i] dynamic_world: no pude inicializar Earth Engine ({e}) — "
              "sensor no disponible, sigo sin él.")
        return False
    return True


def build_dynamic_world_layer(
        geom: dict, bbox, start: datetime, end: datetime, *,
        water_threshold: float = 0.5,
        gee_project: str | None = None) -> DynamicWorldResult | None:
    """Arma la capa de anegamiento real de Dynamic World para la ventana.

    Devuelve None si el sensor no está disponible (sin GEE, sin imágenes
    en la ventana, o falla la descarga) — degradación esperada, no error,
    igual que sar_layer.build_real_flood_layer/
    optical_layer.build_optical_water_layer.
    """
    if not _ee_initialize(gee_project):
        return None

    try:
        region = ee.Geometry(geom)
        collection = (ee.ImageCollection(DYNAMIC_WORLD_COLLECTION)
                     .filterBounds(region)
                     .filterDate(start.strftime("%Y-%m-%dT%H:%M:%S"),
                                 end.strftime("%Y-%m-%dT%H:%M:%S")))
        n = collection.size().getInfo()
    except Exception as e:  # noqa: BLE001
        log(f"[!] dynamic_world: no pude consultar la colección ({e}). "
              "Sensor no disponible esta corrida.")
        return None

    if n == 0:
        log(f"[!] Dynamic World: sin imágenes entre {start:%Y-%m-%d} y "
              f"{end:%Y-%m-%d} para este AOI.")
        return None
    log(f"[+] Dynamic World: {n} imagen(es) en la ventana.")

    try:
        ids = collection.aggregate_array("system:index").getInfo()
        times_ms = collection.aggregate_array("system:time_start").getInfo()
        acquisitions = [
            Acquisition(item_id=i,
                       datetime_utc=datetime.utcfromtimestamp(t / 1000.0)
                                    .isoformat())
            for i, t in zip(ids, times_ms)
        ]

        # Máximo por píxel de la probabilidad de agua sobre la ventana:
        # mismo criterio que el OR-de-escenas de SAR/óptico, aplicado
        # server-side. Se binariza acá (no probabilidad continua en la
        # fusión) para mantener el mismo contrato flood: bool que
        # SarLayerResult/OpticalLayerResult.
        water_prob = collection.select("water").max()
        water_bin = water_prob.gt(water_threshold).rename("flood").uint8()

        url = water_bin.getDownloadURL({
            "region": region,
            "scale": 10,
            "format": "GEO_TIFF",
        })
    except Exception as e:  # noqa: BLE001
        log(f"[!] dynamic_world: no pude armar la composición ({e}). "
              "Sensor no disponible esta corrida.")
        return None

    try:
        import requests
        import rioxarray

        resp = requests.get(url, timeout=120)
        resp.raise_for_status()
        template = rioxarray.open_rasterio(
            io.BytesIO(resp.content)).squeeze("band", drop=True)
    except Exception as e:  # noqa: BLE001
        log(f"[!] dynamic_world: no pude descargar la composición ({e}). "
              "Sensor no disponible esta corrida.")
        return None

    aoi_mask = aoi_grid_mask(geom, template)
    flood = (template.values > 0) & aoi_mask

    pct = 100.0 * flood.sum() / max(aoi_mask.sum(), 1)
    log(f"[+] Dynamic World ({len(acquisitions)} imagen(es), umbral "
          f"{water_threshold}): {int(flood.sum()):,} px anegados "
          f"({pct:.2f}% del AOI)")
    return DynamicWorldResult(flood=flood, template=template,
                              acquisitions=acquisitions, skipped=[])
