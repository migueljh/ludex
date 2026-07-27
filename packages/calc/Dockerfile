FROM node:22-alpine

RUN corepack enable

WORKDIR /app

# El contexto de build es la raiz del repo (pnpm workspace). Se copian los
# manifiestos antes del install para que la capa de dependencias se cachee
# mientras no cambie el lockfile: es lo que congela @pkmn/data, donde viven
# los numeros de la formula.
COPY package.json pnpm-lock.yaml pnpm-workspace.yaml ./
COPY packages/calc/package.json packages/calc/package.json
COPY packages/seed/package.json packages/seed/package.json
RUN pnpm install --frozen-lockfile --filter @ludex/calc...

COPY packages/calc ./packages/calc

# Adentro del contenedor tiene que escuchar en 0.0.0.0 o el port-mapping no
# llega; la exposicion al host queda restringida a 127.0.0.1 en compose (D11).
ENV HOST=0.0.0.0 PORT=8200
EXPOSE 8200

CMD ["pnpm", "--filter", "@ludex/calc", "start"]
