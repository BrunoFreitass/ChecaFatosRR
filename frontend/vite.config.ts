import tailwindcss from "@tailwindcss/vite";
import react from "@vitejs/plugin-react";
import { configDefaults, defineConfig } from "vitest/config";

import { cspPlugin } from "./csp.ts";

// defineConfig do vitest/config = o do Vite + a chave "test".
export default defineConfig({
  // cspPlugin: CSP de produção no dist/_headers e no vite preview (e2e).
  plugins: [react(), tailwindcss(), cspPlugin()],
  test: {
    // e2e/ é do Playwright (npm run e2e), não do Vitest.
    exclude: [...configDefaults.exclude, "e2e/**"],
  },
});
