# F2-01 — Diseño del camino pre-lock para snapshots de decisión frescos

**Issue:** MON-6 · **Estado:** alternativa A **aprobada con enmiendas vinculantes**
**Baseline:** `10b091cece15b988a99b3e2a51b718d9f492a75e` · **poke-env:** 0.15.0

Este documento responde al `TECH LEAD CHECKPOINT` del 2026-07-29 y está
actualizado con el `TECH LEAD DESIGN VERDICT` de la misma fecha. Las secciones
1–5 (evidencia y comparación de alternativas) quedaron aceptadas sin cambios.
Las secciones 6 en adelante incorporan las cinco enmiendas vinculantes.

---

## 1. Evidencia aceptada que gobierna el diseño

- `NARR(k)` llega al socket entre **0.022 ms y 5.557 ms** después del
  `|request|` y **antes** del envío de nuestra elección.
- `NARR(k)` es independiente de nuestra elección; `NARR(k+1)` no lo es.
- Baseline oficial del defecto: **297/762 = 39.0%**, con **270/297 = 90.9%** de
  firma exacta de retraso-en-uno. (Durante la implementación se midió que
  `detailschange` **no** cambia `species` en poke-env, así que la corrección a
  309/762 = 40.6% sobrecontaba 12 filas por Mega evolución: ver §10.3. El
  veredicto de Latwan adopta 297/762 como cifra única de todo el documento.)
- La task que procesaría `NARR(k)` queda esperando el mismo `_battle_locks[tag]`
  que la decisión mantiene abierto.

Dos hechos medidos sobre la traza que el diseño necesita y que **no se pueden
deducir leyendo el protocolo**:

- **Exactamente un frame narrativo por decisión.** Distribución sobre las 76
  decisiones de la variante causal: `{1: 76}`. Nunca cero, nunca dos.
- **El frame de un cambio forzado no lleva `|turn|`.** Las 7 decisiones con
  `forceSwitch` tienen su frame narrativo con `turn=None`: el bloque cierra en
  el `|faint|`.

```
frame#16 decision=5 rqid=12 forceSwitch=[True]
   frame#17 kind=narration turn=None
   ['|move|p2a: Galvantula|Giga Drain|p1a: Torkoal', '|faint|p1a: Torkoal']
```

## 2. Restricciones vinculantes

1. Esperar **únicamente una señal RAW pre-lock** de `NARR(k)`, publicada antes de
   intentar adquirir `_battle_locks[tag]`.
2. Prohibido esperar a que `Battle`, `ProtocolRecorder` o
   `_handle_battle_message` avancen mientras la decisión conserva el lock.
3. Prohibido copiar, reemplazar o editar `PSClient.listen()`. Prohibido tocar
   `site-packages`.
4. Toda API privada: mínima, encapsulada y protegida por test de contrato.
5. Máscara, `action_taken` y mapa acción→`BattleOrder` síncronos antes del primer
   `await`.
6. Nunca releer `Battle` después de que la decisión pasó.
7. Sin listas manuales de especies, movimientos, items o abilities.

## 3. El seam real de poke-env 0.15.0

```
player.py:150   self.ps_client = PSClient(..., start_listening=start_listening)
ps_client.py:108-111   if start_listening:
                           self._listening_coroutine = asyncio.run_coroutine_threadsafe(
                               self.listen(), self.loop)
ps_client.py:156   async def _handle_message(self, message: str)
ps_client.py:171-176   async with self._battle_locks[battle_tag]:
                           await self._handle_battle_message(split_messages)
```

`_handle_message` es el único punto que corre dentro de la task del frame y
antes del lock. `start_listening` es público y difiere el arranque del listener.

## 4. Alternativas

### A — Observador mínimo en `PSClient._handle_message` (aprobada)

Construir con `start_listening=False`, envolver
`self.ps_client._handle_message`, y recién entonces arrancar el listener con la
llamada original. El observador publica el frame crudo en un inbox por
`battle_tag` y delega. No consume, no filtra, no reordena. Superficie privada:
un símbolo envuelto.

### B — Desacoplar captura/decisión/envío del lock — **rechazada**

poke-env envía `choice.message` al retornar de `choose_move`
(`player.py:349-354`). Retornar temprano manda una elección que no queremos;
una segunda para el mismo `rqid` da `[Invalid choice]`. Reescribe la
reconciliación de elecciones rechazadas, que es de **F2-02**, y rompe
`decision_index`.

### C — Conexión espectadora paralela — **rechazada**

Sin `rqid` con qué correlacionar, con su propia carrera y un modo de falla
nuevo y silencioso.

### D — Hook pre-lock upstream en poke-env — **seguimiento**

Única vía que elimina el seam privado de raíz. No puede bloquear F2-01.

## 5. Recomendación

**Alternativa A**, aprobada por el tech lead.

---

## 6. Diseño (con enmiendas vinculantes)

### 6.1 Componentes

| componente | ubicación | naturaleza |
|---|---|---|
| `RawFrameInbox` | `showdown/protocol.py` | secuencia append-only por tag + señal |
| `is_resolution_frame()` | `showdown/protocol.py` | pura, lista blanca de tags |
| `project_observable_state()` | `showdown/protocol.py` | **pura**, sin poke-env, sin I/O |
| `ObservableVocabulary` | `showdown/client.py` | adaptador al dex/enums de poke-env |
| instalación del observador | `showdown/client.py` | encapsulada |
| espera + proyección | `showdown/client.py` | dentro de la coroutine de decisión |

`showdown/protocol.py` **no importa poke-env** y debe seguir así: es lo que hace
al proyector testeable sin levantar nada.

### 6.2 Inbox por `battle_tag`

```
entrada = (seq: int, recv_ns: int, lines: tuple[str, ...])
```

Append-only, `seq` monótono global, `lines` inmutable, una `asyncio.Condition`
por tag. **Independiente del `ProtocolRecorder`**, que sigue grabándose bajo lock
y sigue siendo la fuente de verdad persistida (D17). Esperar al recorder sigue
prohibido; esperar al inbox no.

### 6.3 Instalación del observador — ENMIENDA 1

- Se envuelve `PSClient._handle_message`; nunca se copia `listen()`.
- **Se preserva el valor pedido por el caller.** Si el caller construye
  `LudexPlayer(start_listening=False)`, el listener **no** se arranca después de
  instalar el wrapper. Sólo se arranca cuando el caller pidió `True`.
- **Doble arranque impedido** por un flag idempotente.

### 6.4 Correlación determinista

Síncronamente, antes de cualquier `await`, dentro de `choose_move`:

1. `cursor = inbox.last_seq(tag)` — el frame del propio `|request|` ya está
   publicado, porque el observador corrió antes del lock que ahora tenemos.
2. Se capturan snapshot propio, `legal_actions`, `action_taken`/mapa
   acción→`BattleOrder`, `decision_turn`, `actor_species`.

Después, ya dentro de la coroutine devuelta: esperar la primera entrada con
`seq > cursor` que satisfaga `is_resolution_frame(lines)`.

Lista blanca (no lista negra):

```
RESOLUTION_TAGS = {
  "move","switch","drag","replace","detailschange","-formechange",
  "faint","cant","-damage","-heal","-sethp","-status","-curestatus",
  "-boost","-unboost","-setboost","-clearboost","-clearallboost",
  "-item","-enditem","-ability","-endability",
  "-weather","-fieldstart","-fieldend","-sidestart","-sideend",
  "-activate","upkeep","turn","win","tie",
}
```

Chat (`c`, `c:`), `inactive`, `inactiveoff`, `j`, `l`, `n`, `t:` solo, `request`,
`error`, `popup`, `raw`, `html`, `uhtml`, `expire`, `debug` e `init` **no
completan la espera nunca** y **no entran al prompt**.

`upkeep` está incluido y `turn` **no** es obligatorio: es lo que hace resolver al
cambio forzado (7/76 medidos, sin `|turn|`).

### 6.5 Proyector puro del estado observable — ENMIENDA 4

El helper se llama `project_observable_state` porque proyecta el **estado
observable completo**, no sólo `opponent`:

```
project_observable_state(snapshot, lines, *, opponent_side, vocabulary) -> dict
```

- Devuelve un **dict nuevo**; no muta la entrada.
- No importa poke-env, no lee `Battle`, no abre conexiones.
- **Nunca consume líneas `|request|`.**
- Sin listas manuales: cada dato sale del payload de su propia línea.

Aplica sobre el lado rival:

| evidencia | líneas |
|---|---|
| identidad y forma | `switch`, `drag`, `replace`, `detailschange`, `-formechange` |
| HP y status | `-damage`, `-heal`, `-sethp`, `-status`, `-curestatus`, `faint` |
| boosts | `-boost`, `-unboost`, `-setboost`, `-clearboost`, `-clearallboost` |
| revelaciones | `move` (id revelado), `-item`, `-enditem`, `-ability`, `-endability` |

Y sobre el estado global: `-weather`, `-fieldstart`, `-fieldend`, `-sidestart`,
`-sideend`, más **`turn`**:

- si `NARR(k)` contiene `|turn|N`, `projected["turn"] = N`;
- en cambio forzado sin `|turn|`, conserva el `decision_turn` síncrono.

`replace` es obligatorio (Illusion) y `detailschange`/`-formechange` también,
pero **solo para los tipos**: la medición de §10.3 mostró que en poke-env un
cambio de forma no toca `species`, así que incluirlo en el contador de identidad
sobrecontaba 12 filas. La cifra oficial es **297/762 = 39.0%** en todo este
documento; no hay una variante de 40.6%.

Además, `replace` no es un renombre de la entrada activa sino el intercambio de
**dos** entradas del equipo: el imitado se conserva inactivo (su `|switch|` es
evidencia pública de que el rival lo tiene) y el imitador entra con su nivel y
tipos del `details` y hereda el HP y el status que recibió disfrazado, sin
heredar item, ability ni movimientos. Es paridad exacta con
`AbstractBattle._end_illusion_on`.

Se proyectan también, como inferencias públicas ancladas a la librería:
`|-start|…|typechange|…` (incluido el `[of]` de Reflect Type) y `|-transform|…`
con `[from] ability: Imposter`, este último copiando de un pokémon **nuestro**,
que es información que ya tenemos.

**Nunca escribe `legal_actions`, `me` ni `player_role`**: vienen del `|request|`
propio y ya están frescos.

**`ObservableVocabulary`** (inyectada desde `client.py`) traduce nombres del
protocolo a la representación de poke-env: tipos de una especie recién revelada
(desde el dex de la generación) y nombres de weather/field/side condition (desde
los enums de poke-env). Está inyectada para que `protocol.py` siga puro y para
que las inferencias queden ancladas al **dex**, nunca a una lista a mano — que
es exactamente lo que autoriza `.claude/agent-recording/SKILL.md`.

### 6.6 Un solo objeto inmutable — ENMIENDA 4

```
projected = project_observable_state(...)      # incluye turn, field y opponent
graph_input["raw_state"] = projected
step["state"]            = projected           # MISMA referencia
```

`_correct_step_turns` **no puede modificar después el snapshot de la ruta
graph**. Para esa ruta debe **verificar** que el turno encontrado en el protocolo
coincide con el proyectado y **fallar ruidosamente** si no. La ruta random
conserva su corrección histórica.

### 6.7 Timeout: fallo cerrado — ENMIENDA 3

Se **rechaza** el fallback que invocaba al provider con estado stale y persistía
`projection.applied=false`: contradice el criterio principal de F2-01 y contamina
el corpus antes de que F2-04 pueda excluirlo.

- Timeout **configurable** en `LudexPlayer`, default **1.0 s**. No se fija 0.25 s
  a partir de una única corrida local.
- La espera **consume el mismo presupuesto de decisión**.
- Al vencer: incrementar `projection_timeout_count`, emitir **error tipado**
  (`ProjectionTimeoutError`) y propagarlo por `_background_failure`.
- **No** invocar al provider, **no** enviar una orden basada en el snapshot
  stale, **no** persistir el step reservado.
- **No** se agrega `state["projection"]`: si una fila existe, su proyección es
  válida por construcción.
- F2-10 medirá el canal oficial y podrá ajustar el default con evidencia.

El test debe demostrar que el timeout deja **cero fila/step persistible**, no
sólo que incrementa el contador.

### 6.8 Reintentos por elección rechazada — ENMIENDA 5

**F2-01 es responsable de freshness y de evitar el deadlock:**

- Detectar **ambas rutas** antes de que `super()` vuelva a llamar a `choose_move`:
  - `[Unavailable choice]` — error y después un request nuevo;
  - `[Invalid choice]` — poke-env puede reintentar **dentro del propio frame de
    error**, sin request nuevo.
- **Marcar explícitamente** el próximo `choose_move` como retry. No depender sólo
  de "el frame anterior al request".
- En el retry, capturar otra vez y síncronamente snapshot propio, máscara y mapa
  acción→orden.
- **Saltar la espera**: no existe una resolución nueva que esperar.
- Reutilizar **únicamente la última proyección pública válida** —`opponent`,
  `field` y `turn`— sobre el snapshot propio **nuevo**. **Nunca reutilizar el
  dict completo**, porque la máscara pudo cambiar al descubrirse `trapped`.
- Si no existe proyección previa válida, **fallar ruidosamente**.
- Consumir/limpiar la marca **exactamente una vez**.

**F2-02 sigue siendo responsable de:** identidad canónica de la decisión,
descarte/consolidación del intento rechazado, `decision_index`, y acción y
metadata finales que sí se ejecutaron. F2-01 no rediseña esa reconciliación.

### 6.9 Otros casos borde

| caso | comportamiento |
|---|---|
| **duplicados** | Cada decisión espera `seq > cursor` de *su propio* frame de request. Dos decisiones no pueden consumir el mismo frame. |
| **múltiples decisiones en un turno** | Cambio forzado: cada decisión tiene su request y su cursor. Medido 7/76. |
| **fin de batalla** | `win`/`tie` completan la espera; si el tag ya terminó, la espera aborta. |
| **frame sin `battle_tag`** | No entra al inbox; se delega intacto. |

### 6.10 Schema versión 2 — ENMIENDA 2

`STATE_SCHEMA_VERSION = 2`. Un movimiento rival revelado desde `|move|` entra
como:

```json
{"id": "sludgebomb", "pp": null, "max_pp": null}
```

`null` significa **"no derivable de esa evidencia pública"**, no cero ni PP
faltante por error.

- Filas nuevas: columna `state_schema_version=2` y `state.schema_version=2`.
- Filas históricas v1: **no se reescriben ni se inventan**.
- El test global deja de exigir una única versión y pasa a exigir: columna y JSON
  coinciden por fila; sólo aparecen versiones soportadas `{1, 2}`; las filas
  nuevas de la integración son v2.
- **No hace falta migración SQL**: la columna ya versiona el payload.
- F2-04 implementará los validadores independientes de coexistencia histórica.

### 6.11 Protección del seam privado

Test de contrato que falla si poke-env cambia:

1. `PSClient._handle_message` existe, es corrutina y acepta `(self, message: str)`.
2. `PSClient.__init__` acepta `start_listening`; con `False` no crea
   `_listening_coroutine`.
3. **La observación ocurre antes del lock**: se conduce un frame por el
   observador con el lock del tag tomado a mano y se afirma que el inbox se
   puebla igual, mientras `_on_battle_message` todavía no fue invocado. Si
   poke-env moviera el lock más arriba, este test cae.
4. El mensaje de fallo nombra `poke-env==0.15.0` y apunta a este documento.

## 7. Plan red → green

1. Fixture de **frames separados** (request y narración), conducidos por el
   observador, tomados de la traza real de la sonda, incluido un caso
   `faint → forceSwitch` sin `|turn|`.
2. **Rojo**: el activo rival de `graph_input["raw_state"]` debe ser el switch-in
   de la narración.
3. **Canario de ejercicio**: el test afirma que el valor pre-proyección difería
   del post; no puede pasar sin ejercer la proyección.
4. **Prueba de fuga**: ningún movimiento, item o ability rival fuera de las
   líneas de la narración; el proyector no lee ninguna línea `|request|`.
5. **Máscara y mapa** idénticos a los capturados síncronamente.
6. **Timeout deja cero fila persistible.**
7. **Retry** de ambas clases de error.
8. **Rotura deliberada** → los tests caen → restaurar y repetir.
9. **Integración real** una sola vez, con lock.
10. **Prevalencia 0/N** sobre las filas nuevas, con el mismo script que midió el
    defecto.

## 8. Correcciones conceptuales pendientes

Se aplican **con esta implementación**:

- **`.claude/agent-recording/SKILL.md`** — distinguir `NARR(k+1)` (depende de
  nuestra respuesta; esperarla es punto muerto) de `NARR(k)` (ya emitida e
  independiente: 0.022–5.557 ms, 76/76).
- **`client.py`** — la afirmación de que la narración llega "en el MISMO lote"
  que el request es falsa. Son frames separados.
- **D20 / D22** — su regla central se conserva y se refuerza: la proyección se
  deriva del protocolo crudo, nunca del objeto mutable. Se corrige la premisa de
  disponibilidad, no la disciplina.
- **D31** — contrato snapshot de decisión / persistencia y prohibición de que el
  chat de batalla entre al prompt.

## 9. Límites conocidos y riesgos

1. **Seam privado** `_handle_message`, mitigado por el test de contrato.
2. **Ambas rutas de `choose_move` pasan a ser asíncronas**; varios tests que
   afirman la sincronía de la ruta random se actualizan.
3. **Tipos de una especie recién revelada** salen del dex de la generación vía
   `ObservableVocabulary`, no del protocolo: el protocolo no los trae.
4. **La regla de "exactamente un frame narrativo"** está medida sobre 76
   decisiones de gen 6 randombattle en Showdown local, no probada para todo
   formato.
5. **Latencia**: en el peor caso la espera consume hasta 1.0 s del presupuesto de
   decisión antes de fallar cerrado.

---

## 10. Hallazgos de la implementación

Tres cosas que el diseño no anticipó y que la implementación midió. Ninguna
cambia la alternativa elegida.

### 10.1 El cursor tiene que ser un `ContextVar`, no `inbox.last_seq(tag)`

El diseño proponía tomar el cursor con `last_seq(tag)` al empezar la decisión,
argumentando que "el frame del propio `|request|` ya está publicado". Es cierto,
pero **insuficiente**: poke-env crea una task por frame y todas publican al
arrancar, mientras solo una entra al lock por vez. Bajo carga, para cuando la
decisión del frame N llega a `choose_move`, los frames N+1, N+2… ya están
publicados y `last_seq` devuelve uno de **ellos** — la decisión espera entonces
una narración posterior a la suya, o ninguna.

Lo cazó el test de frames reales, no el razonamiento. El observador publica el
`seq` en un `ContextVar` que cada task copia, y `choose_move` lo lee.

### 10.2 `detailschange` no cambia `species`

`Pokemon.forme_change()` llama a `_update_from_pokedex(..., store_species=False)`
(`pokemon.py:431-433`): tras una Mega evolución poke-env **conserva la forma
base**. La proyección hace lo mismo — cambia los tipos, no la especie. Escribir
`slowbromega` habría hecho que la proyección contradiga al resto del dataset
dentro de la misma batalla (medido en `battle-gen6randombattle-1896`, donde llega
`|detailschange|p2a: Slowbro|Slowbro-Mega, L81, M` y las filas de poke-env siguen
diciendo `slowbro`).

`replace` (Illusion) **sí** cambia qué especie está activa, pero no renombrando
la entrada: mueve la actividad del imitado al imitador, conservando a los dos en
el equipo. Ver §10.5.

### 10.3 La prevalencia oficial es 39.0% en todo el documento

Consecuencia directa de 10.2: la consulta que adoptó el tech lead contaba
`detailschange` como cambio de identidad y sobrecontaba **12 filas** por Mega.

```
switch + drag                       -> 297/762 = 39.0%   (correcto)
+ detailschange + -formechange      -> 309/762 = 40.6%   (sobrecontaba)
+ replace                           -> 297/762 = 39.0%   (replace suma 0 en este corpus)
```

Firma de retraso-en-uno con la regla correcta: **270/297 = 90.9%**. El veredicto
de Latwan adoptó 297/762 y este documento no cita ninguna otra cifra.

### 10.4 La exclusión de cambios forzados exige firma demostrable

Un cambio forzado tras un debilitamiento **no avanza el turno** (D21): hay dos
decisiones con el mismo `turn_number` y el punto observable de la segunda cae a
mitad de turno. "Estado al inicio del turno de resolución" no es la referencia
correcta para esas — medirlas así marcaba 3 decisiones sanas como desfasadas.

Excluir por `turn_number` repetido **a secas** era demasiado ancho: cualquier
defecto futuro que duplicara el `turn_number` por otro motivo quedaba tapado, que
es exactamente la clase de cobertura silenciosa que ya costó 265 pasos perdidos
cuando la PK estaba sobre `turn_number`. La exclusión pide ahora dos hechos
públicos independientes, los dos obligatorios:

1. la máscara persistida no ofrece **ni un** movimiento (un `forceSwitch` llega
   sin `moves`, así que `legal_actions` queda con puros cambios);
2. hay `|faint|{rol}a:` en el protocolo **de ese mismo turno**.

Una decisión que comparte turno y no cumple las dos **hace fallar el test**, con
tag, índice y turno en el mensaje. La frescura del cambio forzado la cubren
además los tests unitarios con el fixture `faint → forceSwitch`, y el test lleva
un canario de piso (`comparadas >= 50`) para que no pueda pasar habiendo excluido
todo.

### 10.5 `replace` son dos entradas, no un renombre

La primera versión renombraba la entrada activa. Eso tenía dos defectos: borraba
al imitado del equipo rival (su `|switch|` es evidencia pública de que el rival lo
tiene) y le regalaba al imitador el item, la ability y los movimientos que se le
habían atribuido al imitado. Ahora hay paridad exacta con
`AbstractBattle._end_illusion_on` (`abstract_battle.py:409-427`):

- el imitador entra con `switch_in(details)` —especie, nivel y tipos del `details`
  del propio `|replace|`— y hereda el HP, el status y el `fainted` que recibió
  mientras estaba disfrazado, porque el que estaba en el campo era él;
- el imitado queda `active=False`, con boosts limpios y `status=None`, y con
  `hp_fraction=0.0` porque `current_hp_fraction` devuelve `0` cuando `_current_hp`
  es `None` (`pokemon.py:988-995`): es lo que dice la fila que poke-env serializa
  para ese mismo pokémon, y la proyección no puede contradecir al resto del
  dataset de la misma batalla;
- item, ability y movimientos **no** viajan.

### 10.6 `typechange` y Transform/Imposter se proyectan

Dos inferencias legítimas que faltaban, las dos ancladas a la librería y no a
listas a mano:

- `|-start|{side}a: X|typechange|Water/Flying` (Protean, Libero, Camouflage) fija
  los tipos narrados vía `PokemonType.from_name`; con la forma `[of] …` de Reflect
  Type se copian del pokémon citado, aplicando la misma regla que
  `abstract_battle.py:802-809` (mira exactamente `event[5]`).
- `|-transform|{side}a: Ditto|p1a: X|[from] ability: Imposter` copia de un pokémon
  **nuestro**, así que no es fuga: tipos del **dex** de la especie copiada (no sus
  tipos actuales), boosts, moveset y ability del objetivo, con la especie intacta,
  igual que `Pokemon.transform()` (`pokemon.py:625-636`). El PP de un movimiento
  copiado es `min(5, max_pp)` desde gen 5 (`move.py:114`, `move.py:477-478`): es
  una regla fija de la generación, derivable, no información oculta.

Un `switch` posterior borra los tipos temporales, igual que `switch_out` limpia
`_temporary_types` (`pokemon.py:612`).

### 10.7 La retención del `RawFrameInbox` está acotada

Sin tope, una corrida de miles de batallas acumulaba cada frame de cada batalla
para siempre. La retención está acotada por los dos extremos: `MAX_RETAINED_FRAMES
= 128` por tag durante la batalla y `close()` al terminarla, que libera los frames
del tag.

El tope no puede convertirse en una respuesta **equivocada**: si el frame que
seguía al cursor ya se desalojó, el primer frame de resolución retenido no es
demostrablemente el de esta decisión, y `wait_for_resolution` falla **cerrado**
(mismo camino que el timeout). Devolverlo sería el defecto de F2-01 otra vez, con
el tope como causa nueva. Se rastrea el mayor `seq` desalojado **por tag**, porque
`_seq` es global y los `seq` de un mismo tag no son contiguos. 128 son dos órdenes
de magnitud sobre lo que una decisión real necesita mirar (uno o dos frames), y un
canario lo verifica.
