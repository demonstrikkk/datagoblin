import { defineConfig } from 'vite'

/**
 * Dev server + API proxy.
 *
 * Three settings that were each quietly wrong on a Windows box:
 *
 * - `host: '127.0.0.1'` — Vite defaults to binding `localhost`, which on this
 *   machine resolves to `[::1]` first, so it listened on IPv6 only and
 *   `http://127.0.0.1:5173` refused the connection while the page looked
 *   "not running". Binding IPv4 explicitly makes the printed URL and the address
 *   people actually type the same thing.
 *
 * - proxy target `127.0.0.1`, not `localhost` — the API is started with
 *   `--host 127.0.0.1` and is therefore IPv4-only; `[::1]:8000` is refused. A
 *   proxy target of `localhost` resolves IPv6-first and only worked because Node's
 *   Happy Eyeballs happened to fall back. Pinning the address the server actually
 *   listens on removes the dependency on that fallback.
 *
 * - `strictPort: true` — without it, a port clash makes Vite silently move to
 *   5174. That is how a half-dead instance came to answer with a 404 on `/` while
 *   looking alive, which cost real time to diagnose.
 */
export default defineConfig({
  esbuild: { jsx: 'automatic' },
  server: {
    host: '127.0.0.1',
    port: 5173,
    strictPort: true,
    proxy: {
      '/api': {
        target: 'http://127.0.0.1:8000',
        changeOrigin: true,
      },
    },
  },
})