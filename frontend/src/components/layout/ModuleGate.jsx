import { Link, Navigate, useLocation } from 'react-router-dom'
import { Lock } from 'lucide-react'
import useAuthStore from '@/store/authStore'
import { moduleForPath, firstAllowedPage } from './navRegistry'

const Waiting = () => <div className="p-10 text-center text-gray-400 text-sm">Loading…</div>

/**
 * Blocks direct-URL access to a page whose sidebar module (MOD_*) or own page
 * permission is off for the user's role — the same rule that hides the link
 * in the sidebar. Page-level ProtectedRoute checks still apply on top of this.
 */
export default function ModuleGate({ children }) {
  const { pathname } = useLocation()
  const { user, hasPermission, isSuperAdmin } = useAuthStore()

  const mod = moduleForPath(pathname)
  if (!mod) return children
  // Permissions arrive with /auth/me; deciding before then would flash "no access"
  if (!user) return <Waiting />
  if (isSuperAdmin()) return children
  const moduleOk = hasPermission(mod.code)
  if (moduleOk && (!mod.page || hasPermission(mod.page))) return children

  const home = firstAllowedPage(hasPermission, false)
  const what = moduleOk ? `${mod.title} › ${mod.pageLabel}` : mod.title
  return (
    <div className="max-w-md mx-auto mt-16 text-center bg-white border border-gray-200 rounded-xl p-8 shadow-sm">
      <div className="w-12 h-12 rounded-full bg-amber-50 flex items-center justify-center mx-auto mb-3">
        <Lock size={22} className="text-amber-600" />
      </div>
      <h2 className="text-lg font-semibold text-gray-900">No access to {what}</h2>
      <p className="text-sm text-gray-500 mt-1.5">
        {moduleOk ? (
          <>Your role doesn't include the <b>{mod.pageLabel}</b> page. Ask an administrator to tick it
            under <b>{mod.title}</b> in Settings → Roles.</>
        ) : (
          <>Your role doesn't include the <b>{mod.title}</b> module. Ask an administrator to turn on
            its <b>Module access</b> in Settings → Roles.</>
        )}
      </p>
      {home && (
        <Link to={home.path} replace className="btn-primary inline-flex mt-5">Go to {home.label}</Link>
      )}
    </div>
  )
}

/** Index route: send the user to the first page their role can open. */
export function HomeRedirect() {
  const { user, hasPermission, isSuperAdmin } = useAuthStore()
  if (!user) return <Waiting />
  const home = firstAllowedPage(hasPermission, isSuperAdmin())
  if (home) return <Navigate to={home.path} replace />
  return (
    <div className="max-w-md mx-auto mt-16 text-center text-sm text-gray-500">
      Your role doesn't have access to any module yet. Ask an administrator to grant one in Settings → Roles.
    </div>
  )
}
