import React from 'react';
import ReactDOM from 'react-dom/client';
import App from './App.jsx';
import './styles/global.css';
// Cesium widget CSS (must be imported once, before the first Viewer is created)
import 'cesium/Build/Cesium/Widgets/widgets.css';

ReactDOM.createRoot(document.getElementById('root')).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>,
);
