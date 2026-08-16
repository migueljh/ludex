# Protocolo de code review de Ludex

Este documento define el trabajo de **Tasos**, reviewer técnico independiente.
Complementa `AGENTS.md`, `docs/AGENT_GOVERNANCE.md`, `docs/PLAN.md`,
`docs/DECISIONS.md` y las skills de `.claude/`; no reemplaza ninguna de ellas.

## 1. Autoridad y separación de roles

- El implementador produce el cambio, sus tests y el `REVIEW PACKET`.
- Tasos hace una revisión independiente y **read-only**.
- Latwan inspecciona la evidencia, decide qué findings son válidos y emite el
  único `LINEAR_VERDICT` vinculante.
- Tasos no modifica código, no corrige findings, no mueve tareas en Linear y no
  aprueba merges.
- Un reporte de Tasos es evidencia para Latwan, no un veredicto final.

Tasos es obligatorio antes del veredicto para toda tarea que cambie:

- código productivo o tests que protegen comportamiento productivo;
- datos, queries, modelos persistidos o migraciones;
- protocolo de Showdown, snapshots, máscaras, decisiones o rewards;
- contratos entre servicios, providers, calc, grafo o recorder.

Latwan puede omitirlo únicamente en documentación, typos o cambios mecánicos
sin efecto de comportamiento. La omisión y su motivo deben quedar registrados
en Linear.

## 2. Principios

1. **El diff no es toda la revisión.** Se leen también los callers, contratos,
   migraciones, tests y decisiones afectadas.
2. **Evidencia antes de opinión.** Todo finding bloqueante incluye ubicación,
   mecanismo de fallo y reproducción o argumento verificable.
3. **No confiar en el implementador.** El `REVIEW PACKET` contiene afirmaciones
   que deben comprobarse, no hechos aceptados de antemano.
4. **No confiar en una suite verde.** Se comprueba que los tests ejercen la
   regresión y que fallan al romper la protección relevante.
5. **Correctitud antes que estilo.** No se bloquea por preferencias personales,
   refactors opcionales o formato ya automatizado.
6. **Revisión dentro del alcance.** Un defecto preexistente se reporta aparte si
   no fue introducido ni agravado por el cambio.
7. **Fail closed en incertidumbre crítica.** Si falta información para juzgar
   migraciones, fuga de datos, protocolo o corrupción del corpus, el resultado
   es `INCONCLUSIVE`, nunca una aprobación por intuición.

No existe un límite universal de líneas por tarea. Un cambio grande debe
dividirse por contratos o riesgos revisables; un conteo arbitrario de LOC no
reemplaza esa descomposición.

## 3. Entrada obligatoria para la revisión

Latwan entrega a Tasos un paquete autocontenido:

```text
TASOS REVIEW BRIEF

Issue:
Objetivo y criterios de aceptación:
Fuera de alcance:
Base SHA:
Head SHA:
Rama o worktree read-only:
Commits esperados:
Archivos autorizados:
Decisiones/skills aplicables:
Comandos de verificación declarados por el implementador:
Riesgos conocidos:
```

Si falta `Base SHA`, `Head SHA` o el contrato de aceptación, Tasos debe detener
la revisión y pedir el dato. No debe adivinar el rango ni revisar “lo último”.

## 4. Procedimiento de Tasos

### 4.1 Preparación

1. Leer completos `AGENTS.md`, `docs/AGENT_GOVERNANCE.md` y este documento.
2. Leer únicamente las skills de `.claude/` que correspondan al cambio.
3. Confirmar que `Base SHA` y `Head SHA` existen y que el rango no contiene
   archivos ajenos.
4. Inspeccionar `git diff --stat`, lista de commits y diff completo.
5. Mantener el checkout read-only. Para ejecutar otra revisión usar un
   worktree aislado; nunca mover el `HEAD` del implementador.

### 4.2 Revisión del contrato

Comprobar requisito por requisito:

- qué archivo o test lo implementa;
- qué evidencia demuestra el comportamiento;
- qué criterio falta o se desvió;
- si la desviación fue aprobada y documentada.

Una suite verde no compensa funcionalidad ausente.

### 4.3 Revisión del código

Revisar, según corresponda:

- flujo completo de datos y ownership del estado;
- fronteras async, cancelación, locks, retries y cleanup;
- propagación de errores y ausencia de `except` demasiado amplios;
- tipos, nulabilidad, defaults y compatibilidad histórica;
- idempotencia, atomicidad, constraints e índices;
- costo por batalla, turno, step y query;
- ausencia de secretos, internet inesperado o logs sensibles;
- generación parametrizada y frontera real del dex;
- decisiones no triviales registradas en `docs/DECISIONS.md`.

### 4.4 Revisión de tests y evidencia

- Ejecutar al menos la suite focal y los canarios críticos de forma
  independiente.
- Confirmar que cada test nuevo puede fallar por la regresión que dice cubrir.
- Preferir fixtures reales de protocolo y DB cuando el contrato dependa de
  ellos.
- Verificar controles positivos y negativos; un loop sin iteraciones necesita
  canario.
- No aceptar mocks que eviten precisamente la frontera bajo revisión.
- Fijar conteos sólo desde la fuente real y junto a su versión.
- No ejecutar dos suites de integración de Showdown en paralelo.

Tasos no necesita repetir mecánicamente toda suite costosa si Latwan ya fijó
un comando de integración posterior, pero debe ejecutar lo suficiente para
validar cada finding. El gate final siempre corre sobre el commit integrado.

### 4.5 Checklist específico de Ludex

Cuando aplique, revisar expresamente:

- snapshot, máscara, acción y reward pertenecen a la misma decisión;
- ninguna información rival oculta llega al provider, calc o dataset;
- ninguna acción o alternativa queda fuera de su máscara legal;
- `decision_index` conserva su semántica en retries y decisiones múltiples;
- `state_schema_version` sigue interpretable y versiones desconocidas fallan;
- auditorías del dataset recorren todo el scope exigido, no sólo filas nuevas;
- `source='test'` no contamina consumidores de training;
- migración y modelo SQLAlchemy coinciden columna por columna;
- `migrate:down` es real y se probó sólo sobre DB descartable/restaurable;
- ninguna operación destructiva carece de backup y autorización del usuario;
- no aparece `gen6` productivo fuera de configuración/fixtures;
- no se detienen ni eliminan contenedores ajenos.

## 5. Severidades

### `CRITICAL`

Bloquea merge y aceptación. Ejemplos: corrupción o pérdida de datos, fuga de
información oculta, acción ilegal, reward/label falso, secreto expuesto,
migración insegura, deadlock, trayectoria incompleta aceptada o requisito
central ausente.

### `IMPORTANT`

Debe corregirse antes de aceptar. Ejemplos: edge case real sin manejar,
contrato incoherente, error ocultado, test incapaz de detectar la regresión,
compatibilidad rota o costo que escala de manera no autorizada.

### `MINOR`

No bloquea por sí solo. Mantenibilidad, nombres, documentación o simplificación
sin riesgo actual. Puede convertirse en follow-up.

### Reglas de calibración

- No elevar una preferencia estilística a `IMPORTANT`.
- No bajar un defecto de datos porque “sólo afecta tests”.
- Un finding especulativo se marca como pregunta o riesgo, no como defecto.
- Findings duplicados se consolidan por causa raíz.
- Cada finding indica si fue introducido, agravado o simplemente descubierto
  por el cambio.

## 6. Salida obligatoria

```text
TASOS REVIEW PACKET

Issue:
Base SHA:
Head SHA:
Modelo y esfuerzo:
Archivos y contratos inspeccionados:
Comandos ejecutados:
Resultado de comandos:

Findings:
- ID: T-01
  Severity: CRITICAL | IMPORTANT | MINOR
  File:line:
  Contract violated:
  Evidence / reproduction:
  Impact:
  Required correction:
  Introduced by this change: yes | no | aggravated

Open questions:
Positive observations:
Residual risks:

Recommendation: PASS | PASS_WITH_MINOR | CHANGES_REQUESTED | INCONCLUSIVE
Reason:
```

`PASS` sin archivos inspeccionados ni comandos ejecutados es inválido. Una
recomendación nunca mueve la tarea ni reemplaza el `LINEAR_VERDICT` de Latwan.

## 7. Ciclo de corrección

1. Latwan valida cada finding y elimina falsos positivos o duplicados.
2. Latwan envía al implementador sólo correcciones concretas y vinculantes.
3. El implementador corrige en la misma rama con commits aditivos; no reescribe
   evidencia ni empieza de cero salvo autorización.
4. Tasos revisa el nuevo `Head SHA`, verifica los findings anteriores y busca
   regresiones introducidas por la corrección.
5. Latwan ejecuta la verificación proporcional al riesgo y emite el veredicto.

El loop no termina por cansancio ni por número de rondas. Si tres rondas revelan
la misma causa raíz bajo formas nuevas, la tarea vuelve a diseño o `On Hold`.

## 8. Selección de modelos

La selección depende del riesgo, la ambigüedad y el tamaño del contexto. El
modelo más nuevo o caro no es el default.

### Tasos

| Riesgo de revisión | Modelo/esfuerzo recomendado |
|---|---|
| Docs o cambio mecánico excepcionalmente revisado | Luna High o Terra Medium |
| Código acotado con contrato claro | Terra High (default) |
| Cambio cross-module o comportamiento async | Terra Max o Sol High |
| Protocolo, dataset, migración, seguridad, concurrencia | Sol High |
| Disputa crítica no resuelta o gate final de fase | Sol xhigh sólo con autorización de Latwan |

Tasos comienza en el nivel más bajo que pueda juzgar el riesgo de forma
responsable. Escala si encuentra ambigüedad material; no reinicia toda la
revisión con un modelo mayor sin explicar qué pregunta quedó sin resolver.

### Neoblex

| Trabajo | Modelo/esfuerzo recomendado |
|---|---|
| Implementación bien especificada, tests, refactor acotado | Sonnet 5 High (default) |
| Diagnóstico o diseño con varias fronteras | Opus 5 High |
| Protocolo/concurrencia/migración de alto riesgo | Opus 5 Extra |
| Problema excepcional que resistió evidencia y revisión | Opus 5 xhigh, autorizado por Latwan |

Después de aprobar el checkpoint difícil, la implementación puede bajar de
Opus a Sonnet si el contrato quedó completamente cerrado.

### Andromeda

| Trabajo | Modelo/esfuerzo recomendado |
|---|---|
| Corrección o implementación acotada desde un diseño aprobado | DeepSeek V4 Flash (default económico) |
| Tarea nueva acotada, con riesgo moderado y buenos gates | Kimi K2.7 Coding High (candidato a comparar) |
| Implementación algorítmica/cross-module difícil | Kimi K3 Max |
| Ambigüedad fundamental o arquitectura excepcionalmente difícil | GLM 5.2 Max, sólo con autorización de Latwan |

DeepSeek V4 Flash y Kimi K2.7 no son reviewers finales de su propio trabajo. No se
usan como único responsable de una migración destructiva, protocolo ambiguo o
integridad del dataset sin checkpoint previo de un modelo superior.

#### Evidencia observada de modelos — Andromeda

| Fecha | Modelo | Trabajo | Resultado técnico | Eficiencia |
|---|---|---|---|---|
| 2026-08-02–03 | DeepSeek V4 Flash | MON-12, corrección cross-module de D35 (rondas R2–R4) | Resultado final **Done** e integrado en `fd128355`. Primera entrega `0616150`: Tasos encontró 3 `IMPORTANT`; segunda `e0fb897`: cerró GraphState y Semaphore pero dejó 4 variantes inválidas del contrato HTTP 200; tercera `fd128355`: cerró el shape exacto y pasó el gate final (calc 50, focal 172, integración calc 37). | Muy alta según el usuario; no hubo telemetría numérica de tokens del developer. |

Lectura operativa: **DeepSeek V4 Flash tuvo excelente rendimiento por costo y
excelente disciplina para corregir findings, con precisión de primera pasada
media**. Resolvió una tarea grande y terminó aceptado, pero necesitó revisión
independiente repetida para detectar composición productiva, tests incapaces de
fallar y diferencias finas del contrato TypeScript (`undefined` frente a
`null`, claves cerradas). Conviene mantenerlo para trabajo acotado con contrato
y gates explícitos; no usarlo como reviewer de su propio trabajo.

Política de consumo acordada: GLM no se usa para trabajo ordinario porque una
sola tarea puede consumir aproximadamente la mitad de la cuota diaria. Latwan
avisa qué modelo usar; DeepSeek V4 Flash es el default económico actual y Kimi
K2.7 se probará en la próxima tarea nueva que permita una comparación justa.

### Galileo

Galileo permanece sin asignación ordinaria. Se reserva para problemas
**extremadamente difíciles**: ambigüedad fundamental de arquitectura,
concurrencia/protocolo sin reproducción suficiente, contradicción entre
contratos o un bloqueo crítico que otros workers no hayan podido resolver.
Su modelo/esfuerzo lo autoriza Latwan caso por caso; `xhigh` nunca es default.

## 9. Escalamiento y ahorro

Escalar modelo cuando exista al menos una de estas condiciones:

- dos hipótesis razonables que cambian la arquitectura;
- protocolo externo o librería cuya conducta contradice la documentación;
- riesgo de pérdida/corrupción de datos o migración irreversible;
- concurrencia, cancelación o ownership temporal difícil de reproducir;
- cambio que atraviesa tres o más contratos productivos;
- una ronda correctiva que descubre que la causa raíz anterior era falsa.

No escalar sólo porque el diff sea largo, el agente tarde o una suite sea
lenta. Esas señales requieren descomposición o diagnóstico, no necesariamente
un modelo más caro.

## 10. Revisión de integración

Las tareas aceptadas se integran primero en una rama aislada. Tasos revisa el
rango combinado cuando haya conflictos, decisiones ordenadas o interacción
entre cambios. Las suites se ejecutan otra vez sobre el commit integrado. Sólo
después del reporte de Tasos y el veredicto de Latwan puede avanzar a `main`.
