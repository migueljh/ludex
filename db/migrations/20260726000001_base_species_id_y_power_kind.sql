-- migrate:up

-- D2 dice que la clave natural es el id normalizado de Showdown, pero
-- pokemon.base_species guardaba el NOMBRE legible ('Charizard'), asi que no
-- joineaba contra pokemon.showdown_id ('charizard') sin normalizar en cada
-- consulta. base_species pasa a guardar el id; el nombre legible se conserva
-- en base_species_name para la UI.
--
-- El backfill resuelve el id contra la propia tabla (la forma default de cada
-- especie base tiene name == base_species), NO reimplementando toID() en SQL:
-- los nombres almacenados usan unicode descompuesto (NFD, p. ej. 'Flabébé'
-- como e + acento combinante), y un regexp sobre eso no reproduce el id de
-- forma confiable. Verificado: 0 filas sin match en gens 6 y 9.
ALTER TABLE pokemon ADD COLUMN base_species_name text;
UPDATE pokemon SET base_species_name = base_species;
UPDATE pokemon p SET base_species = b.showdown_id
FROM pokemon b
WHERE b.gen_id = p.gen_id AND b.name = p.base_species_name AND b.is_default;
ALTER TABLE pokemon ALTER COLUMN base_species_name SET NOT NULL;

-- moves.power = 0 significa cuatro cosas distintas (estado, poder variable,
-- daño fijo, casos especiales). power_kind las distingue sin parsear
-- description. No se puede backfillear en SQL: basePowerCallback y damage no
-- existen en la base, viven solo en el paquete. La columna queda NULL hasta
-- el proximo seed de cada generacion, que reescribe todas las filas por
-- upsert (misma version pineada del paquete, mismo universo de showdown_ids).
ALTER TABLE moves ADD COLUMN power_kind text
  CHECK (power_kind IN ('status', 'variable', 'fixed_damage', 'special', 'standard'));
COMMENT ON COLUMN moves.power_kind IS
  'Derivado por el seed, en este orden: status si category=Status; variable si tiene basePowerCallback; fixed_damage si tiene damage numerico o ''level''; special si basePower=0 y ninguna anterior; standard si basePower>0. NULL solo entre esta migracion y el proximo seed de la generacion.';

-- migrate:down
ALTER TABLE moves DROP COLUMN power_kind;
UPDATE pokemon p SET base_species = b.name
FROM pokemon b
WHERE b.gen_id = p.gen_id AND b.showdown_id = p.base_species AND b.is_default;
ALTER TABLE pokemon DROP COLUMN base_species_name;
