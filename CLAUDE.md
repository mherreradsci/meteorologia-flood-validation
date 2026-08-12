# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Qué es este repo

Valida el producto de susceptibilidad de anegamiento del repo hermano
`meteorologia-flood-projections` contra una capa de "anegamiento real"
construida con sensores remotos públicos (Sentinel-1 RTC + Sentinel-2 L2A
vía Planetary Computer STAC, acceso anónimo), con plausibilidad de terreno
(HAND) y agua estacional (JRC) como exclusiones.

Extraído de `meteorologia-flood-monitor` como proyecto standalone. Todo el
código, comentarios y docs están en español; mantener ese idioma y la
densidad de comentarios existente (los docstrings explican *por qué*, no
solo qué — decisiones verificadas empíricamente, referencias a secciones
del plan, diferencias con el repo de origen).

## Comandos

No hay pyproject/setup: el código se corre con `src/` como raíz de imports
(`sensing` y `flood_validation` son top-level). Venv local con Python 3.12.

```bash
# Instalar dependencias (si rasterio no tiene rueda manylinux:
# sudo apt install -y gdal-bin libgdal-dev python3-dev primero)
venv/bin/pip install -r requirements.txt

# Correr (desde la raíz del repo, con PYTHONPATH)
PYTHONPATH=src venv/bin/python -m flood_validation \
    --place Punitaqui --start-date-utc 2026-07-17 --end-date-utc 2026-07-20

# Dry-run: resuelve AOI/ventana/config y escribe solo el manifiesto,
# sin tocar rásters ni red STAC — la forma barata de probar cambios en
# cli/config/windows.
PYTHONPATH=src venv/bin/python -m flood_validation --place Punitaqui --dry-run

# AOI por archivo o bbox en vez de --place (mutuamente excluyentes)
PYTHONPATH=src venv/bin/python -m flood_validation \
    --aoi aoi/04-Coquimbo/Chile-Region_de_Coquimbo.geojson --days 4
```

No hay tests ni linter configurados.

## Arquitectura

Pipeline orquestado por `src/flood_validation/main.py`, con imports
perezosos por fase (cada módulo pesado se importa solo si su etapa corre):

1. **cli.py / config.py / windows.py** — argparse, carga de
   `config/regions.yaml` + `config/validation.yaml` en dataclasses
   tipadas, y resolución de la ventana UTC. Los defaults de
   `--config-dir`/`--output-dir` se resuelven contra la ubicación del
   paquete, no contra el cwd.
2. **sar_layer.py / optical_layer.py** — una capa binaria de agua por
   sensor, como unión (OR) de todas las escenas de la ventana contra una
   grilla de referencia compartida (la primera escena). S1: umbral Otsu
   por escena (o `--threshold` fijo en dB), máscaras de agua permanente y
   pendiente. S2: AWEI (variante `sh`/`nsh` por región), umbral > 0,
   píxeles nublados excluidos vía SCL.
3. **fusion.py** — voto ponderado por sensor (`fusion_weights` en
   validation.yaml); un sensor sin datos no vota y los pesos se
   renormalizan entre los que sí. Confianza 0..1 cuantizada en tiers
   (seca/baja/media/alta). Exclusiones duras aplicadas una sola vez sobre
   la grilla fusionada: HAND (terrain.py) y agua estacional
   (seasonality.py).
4. **terrain.py** — HAND con pysheds sobre Copernicus DEM GLO-30. Computa
   sobre una grilla con `HAND_PAD_PX` de margen y recorta al final:
   pysheds da dirección de flujo inválida en el borde de la grilla
   (verificado empíricamente; ver docstring del módulo antes de tocar).
5. **susceptibility.py** — localiza el raster del ciclo de pronóstico
   (GFS/IFS) del repo hermano cuya ventana de 72 h se solapa con la de
   validación; usa el ciclo más reciente por default. GeoTIFF binario
   uint8, EPSG:4326, nodata=255.
6. **metrics.py** — Kappa, MCC, Precision/Recall/F1/IoU, error de área,
   `buffered_agreement` (tolerancia en metros, config), y
   estratificación por bins de HAND. Sin ROC/AUC a propósito: el producto
   es binario por ciclo, no continuo.
7. **report.py / outputs.py** — mapa HTML leafmap (degrada suave si falta
   el import), CSV de una fila, Markdown, GeoTIFF/GeoJSON por capa, y
   `run_manifest-<tag>.json` con hash de config para reproducibilidad.
   Todo bajo `output/validation/` con un run-tag único
   (`region_place_ventana_hex_timestamp`).

### Piezas transversales

- **`src/sensing.py`** es un subconjunto *vendorizado* de
  `flood_monitor.py` del repo hermano `meteorologia-flood-monitor` — no
  hay import entre repos; si el original cambia, hay que portear a mano.
- **Config por región**: la clave de cada entrada de `regions.yaml` es el
  string exacto de `--region` (y el string de geocodificación OSM, y la
  identidad `nombre`/`id` alineada con `meteorologia-flood-projections`).
  Entrada `default` como fallback. Umbrales HAND/drenaje están calibrados
  por región (ver comentarios en el YAML antes de cambiarlos).
- **Repos hermanos**: `susceptibility.source_root` es una ruta relativa
  que asume `meteorologia-flood-projections` clonado como hermano bajo el
  mismo directorio padre.
- **Degradación gradual** es la filosofía en todo el pipeline: una escena
  ilegible se salta con aviso; un sensor sin escenas se reporta como no
  disponible (`None`) sin abortar; sin susceptibilidad resoluble, la
  corrida termina sin métricas pero con las capas. Mantener ese patrón al
  agregar sensores (p. ej. `dynamic_world`, hoy con toggle en config pero
  sin módulo).
