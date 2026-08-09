#!/usr/bin/env python3
"""sensing.py — primitivas de sensado remoto (AOI, ventana de fechas, lectura
Sentinel-1, umbral, máscaras de agua permanente/pendiente, detección) que
usa flood_validation.

Subconjunto vendorizado de `flood_monitor.py` en meteorologia-flood-monitor
(repo hermano): son las únicas funciones de ese script que flood_validation
necesitaba, sin el resto del pipeline (CLI propia, `--change`, generación de
salidas), que no aplica acá. No hay import entre los dos repos — cada uno es
standalone —, así que un cambio en el original no se propaga solo; si el
comportamiento de estas funciones cambia en flood_monitor.py, hay que
portearlo acá a mano.
"""

from __future__ import annotations

import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

DB_NODATA = -9999.0
NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
DEFAULT_REGION = "Región de Coquimbo, Chile"
# STAC permite items sin `datetime` (los que declaran start/end_datetime en
# su lugar). Sentinel-1 RTC siempre lo trae, pero ordenar por un valor que
# puede ser None revienta al comparar, así que esos items van al fondo.
EPOCH = datetime.min.replace(tzinfo=timezone.utc)


def geocode_place(place: str, region: str, buffer_km: float):
    """Geocodifica un punto de interés con Nominatim (OSM) y devuelve
    (geometry_dict, bbox_tuple) en EPSG:4326, con `buffer_km` de margen."""
    import requests
    from shapely.geometry import box, mapping

    r = requests.get(
        NOMINATIM_URL,
        params={"q": f"{place}, {region}", "format": "jsonv2", "limit": 1},
        headers={"User-Agent": "flood-validation/1.0 (anegamiento Sentinel-1)"},
        timeout=30,
    )
    r.raise_for_status()
    results = r.json()
    if not results:
        sys.exit(f"[!] Nominatim no encontró '{place}' en '{region}'. "
                 f"Prueba otro nombre o ajusta --region.")
    res = results[0]
    print(f"[+] POI geocodificado: {res['display_name']}")
    # Usamos el centro del resultado, no su boundingbox: para comunas
    # Nominatim devuelve el límite administrativo entero (~100 km de lado).
    lat_c, lon_c = float(res["lat"]), float(res["lon"])

    # Buffer en km -> grados (lon corregida por latitud).
    dlat = buffer_km / 111.32
    dlon = float(buffer_km / (111.32 * max(np.cos(np.radians(lat_c)), 0.01)))
    bbox = (lon_c - dlon, lat_c - dlat, lon_c + dlon, lat_c + dlat)
    print(f"[+] AOI: {2 * buffer_km:.0f}x{2 * buffer_km:.0f} km alrededor de "
          f"({lat_c:.4f}, {lon_c:.4f})")
    geom = box(*bbox)
    return mapping(geom), bbox


def load_aoi(args):
    """Devuelve (geometry_dict, bbox_tuple) en EPSG:4326."""
    from shapely.geometry import box, shape, mapping

    if args.place:
        return geocode_place(args.place, args.region, args.buffer_km)

    if args.bbox:
        geom = box(*args.bbox)
        return mapping(geom), tuple(args.bbox)

    gj = json.loads(args.aoi.read_text())
    if gj.get("type") == "FeatureCollection":
        geom = shape(gj["features"][0]["geometry"])
    elif gj.get("type") == "Feature":
        geom = shape(gj["geometry"])
    else:
        geom = shape(gj)
    return mapping(geom), geom.bounds


def parse_end_date(s: str | None, local: bool = False) -> datetime:
    """Parsea una fecha de corte a datetime aware UTC. Sin fecha, usa el
    momento actual. Con solo fecha (YYYY-MM-DD), usa el final de ese día
    (23:59:59) para incluir todas las imágenes de esa fecha.

    La zona en que se interpreta lo que escribió el usuario:

    1. si el texto trae offset explícito ("...T20:00:00-04:00"), manda ese
       offset y `local` se ignora;
    2. si no, y `local` es True, se interpreta como hora local del sistema,
       con las reglas de horario de verano *de esa fecha* (eso hace
       `.astimezone()` sobre un datetime naive);
    3. si no, se interpreta como UTC, que es el default histórico.

    El valor devuelto siempre es UTC."""
    if s is None:
        if local:
            print("[!] --local-time no hace nada sin fecha: el default es "
                  "el instante actual, que no depende de la zona.")
        return datetime.now(timezone.utc)
    if len(s) == 10:  # "YYYY-MM-DD"
        dt = datetime.strptime(s, "%Y-%m-%d").replace(
            hour=23, minute=59, second=59)
    else:
        dt = datetime.fromisoformat(s)
    if dt.tzinfo is not None or local:
        # Naive + local: astimezone() presume hora del sistema y aplica el
        # offset vigente en esa fecha (en Chile, -03 en verano y -04 el
        # resto del año), cosa que un offset fijo no haría.
        end = dt.astimezone(timezone.utc)
        if local and dt.tzinfo is None:
            print(f"[+] Corte local {dt:%Y-%m-%d %H:%M:%S} "
                  f"({dt.astimezone():%z}) = {end:%Y-%m-%d %H:%M:%S} UTC")
        return end
    return dt.replace(tzinfo=timezone.utc)


def slugify(s: str) -> str:
    """Nombre apto para archivo: espacios en blanco -> '_'. Conserva tildes
    y 'ñ'/'Ñ'; reemplaza '/' (separador de rutas) y elimina ',' (frecuente
    en nombres geocodificados tipo "Región de Coquimbo, Chile" — sin
    quitarla, quedaba literal en el run_tag)."""
    return re.sub(r"\s+", "_", s.strip().replace(",", "")).replace("/", "-")


def to_db(arr: np.ndarray) -> np.ndarray:
    """Potencia lineal -> dB, protegiendo ceros/negativos."""
    out = np.full(arr.shape, DB_NODATA, dtype="float32")
    valid = arr > 0
    out[valid] = 10.0 * np.log10(arr[valid])
    return out


def stac_catalog():
    import planetary_computer as pc
    from pystac_client import Client

    return Client.open(
        "https://planetarycomputer.microsoft.com/api/stac/v1",
        modifier=pc.sign_inplace,
    )


def read_vh_db(item, bbox):
    """Lee la banda VH recortada al bbox y la devuelve en dB (rioxarray)."""
    import rioxarray
    import xarray as xr

    href = item.assets["vh"].href
    raw = rioxarray.open_rasterio(href, masked=True)
    # open_rasterio devuelve Dataset | DataArray | list[Dataset]: la lista
    # aparece en formatos con varios subdatasets (NetCDF/HDF), nunca en un
    # COG GeoTIFF como los assets de Sentinel-1 RTC.
    assert isinstance(raw, xr.DataArray), f"esperaba un DataArray, no {type(raw)}"
    da = (raw
          .rio.clip_box(*bbox, crs="EPSG:4326")
          .squeeze("band", drop=True))
    vh_db = da.copy(data=to_db(da.values))
    vh_db = vh_db.where(vh_db != DB_NODATA)
    print(f"[+] VH leída: {vh_db.shape[1]}x{vh_db.shape[0]} px, "
          f"CRS {da.rio.crs}")
    return vh_db


def water_threshold(vh_db, fixed: float | None) -> float:
    """Umbral fijo o automático (Otsu) sobre los valores válidos."""
    if fixed is not None:
        print(f"[+] Umbral fijo: {fixed:.1f} dB")
        return fixed
    from skimage.filters import threshold_otsu

    vals = vh_db.values[np.isfinite(vh_db.values)]
    t = float(threshold_otsu(vals))
    # Otsu puede fallar en escenas casi sin agua; acotamos a un rango sensato.
    t = float(np.clip(t, -25.0, -14.0))
    print(f"[+] Umbral Otsu (acotado): {t:.1f} dB")
    return t


def permanent_water_mask(geom: dict, bbox, template):
    """Máscara booleana de agua permanente (JRC GSW, occurrence > 50%),
    reproyectada a la grilla de `template`. Si falla, devuelve None."""
    try:
        import rioxarray
        import xarray as xr
        from rioxarray.merge import merge_arrays

        items = list(stac_catalog().search(collections=["jrc-gsw"],
                                           intersects=geom).item_collection())
        if not items:
            return None
        # Margen extra en el recorte: sin él, reproject_match deja NaN en los
        # bordes de la grilla UTM y el océano queda sin enmascarar (cuñas
        # falsas de "anegamiento" pegadas al borde del AOI).
        pad = 0.02
        pbbox = (bbox[0] - pad, bbox[1] - pad, bbox[2] + pad, bbox[3] + pad)
        tiles = []
        for it in items:
            raw = rioxarray.open_rasterio(it.assets["occurrence"].href,
                                          masked=True)
            # Mismo caso que en read_vh_db: los tiles JRC son COG, así que
            # open_rasterio devuelve DataArray y no la lista de Datasets que
            # su firma también admite.
            assert isinstance(raw, xr.DataArray)
            da = (raw
                  .rio.clip_box(*pbbox, crs="EPSG:4326")
                  .squeeze("band", drop=True))
            tiles.append(da)
        occ = tiles[0] if len(tiles) == 1 else merge_arrays(tiles)
        occ = occ.rio.reproject_match(template)
        mask = occ.values > 50  # agua presente >50% del tiempo
        # Dilatación leve (~30 m): absorbe el desalineado JRC/S1 en la línea
        # de costa y la franja de rompiente, sin comerse humedales interiores.
        from skimage.morphology import dilation, disk
        mask = dilation(mask, disk(3))
        print(f"[+] Agua permanente (JRC): {mask.sum():,} px enmascarados")
        return mask
    except Exception as e:  # noqa: BLE001
        print(f"[!] No pude cargar JRC GSW ({e}). Sigo sin enmascarar "
              f"agua permanente.")
        return None


def slope_mask(geom: dict, bbox, template, max_slope_deg: float):
    """Máscara booleana de pendiente > max_slope_deg (Copernicus DEM GLO-30),
    calculada sobre la grilla de `template`. Si falla, devuelve None."""
    if max_slope_deg <= 0:
        return None
    try:
        import rioxarray
        import xarray as xr
        from rasterio.enums import Resampling
        from rioxarray.merge import merge_arrays

        items = list(stac_catalog().search(collections=["cop-dem-glo-30"],
                                           intersects=geom).item_collection())
        if not items:
            return None
        # Mismo margen que en JRC: evita NaN de borde tras reproject_match.
        pad = 0.02
        pbbox = (bbox[0] - pad, bbox[1] - pad, bbox[2] + pad, bbox[3] + pad)
        tiles = []
        for it in items:
            raw = rioxarray.open_rasterio(it.assets["data"].href, masked=True)
            assert isinstance(raw, xr.DataArray)  # COG, ver read_vh_db
            da = (raw
                  .rio.clip_box(*pbbox, crs="EPSG:4326")
                  .squeeze("band", drop=True))
            tiles.append(da)
        dem = tiles[0] if len(tiles) == 1 else merge_arrays(tiles)
        dem = dem.rio.reproject_match(template,
                                      resampling=Resampling.bilinear)
        # Pendiente en grados sobre la grilla UTM (resolución en metros).
        res_x, res_y = template.rio.resolution()
        dzdy, dzdx = np.gradient(dem.values, abs(res_y), abs(res_x))
        slope = np.degrees(np.arctan(np.hypot(dzdx, dzdy)))
        mask = np.isfinite(slope) & (slope > max_slope_deg)
        print(f"[+] Pendiente > {max_slope_deg:.0f}° (Cop-DEM GLO-30): "
              f"{mask.sum():,} px enmascarados")
        return mask
    except Exception as e:  # noqa: BLE001
        print(f"[!] No pude cargar el DEM ({e}). Sigo sin máscara de "
              f"pendiente.")
        return None


def detect_flood(vh_db, threshold: float, perm_mask, steep_mask,
                 min_area_px: int, ref_db=None, change_delta: float = 3.0):
    from skimage.morphology import remove_small_objects

    water = np.isfinite(vh_db.values) & (vh_db.values < threshold)
    if ref_db is not None:
        # Cambio: el píxel debe haberse oscurecido >= change_delta dB
        # respecto a la referencia. Superficies oscuras en ambas fechas
        # (parcelas lisas, pavimento, sombras) quedan descartadas.
        diff = vh_db.values - ref_db.values
        changed = np.isfinite(diff) & (diff < -change_delta)
        print(f"[+] Criterio de cambio: {change_delta:.1f} dB de caída — "
              f"descarta {(water & ~changed).sum():,} px oscuros estables")
        water &= changed
    if perm_mask is not None:
        water &= ~perm_mask
    if steep_mask is not None:
        water &= ~steep_mask
    # skimage >= 0.26: max_size elimina parches de tamaño <= max_size,
    # equivalente al viejo min_size=min_area_px (que eliminaba < min_area_px).
    water = remove_small_objects(water, max_size=min_area_px - 1)
    pct = 100.0 * water.sum() / max(np.isfinite(vh_db.values).sum(), 1)
    print(f"[+] Anegamiento detectado: {water.sum():,} px "
          f"({pct:.2f}% del AOI)")
    return water
