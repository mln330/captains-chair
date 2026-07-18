import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  resolve: { dedupe: ["react", "react-dom"] },
  build: {
    outDir: "../dist/ui",
    emptyOutDir: true,
    rollupOptions: { output: { entryFileNames: "assets/index.js", assetFileNames: "assets/[name][extname]" } },
  },
});
