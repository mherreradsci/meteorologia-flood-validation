"""config.py — carga config/regions.yaml y config/validation.yaml en
dataclasses tipadas.

YAML en vez de constantes Python a propósito: cambiar un umbral, el centro
del mapa, o la raíz de susceptibilidad de una región no debería requerir
tocar un .py (ver la sección de Configuración del plan). Cada dataclass
documenta su propio fallback, igual que flood_monitor.py documenta el
recorte [-25, -14] de Otsu o la dilatación disk(3) como constantes
explicadas, no mágicas.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class SusceptibilityConfig:
    # Raíz de outputs/<region>/ en el repo hermano
    # meteorologia-flood-projections (no vive en este repo). None si la
    # región no tiene un producto de susceptibilidad configurado todavía —
    # en ese caso --susceptibility es obligatorio para poder comparar.
    source_root: str | None = None
    sufijo_preferido: str = "gfs"  # "gfs" | "ifs"


@dataclass
class DatasetToggles:
    sentinel1: bool = True
    sentinel2: bool = True
    # Requiere cuenta GEE (no anónima como Planetary Computer, ver Fase 0
    # del plan): apagado por default, se prende por región cuando haya
    # credenciales disponibles.
    dynamic_world: bool = False


@dataclass
class RegionConfig:
    display_name: str
    # Identidad alineada con el repo hermano meteorologia-flood-projections
    # (config*.yaml → region.nombre / region.id). La clave de la entrada en
    # regions.yaml es su region.osm_geocode, así el mismo string sirve para
    # --region acá y para geocodificar allá. `id` es además la subcarpeta
    # de outputs/<id> del hermano, o sea el último segmento de
    # susceptibility.source_root. Vacíos si la región no los declara
    # (p. ej. la entrada "default", que no mapea a una región del hermano).
    nombre: str = ""
    id: str = ""
    map_center: tuple[float, float] | None = None  # None = centroide del AOI
    zoom: int = 12
    susceptibility: SusceptibilityConfig = field(
        default_factory=SusceptibilityConfig)
    datasets: DatasetToggles = field(default_factory=DatasetToggles)
    # Defaults verificados en vivo sobre Tongoy (terrain.py, Fase 4): ver
    # el docstring de terrain.compute_hand para el barrido empírico
    # detrás de 0.05 km². hand_threshold_m=15 replica el hand_max_m
    # calibrado del repo hermano — el criterio de altura sí traslada
    # aunque la definición de cauce (drainage_threshold_km2) no.
    hand_threshold_m: float = 15.0
    drainage_threshold_km2: float = 0.05
    awei_variant: str = "sh"  # "nsh" | "sh" | "both"
    confidence_threshold: float = 0.5


@dataclass
class FusionWeights:
    sentinel1: float = 0.5
    sentinel2: float = 0.35
    dynamic_world: float = 0.15


@dataclass
class ValidationConfig:
    stac_collections: dict[str, str] = field(default_factory=lambda: {
        "sentinel1": "sentinel-1-rtc",
        "sentinel2": "sentinel-2-l2a",
    })
    fusion_weights: FusionWeights = field(default_factory=FusionWeights)
    confidence_tiers: dict[str, float] = field(default_factory=lambda: {
        "high": 0.75, "medium": 0.5, "low": 0.25,
    })
    basemap: str = "esri"
    buffer_tolerance_m: float = 100.0


def _load_yaml(path: Path) -> dict:
    # Import perezoso: seguimos la convención de flood_monitor.py de dejar
    # las dependencias no esenciales para el arranque dentro de la función
    # que las usa, no al tope del módulo.
    import yaml

    if not path.exists():
        raise SystemExit(f"[!] No existe el archivo de config: {path}")
    with path.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _region_from_dict(entry: dict) -> RegionConfig:
    susc = entry.get("susceptibility") or {}
    ds = entry.get("datasets") or {}
    center = entry.get("map_center")
    defaults = RegionConfig(display_name="")
    return RegionConfig(
        display_name=entry.get("display_name", ""),
        nombre=entry.get("nombre", ""),
        id=entry.get("id", ""),
        map_center=tuple(center) if center else None,
        zoom=entry.get("zoom", defaults.zoom),
        susceptibility=SusceptibilityConfig(
            source_root=susc.get("source_root"),
            sufijo_preferido=susc.get("sufijo_preferido",
                                      defaults.susceptibility.sufijo_preferido),
        ),
        datasets=DatasetToggles(
            sentinel1=ds.get("sentinel1", defaults.datasets.sentinel1),
            sentinel2=ds.get("sentinel2", defaults.datasets.sentinel2),
            dynamic_world=ds.get("dynamic_world",
                                 defaults.datasets.dynamic_world),
        ),
        hand_threshold_m=entry.get("hand_threshold_m",
                                   defaults.hand_threshold_m),
        drainage_threshold_km2=entry.get("drainage_threshold_km2",
                                         defaults.drainage_threshold_km2),
        awei_variant=entry.get("awei_variant", defaults.awei_variant),
        confidence_threshold=entry.get("confidence_threshold",
                                       defaults.confidence_threshold),
    )


def load_regions_config(path: Path) -> dict[str, RegionConfig]:
    """Carga regions.yaml. Clave = string exacto de --region (más "default"
    como fallback si --region no matchea ninguna entrada)."""
    raw = _load_yaml(path)
    regions = {key: _region_from_dict(entry)
              for key, entry in (raw.get("regions") or {}).items()}
    if "default" in raw:
        regions["default"] = _region_from_dict(raw["default"])
    return regions


def resolve_region_config(region: str,
                          regions: dict[str, RegionConfig]) -> RegionConfig:
    if region in regions:
        return regions[region]
    if "default" in regions:
        print(f"[!] No hay entrada en regions.yaml para '{region}': uso "
              f"la entrada 'default'.")
        return regions["default"]
    raise SystemExit(
        f"[!] No hay entrada en regions.yaml para '{region}' ni una "
        f"entrada 'default'. Agrega una de las dos antes de continuar.")


def load_validation_config(path: Path) -> ValidationConfig:
    raw = _load_yaml(path)
    defaults = ValidationConfig()
    fw = raw.get("fusion_weights") or {}
    return ValidationConfig(
        stac_collections=raw.get("stac_collections",
                                 defaults.stac_collections),
        fusion_weights=FusionWeights(
            sentinel1=fw.get("sentinel1", defaults.fusion_weights.sentinel1),
            sentinel2=fw.get("sentinel2", defaults.fusion_weights.sentinel2),
            dynamic_world=fw.get("dynamic_world",
                                 defaults.fusion_weights.dynamic_world),
        ),
        confidence_tiers=raw.get("confidence_tiers",
                                 defaults.confidence_tiers),
        basemap=raw.get("basemap", defaults.basemap),
        buffer_tolerance_m=raw.get("buffer_tolerance_m",
                                   defaults.buffer_tolerance_m),
    )
