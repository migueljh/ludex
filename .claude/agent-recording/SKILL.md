---
name: agent-recording
description: Cómo graba el agente de Ludex y qué invariantes tiene que cumplir el dataset. Usar al tocar apps/agent, el serializador de estado, el cliente de Showdown, el recorder de protocolo, o las tablas battles, battle_turns, trajectories y trajectory_steps. También al interpretar el protocolo de Showdown, al escribir tests sobre las filas grabadas, o al agregar campos al estado.
---

# El grabador del agente

El entregable de `apps/agent` **no es que juegue bien, es que grabe bien**: el
dataset entrena un modelo después. Cada defecto de esta lista costó horas y
ninguno rompía nada visible — el agente jugaba, las filas se guardaban, los
tests pasaban en verde.

## Hechos del protocolo de Showdown, medidos

Estos no se deducen leyendo la documentación. Se midieron con sondas en vivo.

**El `|request|` llega YA RESUELTO, en su propio frame, antes de la narración
del turno.** Refleja el estado posterior a la resolución. poke-env rehace
`battle.team` entero desde ese JSON, mientras `opponent_team` solo avanza
parseando la narración. Por eso el lado propio puede ir "adelante" del rival.

**La narración es la RESPUESTA del servidor a que ambos jugadores eligieron**,
no datos que ya venían en camino. Medido causalmente: retrasar la respuesta
500 ms retrasa la narración exactamente 500 ms. **Corolario: esperar la
narración antes de responder es un punto muerto garantizado.** No lo intentes.

**poke-env llama a `choose_move` apenas parsea el `|request|`**, dentro del
`elif split_message[1] == "request"` de `_handle_battle_message`. Y despacha
una task por mensaje (`asyncio.create_task` en `ps_client`), así que los lotes
NO se procesan en serie.

**`|turn|N` cierra el bloque anterior**: llega al final del frame que narra el
turno N-1.

## Los invariantes del dataset

Un test que verifica propiedades del dataset corre sobre **todo** el dataset,
no sobre las filas de la corrida en curso. Ya se escapó un defecto por filtrar
`WHERE battle_tag = ANY(:tags)`.

### 1. La acción está dentro de su propia máscara

`action_taken` tiene que estar en la `legal_actions` de esa misma fila. Una
fila que se contradice a sí misma le enseña al modelo a elegir fuera de la
máscara.

**Se cumple por construcción, y así tiene que seguir: la foto del estado se
saca SÍNCRONA dentro de `choose_move`.** No leas nada del objeto `battle`
después de que la decisión pasó: es mutable y compartido, y para cuando una
task de fondo corre, el planificador puede haber despachado ya la decisión
siguiente. Eso produjo filas con `action_taken` de la decisión N y
`legal_actions` de la N+1.

Si necesitás una vista del rival más al día, **re-derivala del protocolo crudo**
(D17). Nunca releyendo el objeto mutable.

### 2. Una fila pertenece al turno en que su decisión se RESOLVIÓ

Sin importar cómo se resolvió: se ejecutó, el juego la impidió, o al pokémon
lo debilitaron antes de actuar. Los tres son resoluciones.

`battle.turn` capturado al decidir es **siempre <= el turno real**, nunca un
techo falso. La corrección la hace `_correct_step_turns` buscando la línea en
el protocolo, con un cursor global monótono para que dos decisiones no
compartan la misma línea.

**Showdown SÍ deja rastro cuando la acción no se ejecutó.** Son tres, y los
tres hay que reconocerlos:

- `|cant|{side}a:` — sueño, parálisis, congelamiento.
- `|faint|{side}a:` sin `|move|` propio previo en el mismo bloque. Con el
  `|move|` previo, el debilitamiento resuelve la decisión de otro.
- `|-activate|{side}a: X|confusion` — el autogolpe por confusión.

**El respaldo tiene que nombrar al mismo pokémon que actúa.** Sin esa
comprobación, un `|faint|` de un pokémon no relacionado dos turnos después le
roba la línea a la decisión siguiente y arrastra a todas las posteriores.

### 3. Una decisión por `decision_index`, no por turno

Un cambio forzado tras un debilitamiento **no avanza el turno**: hay dos
decisiones con el mismo `battle.turn`. La PK de `trajectory_steps` es
`(trajectory_id, decision_index)` por eso. Cuando estaba sobre `turn_number`,
la segunda pisaba a la primera y **se perdía sistemáticamente toda la clase
"elegir reemplazo tras un debilitamiento"** — 265 de 1684 cambios, una pérdida
sesgada, no aleatoria.

### 4. Cero fuga de información oculta

Es LA propiedad. Si el modelo entrena con información que un jugador no tiene,
es inútil en batalla real. Del rival se persisten 11 claves y **todas** hay que
verificarlas, no solo `species`.

Hay **inferencias legítimas** que no son fugas, y cada una tiene que estar
anclada a una línea de protocolo real o al dex, nunca a una lista a mano:
habilidad y tipo de una mega evolución cuando `species` todavía no se
actualizó, cambio de tipo dinámico por Protean o Libero, Transform o Imposter
copiando un pokémon propio, y el nivel 100 que Showdown omite.

## Trampas de comparación

**Hidden Power**: poke-env nombra la acción con el tipo (`hiddenpowerice`) pero
Showdown narra solo `Hidden Power`. Sin recortar a `hiddenpower`, la búsqueda
no matchea nunca. Recorte específico, no una regla genérica de prefijos: hay 17
Hidden Power que comparten el id base.

**Formas alternativas**: compará por `base_species`, no por `species`.
Comparar por `species` rompe todo pokémon con forma alternativa (Arceus-Poison,
Rotom-Wash).

**Normalización de nombres**: sacá TODA la puntuación, no solo espacios y
guiones. "Mr. Mime" tiene un punto y "Farfetch'd" un apóstrofo.

**Comparación con el protocolo**: línea por línea, nunca sobre el protocolo
concatenado. En un blob, un nombre puede "aparecer" a caballo entre dos tokens
sin relación y una fuga real pasaría como revelada.

## Cuando algo no cierra

**Medí antes de arreglar.** El caso testigo: se propuso que el residual de
filas mal etiquetadas eran "acciones cuyo pokémon murió antes de actuar". Esa
explicación cubría **3 de 20**. Si se escribía la excusa sin medir, 17 filas
quedaban tapadas para siempre.

**El protocolo crudo es el juez** (D17). Ante cualquier duda sobre qué pasó en
un turno, la respuesta está en `battle_turns.protocol_lines`, no en el objeto
de poke-env ni en la memoria de nadie. Esa misma propiedad ya permitió
reconstruir seis ganadores que un test había borrado.
