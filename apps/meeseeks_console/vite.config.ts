import { defineConfig, loadEnv } from 'vite'
import react from '@vitejs/plugin-react'

// https://vitejs.dev/config/
export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), "");
  const apiTarget =
    env.VITE_API_BASE_URL || env.VITE_API_BASE || "http://127.0.0.1:5124";
  const allowedHostsEnv = env.VITE_ALLOWED_HOSTS || "";
  const allowedHosts = Array.from(
    new Set(
      ["meeseeks.hurricane.home"]
        .concat(
          allowedHostsEnv
            .split(",")
            .map((host) => host.trim())
            .filter(Boolean)
        )
    )
  );

  return {
    plugins: [react()],
    server: {
      allowedHosts,
      proxy: {
        "/api": {
          target: apiTarget,
          changeOrigin: true,
          secure: false
        }
      }
    },
    test: {
      environment: "jsdom",
      setupFiles: "./src/setupTests.ts",
      exclude: ["tests/**", "node_modules/**"]
    }
  };
});
