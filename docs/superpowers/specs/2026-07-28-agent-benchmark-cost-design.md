# Benchmark de calidad y costo del agente

Fecha: 2026-07-28

## Objetivo

Medir modelos fijos contra `SimpleHeuristicsPlayer` y registrar, corrida por
corrida, calidad, validez, uso real de tokens y costo por batalla. El registro
debe permitir proyectar el costo de generar 10.000 batallas sin mezclar modelos,
inventar usage ni depender de precios enterrados en código.

## Alcance

Esta rebanada:

- captura el `usage` devuelto por cada respuesta exitosa;
- suma llamadas y tokens por corrida;
- calcula costo con una tabla versionada y fechada;
- agrega una fila durable por corrida a un ledger;
- soporta los protocolos que requiere la matriz de candidatos;
- ejecuta una escalera corta de benchmarks, siempre con proveedor y modelo
  fijos dentro de cada corrida.

No modifica el estado serializado, el recorder, `showdown/client.py`, las
trayectorias ni el esquema de Postgres. Las batallas de benchmark siguen sin
persistirse por defecto.

## Modelos y orden de evaluación

La evaluación de componentes de diseño aportada por el dueño es evidencia para
priorizar precio/calidad, pero no mide estrategia de Pokémon. Se usa como prior,
no como resultado transferible.

El orden inicial será:

1. humo de 2 batallas con `mimo-v2.5-free` por OpenCode Zen;
2. 15 batallas con `minimax-m2.7` por OpenCode Zen;
3. 15 batallas con `kimi-k2.6` por Kimi directo.

Quedan preparados, sin mezclarlos con esas corridas:

- `qwen3.5-plus`, `qwen3.6-plus` y futuros Qwen servidos por `/messages`;
- `deepseek-v4-pro`, servido por `/chat/completions`;
- los modelos GPT servidos por `/responses`, como extensión posterior del
  mismo selector de protocolo.

DeepSeek V4 Pro es el siguiente candidato pago si MiniMax no demuestra
estrategia suficiente. No entra en las dos primeras mediciones pagas porque
MiniMax M2.7 obtuvo el mismo 9/10 en la evaluación aportada a menor costo. MiMo
es un piso experimental: ser gratuito hoy no garantiza disponibilidad futura.

## Protocolos de proveedor

La configuración separa proveedor comercial de protocolo HTTP:

- Kimi directo: OpenAI-compatible `/chat/completions`;
- OpenCode Zen MiniMax, DeepSeek, Kimi y MiMo: `/chat/completions`;
- OpenCode Zen Qwen: Anthropic-compatible `/messages`;
- OpenCode Zen GPT: OpenAI `/responses`.

El selector se basa en configuración explícita por modelo, no en prefijos
inferidos silenciosamente. Un modelo sin protocolo registrado falla antes de
iniciar la batalla.

Cada corrida construye un solo proveedor y un solo modelo. La cadena entre
proveedores permanece prohibida en benchmark. Un fallo de infraestructura
aborta la corrida y conserva el resultado parcial como no comparable.

## Contrato de respuesta y usage

`DecisionProvider.complete` devuelve un sobre interno con:

- el payload estructurado validable por `DecisionResponse`;
- `input_tokens`;
- `output_tokens`;
- `cached_input_tokens`, cuando el proveedor lo informa;
- `reasoning_tokens`, cuando el proveedor lo informa;
- el identificador de modelo reportado por la API.

La captura ocurre en el borde del SDK usando la respuesta cruda junto con la
respuesta estructurada. No se tokeniza localmente ni se estima usage. El
payload que consume `decide` conserva el contrato semántico actual.

Una llamada exitosa incrementa `calls_total` exactamente una vez. Un intento
que termina en 429, 5xx o timeout sigue registrado en las métricas de
infraestructura existentes, pero no inventa tokens ni costo. Si el proveedor
incluye usage en una respuesta inválida semánticamente, esos tokens sí se
cuentan: fueron consumidos y facturados.

Los tokens de razonamiento forman parte del output facturable cuando así lo
reporta el proveedor. También se exponen por separado para poder explicar por
qué dos modelos con tarifas parecidas terminan con costos distintos.

## Kimi K2.6

K2.6 tiene thinking habilitado por defecto; `reasoning_content` consume tokens.
La configuración de la corrida deja explícitos:

- `thinking.type`;
- temperatura compatible con ese modo;
- límite máximo de salida.

No se hereda el `temperature=0` genérico: la API de Kimi fija 1.0 con thinking
y 0.6 sin thinking. La primera medición usa thinking habilitado porque el
objetivo es evaluar razonamiento táctico, y registra sus tokens reales. Si el
costo por batalla resulta alto, una corrida posterior con thinking
deshabilitado será otro modelo/configuración experimental y tendrá su propia
fila; no se mezclan sus resultados.

## Tabla de precios

Los precios viven en un archivo de configuración versionado, separado del
código. Cada entrada contiene:

- proveedor;
- modelo;
- moneda;
- precio por millón de tokens de entrada;
- precio por millón de tokens de salida;
- precio de lectura de caché, si aplica;
- fecha de consulta;
- URL de la fuente;
- identificador estable de la tabla de precios.

Fuentes iniciales:

- Kimi: `https://platform.kimi.ai/`;
- OpenCode Zen: `https://opencode.ai/docs/zen/`.

El costo de una corrida es la suma de cada clase de tokens por su tarifa, no
`llamadas × promedio`. Si falta una tarifa necesaria, el ledger conserva usage
y deja costo vacío con una nota. No se usa cero para representar precio
desconocido.

## Ledger acumulativo

`docs/BENCHMARKS.md` contiene una fila por corrida y referencia el artefacto
JSON completo de esa ejecución. La fila incluye:

- fecha y estado (`complete` o `aborted`);
- proveedor, modelo y parámetros relevantes;
- rival;
- batallas pedidas y completadas;
- victorias, derrotas, empates, winrate y Wilson 95%;
- llamadas totales y llamadas por batalla;
- tokens de entrada, caché, razonamiento y salida;
- costo total y costo por batalla;
- proyección lineal para 10.000 batallas;
- porcentaje recuperado por reintento semántico;
- porcentaje en fallback;
- turnos en deadline;
- rotaciones;
- tabla de precios utilizada.

El JSON es la fuente detallada y el Markdown es el índice humano acumulativo.
El comando agrega ambos solo al finalizar o abortar la corrida; una fila
abortada nunca publica winrate comparable ni costo por batalla como si la
muestra estuviera completa.

## Validez y decisión

Para cada corrida se reporta:

- `turns_total`;
- turnos con primera respuesta inválida;
- recuperados por reintento (`invalid - fallback`);
- turnos en fallback;
- deadlines;
- rotaciones y fallos de infraestructura.

El 0% de ilegalidad observado con Gemini ya fue auditado externamente: el
modelo no elige siempre la primera acción. Las nuevas corridas conservan estas
métricas para demostrar que el winrate pertenece al modelo y no al respaldo.

Con 15 batallas contra el heurístico:

- 5 o más victorias es señal suficiente de que está en otro orden respecto del
  baseline de 3%;
- 0 o 1 victoria es señal suficiente de que no mejora de manera útil;
- resultados intermedios se leen junto con Wilson, costo y validez, sin
  promover una conclusión más fuerte que la muestra.

## Pruebas

- RED/GREEN para extraer usage real de respuestas crudas de cada protocolo.
- RED/GREEN para contar usage de una respuesta semánticamente inválida.
- RED/GREEN para no inventar usage en 429, 5xx o timeout.
- RED/GREEN para cálculo por tokens de entrada, salida y caché.
- RED/GREEN para costo desconocido cuando falta tarifa.
- RED/GREEN para impedir un modelo sin protocolo registrado.
- RED/GREEN para ledger completo y corrida abortada.
- Pruebas de humo estructuradas con las claves reales antes de cualquier
  benchmark largo.
- Cada test nuevo se rompe deliberadamente según
  `.claude/verification/SKILL.md`.
- Suite completa del agente antes de cada checkpoint de ejecución.

## Límites conocidos

- Los scores de la evaluación de componentes no predicen directamente winrate.
- Los modelos gratuitos pueden desaparecer o aplicar límites sin aviso.
- La proyección a 10.000 batallas es lineal sobre el costo observado; debe
  mostrar tamaño de muestra y fecha, no presentarse como cotización.
- El costo excluye impuestos, recargas y comisiones de tarjeta.
- El caching depende de que el proveedor lo aplique y lo reporte; no se supone.
- Esta rebanada no resuelve el canal de acciones rechazadas por Showdown ya
  documentado en la spec del grafo.
