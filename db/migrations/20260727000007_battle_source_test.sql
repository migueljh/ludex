-- migrate:up

-- I6 (review final): los tests de integracion escriben en las MISMAS tablas
-- que el dataset de entrenamiento (test_repository.py inserta filas con
-- format='f'/p1='A'/p2='B', y test_play.py juega batallas reales contra el
-- mismo Postgres compartido). Ya hubo un test en esta rama que corrompio
-- datos reales.
--
-- 'test' distingue esas filas sinteticas del resto sin necesitar una base
-- separada. Contrato del dataset (ver D19 en docs/DECISIONS.md): cualquier
-- consumidor del dataset de entrenamiento debe filtrar `source <> 'test'`,
-- igual que ya debia filtrar `final_result IS NOT NULL` (I5).
ALTER TYPE battle_source ADD VALUE 'test';

-- migrate:down
-- Postgres no permite quitar un valor de un ENUM sin recrear el tipo. Si
-- hiciera falta revertir, hay que verificar antes que ninguna fila use
-- 'test' (`SELECT 1 FROM battles WHERE source = 'test'`) y recrear el tipo
-- a mano. No se automatiza aca para no borrar datos por accidente.
