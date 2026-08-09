"""flood_validation — valida el producto de susceptibilidad de anegamiento
(meteorologia-flood-projections) contra una capa de "anegamiento real"
estimada con sensores remotos públicos (Sentinel-1, Sentinel-2, opcionalmente
Dynamic World).

Proyecto standalone, extraído de meteorologia-flood-monitor (repo hermano).
Las funciones de AOI/fecha/lectura Sentinel-1 que antes importaba de su
`flood_monitor.py` viven ahora vendorizadas en `sensing.py` (mismo
directorio que este paquete, sin paquete propio) — ver el docstring de ese
módulo para qué implica mantenerlas sincronizadas a mano.
"""
