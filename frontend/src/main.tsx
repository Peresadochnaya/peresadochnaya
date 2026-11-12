import React from 'react';
import ReactDOM from 'react-dom/client';
import { App } from './App';
import './index.css'; // Подключение стилей Tailwind CSS

// Находим <div id="root"></div> в index.html и инициализируем React
ReactDOM.createRoot(document.getElementById('root') as HTMLElement).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>
);
