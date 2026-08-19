"""main.py — orquestación de flood_validation.

Fase 1 (fundamentos): resuelve AOI, ventana y config, arma el tag de la
corrida y escribe el manifiesto de reproducibilidad — solo `--dry-run`, sin
procesar rásters. Las fases siguientes agregan las capas de sensores
(Sentinel-1, Sentinel-2, Dynamic World opcional), la fusión y la
comparación real contra la capa de susceptibilidad.
"""

from __future__ import annotations

import hashlib
import json
import platform
import secrets
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

from sensing import load_aoi, log, slugify

from . import cli, config, windows


def _config_hash(paths: list[Path]) -> str:
    """Hash corto de los YAML de config, para el manifiesto: dos corridas
    con distinto hash usaron distintos umbrales/pesos, aunque el resto de
    los argumentos sea igual."""
    h = hashlib.sha256()
    for p in sorted(paths):
        h.update(p.read_bytes())
    return h.hexdigest()[:16]


def build_run_tag(args, bbox, start: datetime, end: datetime) -> str:
    """Mismo espíritu que build_run_tag de flood_monitor.py (secuencia
    aleatoria + timestamp local para no pisar corridas previas), pero
    fechado por la ventana de validación en vez de la fecha de una sola
    imagen — acá no hay una escena única, hay una ventana multi-sensor."""
    if args.place:
        region = slugify(args.region)
        place = slugify(args.place)
    elif args.aoi:
        region = "aoi"
        place = slugify(args.aoi.stem)
    else:
        region = "bbox"
        place = "_".join(f"{v:.4f}" for v in bbox)
    window = f"{start:%Y%m%d}-{end:%Y%m%d}"
    sequence = secrets.token_hex(4)
    local_ts = f"{datetime.now():%Y%m%dT%H%M%S}"
    return f"{region}_{place}_{window}_{sequence}_{local_ts}"


def main(argv: list[str] | None = None) -> None:
    args = cli.parse_args(argv)

    # Config primero: es una lectura local barata, y falla rápido si la
    # región no está configurada — antes de tocar la red con --place
    # (geocodificación) o hacer ningún otro trabajo.
    regions_path = args.config_dir / "regions.yaml"
    validation_path = args.config_dir / "validation.yaml"
    regions_cfg = config.load_regions_config(regions_path)
    validation_cfg = config.load_validation_config(validation_path)
    region_cfg = config.resolve_region_config(args.region, regions_cfg)
    log(f"[+] Config de región resuelta: zoom={region_cfg.zoom}, "
          f"HAND≤{region_cfg.hand_threshold_m} m, AWEI "
          f"{region_cfg.awei_variant}, umbral de confianza "
          f"{region_cfg.confidence_threshold}")

    geom, bbox = load_aoi(args)
    log(f"[+] AOI bbox: {tuple(round(v, 4) for v in bbox)}")

    start, end = windows.resolve_window(args)
    log(f"[+] Ventana de validación: {start:%Y-%m-%d %H:%M:%S} a "
          f"{end:%Y-%m-%d %H:%M:%S} UTC ({(end - start).days} días)")

    susceptibility_path = args.susceptibility
    if susceptibility_path is not None:
        log(f"[+] Susceptibilidad: ruta explícita {susceptibility_path}")
    elif region_cfg.susceptibility.source_root:
        log(f"[+] Susceptibilidad: se resolverá desde "
              f"{region_cfg.susceptibility.source_root} (sufijo preferido: "
              f"{region_cfg.susceptibility.sufijo_preferido}) — la "
              f"resolución de ciclo llega en la Fase 5.")
    else:
        log("[!] Sin --susceptibility ni source_root configurado para "
              "esta región: no habrá con qué comparar en fases futuras.")

    log("[+] Sensores configurados — Sentinel-1: "
          f"{'on' if region_cfg.datasets.sentinel1 else 'off'}, "
          f"Sentinel-2: {'on' if region_cfg.datasets.sentinel2 else 'off'}, "
          f"Dynamic World: "
          f"{'on' if region_cfg.datasets.dynamic_world else 'off'} "
          "(disponibilidad real por red/credenciales se verifica en las "
          "fases que agregan cada sensor).")

    tag = build_run_tag(args, bbox, start, end)
    log(f"[+] ID de corrida: {tag}")

    manifest = {
        "run_tag": tag,
        "dry_run": args.dry_run,
        "aoi": {
            "mode": ("place" if args.place else
                    "aoi" if args.aoi else "bbox"),
            "bbox": [round(v, 6) for v in bbox],
            "place": args.place,
            "region": args.region,
            "aoi_file": str(args.aoi) if args.aoi else None,
        },
        "window": {
            "start_utc": start.isoformat(),
            "end_utc": end.isoformat(),
        },
        "susceptibility": {
            "explicit_path": (str(susceptibility_path)
                              if susceptibility_path else None),
            "source_root": region_cfg.susceptibility.source_root,
            "sufijo_preferido": region_cfg.susceptibility.sufijo_preferido,
        },
        "sensors_enabled": asdict(region_cfg.datasets),
        "config_hash": _config_hash([regions_path, validation_path]),
        "python_version": platform.python_version(),
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
    }

    if not args.dry_run:
        # Fase 4: Sentinel-1, Sentinel-2 y Dynamic World (Fase 4b, GEE)
        # están implementados, más la fusión de los que respondan con
        # plausibilidad de terreno (HAND) y agua estacional.
        from . import outputs as outputs_io

        result_outputs: dict = {}
        sensors: dict = {}
        sar_result = optical_result = dynamic_world_result = None

        if region_cfg.datasets.sentinel1:
            from . import sar_layer

            sar_result = sar_layer.build_real_flood_layer(
                geom, bbox, start, end, threshold=args.threshold,
                min_area_px=args.min_area_px, max_slope=args.max_slope)
            if sar_result is not None:
                paths = outputs_io.write_geotiff_geojson(
                    sar_result.template, sar_result.flood, args.output_dir,
                    tag, prefix="real_flood_s1")
                result_outputs["sentinel1"] = {
                    "tif": str(paths["tif"]),
                    "geojson": (str(paths["geojson"])
                               if paths["geojson"] else None),
                }
                sensors["sentinel1"] = {
                    "acquisitions": [asdict(a) for a in
                                    sar_result.acquisitions],
                    "skipped": sar_result.skipped,
                }
            else:
                sensors["sentinel1"] = {"acquisitions": [], "skipped": []}
        else:
            log("[i] Sentinel-1 desactivado para esta región (config).")

        if region_cfg.datasets.sentinel2:
            from . import optical_layer

            awei_variant = args.awei_variant or region_cfg.awei_variant
            collection = validation_cfg.stac_collections.get(
                "sentinel2", "sentinel-2-l2a")
            optical_result = optical_layer.build_optical_water_layer(
                geom, bbox, start, end, awei_variant=awei_variant,
                collection=collection)
            if optical_result is not None:
                paths = outputs_io.write_geotiff_geojson(
                    optical_result.template, optical_result.flood,
                    args.output_dir, tag, prefix="real_flood_s2")
                result_outputs["sentinel2"] = {
                    "tif": str(paths["tif"]),
                    "geojson": (str(paths["geojson"])
                               if paths["geojson"] else None),
                }
                sensors["sentinel2"] = {
                    "acquisitions": [asdict(a) for a in
                                    optical_result.acquisitions],
                    "skipped": optical_result.skipped,
                }
            else:
                sensors["sentinel2"] = {"acquisitions": [], "skipped": []}
        else:
            log("[i] Sentinel-2 desactivado para esta región (config).")

        if region_cfg.datasets.dynamic_world:
            from . import dynamic_world

            dynamic_world_result = dynamic_world.build_dynamic_world_layer(
                geom, bbox, start, end,
                water_threshold=region_cfg.dynamic_world_water_threshold,
                gee_project=validation_cfg.gee_project)
            if dynamic_world_result is not None:
                paths = outputs_io.write_geotiff_geojson(
                    dynamic_world_result.template,
                    dynamic_world_result.flood, args.output_dir, tag,
                    prefix="real_flood_dw")
                result_outputs["dynamic_world"] = {
                    "tif": str(paths["tif"]),
                    "geojson": (str(paths["geojson"])
                               if paths["geojson"] else None),
                }
                sensors["dynamic_world"] = {
                    "acquisitions": [asdict(a) for a in
                                    dynamic_world_result.acquisitions],
                    "skipped": dynamic_world_result.skipped,
                }
            else:
                sensors["dynamic_world"] = {"acquisitions": [], "skipped": []}
        else:
            log("[i] Dynamic World desactivado para esta región (config).")

        fusion_info: dict | None = None
        validation_info: dict | None = None
        fused = susceptible_mask = metrics_dict = None
        if (sar_result is not None or optical_result is not None
                or dynamic_world_result is not None):
            from dataclasses import asdict as _asdict

            from . import fusion

            fused = fusion.fuse(
                sar_result, optical_result, geom, bbox,
                fusion_weights=_asdict(validation_cfg.fusion_weights),
                confidence_tiers=validation_cfg.confidence_tiers,
                dynamic_world_result=dynamic_world_result,
                hand_threshold_m=region_cfg.hand_threshold_m,
                drainage_threshold_km2=region_cfg.drainage_threshold_km2)
            if fused is not None:
                paths = outputs_io.write_tiered_geotiff_geojson(
                    fused.template, fused.tier, args.output_dir, tag,
                    prefix="real_flood_fused",
                    tier_labels={1: "baja", 2: "media", 3: "alta"})
                fusion_info = {
                    "tif": str(paths["tif"]),
                    "geojson": (str(paths["geojson"])
                               if paths["geojson"] else None),
                    "sensors_used": fused.sensors_used,
                    "terrain_excluded_px": fused.terrain_excluded_px,
                    "seasonal_excluded_px": fused.seasonal_excluded_px,
                }
                fused_geojson_path = paths["geojson"]

                # Fase 5: comparar la capa fusionada contra el ciclo de
                # susceptibilidad del repo hermano que corresponda a esta
                # ventana (o la ruta explícita de --susceptibility).
                from . import susceptibility as susc

                ciclo = susc.resolve_susceptibility(
                    explicit_path=susceptibility_path,
                    source_root=region_cfg.susceptibility.source_root,
                    sufijo_preferido=region_cfg.susceptibility.sufijo_preferido,
                    start=start, end=end, base_dir=cli.REPO_ROOT)
                if ciclo is None:
                    log("[!] Susceptibilidad: no hay ruta explícita ni "
                          "ningún ciclo que cubra esta ventana — sin nada "
                          "que comparar.")
                else:
                    susceptible_mask = susc.load_susceptibility(
                        ciclo.path, fused.template, bbox, geom)
                    validation_info = {
                        "cycle_path": str(ciclo.path),
                        "cycle_sufijo": ciclo.sufijo,
                        "cycle_utc": (ciclo.cycle_utc.isoformat()
                                     if ciclo.cycle_utc else None),
                    }
                    if susceptible_mask is not None:
                        from . import metrics as metrics_mod

                        res_x, res_y = fused.template.rio.resolution()
                        resultado = metrics_mod.evaluate(
                            susceptible_mask, fused.tier > 0,
                            pixel_area_km2=abs(res_x * res_y) / 1e6,
                            resolution_m=abs(res_x),
                            buffer_tolerance_m=validation_cfg.buffer_tolerance_m,
                            hand=fused.hand)
                        metrics_dict = metrics_mod.evaluation_to_dict(resultado)
                        metrics_path = (args.output_dir /
                                       f"validation_metrics-{tag}.json")
                        metrics_path.write_text(json.dumps(
                            {**validation_info, **metrics_dict},
                            indent=2, ensure_ascii=False))
                        log(f"[+] Métricas de validación: {metrics_path}")
                        validation_info["metrics_path"] = str(metrics_path)

        # Fase 6: bundle de reporte (mapa HTML, CSV, Markdown) — solo si
        # hubo algo que fusionar; sin eso no hay grilla de referencia ni
        # nada geográfico que mostrar.
        report_info: dict | None = None
        if fused is not None:
            from . import report

            ctx = report.ReportContext(
                tag=tag, geom=geom, bbox=bbox, region=args.region,
                place=args.place, start=start, end=end,
                template=fused.template,
                sar_acquisitions=(sensors.get("sentinel1", {})
                                 .get("acquisitions", [])),
                sar_skipped=sensors.get("sentinel1", {}).get("skipped", []),
                optical_acquisitions=(sensors.get("sentinel2", {})
                                     .get("acquisitions", [])),
                optical_skipped=sensors.get("sentinel2", {}).get("skipped", []),
                dynamic_world_acquisitions=(sensors.get("dynamic_world", {})
                                           .get("acquisitions", [])),
                dynamic_world_skipped=(sensors.get("dynamic_world", {})
                                      .get("skipped", [])),
                fused=fused, fused_geojson_path=fused_geojson_path,
                susceptibility_cycle=validation_info,
                susceptible_mask=susceptible_mask, metrics=metrics_dict)
            map_path = report.build_html_map(ctx, args.output_dir)
            csv_path = report.write_csv_summary(ctx, args.output_dir)
            md_path = report.write_markdown_report(ctx, args.output_dir, {
                "GeoTIFF fusionado": fusion_info.get("tif") if fusion_info else None,
                "GeoJSON fusionado": fusion_info.get("geojson") if fusion_info else None,
                "Métricas (JSON)": (validation_info.get("metrics_path")
                                   if validation_info else None),
                "Resumen (CSV)": csv_path,
            })
            report_info = {
                "map_html": str(map_path) if map_path else None,
                "csv": str(csv_path),
                "markdown": str(md_path),
            }

        manifest["outputs"] = result_outputs
        manifest["sensors"] = sensors
        manifest["fusion"] = fusion_info
        manifest["validation"] = validation_info
        manifest["report"] = report_info

    args.output_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = args.output_dir / f"run_manifest-{tag}.json"
    manifest_path.write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False))
    log(f"[+] Manifiesto: {manifest_path}")
    estado = "Dry-run listo (sin procesamiento de rásters)" if args.dry_run \
        else "Corrida lista"
    log(f"[✓] {estado}.")


if __name__ == "__main__":
    main()
