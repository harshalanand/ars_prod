import { create } from 'zustand'
import { authAPI } from '@/services/api'
import toast from 'react-hot-toast'

const useAuthStore = create((set, get) => ({
  user: null,
  isAuthenticated: !!localStorage.getItem('access_token'),
  loading: false,
  permissions: [],
  roles: [],
  loginTime: localStorage.getItem('login_time') ? new Date(localStorage.getItem('login_time')) : null,

  login: async (username, password) => {
    set({ loading: true })
    try {
      const { data } = await authAPI.login(username, password)
      const loginTime = new Date()
      localStorage.setItem('access_token', data.access_token)
      localStorage.setItem('refresh_token', data.refresh_token)
      localStorage.setItem('login_time', loginTime.toISOString())
      set({ isAuthenticated: true, loading: false, loginTime })
      await get().fetchUser()
      toast.success('Login successful')
      return true
    } catch (e) {
      set({ loading: false })
      toast.error(e.response?.data?.detail || 'Login failed')
      return false
    }
  },

  fetchUser: async () => {
    try {
      const { data } = await authAPI.me()
      const u = data.data
      const storedLoginTime = localStorage.getItem('login_time')
      set({
        user: u,
        permissions: u.permissions || [],
        roles: (u.roles || []).map(r => r.role_name || r),
        isAuthenticated: true,
        loginTime: storedLoginTime ? new Date(storedLoginTime) : null,
      })
    } catch {
      set({ user: null, isAuthenticated: false, loginTime: null })
    }
  },

  logout: () => {
    localStorage.clear()
    set({ user: null, isAuthenticated: false, permissions: [], roles: [], loginTime: null })
    toast.success('Logged out')
  },

  hasPermission: (perm) => {
    const { roles, permissions } = get()
    if (roles.includes('SUPER_ADMIN')) return true
    return permissions.includes(perm)
  },

  hasRole: (role) => {
    return get().roles.includes(role)
  },

  isSuperAdmin: () => get().roles.includes('SUPER_ADMIN'),
}))

export default useAuthStore
