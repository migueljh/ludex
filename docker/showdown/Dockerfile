FROM node:22-alpine

RUN apk add --no-cache git

ARG SHOWDOWN_REF=v0.11.10
WORKDIR /app
RUN git clone --depth 1 --branch ${SHOWDOWN_REF} \
      https://github.com/smogon/pokemon-showdown.git . \
 && npm install --omit=dev \
 && node build

COPY config.js /app/config/config.js

EXPOSE 8000
CMD ["node", "pokemon-showdown", "start", "--no-security", "8000"]
