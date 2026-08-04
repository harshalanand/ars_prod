import React, { Component } from 'react'
import ReactDOM from 'react-dom/client'
import { BrowserRouter } from 'react-router-dom'
import { Toaster } from 'react-hot-toast'
import App from './App'
import './styles/globals.css'

/* <Toaster> sits at the app root, OUTSIDE every page-level ErrorBoundary.
 * A non-string toast payload (e.g. a FastAPI `detail` object) therefore
 * threw "Objects are not valid as a React child" at the root and unmounted
 * the ENTIRE app — the user just saw a blank white page (hit on
 * /pend-alc/adhoc-close, 2026-08-03). This boundary keeps a bad toast from
 * ever taking the app down again: the toast layer disappears, the app
 * keeps running. Callers should still pass strings — see errText(). */
class ToasterBoundary extends Component {
  constructor(props) { super(props); this.state = { failed: false } }
  static getDerivedStateFromError() { return { failed: true } }
  componentDidCatch(error, info) {
    console.error('Toaster crashed (non-string toast payload?):', error, info)
  }
  render() { return this.state.failed ? null : this.props.children }
}

ReactDOM.createRoot(document.getElementById('root')).render(
  <React.StrictMode>
    <BrowserRouter>
      <App />
      <ToasterBoundary>
        <Toaster position="top-right" toastOptions={{
          duration: 4000,
          style: { fontSize: '14px', borderRadius: '8px' },
          success: { iconTheme: { primary: '#10b981', secondary: '#fff' } },
          error: { iconTheme: { primary: '#ef4444', secondary: '#fff' } },
        }} />
      </ToasterBoundary>
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
