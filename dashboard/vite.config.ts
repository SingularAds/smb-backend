import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Dev proxy: the dashboard calls /api/* relative to its own origin; in dev we
// forward that to the FastAPI backend. Override the target with
// VITE_PROXY_TARGET when the backend runs elsewhere.
//
// base: '/dashboard/' ensures production asset paths are prefixed correctly
// when FastAPI serves the built app from app.mount("/dashboard", ...).
// In dev (npm run dev) the base is '/' so localhost:5173 still works normally.
export default defineConfig(({ command }) => {
  const target = process.env.VITE_PROXY_TARGET || "http://127.0.0.1:8000";
  const base = command === "build" ? "/dashboard/" : "/";
  return {
    base,
    plugins: [react()],
    server: {
      port: 5173,
      proxy: {
        "/api": { target, changeOrigin: true },
      },
    },
  };
});
