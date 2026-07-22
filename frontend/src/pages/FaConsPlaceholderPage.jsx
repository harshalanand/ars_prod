import { Construction } from 'lucide-react'

/**
 * FA & CONS — placeholder shell.
 *
 * The FA & CONS module (Project-Store & Consumables allocation) is specced in
 * the BRD/FSD dossier at public/docs/manual/fa_cons.md but not yet built. Each
 * sidebar link under "FA & CONS" renders this "under development" card so the
 * menu is navigable while the pages are developed. Replace per-route with the
 * real page as each is implemented.
 */
export default function FaConsPlaceholderPage({ title = 'FA & CONS', description }) {
  const blurb =
    description ||
    'This module is under development. The requirements are documented in the ARS Manual (FA & CONS dossier). The working page will be delivered in a later release.'

  return (
    <div className="flex items-center justify-center" style={{ minHeight: '60vh' }}>
      <div className="max-w-md w-full text-center rounded-xl border border-gray-200 dark:border-gray-700 bg-white dark:bg-gray-800 shadow-sm px-8 py-10">
        <div className="mx-auto mb-4 flex h-14 w-14 items-center justify-center rounded-full bg-primary-50 dark:bg-primary-900/30 text-primary-600 dark:text-primary-400">
          <Construction size={28} />
        </div>
        <h1 className="text-lg font-semibold text-gray-900 dark:text-white">{title}</h1>
        <p className="mt-1 text-xs font-medium uppercase tracking-wide text-primary-600 dark:text-primary-400">
          FA &amp; CONS
        </p>
        <p className="mt-3 text-sm leading-relaxed text-gray-500 dark:text-gray-400">{blurb}</p>
      </div>
    </div>
  )
}
