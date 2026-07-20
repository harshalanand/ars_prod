import { Outlet } from 'react-router-dom'
import Sidebar from './Sidebar'
import Header from './Header'
import Footer from './Footer'
import { useState } from 'react'

export default function Layout() {
  // Rail collapse persists across reloads, like the sidebar's open section
  const [collapsed, setCollapsed] = useState(() => {
    try { return localStorage.getItem('ars_sidebar_collapsed') === '1' } catch { return false }
  })
  const toggleCollapsed = () => setCollapsed(c => {
    const next = !c
    try { localStorage.setItem('ars_sidebar_collapsed', next ? '1' : '0') } catch { /* private mode */ }
    return next
  })

  return (
    <div className="flex h-screen overflow-hidden bg-gray-50">
      <Sidebar collapsed={collapsed} onToggle={toggleCollapsed} />
      <div className="flex-1 flex flex-col overflow-hidden">
        <Header />
        <main className="flex-1 overflow-y-auto p-4">
          <div className="animate-fade-in">
            <Outlet />
          </div>
        </main>
        <Footer />
      </div>
    </div>
  )
}
