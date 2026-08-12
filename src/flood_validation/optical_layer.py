"""optical_layer.py — capa de "anegamiento real" a partir de Sentinel-2 L2A,
por unión de todas las escenas despejadas dentro de la ventana de
validación. Mismo espíritu que sar_layer.py (§4/§6 del plan): cada escena
vota por separado contra una grilla de referencia compartida, y el
resultado es la unión (OR lógico).

AWEI (Automated Water Extraction Index, Feyisa et al. 2014) es el índice
primario, no NDWI/MNDWI, porque su término explícito de sombra (variante
`sh`) importa en el terreno montañoso de Coquimbo — el mismo problema de
sombra de relieve que ya causa falsos positivos en SAR, atacado acá del
lado óptico:

    AWEInsh = 4·(green − swir1) − (0.25·nir + 2.75·swir2)
    AWEIsh  = blue + 2.5·green − 1.5·(nir + swir1) − 0.25·swir2

Umbral: AWEI > 0 (agua), la convención estándar de la literatura — a
diferencia de VH en dB, no hace falta un umbral por escena tipo Otsu.

Cada píxel vota solo si su clase SCL (Scene Classification Layer) no es
nube/sombra de nube/cirro/nieve — el resto de la escena (agua, vegetación,
suelo desnudo, sin clasificar) sí participa. Una escena casi enteramente
nublada no aporta nada al OR y se reporta como salteada con su cobertura
despejada real, en vez de fallar silenciosamente (riesgo explícito de
§11: nubes justo en la semana de la tormenta).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

import numpy as np

from sensing import EPOCH, log, stac_catalog

# Reflectancia de superficie Sentinel-2 L2A: DN / 10000 -> [0, 1].
S2_REFLECTANCE_SCALE = 10000.0
# SCL: 0 sin dato, 1 saturado/defectuoso, 3 sombra de nube, 8/9 nube media/
# alta, 10 cirro delgado, 11 nieve/hielo. El resto (2,4,5,6,7) vota.
SCL_NO_VOTA = frozenset({0, 1, 3, 8, 9, 10, 11})
AWEI_WATER_THRESHOLD = 0.0


@dataclass
class Acquisition:
    item_id: str
    datetime_utc: str
    cloud_cover_pct: float | None
    clear_pct: float
    awei_variant: str


@dataclass
class OpticalLayerResult:
    flood: np.ndarray
    template: object  # DataArray de rioxarray
    acquisitions: list[Acquisition] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)


def search_s2_window(geom: dict, start: datetime, end: datetime,
                     collection: str = "sentinel-2-l2a") -> list:
    """Todas las escenas Sentinel-2 L2A que intersectan `geom` dentro de
    [start, end], de más antigua a más nueva. Mismo patrón que
    sar_layer.search_s1_window."""
    search = stac_catalog().search(
        collections=[collection],
        intersects=geom,
        datetime=f"{start:%Y-%m-%dT%H:%M:%SZ}/{end:%Y-%m-%dT%H:%M:%SZ}",
    )
    return sorted(search.item_collection(),
                 key=lambda it: it.datetime or EPOCH)


def _read_band(item, band: str, bbox):
    import rioxarray
    import xarray as xr

    href = item.assets[band].href
    raw = rioxarray.open_rasterio(href, masked=True)
    assert isinstance(raw, xr.DataArray), f"esperaba un DataArray, no {type(raw)}"
    return raw.rio.clip_box(*bbox, crs="EPSG:4326").squeeze("band", drop=True)


def _detect_scene(item, bbox, awei_variant: str):
    """Lee las bandas necesarias, arma la máscara de despejado (SCL) y
    calcula la detección de agua AWEI para una escena. Devuelve
    (water_bool, clear_bool, template) en la grilla nativa de 10 m de la
    escena (B03)."""
    from rasterio.enums import Resampling

    green = _read_band(item, "B03", bbox)             # 10 m
    nir = _read_band(item, "B08", bbox)                # 10 m
    swir1 = _read_band(item, "B11", bbox).rio.reproject_match(
        green, resampling=Resampling.bilinear)         # 20 m -> 10 m
    swir2 = _read_band(item, "B12", bbox).rio.reproject_match(
        green, resampling=Resampling.bilinear)
    scl = _read_band(item, "SCL", bbox).rio.reproject_match(
        green, resampling=Resampling.nearest)           # categórico: nearest

    g = green.values / S2_REFLECTANCE_SCALE
    n = nir.values / S2_REFLECTANCE_SCALE
    s1 = swir1.values / S2_REFLECTANCE_SCALE
    s2v = swir2.values / S2_REFLECTANCE_SCALE
    valid = np.isfinite(g) & np.isfinite(n) & np.isfinite(s1) & np.isfinite(s2v)

    awei_nsh = 4.0 * (g - s1) - (0.25 * n + 2.75 * s2v)
    if awei_variant in ("sh", "both"):
        blue = _read_band(item, "B02", bbox)            # 10 m, mismo grid que B03
        b = blue.values / S2_REFLECTANCE_SCALE
        valid &= np.isfinite(b)
        awei_sh = b + 2.5 * g - 1.5 * (n + s1) - 0.25 * s2v

    if awei_variant == "nsh":
        raw_water = awei_nsh > AWEI_WATER_THRESHOLD
    elif awei_variant == "sh":
        raw_water = awei_sh > AWEI_WATER_THRESHOLD
    else:  # "both": cualquiera de las dos variantes cuenta como evidencia
        raw_water = ((awei_nsh > AWEI_WATER_THRESHOLD) |
                    (awei_sh > AWEI_WATER_THRESHOLD))

    clear = valid & ~np.isin(scl.values, list(SCL_NO_VOTA))
    water = clear & raw_water
    return water, clear, green


def build_optical_water_layer(
        geom: dict, bbox, start: datetime, end: datetime, *,
        awei_variant: str = "sh",
        collection: str = "sentinel-2-l2a") -> OpticalLayerResult | None:
    """Arma la capa de anegamiento real de Sentinel-2 para la ventana.

    Devuelve None si el sensor no está disponible (sin escenas, o ninguna
    con cobertura despejada utilizable) — degradación esperada, no error.
    """
    items = search_s2_window(geom, start, end, collection)
    if not items:
        log(f"[!] Sentinel-2: sin escenas entre {start:%Y-%m-%d} y "
              f"{end:%Y-%m-%d} para este AOI.")
        return None
    log(f"[+] Sentinel-2: {len(items)} escena(s) en la ventana "
          f"(AWEI {awei_variant}).")

    from rasterio.enums import Resampling

    template = None
    union = None
    acquisitions: list[Acquisition] = []
    skipped: list[str] = []

    for item in items:
        cloud_prop = item.properties.get("eo:cloud_cover")
        try:
            water, clear, scene_template = _detect_scene(
                item, bbox, awei_variant)
        except Exception as e:  # noqa: BLE001
            log(f"[!] Sentinel-2: no pude usar la escena {item.id} "
                  f"({e}). Sigo con las demás.")
            skipped.append(item.id)
            continue

        clear_pct = 100.0 * clear.sum() / max(clear.size, 1)
        log(f"[+] Sentinel-2 {item.id}: {clear_pct:.1f}% despejado "
              f"(nube catálogo {cloud_prop if cloud_prop is not None else '?'}%)")
        if clear.sum() == 0:
            log(f"[!] Sentinel-2: {item.id} sin píxeles despejados "
                  f"(nube/sombra/nieve cubren todo el recorte). Salteada.")
            skipped.append(item.id)
            continue

        if template is None:
            template = scene_template
            water_aligned = water
        else:
            carrier = scene_template.copy(data=water.astype("float32"))
            water_aligned = carrier.rio.reproject_match(
                template, resampling=Resampling.nearest).values > 0.5

        union = water_aligned if union is None else (union | water_aligned)
        acquisitions.append(Acquisition(
            item_id=item.id,
            datetime_utc=(item.datetime or EPOCH).isoformat(),
            cloud_cover_pct=cloud_prop,
            clear_pct=round(clear_pct, 2),
            awei_variant=awei_variant,
        ))

    if union is None:
        log("[!] Sentinel-2: ninguna escena de la ventana tuvo "
              "cobertura despejada utilizable.")
        return None

    pct = 100.0 * union.sum() / max(np.isfinite(template.values).sum(), 1)
    log(f"[+] Sentinel-2 (unión de {len(acquisitions)} escena(s), "
          f"{len(skipped)} salteada(s)): {union.sum():,} px anegados "
          f"({pct:.2f}% del AOI)")
    return OpticalLayerResult(flood=union, template=template,
                              acquisitions=acquisitions, skipped=skipped)
