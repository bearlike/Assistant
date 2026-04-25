#!/bin/sh
# Generates /runtime-config.js from environment variables.
# Dropped into /docker-entrypoint.d/ — nginx:alpine runs it before starting.
cat > /usr/share/nginx/html/runtime-config.js <<EOF
window.__MEWBO_CONFIG__ = {
  VITE_API_BASE_URL: "${VITE_API_BASE_URL:-}",
  VITE_API_KEY: "${VITE_API_KEY:-}",
  VITE_API_MODE: "${VITE_API_MODE:-live}",
  VITE_API_USE_PROXY: "${VITE_API_USE_PROXY:-}"
};
EOF
