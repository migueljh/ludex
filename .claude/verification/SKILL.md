---
name: verification
description: Cómo se verifica el trabajo en Ludex antes de darlo por terminado. Usar al escribir tests, al fijar valores esperados o conteos, al cerrar una rebanada o fase, al revisar código de otro agente, o cuando un test pasa y hay que decidir si eso significa algo. También al agregar canarios o golden files.
---

# Verificación en Ludex

Un test que pasa no es evidencia de nada hasta que sabés qué lo hace fallar.
Este es el procedimiento que encontró todos los bugs reales del proyecto.

## La regla central: romper a propósito

**Antes de dar por bueno un test, rompé la función que prueba y confirmá que el test
falla.** No es opcional y no es paranoia: es la única forma de distinguir un test que
verifica de un test que acompaña.

Ejemplo real (D10). El canario de learnsets afirmaba `toBeGreaterThan(49321)`. Parecía
suficiente. Al romper a propósito la resolución de `baseSpecies`, el conteo cayó a
52482 filas: **el test seguía pasando**, porque 52482 > 49321. La aserción parecía
proteger y no protegía nada. El arreglo fue sumar `toHaveLength(62198)`, el valor
exacto pineado.

El ejercicio de romper además encontró un segundo bug distinto del que se buscaba.
Eso pasa seguido: el intento de romper te obliga a leer la función de verdad.

## Mutaciones: el install editable puede darte falso verde (MON-20 R8)

**Si mutás una copia exportada del repo y no pineás `PYTHONPATH`, el test puede
correr contra el árbol SIN mutar y salir verde en silencio.**

El venv de `apps/agent` es un install editable: `.venv/lib/python*/site-packages/
_editable_impl_ludex_agent.pth` contiene la ruta ABSOLUTA del src del worktree
real. `import ludex_agent` resuelve ahí aunque corras pytest desde la copia
mutada. Medido en R8: la misma mutación (subclase nueva sin rama en la tabla)
dio `1 passed` sin pin y `FAILED` con pin — el falso verde es indistinguible de
un canario vacuo, y por eso un canario vacuo puede sobrevivir varias rondas.

**Método correcto para mutaciones sobre copias (`git archive` a scratch):**

```bash
cd /tmp/<scratch>/apps/agent && \
env -i PATH=/usr/bin:/bin:/usr/sbin:/sbin:/opt/homebrew/bin \
    PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=$PWD/src \
    /Users/<usuario>/Documents/ludex-mon-20/apps/agent/.venv/bin/python \
    -m pytest --noconftest -q tests/...
```

Verificá el pin antes de creer cualquier verde o rojo:

```bash
env -i PATH=/usr/bin:/bin:/usr/sbin:/sbin:/opt/homebrew/bin PYTHONPATH=$PWD/src \
    /Users/<usuario>/Documents/ludex-mon-20/apps/agent/.venv/bin/python \
    -c "import ludex_agent.provider_taxonomy; print(ludex_agent.provider_taxonomy.__file__)"
# tiene que imprimir la ruta del SCRATCH, no la del worktree
```

Alternativa equivalente: mutar in-place sobre el worktree y restaurar cada
mutación verificando `sha256` contra el archivo original. `evals/` y `tests/`
se cargan por path relativo al archivo de test y no están afectados por el
`.pth`; el riesgo está en `src/ludex_agent/*`.

## Números esperados: inspeccionar, nunca recordar

**Nunca escribas un valor esperado de memoria.** Ni un conteo, ni un learnset, ni el
número de habilidades de una generación. Inspeccioná el objeto real, imprimí el valor,
y recién ahí escribilo en el test.

Un test contra un número plausible pero falso es peor que no tener test: te da
confianza falsa y después nadie lo cuestiona porque "ya estaba".

Cuando fijes un número, dejá anotado **al lado de la versión del paquete** que lo
produjo. Un conteo sin versión no sirve para evaluar un bump.

## Canarios: la relación Y el valor exacto

Un buen canario tiene dos aserciones:

- **La relación conceptual** (`toBeGreaterThan(X)`): sobrevive a cambios de versión del
  paquete y expresa la invariante. "La herencia solo puede sumar filas."
- **El valor exacto** (`toHaveLength(Y)`): detecta cualquier deriva, incluida la que la
  relación deja pasar.

Las dos, siempre. Con la relación sola se te escapan bugs que quedan del lado correcto
de la desigualdad, que es exactamente lo que pasó en D10.

Documentá el canario en `DECISIONS.md` **y** dejalo enforced en un test. Un canario
solo documentado no es un canario, es una nota.

## Las tres capas

| capa | qué prueba | corre |
|---|---|---|
| unit, sin DB | extractores puros contra el paquete | siempre |
| frontera de generación | que el mod filtre de verdad | siempre |
| integración, con DB | migraciones, carga, idempotencia | antes de cerrar la rebanada |

El corte `extract/` puro contra `load/` existe para que la capa riesgosa se pueda
testear sin levantar nada. Mantenelo: cualquier lógica de dominio que se filtre a
`load/` deja de ser testeable barato.

**La frontera de generación se prueba con hechos del juego, no con "el objeto existe".**
Que Hada exista en 6 y no en 5. Que Dragón contra Hada dé 0×. Que Acero deje de resistir
Fantasma. Que no haya megas en gen 9. Un test que solo confirma que el paquete devolvió
algo no prueba que el mod filtró.

## Idempotencia: no alcanza con contar dos veces

"Correr el seed dos veces da los mismos conteos" pasa aunque los upserts estén
insertando duplicados que una constraint descarta en silencio.

El test tiene que **modificar una fila a mano y verificar que la segunda corrida la
actualiza**, no que la ignora.

Relacionado (D13): un contador de filas *escritas* no es un conteo de tabla. El pipeline
es upsert-only, sin `DELETE` ni `TRUNCATE`, así que una fila que el paquete deje de
traer sobrevive para siempre. Los conteos que se persisten tienen que salir de
`count(*)` sobre la tabla, filtrado por generación.

## Golden files

Los conteos te dicen que algo cambió. Los golden files te dicen **qué**.

Mantené un snapshot de dos o tres entradas completas: una especie con formes, un
movimiento, un learnset resuelto. Cuando un bump de versión rompa algo, el diff del
snapshot te ahorra la tarde de bisección.

## Refactors sobre data: probá inclusión, no igualdad

Cuando cambies cómo se resuelve algo, la verificación correcta no es "el conteo se
mantiene" sino **"el conjunto anterior es subconjunto del nuevo"**, especie por especie,
en todas las generaciones que soporta el proyecto.

Así se validó D14: gen 6 quedó en delta 0 y gen 9 sumó 18 filas, y quedó demostrado que
nada se perdió. Un conteo agregado no lo hubiera demostrado, porque compensa pérdidas
con ganancias.

## Antes de cerrar una rebanada

- [ ] Los tests de las tres capas pasan.
- [ ] Cada test nuevo se verificó rompiendo lo que prueba.
- [ ] Los valores esperados salieron de inspección, no de memoria, y están anotados
      junto a la versión del paquete que los produjo.
- [ ] Los canarios tienen relación **y** valor exacto, documentados y enforced.
- [ ] Los criterios de aceptación de la fase se corrieron de verdad, no se asumieron.
- [ ] `grep -ri "gen6"` no devuelve nada fuera de config y fixtures.
- [ ] Toda decisión no trivial quedó en `DECISIONS.md` con su motivo.
- [ ] Todo límite conocido quedó documentado como límite, con su alcance acotado, en vez
      de quedar implícito.

## Sobre los límites conocidos

Cuando algo no se arregla, documentar **por qué no** y **cuál es el impacto real por
generación** vale tanto como arreglarlo. Ver D13 ("esto NO agrega borrado de filas
obsoletas") y D14 ("impacto en gen 6: ninguno; en gen 9: real").

Un arreglo parcial sin alcance documentado es una trampa para el que venga después,
incluido vos en tres meses.