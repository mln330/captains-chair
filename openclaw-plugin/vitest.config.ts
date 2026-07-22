import path from "node:path";
import { fileURLToPath } from "node:url";
import { defineConfig } from "vitest/config";

const root = path.dirname(fileURLToPath(import.meta.url));

export default defineConfig({
  test: { environment: "jsdom", include: ["tests/**/*.test.ts", "ui-tests/**/*.test.tsx"] },
  resolve: {
    dedupe: ["react", "react-dom"],
    alias: {
      react: path.resolve(root, "node_modules/react"),
      "react-dom": path.resolve(root, "node_modules/react-dom"),
      "react/jsx-runtime": path.resolve(root, "node_modules/react/jsx-runtime.js"),
      "react/jsx-dev-runtime": path.resolve(root, "node_modules/react/jsx-dev-runtime.js"),
    },
  },
});
