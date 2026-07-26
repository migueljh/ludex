# `Ludex` — Plataforma de IA para torneos de Pokémon Showdown

Documento de planning para implementar con Claude Code. Leer completo antes de escribir código. Las fases están ordenadas y cada una tiene criterios de aceptación: no avanzar de fase sin cumplirlos.

## 1. Visión

Plataforma web personal para torneos de Pokémon Showdown entre amigos. El torneo se juega por rondas basadas en gimnasios de un juego real (ej. ronda 1 = hasta el gimnasio 1, usando solo pokémon y objetos conseguibles hasta ese punto). La plataforma:

1. Tiene toda la data oficial de la generación del torneo cargada localmente (sin búsquedas en internet en runtime).
2. Incluye un agente de IA que juega batallas en el server oficial de Showdown, logueado con una cuenta propia, o contra un server local propio para generar volumen de datos.
3. Muestra la batalla en vivo dentro de la web con el visor oficial de Showdown embebido, junto a un panel con el razonamiento del agente turno a turno.
4. Antes de cada movimiento, el agente propone su jugada con justificación y espera aprobación humana (human-in-the-loop). Existe un toggle de "modo autónomo" que desactiva la aprobación.
5. Permite cambiar el modelo de IA (proveedor y modelo) desde la UI, incluso entre turnos.
6. Analiza batallas terminadas (del bot o del usuario) y genera informes para mejorar ronda a ronda, acumulando perfiles de los rivales.
7. Administra las rondas del torneo: qué pokémon, movimientos y objetos están disponibles en cada una.
8. Captura todas las batallas en un formato apto para entrenar modelos propios más adelante (ver sección 12).

**Requisito transversal: multi-generación.** El torneo actual es Gen 6 (X/Y), pero el próximo torneo (año que viene) será otra generación. Nada del código debe hardcodear "gen6": todo se parametriza por formato/generación. Agregar una generación nueva debe ser: correr el seed con `--gen 9`, crear un torneo nuevo en la UI, listo.

## 2. Stack

- **Monorepo** con pnpm workspaces + un paquete Python.
- **apps/web**: Next.js 15 (App Router), TypeScript, Tailwind, shadcn/ui.
- **apps/agent**: Python 3.12, FastAPI, LangGraph, LangChain (solo `init_chat_model` y clientes de chat; no usar chains legacy), poke-env, SQLAlchemy + asyncpg, uv como package manager.
- **packages/seed**: scripts Node/TypeScript que usan el paquete npm `pokemon-showdown` para volcar la data de una generación a la DB.
- **packages/calc**: microservicio Node mínimo (Fastify o Hono) que envuelve `@smogon/calc` y expone cálculo de daño por HTTP. Lo consumen el agente y la web. `@smogon/calc` acepta el parámetro de generación.
- **packages/viewer**: visor de batallas embebible, basado en el battle engine del cliente oficial (`smogon/pokemon-showdown-client`). Recibe el protocolo de batalla por WebSocket y lo renderiza con sprites y animaciones oficiales. El protocolo de Showdown es el mismo para todas las gens, así que el viewer es gen-agnostic por diseño.
- **Postgres 16 + pgvector** en Docker (docker-compose en la raíz).
- Todo corre local por ahora. No acoplar nada a un proveedor de hosting, pero mantener el diseño 12-factor (config por env vars) para deployar después sin refactor.

## 3. Estructura del repo

```
<nombre>/
  docker-compose.yml          # postgres + calc + viewer + agent + web + showdown-local (profiles)
  .env.example                # todas las vars documentadas
  apps/
    web/                      # Next.js
    agent/                    # FastAPI + LangGraph
  packages/
    seed/                     # dump de data por generación
    calc/                     # damage calc service
    viewer/                   # battle viewer embebible
  ml/                         # track de aprendizaje (fase 8, ver sección 12)
    datasets/                 # exports de trayectorias
    training/                 # scripts de entrenamiento
  docs/
    PLAN.md                   # este archivo
    DECISIONS.md              # registro de decisiones que se tomen durante el desarrollo
```

## 4. Modelo de datos (Postgres)

Principio: la data de juego se versiona por generación; la data de torneo referencia una generación. Esquema inicial (ajustar nombres/detalles si hace falta, pero mantener el principio):

```sql
-- Data de juego (poblada por packages/seed, inmutable en runtime)
generations(id, gen_number, label)                    -- ej. (.., 6, 'XY/ORAS')
pokemon(id, gen_id, dex_num, name, types[], base_stats jsonb,
        abilities jsonb, weight, evolves_from, tier)
moves(id, gen_id, name, type, category, power, accuracy, pp,
      priority, flags jsonb, description)
learnsets(pokemon_id, move_id, learn_methods jsonb)   -- nivel, MT, huevo, tutor
items(id, gen_id, name, description, flags jsonb)
abilities(id, gen_id, name, description)
type_chart(gen_id, attacking_type, defending_type, multiplier)
usage_stats(gen_id, format, pokemon_id, usage_pct, common_sets jsonb)  -- opcional, Smogon

-- Torneo
tournaments(id, name, gen_id, format, ruleset jsonb, status, created_at)
rounds(id, tournament_id, round_number, gym_label, level_cap, notes)
round_availability(round_id, pokemon_id, obtainable_notes)   -- qué se puede usar
round_items(round_id, item_id)
players(id, tournament_id, showdown_username, display_name, notes)
player_profiles(player_id, profile jsonb, updated_at)        -- patrones, tendencias
player_teams(id, player_id, round_id, team jsonb, source)    -- equipos vistos

-- Batallas y análisis
battles(id, tournament_id, round_id, format, p1, p2, winner,
        played_by enum('bot','human'), source enum('challenge','ladder','local','import'),
        replay_url, raw_log text, created_at)
battle_turns(battle_id, turn_number, protocol_lines text[],
             agent_reasoning jsonb)
analyses(id, battle_id, model_used, report_md text, lessons jsonb,
         embedding vector(1536), created_at)

-- Mejora medida
playbook_rules(id, tournament_id, gen_id, rule_text, rationale,
               status enum('proposed','active','retired'),
               source_analysis_id, version, created_at)
evals(id, playbook_version, model_id, baseline_opponent, n_battles,
      wins, losses, avg_turns, notes, created_at)

-- Trayectorias para entrenamiento (fase 8, ver sección 12)
trajectories(id, battle_id, gen_id, format, player_side,
             final_result enum('win','loss'), elo_bucket, created_at)
trajectory_steps(trajectory_id, turn_number, state jsonb, legal_actions jsonb,
                 action_taken jsonb, action_source enum('agent','human','opponent'),
                 reward numeric)

-- Configuración
providers(id, name, base_url, api_key_env, enabled)          -- keys NUNCA en DB, solo el nombre de la env var
models(id, provider_id, model_id, label, is_default)
settings(key, value jsonb)                                   -- ej. autonomous_mode, connection_mode
```

## 5. Seed multi-generación (packages/seed)

- Script `pnpm seed --gen 6` (o 9, etc.).
- Usa el paquete npm `pokemon-showdown`: `Dex.mod('gen6')` devuelve pokédex, moves, learnsets, items, abilities y type chart exactamente como en esa generación (stats de la época, sin contenido posterior).
- Vuelca todo a Postgres bajo el `gen_id` correspondiente. Idempotente (upsert).
- Subcomando opcional `pnpm seed:usage --gen 6 --format gen6ou` que descarga las usage stats de Smogon (https://www.smogon.com/stats/) una sola vez y las carga.
- Regla dura: el agente y la web solo leen de Postgres. Cero fetch a internet en runtime.

## 6. Agente (apps/agent)

### Modos de conexión

Configurable por `CONNECTION_MODE` y `SHOWDOWN_SERVER_URL`:

- **`official`**: server oficial, login con `SHOWDOWN_USERNAME` / `SHOWDOWN_PASSWORD` (cuenta registrada). Soporta challenges directos y ladder.
- **`local`**: server propio (`smogon/pokemon-showdown` levantado con `--no-security`, incluido en docker-compose bajo un profile). Sin autenticación, sin rate limits, paralelismo alto. Es el modo por defecto para generar datos y correr evals.

Métodos de matchmaking, explícitos y separados (no mezclarlos en un helper genérico):

- `send_challenges(username, n)` — desafiar a un rival concreto.
- `accept_challenge(username)` — aceptar un desafío puntual, disparado desde la UI.
- `ladder(n_games)` — matchmaking público. Solo en modo `official`, solo cuando el usuario lo activa explícitamente desde la UI. Nunca es el default.
- `battle_against(opponent_player, n)` — modo local, contra un baseline de poke-env.

Nota operativa: el ladder es matchmaking público y ranqueado, y las reglas del sitio no permiten bots ahí; el riesgo es el baneo de la cuenta que también se usa para el torneo. Queda implementado porque el usuario lo pidió, pero por defecto apagado y con la advertencia visible en la UI. Para generar volumen de datos el modo `local` es estrictamente superior: más batallas por hora, baseline conocido, cero riesgo.

### Challenges entrantes en el dashboard

poke-env recibe el mensaje `|updatechallenges|` del server con los desafíos pendientes. Por defecto la librería los acepta automáticamente dentro de su loop: **hay que interceptar ese handler** para que en vez de aceptar, publique un evento por el WebSocket interno. El dashboard muestra la lista (quién desafía, formato, hora) con botones aceptar/rechazar, y la aceptación dispara `accept_challenge(username)`.

### Grafo LangGraph (por turno)

Nodos:
1. `parse_state`: estado de la batalla desde poke-env (equipos, HP, boosts, hazards, clima, side conditions, movimientos legales). Emite además el `state jsonb` normalizado que se persiste en `trajectory_steps` (ver sección 12).
2. `retrieve_context`: consulta a Postgres — data de los pokémon en juego, learnsets posibles del rival filtrados por `round_availability` de la ronda activa, perfil e historial del rival, reglas activas del playbook, lecciones relevantes de análisis previos (retrieval por pgvector cuando haya volumen).
3. `calc_damage`: llama a packages/calc para los matchups relevantes (daño de mis movimientos legales contra el rival activo, daño esperado del rival contra mi equipo, chequeo de velocidades). Determinista, sin LLM. Es donde se corrigen la mayoría de los errores.
4. `decide`: LLM con prompt estructurado que recibe todo lo anterior y devuelve JSON: `{action, target, reasoning, confidence, alternatives[]}`. Validar contra las acciones legales; si el LLM alucina una acción ilegal, reintentar una vez con el error en el prompt y si falla, elegir el mejor movimiento por daño calculado (fallback determinista).
5. `human_approval`: `interrupt()` de LangGraph. Publica la propuesta por WebSocket, espera la respuesta del front: aprobar, o pisar con otra acción legal. Si `settings.autonomous_mode` está activo, se saltea. En modo `local` siempre se saltea.
6. `execute`: envía la acción vía poke-env, persiste `battle_turns` y `trajectory_steps`.

Usar checkpointer de LangGraph (Postgres) para que los interrupts sobrevivan reinicios del proceso.

### Runner de batch

Comando `agent batch --n 200 --opponent simple-heuristics --format gen6ou --playbook-version 3` que corre N batallas en el server local con paralelismo configurable, persiste todo (batallas, trayectorias) y escribe una fila en `evals` con el resultado agregado. Es la herramienta central de la fase 7.

### Switch de modelos

- `init_chat_model` de LangChain con la config de la tabla `providers`/`models`.
- Proveedores iniciales:
  - OpenAI: nativo (`OPENAI_API_KEY`).
  - Google Gemini: nativo (`GOOGLE_API_KEY`).
  - Kimi/Moonshot: cliente OpenAI-compatible con `base_url=https://api.moonshot.ai/v1` (`MOONSHOT_API_KEY`).
  - OpenCode Zen: cliente OpenAI-compatible con `base_url=https://opencode.ai/zen/v1` (`OPENCODE_API_KEY`). Listar modelos disponibles desde su endpoint `/models` al configurar, no hardcodear el catálogo.
  - (Fase 8) Modelo local propio vía servidor OpenAI-compatible, para que entre por el mismo switch sin código nuevo.
- Endpoint `PATCH /settings/model` para cambiar el modelo activo; el grafo lee el modelo al inicio de cada turno, así el cambio aplica en el turno siguiente sin reiniciar la batalla.

### API (FastAPI)

- REST: torneos, rondas, disponibilidad, jugadores, batallas, análisis, playbook, evals, providers/models, settings.
- WS `/ws/battle/{battle_id}`: stream de protocolo + propuestas del agente + canal de aprobación.
- WS `/ws/lobby`: challenges entrantes, estado de conexión, batallas en curso.
- Endpoints para iniciar challenges, aceptar/rechazar entrantes, arrancar/parar ladder, lanzar batch, e importar un replay por URL.

## 7. Viewer en vivo (packages/viewer)

- Basarse en el battle engine del cliente oficial (repo `smogon/pokemon-showdown-client`, open source): la parte de replays/animaciones puede renderizar un log de protocolo sin server.
- Servirlo como página estática propia que abre un WebSocket al agent y va inyectando las líneas de protocolo a medida que llegan (mismo mecanismo que un replay, pero incremental).
- La web lo embebe en un `<iframe>` local. Al ser nuestro, no hay problema de X-Frame-Options.
- Los sprites/assets se cargan desde el CDN oficial de Showdown (carga estática del navegador, no fetch del agente).
- Fallback si el viewer se complica en la fase inicial: panel propio que renderiza el log parseado (texto + barras de HP) y botón "espectar en Showdown". El viewer oficial es el objetivo final.

## 8. Web (apps/web)

Pantallas:
1. **Dashboard**: torneo activo, ronda actual, **challenges entrantes con aceptar/rechazar**, batallas en curso, últimos análisis, estado de conexión (modo official/local).
2. **Batalla en vivo**: iframe del viewer + panel lateral con el razonamiento del agente por turno + tarjeta de aprobación (propuesta, justificación, alternativas, botones aprobar/elegir otra acción) + selector de modelo + toggle modo autónomo.
3. **Rondas**: CRUD de rondas del torneo; para cada ronda, marcar qué pokémon están disponibles (buscador sobre la pokédex de la gen del torneo), level cap, items, notas.
4. **Rivales**: ficha por jugador — equipos vistos por ronda, patrones detectados, historial de batallas, notas manuales.
5. **Batallas y análisis**: historial, detalle de cada batalla con replay embebido o log propio, y el informe de análisis. Importar replay por URL.
6. **Pokédex**: exploración de la data de la gen activa (stats, learnsets, type chart).
7. **Entrenamiento**: lanzar batches contra baselines, ver progreso, tabla de `evals` comparando versiones de playbook y modelos, gestión del playbook (reglas propuestas / activas / retiradas).
8. **Configuración**: providers y modelos, cuenta de Showdown, modo de conexión, torneo activo, toggle de ladder con su advertencia.

Diseño moderno, dark mode por defecto. La pantalla estrella es "Batalla en vivo".

## 9. Análisis post-batalla

- Worker que se dispara al terminar una batalla o al importar un replay.
- Input: log completo, equipos, ronda, perfil previo del rival, análisis previos, playbook activo.
- Output (LLM, mismo switch de modelos):
  1. Informe markdown: momentos clave, errores, líneas alternativas, evaluación de la elección de equipo.
  2. `lessons` estructuradas (JSON) acumulables: para el usuario ("tendés a X") y para el rival ("suele abrir con Y").
  3. Actualización de `player_profiles` (merge, no overwrite).
  4. Reglas candidatas para `playbook_rules` con estado `proposed`.
- Embeddings del informe en pgvector para retrieval durante batallas futuras.

**Advertencia de diseño**: acumular reglas sin curar degrada el rendimiento (prompt inflado, consejos contradictorios). Ninguna regla pasa a `active` sin una eval que la respalde. El límite de reglas activas se configura (arrancar en 15) y forzar retiro de las peores cuando se supera.

## 10. Fases de implementación

**Fase 0 — Infraestructura.**
docker-compose con Postgres+pgvector y el server local de Showdown bajo profile; monorepo con los paquetes esqueleto; migraciones (elegir migrador y documentar en DECISIONS.md); `.env.example` completo.
✓ `docker compose up` levanta DB y server local, y las migraciones corren.

**Fase 1 — Seed multi-gen.**
`pnpm seed --gen 6` puebla toda la data de juego. Probar también `--gen 9` para validar el diseño multi-gen.
✓ Queries de sanidad: cantidad de pokémon de gen 6 correcta, un learnset conocido verificado a mano, type chart correcta.

**Fase 2 — Agente por CLI.**
Grafo completo jugando en el server local contra un baseline, con calc service y switch de modelos.
✓ Juega batallas completas sin crashear, con `battle_turns` y `trajectory_steps` persistidos.

**Fase 3 — API + HITL + conexión oficial.**
FastAPI con WS, `interrupt()` de aprobación, checkpointer en Postgres, modo `official` con challenges entrantes y salientes.
✓ Desde un cliente WS de prueba se ve el protocolo en vivo, se aprueba cada movimiento, y se acepta un challenge real.

**Fase 4 — Web mínima + viewer.**
Next.js con dashboard (challenges entrantes) y batalla en vivo, aprobación desde la UI, selector de modelo.
✓ Jugar una batalla completa aprobando cada movimiento desde el navegador, viéndola animada.

**Fase 5 — Torneo.**
Rondas, disponibilidad, rivales, pokédex, configuración.
✓ Crear el torneo real, cargar la ronda 1, y que `retrieve_context` filtre por eso en una batalla.

**Fase 6 — Análisis.**
Worker de análisis, informes, perfiles de rivales, import de replays, retrieval de lecciones, propuestas de playbook.
✓ Importar un replay real y obtener un informe útil; en una segunda batalla contra el mismo rival, el contexto incluye su perfil.

**Fase 7 — Mejora medida.**
Runner de batch, tabla de evals, ciclo: batch local → análisis → reglas propuestas → eval A/B → activar o descartar. Pantalla de entrenamiento.
✓ Una regla del playbook activada porque una eval de 100+ batallas mostró mejora de winrate, y otra descartada por no mostrarla.

**Fase 8 — Aprendizaje propio.** Ver sección 12. No arrancar antes de tener volumen de trayectorias.

## 11. Reglas para el desarrollo

- Nada de "gen6" hardcodeado fuera de configuración/seed. Buscar `gen6` en el código antes de cerrar cada fase.
- El agente jamás hace requests a internet en runtime (excepto el WebSocket de Showdown y las APIs de LLM).
- API keys solo en env vars. La tabla `providers` guarda el nombre de la var, nunca el valor.
- Ladder apagado por defecto, con advertencia visible.
- Registrar toda decisión no trivial en docs/DECISIONS.md.
- Tests mínimos pero reales: parser de estado, fallback de acción ilegal, filtro de disponibilidad por ronda, seed (conteos por gen) y serializador de trayectorias.

## 12. Fase 8 — Aprendizaje propio (track de ML)

Esto es cualitativamente distinto de todo lo anterior. Las fases 1 a 7 mejoran **el contexto** que recibe un LLM cuyos pesos nunca cambian. Esta fase entrena **modelos propios** a partir de las batallas capturadas. No empezar hasta tener el dataset.

### Qué cambia respecto a lo que ya construimos

| | Fases 1-7 | Fase 8 |
|---|---|---|
| Qué mejora | prompt, retrieval, reglas | pesos de un modelo propio |
| Datos necesarios | ninguno | miles a decenas de miles de batallas |
| Infra | API key | GPU (o Colab/runpod), loop de entrenamiento, harness de eval |
| Costo por turno | llamada a API pagada | inferencia local casi gratis |
| Latencia | segundos | milisegundos |
| Transferencia entre gens | alta (el prompt se re-parametriza) | baja (hay que reentrenar por generación) |

Ese último punto importa dado el requisito multi-gen: un modelo entrenado con datos de gen 6 no sirve para gen 9. Lo reutilizable es **el pipeline**, no el modelo. Diseñar los scripts de `ml/` parametrizados por `gen_id` desde el día uno, igual que el resto.

### Preparación (se hace ya, en fases 2 y 3)

Las tablas `trajectories` / `trajectory_steps` se llenan desde el principio. Requisitos del serializador de estado, que es lo único difícil de cambiar después:

- **Formato estable y versionado**: `state_schema_version` en cada fila. Si cambia el serializador, sube la versión y los datos viejos siguen siendo interpretables.
- **Completo pero sin fugas**: el estado debe contener exactamente lo que un jugador ve en ese turno (no el equipo completo del rival si no fue revelado). Si se filtra información oculta, cualquier modelo entrenado con eso es inútil en batalla real.
- **Acciones legales incluidas**: sin la máscara de acciones legales no se puede entrenar una política.
- **Resultado propagado**: al terminar la batalla, escribir el reward en todos los steps (mínimo: +1/-1 por resultado final; mejor: descuento por turno).
- **Metadata de calidad**: quién jugó (agente, humano, oponente), qué modelo, elo estimado del rival. Sirve para filtrar el dataset después.

### Las tres vías, de más a menos realista

**1. Modelo de valor + búsqueda (la de mayor retorno).**
Entrenar un evaluador de estados: dado un estado, ¿cuál es la probabilidad de ganar? Con eso se puede hacer búsqueda en árbol (minimax o similar) evaluando las hojas, que es exactamente lo que hace fuerte a PokéChamp. El LLM pasa a proponer movimientos candidatos y la búsqueda elige. Empezar simple: gradient boosting (LightGBM) sobre features del estado, que con unos pocos miles de batallas ya da señal. Es el mejor ratio esfuerzo/beneficio de las tres.

**2. Behavior cloning desde replays públicos (la entrada realista al dataset).**
El problema evidente es que un torneo entre amigos no genera miles de batallas. La solución: Smogon publica replays públicos de gen6ou. Descargar unos miles, parsearlos con el mismo serializador y entrenar un modelo chico a predecir el movimiento que eligió el jugador fuerte. Da una política rápida y barata, específica de la generación, que sirve como baseline o como generadora de candidatos. También sirve para pre-entrenar el modelo de valor de la vía 1 (los replays traen el resultado final gratis).

**3. Self-play / RL (la ambiciosa).**
El server local permite agente contra agente sin límite, así que la infraestructura ya está. Pero RL es hambriento de muestras y de tiempo de GPU, y con equipos restringidos por ronda el espacio cambia entre rondas. Dejarla para cuando 1 y 2 estén funcionando y medidas.

### Orden sugerido dentro de la fase 8

1. Exportador de dataset: `ml/datasets/export.py --gen 6 --format gen6ou --min-elo X` → parquet.
2. Importador de replays públicos con el mismo serializador (valida que el formato de estado sirve para datos externos).
3. Modelo de valor con LightGBM, evaluado por AUC en un holdout y, más importante, por winrate del agente que lo usa en el batch runner.
4. Integrar la búsqueda en el nodo `decide` como estrategia alternativa, seleccionable desde la UI igual que un modelo.
5. Recién después, behavior cloning y fine-tuning.

Criterio de aceptación de la fase: un agente que usa el modelo de valor gana consistentemente más batallas contra el mismo baseline que el agente puramente LLM, medido con el harness de la fase 7.
