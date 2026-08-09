"""fusion.py — combina las capas de sensor disponibles (Sentinel-1,
Sentinel-2, más adelante Dynamic World) en una sola capa de "anegamiento
real" con confianza por píxel, aplicando plausibilidad de terreno (HAND,
terrain.py) y agua estacional/de riego (seasonality.py) como exclusiones
duras — una sola vez sobre la grilla fusionada, no por sensor (evita
calcular HAND/JRC dos veces, y evita que cada sensor tenga su propio
criterio de plausibilidad).

Cada sensor disponible vota con su propio peso (`fusion_weights`,
config/validation.yaml); un sensor sin datos para esta ventana/AOI
simplemente no vota — el peso restante se renormaliza entre los sensores
que sí tienen dato, así que "solo Sentinel-1 disponible, dice agua" da
confianza 1.0 igual que "los dos sensores disponibles y de acuerdo", en
vez de una confianza artificialmente baja por faltar un sensor que nunca
tuvo información que aportar. La confianza resultante se cuantiza en
tiers (alta/media/baja/seca) según los cortes de `confidence_tiers`.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from . import seasonality, terrain

TIER_SECA, TIER_BAJA, TIER_MEDIA, TIER_ALTA = 0, 1, 2, 3


@dataclass
class FusionResult:
    confidence: np.ndarray  # float32, 0..1
    tier: np.ndarray        # uint8, 0=seca 1=baja 2=media 3=alta
    template: object        # DataArray de rioxarray, grilla de referencia
    sensors_used: list[str] = field(default_factory=list)
    terrain_excluded_px: int = 0
    seasonal_excluded_px: int = 0
    hand: np.ndarray | None = None  # metros, crudo (no solo la máscara de
    # exclusión) — para estratificar métricas por bin de HAND en la Fase 5
    # sin tener que recalcular pysheds otra vez.


def fuse(sar_result, optical_result, geom: dict, bbox, *,
        fusion_weights: dict[str, float],
        confidence_tiers: dict[str, float],
        hand_threshold_m: float = 15.0,
        drainage_threshold_km2: float = 0.05,
        seasonal_min_months: int = 2) -> FusionResult | None:
    """`sar_result`/`optical_result` son `SarLayerResult`/
    `OpticalLayerResult` (o `None` si ese sensor no tuvo datos). Devuelve
    `None` solo si NINGÚN sensor tiene datos — degradación consistente con
    cómo cada capa de sensor ya reporta `None` cuando le toca a ella."""
    disponibles = {"sentinel1": sar_result, "sentinel2": optical_result}
    disponibles = {n: r for n, r in disponibles.items() if r is not None}
    if not disponibles:
        print("[!] Fusión: ningún sensor tiene datos para esta ventana/AOI, "
              "no hay nada que fusionar.")
        return None

    # Grilla de referencia: la del sensor disponible con más peso — así el
    # resultado no depende de en qué orden llegaron los sensores.
    ref_nombre = max(disponibles,
                     key=lambda n: fusion_weights.get(n, 0.0))
    template = disponibles[ref_nombre].template

    peso_total = 0.0
    agua_ponderada = np.zeros(template.shape, dtype="float32")
    sensores_usados = []
    for nombre, resultado in disponibles.items():
        peso = fusion_weights.get(nombre, 0.0)
        if peso <= 0:
            continue
        agua = resultado.flood
        if resultado.template is not template:
            from rasterio.enums import Resampling

            portador = resultado.template.copy(data=agua.astype("float32"))
            agua = portador.rio.reproject_match(
                template, resampling=Resampling.nearest).values > 0.5
        agua_ponderada += peso * agua
        peso_total += peso
        sensores_usados.append(nombre)

    if peso_total <= 0:
        print("[!] Fusión: los sensores con datos tienen peso 0 en "
              "fusion_weights, no hay nada que ponderar.")
        return None
    confidence = (agua_ponderada / peso_total).astype("float32")

    # Se llama a compute_hand directo (no al conveniente
    # hand_implausible_mask) para quedarse con el HAND crudo, no solo la
    # máscara de exclusión — metrics.py lo reusa para estratificar sin
    # recalcular pysheds una segunda vez.
    hand = None
    terrain_excluded_px = 0
    if hand_threshold_m > 0:
        hand = terrain.compute_hand(
            geom, bbox, template,
            drainage_threshold_km2=drainage_threshold_km2)
        if hand is not None:
            terreno_mask = np.isfinite(hand) & (hand > hand_threshold_m)
            terrain_excluded_px = int(((confidence > 0) & terreno_mask).sum())
            confidence = np.where(terreno_mask, 0.0, confidence)

    estacional_mask = seasonality.seasonal_water_mask(
        geom, bbox, template, min_months=seasonal_min_months)
    seasonal_excluded_px = 0
    if estacional_mask is not None:
        seasonal_excluded_px = int(((confidence > 0) & estacional_mask).sum())
        confidence = np.where(estacional_mask, 0.0, confidence)

    alta = confidence_tiers.get("high", 0.75)
    media = confidence_tiers.get("medium", 0.5)
    baja = confidence_tiers.get("low", 0.25)
    tier = np.zeros(confidence.shape, dtype="uint8")
    tier[confidence >= baja] = TIER_BAJA
    tier[confidence >= media] = TIER_MEDIA
    tier[confidence >= alta] = TIER_ALTA

    print(f"[+] Fusión ({'+'.join(sensores_usados)}): "
         f"alta={int((tier == TIER_ALTA).sum()):,} "
         f"media={int((tier == TIER_MEDIA).sum()):,} "
         f"baja={int((tier == TIER_BAJA).sum()):,} px "
         f"(terreno excluido: {terrain_excluded_px:,}, estacional "
         f"excluido: {seasonal_excluded_px:,})")

    return FusionResult(confidence=confidence, tier=tier, template=template,
                        sensors_used=sensores_usados,
                        terrain_excluded_px=terrain_excluded_px,
                        seasonal_excluded_px=seasonal_excluded_px,
                        hand=hand)
