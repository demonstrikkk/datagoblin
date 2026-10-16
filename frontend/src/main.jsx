import React from 'react';
import { createRoot } from 'react-dom/client';
import App from './App.jsx';
import './index.css';

// Applied before first paint, so a saved density preference does not render the
// roomy layout for one frame on every reload.
try {
  if (window.localStorage.getItem('dg_dense') === 'true') {
    document.documentElement.setAttribute('data-density', 'compact');
  }
} catch {
  /* private mode — the default density is fine */
}

const root = createRoot(document.getElementById('root'));
root.render(
  <React.StrictMode>
    <App />
  </React.StrictMode>
);

// Fade out the pre-paint placeholder only once React has actually committed.
requestAnimationFrame(() => {
  requestAnimationFrame(() => {
    const boot = document.getElementById('boot');
    if (!boot) return;
    boot.style.opacity = '0';
    setTimeout(() => boot.remove(), 300);
  });
});
