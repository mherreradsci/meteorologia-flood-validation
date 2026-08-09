"""terrain.py — plausibilidad de terreno vía HAND (Height Above Nearest
Drainage), desde Copernicus DEM GLO-30 con pysheds.

Extiende la idea de `slope_mask` de flood_monitor.py (mismo fetch de
cop-dem-glo-30, mismo padding en grados para el clip STAC) pero en vez de
una pendiente puntual calcula la cadena hidrológica completa: acondicionar
el DEM, dirección de flujo D8, acumulación, red de drenaje por umbral de
área, y HAND propiamente dicho — el mismo método que ya usa en producción
el repo hermano meteorologia-flood-projections (ver su
`src/inundaciones/terrain.py`), no una reimplementación adivinada desde
cero. La red de drenaje se deriva del DEM por umbral de acumulación de
flujo (como en ese repo), no de un dataset externo tipo HydroRIVERS: más
simple y sin una fuente de datos adicional que mantener, y ya probado en
un pipeline hermano real.

Reproyectar directo a la grilla exacta del AOI (como hace `slope_mask`) NO
alcanza acá: pysheds asigna dirección de flujo inválida a las celdas que
caen justo en el borde de la grilla analizada (no tienen adónde "salir"),
así que cualquier cauce sintético o real cerca del borde del AOI arruina
el HAND de esa zona entera — verificado empíricamente antes de escribir
este módulo (una rampa con el cauce en la columna 0 de una grilla de
20x20 da HAND completamente NaN, incluso en las celdas del propio cauce).
La solución: computar sobre una grilla más grande que el AOI (`HAND_PAD_PX`
de margen por lado) y recortar el resultado al tamaño exacto de `template`
al final, para que el borde inestable quede siempre afuera de lo que se
devuelve. Verificado con un valle sintético: el recorte interior reproduce
la altura analítica exacta (error < 1 mm).
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import numpy as np

from sensing import stac_catalog

# Margen del clip STAC en grados: más generoso que el 0.02 de
# permanent_water_mask/slope_mask porque acá además hay que cubrir el
# margen en píxeles de HAND_PAD_PX una vez reproyectado a UTM.
DEM_CLIP_PAD_DEG = 0.05
# Margen en píxeles alrededor de la grilla de `template` donde corre
# pysheds, después recortado — ver docstring del módulo.
HAND_PAD_PX = 30


def _read_dem_padded(geom: dict, bbox, template):
    """Mismo patrón de fetch que flood_monitor.slope_mask (cop-dem-glo-30,
    bbox con margen, mergea tiles si hace falta), pero reproyectado a una
    grilla más grande que `template` (HAND_PAD_PX de margen por lado), no
    a su tamaño exacto — ver docstring del módulo."""
    import rioxarray
    import xarray as xr
    from affine import Affine
    from rasterio.enums import Resampling
    from rioxarray.merge import merge_arrays

    items = list(stac_catalog().search(collections=["cop-dem-glo-30"],
                                       intersects=geom).item_collection())
    if not items:
        return None
    pbbox = (bbox[0] - DEM_CLIP_PAD_DEG, bbox[1] - DEM_CLIP_PAD_DEG,
             bbox[2] + DEM_CLIP_PAD_DEG, bbox[3] + DEM_CLIP_PAD_DEG)
    tiles = []
    for it in items:
        raw = rioxarray.open_rasterio(it.assets["data"].href, masked=True)
        assert isinstance(raw, xr.DataArray)
        da = raw.rio.clip_box(*pbbox, crs="EPSG:4326").squeeze("band", drop=True)
        tiles.append(da)
    dem = tiles[0] if len(tiles) == 1 else merge_arrays(tiles)

    h, w = template.shape
    padded_transform = (template.rio.transform() *
                        Affine.translation(-HAND_PAD_PX, -HAND_PAD_PX))
    padded_shape = (h + 2 * HAND_PAD_PX, w + 2 * HAND_PAD_PX)
    return dem.rio.reproject(template.rio.crs, transform=padded_transform,
                             shape=padded_shape, resampling=Resampling.bilinear)


def compute_hand(geom: dict, bbox, template,
                 drainage_threshold_km2: float = 0.05) -> np.ndarray | None:
    """HAND (metros, float32) en la grilla exacta de `template`. None si
    el DEM no está disponible o el cálculo falla — mismo fallo suave que
    `permanent_water_mask`/`slope_mask`.

    `drainage_threshold_km2` es el área mínima de acumulación de flujo
    para que una celda se considere cauce. El repo hermano
    meteorologia-flood-projections usa 15 km², pero ese valor está
    calibrado para su DEM de la región entera — acá, con solo
    `HAND_PAD_PX` de margen alrededor de un AOI acotado, un cauce real
    rara vez junta 15 km² de área aguas arriba *dentro de ese margen*, así
    que casi ninguna celda encuentra un cauce dentro de la ventana de
    cómputo y HAND sale NaN en casi todo el AOI (verificado en vivo sobre
    Tongoy: con 15 km² solo el 18.6% de las celdas dieron HAND válido). Un
    umbral bien menor reconoce quebradas chicas en vez de solo ríos
    mayores — apropiado igual para el filtro: una crecida real en terreno
    árido de Coquimbo sigue justamente esas quebradas menores, no solo los
    cursos con nombre. 0.05 km² (barrido empírico sobre Tongoy: 15 km² →
    18.6% válido, 1 km² → 63.0%, 0.1 km² → 76.6%, 0.05 km² → 81.0%,
    0.01 km² → 98.9% pero casi toda celda pasa a ser "cauce", vaciando el
    filtro de sentido) es el punto medio elegido; sujeto al ajuste de la
    Fase 7 contra el evento real.
    """
    try:
        dem_padded = _read_dem_padded(geom, bbox, template)
        if dem_padded is None:
            return None
        interior, n_streams = _hand_from_padded_dem(
            dem_padded, template.shape, drainage_threshold_km2)
        pct_valid = 100.0 * np.isfinite(interior).mean()
        print(f"[+] HAND calculado (umbral de cauce "
              f"{drainage_threshold_km2:.3g} km², {n_streams:,} celdas de "
              f"cauce en la grilla con margen): {pct_valid:.1f}% de celdas "
              f"válidas")
        return interior
    except Exception as e:  # noqa: BLE001
        print(f"[!] No pude calcular HAND ({e}). Sigo sin filtro de "
              f"plausibilidad de terreno.")
        return None


def _hand_from_padded_dem(dem_padded, template_shape: tuple[int, int],
                          drainage_threshold_km2: float) -> tuple[np.ndarray, int]:
    """Corre la cadena pysheds (acondicionar, dirección de flujo,
    acumulación, cauce por umbral, HAND) sobre `dem_padded` — ya en la
    grilla con margen HAND_PAD_PX — y devuelve (HAND recortado al tamaño
    `template_shape`, cantidad de celdas de cauce). Separado de
    `compute_hand` para poder probarlo con una grilla armada a mano, sin
    pasar por el fetch STAC (ver tests)."""
    from pysheds.grid import Grid

    if dem_padded.rio.nodata is None:
        # Sin nodata explícito, pysheds asume 0 (avisa por stderr) — un 0
        # de "nada, no elevación" en el borde del margen leería como nivel
        # del mar y podría crear cauces falsos justo ahí. NaN es lo que ya
        # trae un DataArray leído con masked=True (el caso real); esto solo
        # cubre el caso sintético/de test que no lo fija.
        dem_padded = dem_padded.rio.write_nodata(np.nan)

    with tempfile.TemporaryDirectory() as tmp:
        # pysheds solo construye un Grid desde archivo (Grid.from_raster/
        # from_ascii, no hay constructor en memoria) — mismo patrón que el
        # repo hermano, que también pasa por un GeoTIFF intermedio.
        dem_path = str(Path(tmp) / "dem.tif")
        dem_padded.rio.to_raster(dem_path)
        grid = Grid.from_raster(dem_path)
        dem_r = grid.read_raster(dem_path)

        dem_cond = grid.resolve_flats(
            grid.fill_depressions(grid.fill_pits(dem_r)))
        fdir = grid.flowdir(dem_cond)
        acc = grid.accumulation(fdir)

        res_x, res_y = dem_padded.rio.resolution()
        cell_km2 = abs(res_x * res_y) / 1e6
        threshold_cells = drainage_threshold_km2 / cell_km2
        # Comparar el Raster de pysheds directo, no np.asarray(acc): esto
        # último devuelve un ndarray plano, y compute_hand exige que `mask`
        # siga siendo un Raster (falla con TypeError si no).
        streams = acc > threshold_cells
        n_streams = int(np.asarray(streams).sum())

        hand = grid.compute_hand(fdir, dem_cond, streams)
        hand_arr = np.asarray(hand, dtype="float32")

    h, w = template_shape
    interior = hand_arr[HAND_PAD_PX:HAND_PAD_PX + h,
                        HAND_PAD_PX:HAND_PAD_PX + w]
    return interior, n_streams


def hand_implausible_mask(
        geom: dict, bbox, template, *,
        drainage_threshold_km2: float = 0.05,
        hand_threshold_m: float = 15.0) -> np.ndarray | None:
    """Máscara booleana (True = terreno implausible para anegamiento real,
    a excluir) sobre la grilla de `template`. `hand_threshold_m <= 0`
    desactiva el filtro, igual convención que `--max-slope 0` en
    flood_monitor.py. None si HAND no se pudo calcular — celdas con HAND
    desconocido (NaN, típicamente por quedar fuera de cualquier cadena de
    flujo hacia un cauce dentro del margen) NO se excluyen: el filtro solo
    actúa donde hay evidencia positiva de que el terreno está demasiado
    alto sobre el drenaje, no donde falta el dato.
    """
    if hand_threshold_m <= 0:
        return None
    hand = compute_hand(geom, bbox, template,
                        drainage_threshold_km2=drainage_threshold_km2)
    if hand is None:
        return None
    mask = np.isfinite(hand) & (hand > hand_threshold_m)
    print(f"[+] HAND > {hand_threshold_m:.0f} m sobre el cauce más cercano: "
          f"{mask.sum():,} px descartados por implausibles")
    return mask
