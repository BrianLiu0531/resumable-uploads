import react from '@vitejs/plugin-react';
import { defineConfig } from 'vite';

// The build goes into the Django project, which serves it (same origin, so tus needs no CORS)
export default defineConfig({
  plugins: [react()],
  build: { outDir: '../backend/frontend_dist', emptyOutDir: true },
  server: {
    port: 5190,
    proxy: { '/files': 'http://localhost:8000', '/api': 'http://localhost:8000' },
  },
});
