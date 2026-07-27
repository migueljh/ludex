# Tarea A — `packages/teams`

## Status

Completa. El paquete es una librería independiente, usa el parser oficial
`Teams.import()`, recibe la generación como parámetro, acumula errores
accionables y consulta solo `generations`, `pokemon`, `moves`, `abilities`,
`items` y `learnsets`.

Se conservó el trabajo previo. El único defecto funcional encontrado en el
código heredado estaba en `audit.ts`: `TeamValidator.validateMoves` muta
`PokemonSources`, pero la auditoría reutilizaba el mismo objeto entre todos los
movimientos de una forma. Eso generaba falsos positivos acumulativos. Hay un
test de regresión que primero falló con `Arceus-Bug / Blast Burn` y luego pasó
al crear fuentes nuevas por cada par `(forma, movimiento)`.

## Verificación

Ejecutada con Node `v22.16.0`, PostgreSQL por
`127.0.0.1:15432` y `pokemon-showdown@0.11.10`:

```text
Test Files  2 passed (2)
Tests       15 passed (15)
```

`tsc --noEmit` terminó con código 0.

Los tests cubren equipo legal, movimiento ilegal, movimiento inexistente,
especie ausente en la generación, límites de EV/IV/nivel, habilidad, objeto,
acumulación de cuatro errores y las formas Rotom-Wash, Gourgeist-Super y
Deoxys-Attack.

## Demostración punta a punta

Entrada legal real exportada del Teambuilder:

```text
Chompy (Garchomp) (M) @ Life Orb
Ability: Rough Skin
Level: 50
EVs: 252 Atk / 4 SpD / 252 Spe
Jolly Nature
- Earthquake
- Outrage
- Swords Dance
- Fire Fang

Rotom-Wash @ Leftovers
Ability: Levitate
EVs: 252 HP / 200 Def / 56 Spe
Bold Nature
IVs: 0 Atk
- Hydro Pump
- Volt Switch
- Will-O-Wisp
- Pain Split

Pinky (Clefable) @ Rocky Helmet
Ability: Magic Guard
EVs: 252 HP / 172 Def / 84 Spe
Calm Nature
IVs: 0 Atk / 30 SpA
- Moonblast
- Hidden Power Fire
- Soft-Boiled
- Thunder Wave

Bisharp @ Black Glasses
Ability: Defiant
EVs: 252 Atk / 4 SpD / 252 Spe
Adamant Nature
- Knock Off
- Sucker Punch
- Iron Head
- Swords Dance

Azumarill @ Choice Band
Ability: Huge Power
EVs: 252 HP / 252 Atk / 4 SpD
Adamant Nature
- Play Rough
- Aqua Jet
- Waterfall
- Superpower

Gourgeist-Super @ Leftovers
Ability: Frisk
EVs: 252 HP / 252 Def / 4 SpD
Impish Nature
- Shadow Ball
- Seed Bomb
- Trick-or-Treat
- Will-O-Wisp
```

Salida:

```json
{
  "ok": true,
  "gen": 6,
  "sets": 6,
  "issues": []
}
```

Entrada con cuatro errores:

```text
Incineroar @ Leftovers
Ability: Intimidate
- Flare Blitz

Garchomp @ Life Orb
Ability: Rough Skin
- Ice Beam

Bisharp @ Black Glasses
Ability: Defiant
- Not A Real Move

Azumarill @ Choice Band
Ability: Huge Power
EVs: 300 Atk
- Aqua Jet
```

Salida:

```json
{
  "ok": false,
  "gen": 6,
  "sets": 4,
  "issues": [
    {
      "pokemon": "Incineroar",
      "field": "species",
      "kind": "unknown",
      "message": "'Incineroar' no existe en gen 6 (aparece en gen 7)"
    },
    {
      "pokemon": "Garchomp",
      "field": "move",
      "kind": "illegal",
      "move": "Ice Beam",
      "message": "Garchomp no puede aprender 'Ice Beam' en gen 6"
    },
    {
      "pokemon": "Bisharp",
      "field": "move",
      "kind": "unknown",
      "move": "Not A Real Move",
      "message": "el movimiento 'Not A Real Move' no existe (gen 6)"
    },
    {
      "pokemon": "Azumarill",
      "field": "evs",
      "kind": "invalid",
      "message": "EVs de Atk fuera de rango: 300 (maximo 252 por stat)"
    }
  ]
}
```

## Auditoría de learnsets

Resultado completo sobre las generaciones seedeadas:

| Generación | Faltan en DB | Sobran frente al oráculo | Total |
|---|---:|---:|---:|
| 6 | 0 | 342 | 342 |
| 9 | 15 | 3.205 | 3.220 |

Los 15 agujeros son:

```text
arcaninehisui/headsmash
dugtrioalola/thrash
electrodehisui/leechseed
electrodehisui/worryseed
golemalola/screech
golemalola/zapcannon
graveleralola/screech
graveleralola/zapcannon
mukalola/assurance
mukalola/clearsmog
ninetalesalola/moonblast
persianalola/flatter
persianalola/partingshot
sandslashalola/mirrorcoat
zoroarkhisui/comeuppance
```

Esto amplía D14: no es solo `Ninetales-Alola/Moonblast`; hay 14 faltantes
adicionales, todos en formas regionales de gen 9. No se modificó el seed.

De los 3.547 extras de ambas generaciones:

- 2.058 son rechazos por transferencia entre generaciones o HM.
- 1.489 son rechazos donde Showdown dice que la forma no aprende el movimiento.

Los canarios pedidos de gen 6 no tienen agujeros: Rotom-Wash/Hydro Pump,
Gourgeist-Super/Shadow Ball y Seed Bomb, y Deoxys-Attack/Psycho Boost validan.

## Concerns

- `learnsets` conserva métodos históricos por D3, mientras
  `TeamValidator(genNou)` aplica además restricciones de transferencia y
  compatibilidad. Por eso los 2.058 extras de transferencia no demuestran por sí
  solos corrupción del seed; sí son una diferencia real que un futuro
  consumidor de legalidad estricta deberá filtrar usando `learn_methods`.
- Las formas cosméticas/de batalla generan diferencias de semántica frente a
  OU. La auditoría las reporta y no las corrige.
- El wrapper global de `pnpm` cayó en Node 18 durante esta sesión. La
  verificación se ejecutó con los entrypoints pineados y Node 22 explícito.
