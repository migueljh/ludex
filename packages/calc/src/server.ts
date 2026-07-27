import { realpathSync } from "node:fs";
import { createServer, type IncomingMessage, type Server } from "node:http";
import { fileURLToPath } from "node:url";
import { CalcError, runCalc, SUPPORTED_GENS } from "./calc.js";
import { CALC_VERSION } from "./version.js";

/** Los descriptores son chicos; un body mayor es un bug del cliente. */
const MAX_BODY_BYTES = 64 * 1024;

function readBody(req: IncomingMessage): Promise<string> {
  return new Promise((resolve, reject) => {
    const chunks: Buffer[] = [];
    let size = 0;
    req.on("data", (chunk: Buffer) => {
      size += chunk.length;
      if (size > MAX_BODY_BYTES) {
        reject(new CalcError("invalid_request", `body demasiado grande (max ${MAX_BODY_BYTES} bytes)`));
        req.destroy();
        return;
      }
      chunks.push(chunk);
    });
    req.on("end", () => resolve(Buffer.concat(chunks).toString("utf8")));
    req.on("error", reject);
  });
}

export function createCalcServer(): Server {
  const send = (res: import("node:http").ServerResponse, status: number, body: unknown) => {
    res.writeHead(status, { "content-type": "application/json" });
    res.end(JSON.stringify(body));
  };

  return createServer(async (req, res) => {
    try {
      const url = (req.url ?? "").split("?")[0];

      if (req.method === "GET" && url === "/health") {
        return send(res, 200, {
          status: "ok",
          calc_version: CALC_VERSION,
          gens_supported: [...SUPPORTED_GENS],
        });
      }

      if (url === "/calc") {
        if (req.method !== "POST") {
          return send(res, 405, { error: { code: "method_not_allowed", message: "/calc solo acepta POST" } });
        }
        const raw = await readBody(req);
        let parsed: unknown;
        try {
          parsed = JSON.parse(raw);
        } catch {
          throw new CalcError("invalid_json", "el body no es JSON valido");
        }
        return send(res, 200, runCalc(parsed));
      }

      return send(res, 404, { error: { code: "not_found", message: `${req.method} ${url} no existe` } });
    } catch (e) {
      if (e instanceof CalcError) {
        return send(res, e.status, { error: { code: e.code, message: e.message } });
      }
      // La validacion deberia cubrir todo lo del paquete; si algo escapa es un
      // bug nuestro y se loguea, no se devuelve al cliente.
      console.error(e);
      return send(res, 500, { error: { code: "internal", message: "error interno inesperado" } });
    }
  });
}

/**
 * Solo corre como proceso, no cuando lo importa un test. Misma tecnica que en
 * packages/seed/src/cli.ts: comparar rutas resueltas, no nombres de archivo.
 */
const invokedPath = process.argv[1] ? realpathSync(process.argv[1]) : null;
if (invokedPath === fileURLToPath(import.meta.url)) {
  const port = Number(process.env.PORT ?? 8200);
  // Default loopback para dev local; en el contenedor compose setea HOST=0.0.0.0
  // y publica solo 127.0.0.1 en el host (misma regla que D11).
  const host = process.env.HOST ?? "127.0.0.1";
  createCalcServer().listen(port, host, () => {
    console.log(`calc escuchando en http://${host}:${port} (@smogon/calc@${CALC_VERSION})`);
  });
}
