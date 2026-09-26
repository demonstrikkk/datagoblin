import { defineConfig } from 'vite'
export default defineConfig({ esbuild: { jsx: 'automatic' }, server: { port: 5173, proxy: { '/api': 'http://localhost:8000' } } })
