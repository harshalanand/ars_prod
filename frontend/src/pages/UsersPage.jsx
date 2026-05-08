import { useEffect, useState } from 'react'
import { Plus, Search, Unlock, Edit2, X, Trash2 } from 'lucide-react'
import { usersAPI, rolesAPI } from '@/services/api'
import toast from 'react-hot-toast'

export default function UsersPage() {
  const [users, setUsers] = useState([])
  const [roles, setRoles] = useState([])
  const [loading, setLoading] = useState(true)
  const [search, setSearch] = useState('')
  const [modal, setModal] = useState(null) // null | 'create' | {editing user}

  const load = async () => {
    setLoading(true)
    try {
      const [u, r] = await Promise.allSettled([usersAPI.list(), rolesAPI.list()])
      if (u.status === 'fulfilled') setUsers(u.value.data.data?.users || [])
      if (r.status === 'fulfilled') setRoles(r.value.data.data || [])
    } finally { setLoading(false) }
  }

  useEffect(() => { load() }, [])

  const filtered = users.filter(u =>
    (u.username || '').toLowerCase().includes(search.toLowerCase()) ||
    (u.full_name || '').toLowerCase().includes(search.toLowerCase()) ||
    (u.mobile_no || '').toLowerCase().includes(search.toLowerCase()) ||
    (u.email || '').toLowerCase().includes(search.toLowerCase())
  )

  const handleUnlock = async (id) => {
    try { await usersAPI.unlock(id); toast.success('User unlocked'); load() } catch {}
  }

  const handleDelete = async (id, username) => {
    if (!confirm(`Delete user "${username}"? This cannot be undone.`)) return
    try { await usersAPI.delete(id); toast.success('User deleted'); load() }
    catch (e) { toast.error(e.response?.data?.detail || 'Delete failed') }
  }

  return (
    <div className="space-y-5">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-bold text-gray-900">Users</h1>
          <p className="text-gray-500 text-sm mt-0.5">Manage system users and access</p>
        </div>
        <button onClick={() => setModal('create')} className="btn-primary"><Plus size={16} /> Add User</button>
      </div>

      <div className="relative max-w-md">
        <Search size={16} className="absolute left-3 top-1/2 -translate-y-1/2 text-gray-400" />
        <input value={search} onChange={e => setSearch(e.target.value)} className="input pl-9" placeholder="Search users..." />
      </div>

      <div className="card overflow-hidden">
        <table className="w-full text-sm">
          <thead>
            <tr className="bg-gray-50 border-b text-left">
              <th className="px-4 py-3 font-semibold text-gray-600">Username</th>
              <th className="px-4 py-3 font-semibold text-gray-600">Full Name</th>
              <th className="px-4 py-3 font-semibold text-gray-600">Mobile No</th>
              <th className="px-4 py-3 font-semibold text-gray-600">Email</th>
              <th className="px-4 py-3 font-semibold text-gray-600">Roles</th>
              <th className="px-4 py-3 font-semibold text-gray-600">Status</th>
              <th className="px-4 py-3 font-semibold text-gray-600">Actions</th>
            </tr>
          </thead>
          <tbody>
            {loading ? (
              <tr><td colSpan="7" className="px-4 py-10 text-center text-gray-400">Loading...</td></tr>
            ) : filtered.length === 0 ? (
              <tr><td colSpan="7" className="px-4 py-10 text-center text-gray-400">No users found</td></tr>
            ) : (
              filtered.map(u => (
                <tr key={u.id} className="border-b hover:bg-gray-50">
                  <td className="px-4 py-3 font-medium text-gray-900">{u.username}</td>
                  <td className="px-4 py-3">{u.full_name}</td>
                  <td className="px-4 py-3">{u.mobile_no}</td>
                  <td className="px-4 py-3 text-gray-500">{u.email}</td>
                  <td className="px-4 py-3">
                    <div className="flex flex-wrap gap-1">
                      {(u.roles || []).map(r => (
                        <span key={r.role_name || r} className="badge-primary">{r.role_name || r}</span>
                      ))}
                    </div>
                  </td>
                  <td className="px-4 py-3">
                    {u.is_active ? (
                      u.is_locked ? <span className="badge-danger">Locked</span> : <span className="badge-success">Active</span>
                    ) : <span className="badge-gray">Inactive</span>}
                  </td>
                  <td className="px-4 py-3">
                    <div className="flex gap-1">
                      <button onClick={() => setModal(u)} className="btn-ghost btn-sm p-1"><Edit2 size={14} /></button>
                      {u.is_locked && (
                        <button onClick={() => handleUnlock(u.id)} className="btn-ghost btn-sm p-1 text-amber-600"><Unlock size={14} /></button>
                      )}
                      {u.username !== 'superadmin' && (
                        <button onClick={() => handleDelete(u.id, u.username)} className="btn-ghost btn-sm p-1 text-red-500"><Trash2 size={14} /></button>
                      )}
                    </div>
                  </td>
                </tr>
              ))
            )}
          </tbody>
        </table>
      </div>

      {/* Create / Edit Modal */}
      {modal && <UserModal user={modal === 'create' ? null : modal} roles={roles} onClose={() => setModal(null)} onSaved={() => { setModal(null); load() }} />}
    </div>
  )
}

function UserModal({ user, roles, onClose, onSaved }) {
  const isEdit = !!user?.id
  const [form, setForm] = useState({
    username: user?.username || '',
    full_name: user?.full_name || '',
    email: user?.email || '',
    mobile_no: user?.mobile_no || '',
    password: '',
    role_ids: roles.filter(r => (user?.roles || []).includes(r.role_code)).map(r => r.id),
    is_active: user?.is_active ?? true,
  })
  const [saving, setSaving] = useState(false)

  const update = (k, v) => setForm(f => ({ ...f, [k]: v }))
  const toggleRole = (rid) => {
    setForm(f => ({
      ...f,
      role_ids: f.role_ids.includes(rid) ? f.role_ids.filter(x => x !== rid) : [...f.role_ids, rid]
    }))
  }

  const handleSubmit = async (e) => {
    e.preventDefault()
    setSaving(true)
    try {
      if (isEdit) {
        const payload = { ...form }
        if (!payload.password) delete payload.password
        await usersAPI.update(user.id, payload)
        toast.success('User updated')
      } else {
        if (!form.password) return toast.error('Password required for new user')
        await usersAPI.create(form)
        toast.success('User created')
      }
      onSaved()
    } catch (e) { toast.error(e.response?.data?.detail || 'Save failed') } finally { setSaving(false) }
  }

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40">
      <div className="bg-white rounded-xl shadow-xl w-full max-w-lg m-4">
        <div className="flex items-center justify-between px-6 py-4 border-b">
          <h2 className="text-lg font-semibold">{isEdit ? 'Edit User' : 'Create User'}</h2>
          <button onClick={onClose} className="p-1 hover:bg-gray-100 rounded-lg"><X size={18} /></button>
        </div>
        <form onSubmit={handleSubmit} className="p-6 space-y-4">
          <div className="grid grid-cols-2 gap-4">
            <div>
              <label className="label">Username*</label>
              <input value={form.username} onChange={e => update('username', e.target.value)} className="input" required disabled={isEdit} />
            </div>
            <div>
              <label className="label">Full Name</label>
              <input value={form.full_name} onChange={e => update('full_name', e.target.value)} className="input" />
            </div>
          </div>
          <div className="grid grid-cols-2 gap-4">
            <div>
              <label className="label">Email</label>
              <input type="email" value={form.email} onChange={e => update('email', e.target.value)} className="input" />
            </div>
            <div>
              <label className="label">Mobile No*</label>
              <input value={form.mobile_no} onChange={e => update('mobile_no', e.target.value)} className="input" required placeholder="10 digit mobile number" />
            </div>
          </div>
          <div>
            <label className="label">{isEdit ? 'New Password (leave blank to keep)' : 'Password*'}</label>
            <input type="password" value={form.password} onChange={e => update('password', e.target.value)} className="input" />
          </div>
          <div>
            <label className="label">Roles</label>
            <div className="flex flex-wrap gap-2 mt-1">
              {roles.map(r => (
                <button key={r.id} type="button" onClick={() => toggleRole(r.id || r)}
                  className={`px-3 py-1.5 text-xs rounded-lg border transition-colors ${form.role_ids.includes(r.id || r) ? 'bg-primary-600 text-white border-primary-600' : 'bg-white text-gray-600 border-gray-300 hover:border-primary-400'}`}>
                  {r.role_name}
                </button>
              ))}
            </div>
          </div>
          <label className="flex items-center gap-2 text-sm">
            <input type="checkbox" checked={form.is_active} onChange={e => update('is_active', e.target.checked)} className="rounded" /> Active
          </label>
          <div className="flex justify-end gap-3 pt-2">
            <button type="button" onClick={onClose} className="btn-secondary">Cancel</button>
            <button type="submit" disabled={saving} className="btn-primary">{saving ? 'Saving...' : isEdit ? 'Update User' : 'Create User'}</button>
          </div>
        </form>
      </div>
    </div>
  )
}
