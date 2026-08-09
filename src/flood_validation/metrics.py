"""metrics.py — compara la capa de susceptibilidad (binaria, por ciclo)
contra la capa de anegamiento real fusionada.

Framing crítico del plan (§7): la susceptibilidad es una capa de
*propensión*, no una predicción binaria exacta de este evento. Por eso:

- Cohen's Kappa y MCC van junto a Precision/Recall/F1/IoU, no accuracy
  sola: los píxeles anegados son una minoría chica del AOI, así que
  accuracy quedaría dominada por el fondo seco donde las dos capas
  trivialmente coinciden.
- El error de área es señal aparte de la matriz de confusión pixel a
  pixel: una capa puede tener el área total bien pero mal ubicada, o
  viceversa.
- `buffered_agreement` mide qué fracción del anegamiento real cae *cerca*
  de una zona susceptible (dentro de una tolerancia en metros), premiando
  a la susceptibilidad por estar cerca aunque no coincida píxel a píxel —
  apropiado porque es inherentemente más gruesa que una huella observada.
- `stratify_by_hand_bin` reutiliza el HAND crudo que ya calculó fusion.py
  (no lo recalcula) para desglosar el desacuerdo por altura sobre el
  drenaje más cercano, en vez de un solo número agregado que lo esconde.

No hay barrido ROC/AUC (el §7 original lo pedía, pero §16.1 del plan
encontró que el producto es binario por ciclo, no continuo — nada que
threshold-sweepear dentro de un mismo ciclo). El "sweep" que sí tiene
sentido es a través de varios ciclos por lead time
(`susceptibility.find_cycles` ya devuelve todos los candidatos); ese
barrido se arma llamando a `evaluate` una vez por ciclo, no es
responsabilidad de este módulo.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field

import numpy as np


@dataclass
class ConfusionMatrix:
    tp: int
    fp: int
    fn: int
    tn: int

    @property
    def n(self) -> int:
        return self.tp + self.fp + self.fn + self.tn


@dataclass
class DerivedMetrics:
    precision: float | None
    recall: float | None
    f1: float | None
    iou: float | None
    kappa: float | None
    mcc: float | None


@dataclass
class AreaMetrics:
    susceptible_km2: float
    real_km2: float
    diff_km2: float
    pct_error_signed: float | None  # None si real_km2 == 0
    pct_error_abs: float | None


@dataclass
class EvaluationResult:
    confusion: ConfusionMatrix
    derived: DerivedMetrics
    area: AreaMetrics
    buffered_agreement_pct: float | None
    n_valid_px: int
    hand_bins: list[dict] = field(default_factory=list)


def confusion_matrix(susceptible: np.ndarray, real: np.ndarray,
                     valid: np.ndarray | None = None) -> ConfusionMatrix:
    """`susceptible`/`real` son booleanos sobre la misma grilla. `valid`
    (opcional) excluye píxeles sin dato en cualquiera de las dos capas —
    sin él, se asume que ambas ya vienen completas."""
    if valid is None:
        valid = np.ones(susceptible.shape, dtype=bool)
    s, r, v = susceptible & valid, real & valid, valid
    tp = int((s & r).sum())
    fp = int((s & ~r & v).sum())
    fn = int((~s & r & v).sum())
    tn = int((~s & ~r & v).sum())
    return ConfusionMatrix(tp=tp, fp=fp, fn=fn, tn=tn)


def derived_metrics(cm: ConfusionMatrix) -> DerivedMetrics:
    tp, fp, fn, tn = cm.tp, cm.fp, cm.fn, cm.tn
    n = cm.n

    precision = tp / (tp + fp) if (tp + fp) > 0 else None
    recall = tp / (tp + fn) if (tp + fn) > 0 else None
    f1 = (2 * precision * recall / (precision + recall)
          if precision is not None and recall is not None
          and (precision + recall) > 0 else None)
    iou = tp / (tp + fp + fn) if (tp + fp + fn) > 0 else None

    if n > 0:
        po = (tp + tn) / n
        pe = ((tp + fp) * (tp + fn) + (fn + tn) * (fp + tn)) / (n * n)
        kappa = (po - pe) / (1 - pe) if pe < 1 else None
    else:
        kappa = None

    mcc_denom = float(np.sqrt(float(tp + fp) * (tp + fn) * (tn + fp) * (tn + fn)))
    mcc = ((tp * tn - fp * fn) / mcc_denom) if mcc_denom > 0 else None

    return DerivedMetrics(precision=precision, recall=recall, f1=f1,
                          iou=iou, kappa=kappa, mcc=mcc)


def area_metrics(susceptible: np.ndarray, real: np.ndarray,
                 pixel_area_km2: float,
                 valid: np.ndarray | None = None) -> AreaMetrics:
    if valid is None:
        valid = np.ones(susceptible.shape, dtype=bool)
    susceptible_km2 = float((susceptible & valid).sum()) * pixel_area_km2
    real_km2 = float((real & valid).sum()) * pixel_area_km2
    diff_km2 = susceptible_km2 - real_km2
    if real_km2 > 0:
        pct_signed = 100.0 * diff_km2 / real_km2
        pct_abs = abs(pct_signed)
    else:
        pct_signed = pct_abs = None
    return AreaMetrics(susceptible_km2=susceptible_km2, real_km2=real_km2,
                       diff_km2=diff_km2, pct_error_signed=pct_signed,
                       pct_error_abs=pct_abs)


def buffered_agreement(susceptible: np.ndarray, real: np.ndarray,
                       resolution_m: float, buffer_tolerance_m: float,
                       valid: np.ndarray | None = None) -> float | None:
    """% del área real dentro de `buffer_tolerance_m` de una zona
    susceptible — no exige coincidencia píxel a píxel, solo cercanía.
    None si no hay ningún píxel real (nada que medir)."""
    from scipy.ndimage import distance_transform_edt

    if valid is None:
        valid = np.ones(susceptible.shape, dtype=bool)
    real_v = real & valid
    denom = int(real_v.sum())
    if denom == 0:
        return None
    if not susceptible.any():
        return 0.0
    dist_px = distance_transform_edt(~susceptible)
    dist_m = dist_px * resolution_m
    within = real_v & (dist_m <= buffer_tolerance_m)
    return 100.0 * int(within.sum()) / denom


HAND_BIN_EDGES = (0.0, 1.0, 3.0, 5.0, 10.0, 15.0, np.inf)


def stratify_by_hand_bin(susceptible: np.ndarray, real: np.ndarray,
                         hand: np.ndarray,
                         bin_edges=HAND_BIN_EDGES) -> list[dict]:
    """Desglosa la matriz de confusión y sus métricas derivadas por bin de
    HAND (metros sobre el drenaje más cercano) — el desacuerdo cerca de un
    cauce no es el mismo fenómeno que el desacuerdo en una ladera, y un
    solo número agregado lo esconde. Píxeles con HAND no finito (fuera de
    cualquier cadena de flujo hacia un cauce dentro del margen — ver
    terrain.py) quedan fuera de todos los bins, no en uno aparte: no hay
    evidencia de a qué bin pertenecerían."""
    resultado = []
    finito = np.isfinite(hand)
    for lo, hi in zip(bin_edges[:-1], bin_edges[1:]):
        en_bin = finito & (hand >= lo) & (hand < hi)
        if not en_bin.any():
            continue
        cm = confusion_matrix(susceptible, real, valid=en_bin)
        resultado.append({
            "hand_min_m": None if np.isinf(lo) else lo,
            "hand_max_m": None if np.isinf(hi) else hi,
            "n_px": int(en_bin.sum()),
            "confusion": asdict(cm),
            "derived": asdict(derived_metrics(cm)),
        })
    return resultado


def evaluate(susceptible: np.ndarray, real: np.ndarray, *,
            pixel_area_km2: float, resolution_m: float,
            buffer_tolerance_m: float = 100.0,
            valid: np.ndarray | None = None,
            hand: np.ndarray | None = None) -> EvaluationResult:
    """Arma el reporte completo: matriz de confusión + métricas derivadas
    + error de área + agreement con tolerancia espacial +, si se pasa
    `hand` (de `fusion.FusionResult.hand`), desglose por bin de HAND."""
    cm = confusion_matrix(susceptible, real, valid=valid)
    derived = derived_metrics(cm)
    area = area_metrics(susceptible, real, pixel_area_km2, valid=valid)
    buffered = buffered_agreement(susceptible, real, resolution_m,
                                  buffer_tolerance_m, valid=valid)
    hand_bins = (stratify_by_hand_bin(susceptible, real, hand)
                if hand is not None else [])
    n_valid = int(valid.sum()) if valid is not None else int(susceptible.size)
    return EvaluationResult(confusion=cm, derived=derived, area=area,
                            buffered_agreement_pct=buffered,
                            n_valid_px=n_valid, hand_bins=hand_bins)


def evaluation_to_dict(result: EvaluationResult) -> dict:
    """Representación serializable a JSON (dataclasses anidadas -> dict)."""
    return {
        "confusion_matrix": asdict(result.confusion),
        "derived_metrics": asdict(result.derived),
        "area_metrics": asdict(result.area),
        "buffered_agreement_pct": result.buffered_agreement_pct,
        "n_valid_px": result.n_valid_px,
        "hand_bins": result.hand_bins,
    }
