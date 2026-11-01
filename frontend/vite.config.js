import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import { resolve } from "node:path";

export default defineConfig({
  plugins: [react()],
  base: "/static/react/",
  build: {
    outDir: resolve(import.meta.dirname, "../app/static/react"),
    emptyOutDir: true,
    sourcemap: false,
    rollupOptions: {
      input: resolve(import.meta.dirname, "src/main.jsx"),
      output: {
        entryFileNames: "legal-relations.js",
        assetFileNames: "legal-relations.[ext]",
      },
    },
  },
});
