import { existsSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { defineConfig } from "vitest/config";

// Los tests de la capa load/ necesitan DATABASE_URL. vitest no lee .env solo, y
// sin esto `pnpm test` desde una shell limpia falla mientras que desde la shell
// del que lo escribio pasa — el peor modo de falla posible. process.loadEnvFile
// es nativo de Node 22, asi que no hace falta dotenv.
const envPath = fileURLToPath(new URL("../../.env", import.meta.url));
if (existsSync(envPath)) process.loadEnvFile(envPath);

export default defineConfig({
  test: {
    include: ["test/**/*.test.ts"],
    testTimeout: 120_000,
  },
});
