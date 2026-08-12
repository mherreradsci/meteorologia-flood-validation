"""seasonality.py — distingue agua estacional/de riego de anegamiento
anómalo, usando la banda "seasonality" de JRC Global Surface Water (meses
por año que un píxel se clasificó como agua, 0-12; confirmado en vivo
sobre Tongoy: valores enteros 0-9 y 12 presentes) — no solo la ocurrencia
binaria que ya usa `flood_monitor.permanent_water_mask` (>50% del
registro histórico completo).

Un canal de riego que se llena 3-4 meses al año puede tener ocurrencia
baja (no pasa el 50% de `permanent_water_mask`) pero sigue siendo agua
"normal" del lugar en esa época, no anegamiento nuevo por la tormenta —
`seasonality` captura ese patrón directamente (meses/año), donde
`occurrence` (fracción del registro entero) no lo distingue. Complementa a
`permanent_water_mask`, no lo reemplaza: agua verdaderamente permanente
sigue estando mejor cubierta por ese umbral de ocurrencia; esta máscara
apunta al caso intermedio que ese umbral deja pasar.
"""

from __future__ import annotations

import numpy as np

from sensing import log, stac_catalog

# Mismo margen que permanent_water_mask/slope_mask en flood_monitor.py.
JRC_PAD_DEG = 0.02


def seasonal_water_mask(geom: dict, bbox, template,
                        min_months: int = 2) -> np.ndarray | None:
    """Máscara booleana (True = agua estacional/de riego, no anegamiento
    nuevo) sobre la grilla de `template`. None si JRC no está disponible o
    el cálculo falla — mismo fallo suave que
    `permanent_water_mask`/`slope_mask`.

    `min_months` es el mínimo de meses/año para considerar el patrón
    "estacional recurrente" y no ruido de un año puntual con agua
    ocasional (default 2 — un solo mes es más ruido de detección satelital
    que un patrón real).
    """
    try:
        import rioxarray
        import xarray as xr
        from rioxarray.merge import merge_arrays

        items = list(stac_catalog().search(
            collections=["jrc-gsw"], intersects=geom).item_collection())
        if not items:
            return None
        pbbox = (bbox[0] - JRC_PAD_DEG, bbox[1] - JRC_PAD_DEG,
                 bbox[2] + JRC_PAD_DEG, bbox[3] + JRC_PAD_DEG)
        tiles = []
        for it in items:
            raw = rioxarray.open_rasterio(it.assets["seasonality"].href,
                                          masked=True)
            assert isinstance(raw, xr.DataArray)
            da = raw.rio.clip_box(*pbbox, crs="EPSG:4326").squeeze("band", drop=True)
            tiles.append(da)
        seas = tiles[0] if len(tiles) == 1 else merge_arrays(tiles)
        seas = seas.rio.reproject_match(template)

        mask = np.isfinite(seas.values) & (seas.values >= min_months)
        log(f"[+] Agua estacional/de riego (JRC seasonality ≥{min_months} "
              f"meses/año): {mask.sum():,} px")
        return mask
    except Exception as e:  # noqa: BLE001
        log(f"[!] No pude cargar JRC seasonality ({e}). Sigo sin "
              f"distinguir agua estacional.")
        return None
