import preact from "@preact/preset-vite";
import { defineConfig } from "vite";

// Dev port generated once (repo norm: no default vite port).
const DEV_PORT = 57149;

export default defineConfig({
  plugins: [preact()],
  server: {
    port: DEV_PORT,
    strictPort: true,
    // dev against a real backend: `hark serve` on its configured port
    proxy: {
      "/api": {
        target: process.env.HARK_SERVE_URL ?? "http://127.0.0.1:4136",
        changeOrigin: false,
      },
    },
    fs: {
      // fixtures dev mode imports ../fixtures/dashboard/*.jsonl directly
      allow: [".."],
    },
  },
  build: {
    outDir: "dist",
    sourcemap: false,
    target: "es2022",
  },
});
