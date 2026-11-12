import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

export default defineConfig({
  plugins: [react()],
  server: {
    proxy: {
      '/map-api.js': {
        target: 'https://peresadochnaya.rjnk.cc',
        changeOrigin: true
      },
      '/api': {
	target: 'https://peresadochnaya.rjnk.cc', // для локальных тестров 
        // target: 'http://localhost:8000', для прода
        changeOrigin: true
      }
    }
  }
});
