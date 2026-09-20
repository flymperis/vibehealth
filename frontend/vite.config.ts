import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";

const apiTarget = process.env.VITE_API_TARGET ?? "http://127.0.0.1:5001";

export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: {
    // `npm run dev` talks to a backend: set VITE_API_TARGET to use another one.
    // The Origin header is rewritten too, or the backend's CSRF check would
    // refuse every POST coming from the dev server's own address.
    proxy: { "/api": { target: apiTarget, changeOrigin: true, headers: { Origin: apiTarget } } },
  },
  build: { outDir: "dist", emptyOutDir: true },
});
