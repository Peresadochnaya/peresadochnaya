import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

export default defineConfig({
  plugins: [react()],
  server: {
    proxy: {
      '/api': {
	target: 'https://peresadochnaya.lol/', // для локальных тестров 
        // target: 'http://localhost:8000', для прода
        changeOrigin: true
      }
    }
  }
});
