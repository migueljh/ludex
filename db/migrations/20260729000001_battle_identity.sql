-- migrate:up

-- MON-10/F2-03 (D36): `battle_tag` (`battle-<formato>-<N>`) no es un
-- identificador global. `N` es el contador del server de Showdown, que vive
-- en `logs/lastbattle.txt` DENTRO del contenedor sin volumen: un rebuild lo
-- reinicia en 1 y reusa tags viejos para batallas completamente distintas.
-- Con `battle_tag UNIQUE`, dos batallas distintas que comparten tag, p1, p2 y
-- format se fusionaban en silencio (reproducido con dos INSERT dentro de una
-- transaccion con ROLLBACK sobre datos reales, ver checkpoint de diagnostico
-- en MON-10).
--
-- `identity_key` reemplaza al tag como identidad: `ps-open-v1:sha256:<64hex>`
-- sobre el bloque de apertura PUBLICO del turno 0 (protocol.py,
-- `compute_opening_identity`). La unicidad es por `(source, identity_key)`,
-- no global: identidad y procedencia quedan separadas a proposito, para que
-- un import y una grabacion local NO se fusionen mientras `battles.source`
-- siga gobernando que entra a training.
ALTER TABLE battles ADD COLUMN identity_key text;

-- Backfill: las filas historicas se escribieron bajo el regimen viejo, donde
-- `battle_tag` YA era global y unico. Ese hecho las hace trivialmente
-- irrepetibles sin necesidad de recalcular el fingerprint del protocolo: su
-- `ProtocolRecorder` murio con el proceso que las grabo, asi que no se pueden
-- re-persistir de todos modos. Documentado (D36): las filas legacy NO se
-- deduplican contra una futura reingesta de la misma batalla.
UPDATE battles SET identity_key = 'legacy:' || battle_tag WHERE identity_key IS NULL;

ALTER TABLE battles ALTER COLUMN identity_key SET NOT NULL;

-- El tag deja de ser identidad y vuelve a ser lo que siempre fue: la
-- etiqueta de sala del servidor. Sigue indexado porque se sigue consultando
-- por el (p.ej. re-persistencia desde el CLI).
ALTER TABLE battles DROP CONSTRAINT battles_battle_tag_key;
CREATE INDEX battles_source_battle_tag_idx ON battles (source, battle_tag);
ALTER TABLE battles ADD CONSTRAINT battles_source_identity_key_uniq UNIQUE (source, identity_key);

-- migrate:down

-- Preflight OBLIGATORIO antes de tocar nada: revertir reintroduce
-- `UNIQUE (battle_tag)` GLOBAL. Si para entonces existen dos filas con el
-- mismo battle_tag (el caso mismo que esta migracion vino a permitir de
-- forma segura via identity_key), ese ADD CONSTRAINT es irreversible sin
-- perder datos. Se aborta ruidoso, con el esquema intacto, en vez de dejar
-- que la constraint falle a mitad de camino o que alguien elija a mano que
-- fila borrar.
DO $$
DECLARE
  dup_count int;
BEGIN
  SELECT count(*) INTO dup_count FROM (
    SELECT battle_tag FROM battles GROUP BY battle_tag HAVING count(*) > 1
  ) d;
  IF dup_count > 0 THEN
    RAISE EXCEPTION
      'battle_identity down abortado: % battle_tag(s) tienen mas de una fila. '
      'Revertir a UNIQUE(battle_tag) global destruiria la separacion de '
      'identidad que esta migracion introdujo. Esquema NO modificado.',
      dup_count;
  END IF;
END $$;

ALTER TABLE battles DROP CONSTRAINT battles_source_identity_key_uniq;
DROP INDEX battles_source_battle_tag_idx;
ALTER TABLE battles ADD CONSTRAINT battles_battle_tag_key UNIQUE (battle_tag);
ALTER TABLE battles ALTER COLUMN identity_key DROP NOT NULL;
ALTER TABLE battles DROP COLUMN identity_key;
