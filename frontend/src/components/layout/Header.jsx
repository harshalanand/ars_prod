import { useState, useRef, useEffect } from 'react'
import { useNavigate } from 'react-router-dom'
import { LogOut, User, ChevronDown, Key, UserCog, Bell, Clock, X, Timer } from 'lucide-react'
import useAuthStore from '@/store/authStore'
import { authAPI } from '@/services/api'
import toast from 'react-hot-toast'

export default function Header() {
  const { user, logout, roles, loginTime } = useAuthStore()
  const [open, setOpen] = useState(false)
  const [showProfile, setShowProfile] = useState(false)
  const [showPassword, setShowPassword] = useState(false)
  const [showNotifications, setShowNotifications] = useState(false)
  const [currentTime, setCurrentTime] = useState(new Date())
  const [sessionDuration, setSessionDuration] = useState('')
  const [notifications, setNotifications] = useState([])
  const ref = useRef()
  const notifRef = useRef()
  const navigate = useNavigate()

  // Update time every second
  useEffect(() => {
    const timer = setInterval(() => {
      const now = new Date()
      setCurrentTime(now)
      
      // Calculate session duration
      if (loginTime) {
        const diff = now - new Date(loginTime)
        const hours = Math.floor(diff / (1000 * 60 * 60))
        const mins = Math.floor((diff % (1000 * 60 * 60)) / (1000 * 60))
        const secs = Math.floor((diff % (1000 * 60)) / 1000)
        setSessionDuration(`${hours.toString().padStart(2, '0')}:${mins.toString().padStart(2, '0')}:${secs.toString().padStart(2, '0')}`)
      }
    }, 1000)
    return () => clearInterval(timer)
  }, [loginTime])

  useEffect(() => {
    const handler = (e) => {
      if (ref.current && !ref.current.contains(e.target)) setOpen(false)
      if (notifRef.current && !notifRef.current.contains(e.target)) setShowNotifications(false)
    }
    document.addEventListener('mousedown', handler)
    return () => document.removeEventListener('mousedown', handler)
  }, [])

  const handleLogout = () => { logout(); navigate('/login') }
  const displayRole = roles[0] || 'User'

  const formatTime = (date) => {
    return date.toLocaleTimeString('en-US', { hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: true })
  }

  const formatDate = (date) => {
    return date.toLocaleDateString('en-US', { weekday: 'short', month: 'short', day: 'numeric' })
  }

  const dismissNotification = (id) => {
    setNotifications(prev => prev.filter(n => n.id !== id))
  }

  const clearAllNotifications = () => {
    setNotifications([])
  }

  const unreadCount = notifications.filter(n => !n.read).length

  return (
    <>
      <header className="h-12 bg-white border-b border-gray-200 flex items-center justify-between px-4 shrink-0">
        {/* Left side - empty or can have breadcrumbs */}
        <div className="flex-1" />

        {/* Right side: Clock, Session, Notifications, User */}
        <div className="flex items-center gap-3">
          {/* Session Duration */}
          {sessionDuration && (
            <div className="flex items-center gap-1.5 px-2 py-1 bg-green-50 rounded-lg border border-green-200">
              <Timer size={12} className="text-green-600" />
              <div className="flex flex-col leading-tight">
                <span className="text-[10px] text-green-600 font-medium">Session</span>
                <span className="text-[11px] font-semibold text-green-700">{sessionDuration}</span>
              </div>
            </div>
          )}

          {/* Clock */}
          <div className="flex items-center gap-1.5 px-2 py-1 bg-gray-50 rounded-lg">
            <Clock size={14} className="text-primary-500" />
            <div className="flex flex-col leading-tight">
              <span className="text-[11px] font-semibold text-gray-800">{formatTime(currentTime)}</span>
              <span className="text-[10px] text-gray-500">{formatDate(currentTime)}</span>
            </div>
          </div>

          {/* Notification Bell */}
          <div className="relative" ref={notifRef}>
            <button 
              onClick={() => setShowNotifications(!showNotifications)} 
              className="relative p-1.5 hover:bg-gray-100 rounded-lg transition-colors"
              title="Notifications"
            >
              <Bell size={18} className="text-gray-600" />
              {unreadCount > 0 && (
                <span className="absolute -top-0.5 -right-0.5 w-4 h-4 bg-red-500 text-white text-[10px] font-bold rounded-full flex items-center justify-center animate-pulse">
                  {unreadCount > 9 ? '9+' : unreadCount}
                </span>
              )}
            </button>
            {showNotifications && (
              <div className="absolute right-0 mt-2 w-80 bg-white rounded-lg shadow-xl border border-gray-200 z-50 animate-fade-in overflow-hidden">
                <div className="flex items-center justify-between px-4 py-3 bg-gray-50 border-b">
                  <h3 className="font-semibold text-gray-800">Notifications</h3>
                  {notifications.length > 0 && (
                    <button onClick={clearAllNotifications} className="text-xs text-primary-600 hover:underline">
                      Clear all
                    </button>
                  )}
                </div>
                <div className="max-h-80 overflow-y-auto">
                  {notifications.length === 0 ? (
                    <div className="px-4 py-8 text-center text-gray-500">
                      <Bell size={32} className="mx-auto mb-2 text-gray-300" />
                      <p className="text-sm">No notifications</p>
                    </div>
                  ) : (
                    notifications.map(notif => (
                      <div key={notif.id} className={`px-4 py-3 border-b border-gray-100 hover:bg-gray-50 ${!notif.read ? 'bg-primary-50' : ''}`}>
                        <div className="flex items-start justify-between gap-2">
                          <div className="flex-1">
                            <p className="text-sm font-medium text-gray-800">{notif.title}</p>
                            <p className="text-xs text-gray-500 mt-0.5">{notif.message}</p>
                            <p className="text-xs text-gray-400 mt-1">{notif.time}</p>
                          </div>
                          <button onClick={() => dismissNotification(notif.id)} className="p-1 hover:bg-gray-200 rounded">
                            <X size={14} className="text-gray-400" />
                          </button>
                        </div>
                      </div>
                    ))
                  )}
                </div>
              </div>
            )}
          </div>

          {/* User dropdown */}
          <div className="relative" ref={ref}>
            <button onClick={() => setOpen(!open)} className="flex items-center gap-1.5 hover:bg-gray-50 rounded-lg px-2 py-1 transition-colors">
              <div className="w-7 h-7 rounded-full bg-primary-100 flex items-center justify-center">
                <User size={14} className="text-primary-600" />
              </div>
              <div className="text-left hidden sm:block">
                <div className="text-[11px] font-medium text-gray-900">{user?.full_name || user?.username || 'User'}</div>
                <div className="text-[10px] text-gray-500">{displayRole}</div>
              </div>
              <ChevronDown size={12} className="text-gray-400" />
            </button>
            {open && (
              <div className="absolute right-0 mt-2 w-52 bg-white rounded-lg shadow-lg border border-gray-200 py-1 z-50 animate-fade-in">
                <div className="px-3 py-1.5 border-b border-gray-100">
                  <div className="text-[11px] font-medium">{user?.username}</div>
                  <div className="text-[10px] text-gray-500">{user?.email || user?.mobile_no}</div>
                </div>
                <button onClick={() => { setOpen(false); setShowProfile(true) }} className="w-full flex items-center gap-2 px-3 py-1.5 text-[11px] text-gray-700 hover:bg-gray-50">
                  <UserCog size={13} /> Edit Profile
                </button>
                <button onClick={() => { setOpen(false); setShowPassword(true) }} className="w-full flex items-center gap-2 px-3 py-1.5 text-[11px] text-gray-700 hover:bg-gray-50">
                  <Key size={13} /> Change Password
                </button>
                <div className="border-t border-gray-100 mt-1 pt-1">
                  <button onClick={handleLogout} className="w-full flex items-center gap-2 px-3 py-1.5 text-[11px] text-red-600 hover:bg-red-50">
                    <LogOut size={13} /> Sign Out
                  </button>
                </div>
              </div>
            )}
          </div>
        </div>
      </header>

      {/* Profile Modal */}
      {showProfile && <ProfileModal user={user} onClose={() => setShowProfile(false)} />}
      
      {/* Change Password Modal */}
      {showPassword && <ChangePasswordModal onClose={() => setShowPassword(false)} />}
    </>
  )
}

function ProfileModal({ user, onClose }) {
  const [form, setForm] = useState({
    full_name: user?.full_name || '',
    email: user?.email || '',
    mobile_no: user?.mobile_no || '',
  })
  const [saving, setSaving] = useState(false)
  const { fetchUser } = useAuthStore()

  const handleSubmit = async (e) => {
    e.preventDefault()
    setSaving(true)
    try {
      await authAPI.updateProfile(form)
      toast.success('Profile updated')
      fetchUser()
      onClose()
    } catch (err) {
      toast.error(err.response?.data?.detail || 'Failed to update profile')
    } finally {
      setSaving(false)
    }
  }

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40">
      <div className="bg-white rounded-xl shadow-xl w-full max-w-md m-4">
        <div className="flex items-center justify-between px-6 py-4 border-b">
          <h2 className="text-lg font-semibold">Edit Profile</h2>
          <button onClick={onClose} className="p-1 hover:bg-gray-100 rounded-lg">×</button>
        </div>
        <form onSubmit={handleSubmit} className="p-6 space-y-4">
          <div>
            <label className="label">Full Name</label>
            <input value={form.full_name} onChange={e => setForm({ ...form, full_name: e.target.value })} className="input" />
          </div>
          <div>
            <label className="label">Email</label>
            <input type="email" value={form.email} onChange={e => setForm({ ...form, email: e.target.value })} className="input" />
          </div>
          <div>
            <label className="label">Mobile No</label>
            <input value={form.mobile_no} onChange={e => setForm({ ...form, mobile_no: e.target.value })} className="input" />
          </div>
          <div className="flex justify-end gap-3 pt-2">
            <button type="button" onClick={onClose} className="btn-secondary">Cancel</button>
            <button type="submit" disabled={saving} className="btn-primary">{saving ? 'Saving...' : 'Save'}</button>
          </div>
        </form>
      </div>
    </div>
  )
}

function ChangePasswordModal({ onClose }) {
  const [form, setForm] = useState({ current_password: '', new_password: '', confirm_password: '' })
  const [saving, setSaving] = useState(false)

  const handleSubmit = async (e) => {
    e.preventDefault()
    if (form.new_password !== form.confirm_password) {
      return toast.error('Passwords do not match')
    }
    if (form.new_password.length < 8) {
      return toast.error('Password must be at least 8 characters')
    }
    setSaving(true)
    try {
      await authAPI.changePassword({
        current_password: form.current_password,
        new_password: form.new_password,
      })
      toast.success('Password changed successfully')
      onClose()
    } catch (err) {
      toast.error(err.response?.data?.detail || 'Failed to change password')
    } finally {
      setSaving(false)
    }
  }

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40">
      <div className="bg-white rounded-xl shadow-xl w-full max-w-md m-4">
        <div className="flex items-center justify-between px-6 py-4 border-b">
          <h2 className="text-lg font-semibold">Change Password</h2>
          <button onClick={onClose} className="p-1 hover:bg-gray-100 rounded-lg">×</button>
        </div>
        <form onSubmit={handleSubmit} className="p-6 space-y-4">
          <div>
            <label className="label">Current Password</label>
            <input type="password" value={form.current_password} onChange={e => setForm({ ...form, current_password: e.target.value })} className="input" required />
          </div>
          <div>
            <label className="label">New Password</label>
            <input type="password" value={form.new_password} onChange={e => setForm({ ...form, new_password: e.target.value })} className="input" required />
          </div>
          <div>
            <label className="label">Confirm New Password</label>
            <input type="password" value={form.confirm_password} onChange={e => setForm({ ...form, confirm_password: e.target.value })} className="input" required />
          </div>
          <div className="flex justify-end gap-3 pt-2">
            <button type="button" onClick={onClose} className="btn-secondary">Cancel</button>
            <button type="submit" disabled={saving} className="btn-primary">{saving ? 'Changing...' : 'Change Password'}</button>
          </div>
        </form>
      </div>
    </div>
  )
}
