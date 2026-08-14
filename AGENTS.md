# Reglas para agentes que trabajan en Ludex

Este archivo es para cualquier agente que toque el repo. Las decisiones de
arquitectura están en `docs/DECISIONS.md` y el plan general en `docs/PLAN.md`,
sección 11. Acá van solo las reglas operativas.

## Gobernanza de tareas y revisiones

El flujo operativo de Linear, los estados permitidos, el paquete de evidencias
y la autoridad para aceptar o rechazar trabajo están definidos en
`docs/AGENT_GOVERNANCE.md`. **Leelo antes de tomar una tarea o pedir una
revisión.**

- Linear es la fuente de verdad del estado operativo de una tarea.
- El repo es la fuente de verdad de decisiones, diseños, planes, métricas,
  código y evidencia ejecutable.
- Un agente implementador puede mover su tarea a `In Review`, pero nunca a
  `Completed`.
- Solo el tech lead puede emitir el veredicto técnico que habilita
  `Completed`, `Changes Requested`, `On Hold` o `Rejected`.
- Ninguna tarea entra a revisión sin el `REVIEW PACKET` completo definido en
  la guía.

## Skills del proyecto — leelas antes de empezar

En `.claude/` hay cuatro guías escritas para este repo. Cada una vive en un
`SKILL.md` y **condensa trampas que ya nos costaron trabajo tirado**:

- **`.claude/migrations/SKILL.md`** — convenciones de esquema y migraciones con
  dbmate y Postgres. Al crear o tocar migraciones, agregar tablas o columnas,
  elegir tipos o claves, o escribir queries contra el esquema.
- **`.claude/showdown-data/SKILL.md`** — trampas conocidas del paquete npm
  `pokemon-showdown` y de `@smogon/calc`. Al tocar `packages/seed`,
  `packages/calc`, cualquier extractor de data de juego, o las tablas
  `pokemon`, `moves`, `learnsets`, `items`, `abilities`, `type_chart`. También
  al agregar una generación nueva.
- **`.claude/agent-recording/SKILL.md`** — cómo graba el agente y qué
  invariantes cumple el dataset. Al tocar `apps/agent`, el serializador, el
  cliente de Showdown, el recorder, o las tablas `battles`, `battle_turns`,
  `trajectories` y `trajectory_steps`. También al interpretar el protocolo de
  Showdown o al agregar campos al estado.
- **`.claude/verification/SKILL.md`** — cómo se verifica el trabajo antes de
  darlo por terminado. Al escribir tests, fijar conteos esperados, cerrar una
  rebanada, revisar código de otro agente, o cuando un test pasa y hay que
  decidir si eso significa algo.

Claude Code las carga solo por descripción. **Los demás agentes tienen que
abrirlas y leerlas como documentación**: son archivos de texto común.

## Commits

- **Los mensajes de commit se escriben en inglés.** El resto del proyecto
  —documentación, comentarios, reportes— sigue en español.
- **El `-m` va ANTES del `--`.** `git commit -m "msg" -- ruta/archivo`.
- **Commiteá siempre con rutas explícitas. Nunca `git add -A` ni `git add .`**
  Hay varios agentes trabajando en paralelo sobre el mismo repo y un `add`
  suelto se lleva el trabajo a medio terminar de otro. Ya pasó tres veces.
- Hay un directorio `.worktrees/` con áreas de trabajo de otros agentes.
  **Ignoralo por completo.**

## Docker y la base

En esta máquina conviven contenedores de **otros proyectos del usuario**. Los de
`jets` llevan días corriendo y no son de Ludex.

- **Nunca `docker compose down`, `down -v`, `docker stop`, `docker rm` ni
  `brew services stop`.** Para levantar servicios de Ludex,
  `docker compose up -d <servicio>` desde la raíz del repo.
- Desde un worktree hay que fijar `COMPOSE_PROJECT_NAME=ludex` o se crea un
  stack paralelo con su propia base vacía.
- Postgres en `127.0.0.1:15432` (contenedor `ludex-postgres-1`, base `ludex`).
  Los puertos 5432 y 5433 están tomados por otros Postgres, incluido uno nativo
  de Homebrew que una vez ensombreció el puerto y casi hizo que el pipeline
  escribiera en la base equivocada **sin fallar**.
- Showdown en `8100`, calc en `8200`, todo bindeado a loopback.
- **Antes de cualquier maniobra de riesgo sobre los datos, tomá un backup.**
  La data seedeada cuesta horas: `docker exec ludex-postgres-1 pg_dump -U ludex
  -d ludex --format=custom --compress=9 -f /tmp/backup.dump`.

## Cómo trabajar

- **Inspeccioná antes de diseñar.** Antes de escribir código contra una
  librería o contra el esquema, miralo de verdad y pegá lo que encontraste en
  tu reporte. Los números y nombres de los tests salen de ahí, no de la
  memoria.
- **El esquema real manda sobre cualquier cosa que diga el prompt.** Si una
  tabla o columna no se llama como te dijeron, **pará y preguntá** en vez de
  interpretar. Las cuatro veces que un agente frenó por esto, el prompt estaba
  mal y frenar evitó trabajo tirado.
- **Ante una discrepancia entre lo que te pidieron y lo que ves, pará y
  contá**, con los números. No improvises una variante.
- **La generación es siempre un parámetro**, nunca un valor fijo. El torneo del
  año que viene es de otra generación. `grep -ri "gen6" src/` no debe devolver
  nada fuera de configuración.
- Registrá toda decisión no trivial en `docs/DECISIONS.md`.

## Restricción operativa de modelos para juego

- **Nunca uses `gpt-5.6-luna` para provider-smoke, completions de decisión ni
  batallas.** No lo reintentes aunque exista una ruta, saldo o artefacto
  incompleto.
- Las futuras ejecuciones live de juego se limitan a **modelos chinos** y a
  **Gemini dentro del free tier**. Discovery puramente metadata (`/models`) no
  cuenta como juego, pero no habilita smokes ni completions fuera de este
  alcance.
- Conservá los artefactos históricos de otros modelos como evidencia; no los
  uses como autorización para repetirlos.
- Un modelo chino pago tampoco se repite ni amplía presupuesto sin un nuevo
  tope explícito del usuario. Nunca compres créditos, habilites auto-reload ni
  aumentes cuota por inferencia.

## Higiene de contexto y consumo de Codex

La cuota de Codex también es un recurso operativo. Ahorrarla no autoriza a
reducir alcance, omitir evidencia ni relajar verificaciones: hay que reducir el
ruido que entra al contexto.

- Para procesos largos o ruidosos, redirigí `stdout`/`stderr` a un archivo
  nombrado bajo `/tmp` y leé sólo deltas o resúmenes con `rg`/`jq` (y `tail`
  acotado cuando haga falta). No vuelques al chat trazas, logs, JSON o salidas
  de suites completas salvo que la evidencia exacta lo exija.
- Acotá siempre la salida de herramientas. Buscá y seleccioná primero; no
  vuelvas a leer archivos grandes sin cambios ni repitas contexto ya verificado.
- En monitoreos, espaciá las consultas según el timeout real y reportá sólo
  cambios de estado, clasificaciones o bloqueos. No hagas polling ruidoso.
- Antes de una compactación, handoff o fin de sesión, registrá en Linear el
  comando activo, límites, último estado durable, artefactos y próxima acción.
  Si forma parte del entregable, versioná también la evidencia saneada.
- Al iniciar o retomar una sesión, releé este archivo, la gobernanza, el issue
  activo, el `/goal`, el último handoff/comentario y el HEAD del worktree antes
  de ejecutar. No reconstruyas el trabajo cargando logs históricos completos.

## Sobre los tests

El entregable del agente no es que juegue bien, es que **grabe bien**: el
dataset entrena un modelo después. De ahí que estas reglas sean duras.

- **Un test que puede pasar sin ejercer lo que dice ejercer es peor que no
  tenerlo.** Si un loop puede no iterar nunca, agregá un canario que falle
  cuando no verificó nada.
- **Verificá que un test nuevo detecta la regresión**: revertí el arreglo,
  confirmá que el test falla, y volvé a aplicarlo.
- **No agregues una excusa a un test sin diagnosticar antes qué la necesita.**
  Una excusa puesta a ojo tapa lo que todavía no entendiste.
- Los tests que verifican propiedades del dataset deben correr sobre **todo**
  el dataset, no solo sobre las filas de la corrida en curso. Ya se escapó un
  defecto por filtrar `WHERE battle_tag = ANY(:tags)`.
