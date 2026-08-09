"""report.py — bundle de salida legible para una corrida: mapa HTML
interactivo, resumen CSV de una fila, y reporte narrativo en Markdown.

El mapa sigue el mismo patrón leafmap que `flood_monitor.save_outputs`
(CartoDB Voyager + satelital, sin capa base OSM cruda — sus servidores
exigen `Referer` y devuelven 403 al abrir el HTML desde `file://`),
fallando suave si leafmap no está instalado, igual filosofía que las
máscaras de plausibilidad.

Framing crítico que el reporte tiene que dejar explícito (§7 del plan):
la susceptibilidad es una capa de *propensión*, no una predicción binaria
de este evento puntual — los caveats no son un detalle legal, son la
diferencia entre leer bien o mal los números.
"""

from __future__ import annotations

import csv as csv_module
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

TIER_LABELS = {1: "baja", 2: "media", 3: "alta"}
TIER_COLORS = {1: "#ffeb3b", 2: "#ff9800", 3: "#e53935"}

# Leaflet colapsa `.leaflet-control-layers` en el evento `mouseleave` del
# propio control: con varias capas para tildar, basta que el cursor se
# desvíe un poco del panel angosto para que se cierre antes de terminar de
# elegir. `DOMContentLoaded` corre después del <script> que arma el mapa
# (que folium siempre emite más abajo en el documento), así que encuentra
# el control ya creado sin importar dónde quede este script en el HTML.
# Los clics dentro del panel no llegan a `document` (Leaflet les aplica
# `disableClickPropagation`), así que tildar una capa no lo cierra; el
# `panel.contains` es solo una segunda red por si esa API cambia.
_LAYER_CONTROL_STAY_OPEN_JS = """
<script>
document.addEventListener('DOMContentLoaded', function () {
  var panel = document.querySelector('.leaflet-control-layers');
  if (!panel) return;
  var expandedClass = 'leaflet-control-layers-expanded';
  panel.addEventListener('mouseleave', function () {
    panel.classList.add(expandedClass);
  });
  document.addEventListener('click', function (ev) {
    if (!panel.contains(ev.target)) {
      panel.classList.remove(expandedClass);
    }
  });
});
</script>
"""


@dataclass
class ReportContext:
    """Todo lo que el mapa/CSV/Markdown necesitan, ya calculado — este
    módulo no vuelve a tocar red ni STAC, solo ensambla lo que las fases
    anteriores ya produjeron."""
    tag: str
    geom: dict
    bbox: tuple
    region: str
    place: str | None
    start: datetime
    end: datetime
    template: object  # DataArray de rioxarray, grilla fusionada
    sar_acquisitions: list[dict] = field(default_factory=list)
    sar_skipped: list[str] = field(default_factory=list)
    optical_acquisitions: list[dict] = field(default_factory=list)
    optical_skipped: list[str] = field(default_factory=list)
    fused: object = None  # fusion.FusionResult
    fused_geojson_path: Path | None = None
    susceptibility_cycle: dict | None = None
    susceptible_mask: object = None  # np.ndarray | None
    metrics: dict | None = None  # metrics.evaluation_to_dict(...)


def _vectorize(mask, template):
    import geopandas as gpd
    import rasterio.features
    from shapely.geometry import shape as shp_shape

    if mask is None or not mask.any():
        return None
    transform = template.rio.transform()
    shapes = rasterio.features.shapes(mask.astype("uint8"), mask=mask,
                                      transform=transform)
    geoms = [shp_shape(g) for g, v in shapes if v == 1]
    if not geoms:
        return None
    return gpd.GeoDataFrame(geometry=geoms, crs=template.rio.crs).to_crs(
        "EPSG:4326")


def build_html_map(ctx: ReportContext, output_dir: Path) -> Path | None:
    """Mapa interactivo: AOI, anegamiento real por tier, susceptibilidad,
    acuerdo/desacuerdo (TP/FP/FN), leyenda y panel de metadata de la
    corrida. None si leafmap no está disponible o el mapa no se pudo
    armar — el resto de las salidas (GeoTIFF/GeoJSON/JSON) ya son
    suficientes para seguir sin él."""
    try:
        import geopandas as gpd
        import leafmap.foliumap as leafmap

        xmin, ymin, xmax, ymax = ctx.bbox
        center = [(ymin + ymax) / 2, (xmin + xmax) / 2]
        m = leafmap.Map(center=center, zoom=12, tiles=None)
        m.add_basemap("CartoDB.Voyager")
        m.add_basemap("SATELLITE")

        from shapely.geometry import shape as shp_shape

        aoi_gdf = gpd.GeoDataFrame(geometry=[shp_shape(ctx.geom)],
                                   crs="EPSG:4326")
        m.add_gdf(aoi_gdf, layer_name="AOI",
                 style={"color": "#00e5ff", "fillOpacity": 0, "weight": 2})

        if ctx.fused_geojson_path and Path(ctx.fused_geojson_path).exists():
            gdf = gpd.read_file(ctx.fused_geojson_path)
            for tier_val, color in TIER_COLORS.items():
                sub = gdf[gdf["tier"] == tier_val]
                if len(sub):
                    m.add_gdf(sub, layer_name=(
                        f"Anegamiento real — confianza "
                        f"{TIER_LABELS[tier_val]}"),
                             style={"color": color, "fillColor": color,
                                    "fillOpacity": 0.55, "weight": 1})

        real_mask = (ctx.fused.tier > 0) if ctx.fused is not None else None
        susceptible_gdf = _vectorize(ctx.susceptible_mask, ctx.template)
        if susceptible_gdf is not None:
            m.add_gdf(susceptible_gdf, layer_name="Susceptibilidad",
                     style={"color": "#1e88e5", "fillColor": "#1e88e5",
                            "fillOpacity": 0.35, "weight": 1})

        if ctx.susceptible_mask is not None and real_mask is not None:
            capas_acuerdo = {
                "Acuerdo (TP)": (ctx.susceptible_mask & real_mask, "#43a047"),
                "Solo susceptibilidad (FP)": (
                    ctx.susceptible_mask & ~real_mask, "#8e24aa"),
                "Solo anegamiento real (FN)": (
                    ~ctx.susceptible_mask & real_mask, "#00acc1"),
            }
            for nombre, (arr, color) in capas_acuerdo.items():
                gdf_x = _vectorize(arr, ctx.template)
                if gdf_x is not None:
                    m.add_gdf(gdf_x, layer_name=nombre,
                             style={"color": color, "fillColor": color,
                                    "fillOpacity": 0.5, "weight": 1})

        leyenda = {
            f"Anegamiento real — {TIER_LABELS[t]}": c
            for t, c in TIER_COLORS.items()
        }
        leyenda["Susceptibilidad"] = "#1e88e5"
        leyenda["Acuerdo (TP)"] = "#43a047"
        leyenda["Solo susceptibilidad (FP)"] = "#8e24aa"
        leyenda["Solo anegamiento real (FN)"] = "#00acc1"
        m.add_legend(title="Leyenda", legend_dict=leyenda)

        metadata_html = _metadata_panel_html(ctx)
        m.get_root().html.add_child(_folium_element(metadata_html))
        m.get_root().html.add_child(_folium_element(_LAYER_CONTROL_STAY_OPEN_JS))

        html_path = output_dir / f"flood_map-{ctx.tag}.html"
        m.to_html(str(html_path))
        print(f"[+] Mapa interactivo: {html_path}")
        return html_path
    except Exception as e:  # noqa: BLE001
        print(f"[!] No pude generar el mapa HTML ({e}). Usa los GeoJSON "
              f"en QGIS/geojson.io.")
        return None


def _folium_element(html: str):
    from folium import Element

    return Element(html)


def _metadata_panel_html(ctx: ReportContext) -> str:
    lugar = ctx.place or ctx.region
    sensores = (ctx.fused.sensors_used if ctx.fused is not None else [])
    ciclo = ctx.susceptibility_cycle
    ciclo_txt = (f"{ciclo['cycle_sufijo']} @ {ciclo['cycle_utc']}"
                if ciclo and ciclo.get("cycle_utc")
                else (ciclo["cycle_path"] if ciclo else "sin comparar"))
    return f"""
    <div style="position: fixed; bottom: 10px; left: 10px; z-index: 9999;
                background: white; padding: 8px 12px; border-radius: 6px;
                box-shadow: 0 1px 4px rgba(0,0,0,0.4); font-size: 12px;
                max-width: 300px;">
      <b>{lugar}</b><br>
      Ventana: {ctx.start:%Y-%m-%d %H:%M} a {ctx.end:%Y-%m-%d %H:%M} UTC<br>
      Sensores: {', '.join(sensores) or 'ninguno'}<br>
      Susceptibilidad: {ciclo_txt}<br>
      <i>Producto de susceptibilidad = propensión, no predicción de este
      evento puntual.</i>
    </div>
    """


def write_csv_summary(ctx: ReportContext, output_dir: Path) -> Path:
    """Una fila con el resumen agregado (no el desglose por bin de HAND,
    que no entra en una fila plana) — pensada para juntar filas de varias
    corridas más adelante (§14, todavía no existe ese batch)."""
    output_dir.mkdir(parents=True, exist_ok=True)
    csv_path = output_dir / f"validation_summary-{ctx.tag}.csv"

    m = ctx.metrics or {}
    cm = m.get("confusion_matrix", {})
    d = m.get("derived_metrics", {})
    a = m.get("area_metrics", {})
    ciclo = ctx.susceptibility_cycle or {}
    fila = {
        "run_tag": ctx.tag,
        "region": ctx.region,
        "place": ctx.place or "",
        "window_start_utc": ctx.start.isoformat(),
        "window_end_utc": ctx.end.isoformat(),
        "sensors_used": "+".join(ctx.fused.sensors_used) if ctx.fused else "",
        "susceptibility_cycle_sufijo": ciclo.get("cycle_sufijo", ""),
        "susceptibility_cycle_utc": ciclo.get("cycle_utc", ""),
        "tp": cm.get("tp"), "fp": cm.get("fp"),
        "fn": cm.get("fn"), "tn": cm.get("tn"),
        "precision": d.get("precision"), "recall": d.get("recall"),
        "f1": d.get("f1"), "iou": d.get("iou"),
        "kappa": d.get("kappa"), "mcc": d.get("mcc"),
        "susceptible_km2": a.get("susceptible_km2"),
        "real_km2": a.get("real_km2"),
        "area_diff_km2": a.get("diff_km2"),
        "pct_error_signed": a.get("pct_error_signed"),
        "pct_error_abs": a.get("pct_error_abs"),
        "buffered_agreement_pct": m.get("buffered_agreement_pct"),
        "n_valid_px": m.get("n_valid_px"),
    }
    with csv_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv_module.DictWriter(f, fieldnames=list(fila.keys()))
        writer.writeheader()
        writer.writerow(fila)
    print(f"[+] Resumen CSV: {csv_path}")
    return csv_path


def write_markdown_report(ctx: ReportContext, output_dir: Path,
                          extra_paths: dict[str, Path | None]) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    md_path = output_dir / f"validation_report-{ctx.tag}.md"

    lugar = ctx.place or ctx.region
    m = ctx.metrics or {}
    cm = m.get("confusion_matrix", {})
    d = m.get("derived_metrics", {})
    a = m.get("area_metrics", {})
    ciclo = ctx.susceptibility_cycle

    def fmt(v, nd=3):
        return "—" if v is None else (f"{v:.{nd}f}" if isinstance(v, float) else str(v))

    partes = [
        f"# Validación de anegamiento — {lugar}",
        "",
        f"**Ventana:** {ctx.start:%Y-%m-%d %H:%M} a {ctx.end:%Y-%m-%d %H:%M} UTC  ",
        f"**ID de corrida:** `{ctx.tag}`",
        "",
        "## Metodología (resumen)",
        "",
        "Esta validación compara un producto de **susceptibilidad** de "
        "anegamiento (proyección a partir de lluvia pronosticada, "
        "calibrada contra eventos históricos) contra una capa de "
        "**anegamiento real estimado** a partir de sensores remotos "
        "públicos (Sentinel-1 SAR, Sentinel-2 óptico) dentro de la "
        "ventana de validación, fusionados con exclusiones de "
        "plausibilidad de terreno (HAND) y agua estacional/de riego "
        "(JRC).",
        "",
        "> **La susceptibilidad es una capa de propensión, no una "
        "predicción binaria de este evento puntual.** El modelo "
        "sobrepredice extensión a propósito (ver la documentación del "
        "propio producto). Los números de esta sección describen qué "
        "tan cerca estuvo de lo observado, no si \"acertó\" o \"falló\" "
        "en sentido estricto.",
        "",
        "## Sensores",
        "",
        f"- Sentinel-1: {len(ctx.sar_acquisitions)} escena(s) usada(s)"
        + (f", {len(ctx.sar_skipped)} salteada(s)" if ctx.sar_skipped else ""),
        f"- Sentinel-2: {len(ctx.optical_acquisitions)} escena(s) usada(s)"
        + (f", {len(ctx.optical_skipped)} salteada(s)"
           if ctx.optical_skipped else ""),
        f"- Sensores que aportaron a la fusión: "
        f"{', '.join(ctx.fused.sensors_used) if ctx.fused else 'ninguno'}",
        "",
        "## Susceptibilidad comparada",
        "",
        (f"- Ciclo: `{ciclo['cycle_sufijo']}` @ {ciclo['cycle_utc']}"
         if ciclo and ciclo.get("cycle_utc")
         else f"- Ruta: `{ciclo['cycle_path']}`" if ciclo
         else "- **Sin comparación**: no había ruta explícita ni ningún "
              "ciclo que cubriera esta ventana."),
        "",
    ]

    if ctx.metrics:
        partes += [
            "## Métricas",
            "",
            "| Métrica | Valor |",
            "|---|---|",
            f"| TP / FP / FN / TN | {cm.get('tp')} / {cm.get('fp')} / "
            f"{cm.get('fn')} / {cm.get('tn')} |",
            f"| Precision | {fmt(d.get('precision'))} |",
            f"| Recall | {fmt(d.get('recall'))} |",
            f"| F1 | {fmt(d.get('f1'))} |",
            f"| IoU | {fmt(d.get('iou'))} |",
            f"| Cohen's Kappa | {fmt(d.get('kappa'))} |",
            f"| MCC | {fmt(d.get('mcc'))} |",
            f"| Área susceptible | {fmt(a.get('susceptible_km2'), 2)} km² |",
            f"| Área real | {fmt(a.get('real_km2'), 2)} km² |",
            f"| Diferencia de área | {fmt(a.get('diff_km2'), 2)} km² |",
            f"| Error % (señalado) | {fmt(a.get('pct_error_signed'), 1)}% |",
            f"| Error % (absoluto) | {fmt(a.get('pct_error_abs'), 1)}% |",
            f"| Agreement con tolerancia espacial | "
            f"{fmt(m.get('buffered_agreement_pct'), 1)}% |",
            "",
        ]
        if m.get("hand_bins"):
            partes += ["### Desglose por HAND (altura sobre el drenaje "
                      "más cercano)", "",
                      "| HAND (m) | px | TP | FP | FN | TN | F1 |",
                      "|---|---|---|---|---|---|---|"]
            for b in m["hand_bins"]:
                lo = b["hand_min_m"] if b["hand_min_m"] is not None else 0
                hi = b["hand_max_m"] if b["hand_max_m"] is not None else "∞"
                bc = b["confusion"]
                partes.append(
                    f"| {lo}–{hi} | {b['n_px']:,} | {bc['tp']} | {bc['fp']} "
                    f"| {bc['fn']} | {bc['tn']} | "
                    f"{fmt(b['derived'].get('f1'))} |")
            partes.append("")

    partes += ["## Archivos de esta corrida", ""]
    for etiqueta, ruta in extra_paths.items():
        if ruta:
            partes.append(f"- {etiqueta}: `{ruta}`")
    partes += [
        "",
        "## Limitaciones conocidas",
        "",
        "- La fusión pondera por sensor disponible, no por píxel "
        "despejado: un píxel nublado en Sentinel-2 vota \"seco\" en vez "
        "de \"sin opinión\", lo que puede topear la confianza en zonas "
        "con mucha nubosidad durante la ventana.",
        "- Sin activación conocida de Copernicus EMS para este evento: "
        "\"anegamiento real\" es en sí una estimación multi-sensor, no "
        "una verdad de terreno independiente.",
        "- Dynamic World no está integrado (sin credenciales GEE en este "
        "entorno).",
    ]

    md_path.write_text("\n".join(partes), encoding="utf-8")
    print(f"[+] Reporte Markdown: {md_path}")
    return md_path
