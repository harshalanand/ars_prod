import React from 'react'
import ReactDOM from 'react-dom/client'
import { BrowserRouter } from 'react-router-dom'
import { Toaster } from 'react-hot-toast'
import App from './App'
import './styles/globals.css'

ReactDOM.createRoot(document.getElementById('root')).render(
  <React.StrictMode>
    <BrowserRouter>
      <App />
      <Toaster position="top-right" toastOptions={{
        duration: 4000,
        style: { fontSize: '14px', borderRadius: '8px' },
        success: { iconTheme: { primary: '#10b981', secondary: '#fff' } },
        error: { iconTheme: { primary: '#ef4444', secondary: '#fff' } },
      }} />
    </BrowserRouter>
  </React.StrictMode>
)

// Fade out the boot preloader once React has mounted
requestAnimationFrame(() => {
  const preloader = document.getElementById('ars-preloader')
  if (!preloader) return
  preloader.classList.add('fade-out')
  setTimeout(() => preloader.remove(), 350)
})
