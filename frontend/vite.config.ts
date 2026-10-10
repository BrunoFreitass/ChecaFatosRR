import tailwindcss from "@tailwindcss/vite";
import react from "@vitejs/plugin-react";
import { configDefaults, defineConfig } from "vitest/config";

// defineConfig do vitest/config = o do Vite + a chave "test".
export default defineConfig({
  plugins: [react(), tailwindcss()],
  test: {
    // e2e/ é do Playwright (npm run e2e), não do Vitest.
    exclude: [...configDefaults.exclude, "e2e/**"],
  },
});
