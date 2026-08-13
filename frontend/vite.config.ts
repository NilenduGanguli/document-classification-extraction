import react from '@vitejs/plugin-react';
import { defineConfig } from 'vite';

// Everything is bundled into frontend/dist. Nothing is fetched at runtime from a CDN, a font
// host or an analytics endpoint: a service whose argument is that documents do not leave must
// not ship a console that phones out. If you add a dependency, it goes in the bundle.
export default defineConfig({
  plugins: [react()],
  build: {
    // The bundle is committed, so keep the diff readable and the asset names stable-ish.
    sourcemap: false,
    chunkSizeWarningLimit: 900,
  },
  server: {
    proxy: {
      '/api': 'http://localhost:8200',
      '/readyz': 'http://localhost:8200',
      '/health': 'http://localhost:8200',
      '/metrics': 'http://localhost:8200',
    },
  },
});
