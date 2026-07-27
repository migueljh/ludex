# Ludex — Fase 2, rebanada 1: conexión, estado y persistencia

Fecha: 2026-07-26
Estado: aprobado
Alcance: primera rebanada de la fase 2 del plan de `docs/PLAN.md`

## 1. Objetivo

Que el agente se conecte al server local de Showdown, juegue batallas completas
contra un bot baseline, y **grabe cada turno de forma que sirva para entrenar
modelos dentro de un año**.

El agente de esta rebanada es deliberadamente tonto: elige al azar entre las
acciones legales. No hay LLM. El entregable no es que juegue bien, es que
**grabe bien**.

## 2. Por qué esta rebanada es primero

La fase 2 son cuatro subsistemas: el servicio de cálculo de daño, la conexión a
Showdown, el grafo de LangGraph y el serializador de estado. Tres son
reversibles. El serializador no lo es del todo: si captura mal, los datos de
todas las batallas jugadas hasta que alguien lo note quedan inservibles para la
fase 8, y el plan general lo marca como "lo único difícil de cambiar después".

Poner el riesgo irreversible primero y validarlo con un agente random cuesta
mucho menos que descubrirlo con el grafo entero construido encima.

## 3. Fuera de alcance

Deliberado, no olvido:

- **Nada de LLM.** El nodo `decide` y el switch de modelos son la rebanada
  siguiente.
- **Nada de cálculo de daño.** El agente random no lo necesita. La decisión de
  usar un servicio Node con `@smogon/calc` ya está tomada (ver D16), pero se
  construye en paralelo y no se consume acá.
- **Nada de FastAPI, WebSockets ni aprobación humana.** Eso es fase 3.
- **Nada de conexión al server oficial.** Solo `local`.
- **Nada de equipos propios.** Se juega `gen6randombattle`, que el servidor
  genera (ver D19).
- **Nada de LangGraph.** El grafo entra con el nodo `decide`. Esta rebanada es
  un loop directo sobre poke-env.

## 4. Decisiones

Se replican en `docs/DECISIONS.md` al implementar.

### D16 — Cálculo de daño: servicio Node con `@smogon/calc`

`@smogon/calc` es la calculadora oficial de Smogon, mantenida y parametrizada
por generación. No hay equivalente en Python.

Motivo: el cálculo contempla los 16 rolls de daño, STAB, efectividad, clima,
pantallas, habilidades que modifican daño, objetos, críticos, quemadura y
multi-golpes, con diferencias por generación. Una reimplementación en Python no
falla ruidosamente: devuelve números sutilmente equivocados, y el agente decide
"esto no lo mata" cuando sí lo mataba.

Costo aceptado: un contenedor más y una llamada HTTP local por turno,
despreciable frente a la latencia de un LLM. Ventaja: la web de la fase 4 le
pega al mismo servicio.

`packages/seed` seguirá siendo Node, pero es una herramienta de build: no hay
Node vivo durante una batalla salvo el servicio de calc.

### D17 — El protocolo crudo es la fuente de verdad; el estado derivado es una vista

Se persisten **ambos**: el stream de protocolo tal como lo recibe cada jugador,
turno por turno, y el estado normalizado que produce el serializador, con su
`state_schema_version`.

Motivo: convierte la única decisión irreversible de la fase en reversible. Si
dentro de seis meses se descubre que el serializador filtraba información oculta
o le faltaba un campo, se re-deriva todo el histórico sin perder una batalla. El
costo es texto, que es barato.

Consecuencia de diseño: el serializador debe ser una función pura del protocolo
crudo más el estado de poke-env, sin depender de nada que no quede persistido.

### D18 — El serializador es una lista blanca explícita, nunca una copia

El serializador nombra campo por campo lo que entra en el estado. Está prohibido
recorrer atributos del objeto `Battle` de poke-env o serializarlo genéricamente.

Motivo: poke-env expone tu equipo completo y el del rival con la misma forma,
pero del rival solo debés ver lo revelado. Una copia genérica filtra el equipo
entero del oponente en el turno 1. Con lista blanca, un campo nuevo en una
versión futura de la librería no se cuela solo.

### D19 — Se juega `gen6randombattle`, no `gen6ou`

Los equipos los genera el servidor.

Motivo: construir y validar equipos es fase 5. Y como el server local genera los
dos equipos, tenemos la verdad completa contra la cual contrastar lo que el
serializador *debería* haber visto — un banco de pruebas mejor que un formato
con equipos propios.

### D20 — Un solo escritor por directorio

Cada tarea declara sus archivos exactos. Tocar algo fuera de esa lista es un
hallazgo de review, no una decisión del implementador. Si dos tareas necesitan
el mismo archivo, están mal cortadas.

Motivo: los tres choques de la fase anterior (una rama pisada, dos commits que
se llevaron trabajo ajeno) salieron de un plan que asumía un solo escritor.

Corolario operativo: **commits con `git commit -m "..." -- <rutas>`**. Nunca
`git add` suelto seguido de `git commit`, porque `git commit` sin `--` commitea
todo el índice, incluido lo que otro agente dejó preparado.

## 5. Reparto paralelo

Dos frentes que no comparten un solo archivo, ni lenguaje, ni gestor de
paquetes:

| frente | directorios | depende de |
|---|---|---|
| **Kimi** | `packages/calc/**` | nada |
| **Nosotros** | `db/migrations/**`, `apps/agent/**` | nada externo |

El único punto de contacto es un contrato HTTP que **no se consume en esta
rebanada**. Kimi puede construir, testear y cerrar calc sin esperar nada.

Orden interno de nuestro frente:

```
1. migraciones            ← gate: nadie escribe Python contra tablas que no existen
2. state/       (puro, sin DB ni red)
3. showdown/    (conexión y captura de protocolo)
4. db/          (modelos y repositorio)
5. cli + integración      ← converge 2, 3 y 4
6. tests de fuga y de re-derivación
```

Las tareas 2, 3 y 4 tocan directorios disjuntos. Se ejecutan de a una por la
regla del framework de no despachar implementadores en paralelo, pero el corte
limpio garantiza que ninguna pise a la otra y permite reordenarlas si una se
traba.

## 6. Estructura de archivos

```
packages/calc/                 # Kimi, íntegro
apps/agent/
  pyproject.toml               # uv, Python 3.12
  src/ludex_agent/
    config.py                  # env vars, sin lógica
    db/
      models.py                # modelos SQLAlchemy escritos a mano (D1)
      session.py               # engine y factory de sesión
      repository.py            # escrituras: battles, turns, trajectories
    showdown/
      client.py                # subclase de Player de poke-env
      protocol.py              # captura del stream crudo por jugador
    state/
      schema.py                # dataclasses del estado + versión
      serializer.py            # Battle -> dict, lista blanca (D18)
      actions.py               # extracción de acciones legales
    cli.py                     # `agent play --n N`
  tests/
    state/                     # puros, con fixtures de protocolo
    db/
    integration/
```

`state/` no importa nada de `db/` ni de red: es la capa pura de esta fase, el
equivalente a `extract/` en la anterior. `db/` no importa nada de poke-env.

## 7. Esquema

Migración nueva con las tablas de la sección 4 del plan general que esta
rebanada usa. `analyses`, `playbook_rules` y `evals` **no** se crean todavía.

```sql
battles(
  id, tournament_id NULL, round_id NULL, format, p1, p2, winner,
  played_by enum('bot','human'), source enum('challenge','ladder','local','import'),
  replay_url NULL, raw_log text, created_at
)

battle_turns(
  battle_id, turn_number, protocol_lines text[], agent_reasoning jsonb NULL,
  PRIMARY KEY (battle_id, turn_number)
)

trajectories(
  id, battle_id, gen_id, format, player_side, final_result enum('win','loss'),
  elo_bucket NULL, created_at
)

trajectory_steps(
  trajectory_id, turn_number, state jsonb, state_schema_version int,
  legal_actions jsonb, action_taken jsonb,
  action_source enum('agent','human','opponent'), reward numeric NULL,
  PRIMARY KEY (trajectory_id, turn_number)
)
```

`tournament_id` y `round_id` son nullable porque las batallas de esta rebanada
no pertenecen a ningún torneo: el torneo llega en la fase 5.

## 8. El serializador

Contiene, por lista blanca:

- **Mi lado**: los seis pokémon con especie, HP actual y máximo, estado
  alterado, boosts, movimientos conocidos con sus PP, objeto y habilidad si son
  conocidos, cuál está activo.
- **El lado del rival**: solo los pokémon **revelados**, con lo revelado de cada
  uno. Nada más.
- **El campo**: clima con sus turnos restantes, terreno, hazards por lado,
  side conditions, número de turno.
- **Las acciones legales** del turno, que sin ellas no se puede entrenar una
  política.

`reward` se escribe al terminar la batalla, propagado a todos los pasos: +1 si
ganó, −1 si perdió.

## 9. Verificación

Cuatro capas, en orden de valor:

**Test de fuga de información.** Sobre batallas reales: para cada turno *N*,
ningún pokémon del rival puede aparecer en el estado si el protocolo crudo no lo
reveló hasta ese turno. El protocolo persistido es el juez. Es la propiedad de
corrección más importante de la rebanada y no puede pasar por casualidad.

**Test de re-derivación.** Tomar el protocolo crudo de una batalla grabada,
volver a correr el serializador, y verificar que da idéntico al estado
persistido. Es lo que prueba que D17 se cumple de verdad y que el histórico es
recuperable.

**Tests puros de `state/`.** Con fixtures de protocolo commiteadas, sin red ni
base: serialización de casos concretos, extracción de acciones legales,
normalización de campos.

**Test de integración.** Cinco batallas completas contra el bot random en el
server local, verificando conteos, que no queden filas huérfanas, y que correrlo
dos veces no duplique.

## 10. Criterios de aceptación

1. `agent play --n 5 --format gen6randombattle` corre cinco batallas completas
   contra el bot random de poke-env en el server local, sin crashear.
2. Cada batalla deja su fila en `battles` y en `trajectories`, con resultado.
3. Cada turno deja el protocolo crudo, el estado derivado con
   `state_schema_version = 1`, y las acciones legales.
4. El test de fuga pasa.
5. El estado re-derivado desde el protocolo crudo es idéntico al persistido.
6. Correrlo dos veces no duplica batallas ni deja filas huérfanas.
7. `reward` está propagado en todos los pasos de las batallas terminadas.
8. `grep -ri "gen6" apps/agent/src/` no devuelve nada fuera de configuración.

## 11. Riesgos

| riesgo | mitigación |
|---|---|
| La API de poke-env no expone el protocolo crudo por jugador de forma limpia | Es el primer paso de implementación y hay que verificarlo empíricamente antes de escribir el plan, igual que se hizo con `pokemon-showdown` en la fase anterior. Si no lo expone, hay que interceptar el handler de mensajes. |
| El serializador filtra información oculta sin que nadie lo note | El test de fuga corre sobre batallas reales y usa el protocolo como juez. |
| El formato del estado resulta insuficiente para la fase 8 | D17: el protocolo crudo permite re-derivar. |
| Dos agentes escriben el mismo archivo | D20 y el corte por directorios. |

## 12. Antes de escribir el plan

La fase anterior salió bien en buena medida porque antes de escribir el plan se
instaló `pokemon-showdown` y se lo inspeccionó, lo que reveló que los mods no
filtran por generación — el defecto que habría roto todo.

Acá corresponde lo mismo con **poke-env**: verificar empíricamente cómo expone
el objeto `Battle`, si da acceso al stream crudo por jugador, cómo entrega las
acciones legales, y qué trae exactamente `opponent_team` en el turno 1. Los
valores concretos van al plan, no a esta spec.
