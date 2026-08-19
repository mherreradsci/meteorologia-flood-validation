# meteorologia-flood-validation

Valida el producto de susceptibilidad de anegamiento del repo hermano
[`meteorologia-flood-projections`](../meteorologia-flood-projections) contra
una capa de **anegamiento real** construida con sensores remotos públicos:
Sentinel-1 RTC (SAR) + Sentinel-2 L2A (óptico) vía Planetary Computer STAC
(acceso anónimo), más Dynamic World (Google Earth Engine, opcional) como
tercer sensor corroborante. Plausibilidad de terreno (HAND) y agua
estacional/de riego (JRC) se aplican como exclusiones duras sobre la capa
fusionada.

Extraído de `meteorologia-flood-monitor` como proyecto standalone.

## Instalación

Requiere Python 3.12. Si `rasterio` no tiene rueda manylinux para tu
plataforma, instalá primero las dependencias de sistema:

```bash
sudo apt install -y gdal-bin libgdal-dev python3-dev
```

Luego:

```bash
python3.12 -m venv venv
venv/bin/pip install -r requirements.txt
```

No hay `pyproject.toml`/`setup.py`: el código se corre con `src/` como raíz
de imports (`sensing` y `flood_validation` son paquetes top-level), así que
todos los comandos necesitan `PYTHONPATH=src`.

## Dynamic World / Google Earth Engine (opcional)

Sentinel-1 y Sentinel-2 funcionan con acceso anónimo a Planetary Computer,
sin configuración extra. Dynamic World es distinto: necesita una cuenta de
Google Earth Engine autenticada (`earthengine authenticate`, gratis para
uso no comercial) y un Cloud Project.

```bash
cp .env.example .env
# completar GEE_PROJECT=tu-project-id en .env
```

`.env` está en `.gitignore` — el project id es una credencial de cuenta, no
se versiona. Sin `GEE_PROJECT` (o sin `earthengine authenticate` hecho),
Dynamic World se reporta como no disponible y el pipeline sigue con
Sentinel-1/Sentinel-2 solamente — no es un error, es degradación esperada.
El toggle `datasets.dynamic_world` por región vive en `config/regions.yaml`.

## Uso

```bash
# Corrida típica: geocodifica el lugar con OSM, arma un AOI de 10x10 km
# alrededor, y usa la región para resolver umbrales/config.
PYTHONPATH=src venv/bin/python -m flood_validation \
    --place Punitaqui --region "Región de Coquimbo, Chile" \
    --start-date-utc 2026-07-17T00:00:00 --end-date-utc 2026-07-20T00:00:00

# Dry-run: resuelve AOI/ventana/config y escribe solo el manifiesto, sin
# tocar rásters ni red — la forma barata de probar cambios en cli/config.
PYTHONPATH=src venv/bin/python -m flood_validation --place Punitaqui --dry-run

# AOI por archivo GeoJSON o bbox en vez de --place (mutuamente excluyentes)
PYTHONPATH=src venv/bin/python -m flood_validation \
    --aoi aoi/04-Coquimbo/Chile-Region_de_Coquimbo.geojson --days 4

# Comparar contra un ciclo de susceptibilidad específico en vez de dejar
# que se resuelva automáticamente por ventana
PYTHONPATH=src venv/bin/python -m flood_validation \
    --place Vallenar --region "Región de Atacama, Chile" \
    --susceptibility ../meteorologia-flood-projections/outputs/atacama/gfs/mapa_anegamientos_gfs_extension_20260818_18utc_20260818-191619.tif
```

Sin `--start-date-utc`/`--end-date-utc`/`--days`, la ventana por default
son los últimos 10 días hasta ahora. No hay tests ni linter configurados.

## Salidas

Cada corrida escribe a `output/validation/` (o `--output-dir`) bajo un
run-tag único (`región_lugar_ventana_hex_timestamp`):

- `real_flood_s1_<tag>.tif`/`.geojson` — capa Sentinel-1
- `real_flood_s2_<tag>.tif`/`.geojson` — capa Sentinel-2
- `real_flood_dw_<tag>.tif`/`.geojson` — capa Dynamic World (si estaba
  disponible)
- `real_flood_fused_<tag>.tif`/`.geojson` — fusión ponderada por tier de
  confianza (alta/media/baja)
- `validation_metrics-<tag>.json` — Kappa, MCC, Precision/Recall/F1/IoU,
  error de área, agreement con tolerancia espacial, desglose por HAND
- `flood_map-<tag>.html` — mapa interactivo (leafmap; degrada suave si
  falta el import)
- `validation_summary-<tag>.csv` / `validation_report-<tag>.md` — resumen
  de una fila y reporte narrativo
- `run_manifest-<tag>.json` — hash de config + metadata para reproducir la
  corrida

## Arquitectura

Pipeline orquestado por `src/flood_validation/main.py`, con imports
perezosos por fase (cada módulo pesado se importa solo si su etapa corre).
Ver `CLAUDE.md` para el detalle módulo por módulo, las convenciones del
repo, y las decisiones de diseño verificadas empíricamente (umbrales HAND,
recorte de Otsu, por qué Dynamic World se compone server-side en vez de
loopear escena por escena, etc.) — es la referencia viva para quien vaya a
tocar este código, humano o Claude Code.

## Repos hermanos

`susceptibility.source_root` (en `config/regions.yaml`) asume que
`meteorologia-flood-projections` está clonado como hermano, bajo el mismo
directorio padre que este repo.
