"""test_config.py — primer test real de este repo: chequeo offline de
config.py contra los config/*.yaml reales del repo (sin red, sin rásters).

flood-projections-feature-real-flood.V2.0.md describía un historial de
tests que nunca existió acá (ver la corrección del 2026-08-19 en ese
documento) — este archivo es el punto de partida real, no una
reconstrucción de esa narrativa fabricada.
"""

from pathlib import Path

import pytest

from flood_validation import config

REPO_ROOT = Path(__file__).parent.parent
REGIONS_PATH = REPO_ROOT / "config" / "regions.yaml"
VALIDATION_PATH = REPO_ROOT / "config" / "validation.yaml"


def test_load_regions_config_tiene_las_entradas_esperadas():
    regions = config.load_regions_config(REGIONS_PATH)
    assert "Región de Coquimbo, Chile" in regions
    assert "Región de Atacama, Chile" in regions
    assert "default" in regions


def test_region_coquimbo_tiene_los_thresholds_calibrados():
    regions = config.load_regions_config(REGIONS_PATH)
    coquimbo = regions["Región de Coquimbo, Chile"]
    assert coquimbo.hand_threshold_m == 15.0
    assert coquimbo.drainage_threshold_km2 == 0.05
    assert coquimbo.datasets.sentinel1 is True
    assert coquimbo.datasets.sentinel2 is True


def test_region_atacama_tiene_drainage_threshold_mas_alto_que_coquimbo():
    # Ver el comentario en regions.yaml: con el 0.05 default calibrado en
    # Tongoy, casi cualquier vaguada del relieve de Vallenar calificaba
    # como cauce y dejaba pasar anegamiento falso en crestas.
    regions = config.load_regions_config(REGIONS_PATH)
    coquimbo = regions["Región de Coquimbo, Chile"]
    atacama = regions["Región de Atacama, Chile"]
    assert atacama.drainage_threshold_km2 > coquimbo.drainage_threshold_km2


def test_resolve_region_config_cae_a_default_si_no_matchea():
    regions = config.load_regions_config(REGIONS_PATH)
    resuelto = config.resolve_region_config("región inexistente", regions)
    assert resuelto is regions["default"]


def test_resolve_region_config_devuelve_la_region_exacta_si_matchea():
    regions = config.load_regions_config(REGIONS_PATH)
    resuelto = config.resolve_region_config("Región de Coquimbo, Chile",
                                            regions)
    assert resuelto is regions["Región de Coquimbo, Chile"]


def test_load_validation_config_fusion_weights_suman_uno():
    vc = config.load_validation_config(VALIDATION_PATH)
    total = (vc.fusion_weights.sentinel1 + vc.fusion_weights.sentinel2
             + vc.fusion_weights.dynamic_world)
    assert total == pytest.approx(1.0)


def test_load_validation_config_gee_project_es_str_o_none():
    # No afirmamos un valor específico: depende de si hay un .env local
    # con GEE_PROJECT (gitignored, no siempre presente en CI/otro clon).
    vc = config.load_validation_config(VALIDATION_PATH)
    assert vc.gee_project is None or isinstance(vc.gee_project, str)


def test_load_validation_config_no_tiene_gee_project_en_el_yaml():
    # Regresión específica: gee_project es una credencial de cuenta y se
    # lee de la variable de entorno GEE_PROJECT (.env), no del YAML
    # versionado — ver el comentario en validation.yaml.
    raw_yaml = VALIDATION_PATH.read_text(encoding="utf-8")
    assert "gee_project:" not in raw_yaml
