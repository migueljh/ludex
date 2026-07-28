# Ludex — construir un dataset que no mienta

**Caso de estudio.** Una plataforma de IA para torneos de Pokémon Showdown entre
amigos: un agente que juega, graba lo que ve, y aprende de lo grabado.

El proyecto es personal y el torneo es real: una vez por año, con reglas
propias y una sola oportunidad. Esa restricción define todo lo demás.

---

## El problema

Pokémon competitivo es un juego de información incompleta. No sabés qué
Pokémon tiene el rival hasta que los saca, ni qué movimientos tienen hasta que
los usa, ni qué objeto llevan hasta que se activa. Cada turno elegís a ciegas
y simultáneamente: los dos jugadores deciden sin ver la elección del otro.

El objetivo era construir un agente que juegue esas batallas. Pero el problema
que terminó ocupando el 90% del esfuerzo fue otro, y es el interesante.

## Por qué es más difícil de lo que parece

Para que un agente aprenda de sus partidas hay que grabarlas. Grabar parece
trivial: guardás el estado, la acción elegida y el resultado. Y ahí está la
trampa.

**Si el estado grabado contiene información que el jugador no tenía en ese
momento, el modelo entrenado con eso es inútil.** Aprende a decidir mirando el
equipo completo del rival, y en una batalla real no lo va a tener. Eso tiene
nombre —*train/serve skew*— y es de los errores más caros de machine learning,
porque **no falla durante el entrenamiento: falla en producción, meses
después, sin ningún mensaje de error.**

A un backend normal nada de esto le importaría. Es toda data propia, en una
base propia; que la tabla tenga el equipo completo del rival no rompe ninguna
consulta. Si esto fuera una app de estadísticas, se habría terminado en dos
días.

De ahí salió el principio que ordenó el proyecto:

> El entregable no es que juegue bien. Es que grabe bien.

Jugar mal es reversible: se cambia el modelo y listo. Un dataset corrupto
obliga a regrabar todo — y a descartar cualquier modelo entrenado encima.

## Las cuatro invariantes

El dataset tiene que cumplir cuatro propiedades. Cada una está cubierta por
tests que corren sobre **todo** el corpus, no solo sobre la última corrida.

**1. Cero fuga de información oculta.** En el turno N, ningún Pokémon del rival
puede aparecer en el estado si el protocolo no lo reveló hasta ese turno. Se
verifican las once claves que se persisten del rival, no solo la especie.

**2. La acción está dentro de su propia máscara.** La acción elegida tiene que
estar entre las acciones legales grabadas en esa misma fila. Una fila que se
contradice a sí misma le enseña al modelo a elegir lo imposible.

**3. Una fila pertenece al turno en que su decisión se resolvió.** Sin importar
cómo se resolvió: se ejecutó, el juego la impidió, o al Pokémon lo debilitaron
antes de actuar.

**4. Una fila por decisión, no por turno.** Un cambio forzado tras un
debilitamiento no avanza el turno del juego, pero es una decisión distinta.

## Decisiones técnicas

**El protocolo crudo es la fuente de verdad; el estado derivado es una vista.**
Se persisten los dos: las líneas del protocolo de Showdown tal como las recibe
cada jugador, y el estado normalizado que se calcula a partir de ellas. Cuesta
espacio y parecía burocracia.

Rindió tres veces. La primera, cuando un test defectuoso borró el ganador de
seis batallas: se reconstruyeron desde la línea `|win|` del protocolo, y la
reconstrucción se validó contra una fuente independiente que el bug nunca había
tocado. Seis de seis coherentes.

**El serializador es una lista blanca explícita, campo por campo.** Está
prohibido recorrer atributos del objeto de la librería o serializarlo
genéricamente. Es más trabajo y más frágil ante cambios de la librería — a
propósito: un campo nuevo que aparezca río arriba **no** entra solo al dataset.
La fuga de información se previene en el momento de escribir el código, no
auditando después.

**Servicio de cálculo de daño en Node, aunque el agente sea Python.** El
cálculo real contempla los 16 rolls de daño, STAB, efectividad, clima,
habilidades, objetos, críticos y diferencias por generación. Existe la
calculadora oficial de Smogon, mantenida y parametrizada por generación, y no
tiene equivalente en Python. Reimplementarla no falla ruidosamente: devuelve
números sutilmente equivocados, y el agente decide "esto no lo mata" cuando sí
lo mataba. Un contenedor más y una llamada HTTP local por turno es un costo
despreciable frente a eso.

**Todo parametrizado por generación, desde el día uno.** El torneo del año que
viene es de otra generación. La data de gen 6 y gen 9 convive en las mismas
tablas y ningún módulo tiene una generación clavada.

## Los defectos que importaron

Ninguno de estos rompía nada visible. El agente jugaba, las filas se guardaban,
los tests pasaban en verde. **Ese es el punto.**

### Una pérdida sesgada, no aleatoria

La tabla de pasos usaba `(trayectoria, turno)` como clave. Pero un cambio
forzado tras un debilitamiento no avanza el turno: hay dos decisiones con el
mismo número, y la segunda pisaba a la primera.

Se perdían 265 de 1684 cambios. El número no es lo grave: **lo grave es que la
pérdida era sistemática.** Faltaba *toda* la clase "elegir reemplazo después de
que te debiliten un Pokémon". Un modelo entrenado con ese corpus no habría
visto un solo ejemplo de esa decisión.

Arreglarlo requirió cambiar la clave primaria de la tabla.

### El estado mezclaba dos momentos del juego

Showdown envía el pedido de decisión **ya resuelto**, antes de narrar lo que
pasó. La librería reconstruye el lado propio desde ese mensaje, pero el lado
del rival solo avanza parseando la narración, que llega después. Resultado:
cada fila tenía el lado propio de un turno y el del rival del anterior.

Se detectó midiendo 3293 filas contra el protocolo: el activo propio coincidía
con un turno y el del rival con el anterior. Se diagnosticó con una sonda en
vivo y **un experimento causal**: retrasar la respuesta 500 ms retrasaba la
narración exactamente 500 ms, lo que probó que la narración es la respuesta del
servidor y no datos que ya venían en camino. Esa medición descartó el arreglo
"obvio" —esperar la narración antes de responder— que habría sido un punto
muerto garantizado.

### Un test que corrompía la base en cada corrida

El test de idempotencia leía una batalla, la volvía a guardar, y verificaba que
no se duplicara. Leía cuatro columnas y volvía a escribir cinco: el ganador
viajaba como nulo y el `ON CONFLICT` lo propagaba.

**Cada corrida de la suite borraba el ganador de una batalla real, y ninguna de
sus aserciones lo detectaba** — el id y el conteo seguían coincidiendo. Había
corrompido seis batallas antes de que alguien lo notara.

El arreglo obvio —traer el ganador en el `SELECT`— era insuficiente: la
aserción se cumple sola cuando el ganador ya viene nulo, que es exactamente el
estado que el bug producía. Hizo falta un canario que exija que la batalla
tenga ganador antes de comparar.

### Una base de datos fantasma

Un Postgres nativo instalado por Homebrew escuchaba en el mismo puerto que el
contenedor del proyecto. Docker se ata a todas las interfaces, el nativo solo a
loopback, y **el específico gana**.

El pipeline habría escrito en la base equivocada **sin fallar nunca**. Se
descubrió por casualidad. Se movió el proyecto a un puerto sin ocupantes.

### El clima que se acepta y no hace nada

El servicio de cálculo validaba el clima contra una lista de valores exactos.
En gen 9 el granizo se llama `Snow` y da +50% de Defensa a los tipo Hielo; el
nombre viejo, `Hail`, sigue siendo un string válido pero el motor lo ignora.

La lista no dependía de la generación, así que **rechazaba el string correcto y
aceptaba en silencio el que no hace nada.** Un agente que quisiera modelar
granizo en gen 9 habría calculado sin clima. Se cerró gateando la validación
por generación, y midiendo empíricamente desde qué generación aplica cada
efecto en vez de asumirlo — el límite real resultó ser una generación distinta
de la esperada.

### Un arreglo que restaba más de lo que sumaba

Las formas alternativas de un Pokémon heredan el conjunto de movimientos de su
especie base, pero algunas tienen movimientos propios **además** de los
heredados. La primera resolución se quedaba con uno solo, así que faltaban
movimientos reales.

El arreglo propuesto elegía entre uno u otro. Aplicado, dos formas quedaron con
**cero** movimientos y hubo que revertir y restaurar la base. La solución
correcta era aditiva: unión de ambos conjuntos. Al implementarla meses después,
la verificación exigió los dos lados —que aparecieran los movimientos que
faltaban y que no desapareciera ninguno de los que estaban— porque un arreglo
que suma 15 y resta 400 es peor que el problema.

## Cómo se trabajó

**Todo el desarrollo es asistido por agentes**, con un patrón que se fue
afinando:

- Un agente implementa, otro revisa con contexto fresco, y un tercero coordina
  y verifica de forma independiente contra los datos reales.
- Trabajo en paralelo con agentes externos, **repartido por territorio
  disjunto**: un paquete por agente, sin archivos compartidos. Las tres
  colisiones de git que hubo al principio salieron de asumir un solo escritor.
- Toda decisión no trivial se registra. Hoy hay 22 decisiones documentadas, con
  su motivo y su costo aceptado.

Dos reglas que salieron de errores concretos y valen más que el código:

**Medí antes de arreglar.** En una ronda de revisión se propuso que las filas
mal etiquetadas eran "acciones cuyo Pokémon murió antes de actuar". Medido, esa
explicación cubría **3 de 20 casos**. De haberla aceptado, 17 filas quedaban
tapadas para siempre.

**Un test que puede pasar sin ejercer lo que dice ejercer es peor que no
tenerlo.** Un test cuyo bucle no itera nunca pasa en verde sin verificar nada.
Todos los tests de propiedades llevan un canario que falla cuando no
verificaron nada.

## Estado actual

Fase 2, rebanada 1, cerrada y mergeada.

| | |
|---|---|
| Código y tests | ~8.400 líneas (TypeScript + Python) |
| Documentación | ~6.500 líneas |
| Decisiones registradas | 22 |
| Commits | 95 |
| Tests | 234 en cinco paquetes |

**Lo que funciona hoy:** la data de juego de dos generaciones en Postgres
(834 y 874 especies, 128.000 filas de learnsets con su procedencia). Un agente
que juega batallas completas contra un servidor local y las graba con las
cuatro invariantes verificadas. Un servicio de cálculo de daño con validación
por generación. Un validador de equipos y un auditor del dataset independiente.

**Lo que sigue:** el grafo de decisión con un LLM —validado contra las acciones
legales, con reintento y respaldo determinista—, la medición del baseline
contra rivales de referencia, y después la API, la web y el torneo.

## Límites conocidos

- Un test de alineación falla de forma intermitente, alrededor de una vez cada
  diez corridas, por un caso que todavía no se caracterizó. Está documentado y
  en investigación.
- El agente juega eligiendo al azar entre las acciones legales. Es un grabador,
  no un jugador: la parte de decidir es la rebanada siguiente.
- Las 10.000 filas grabadas hasta hoy son de partidas al azar contra sí mismo y
  no sirven para entrenar. Generar volumen útil es una fase posterior.
- Tres movimientos de evento se heredan entre generaciones cuando no
  corresponde. Sin relevancia competitiva, documentado en vez de forzado.

## Lo que me llevo

El trabajo interesante no estuvo en hacer que el agente juegue. Estuvo en
darme cuenta de **cuál de todas las decisiones era la irreversible**, y
atacarla primero.

Jugar mal se arregla cambiando un prompt. Un dataset que miente se arregla
regrabando meses de partidas y tirando cualquier modelo entrenado encima. Todo
el rigor —el protocolo crudo, la lista blanca, los canarios, medir antes de
arreglar— existe por esa asimetría, no por prolijidad.

Y casi todos los defectos que importaron eran silenciosos. Ninguno rompía nada
visible.
