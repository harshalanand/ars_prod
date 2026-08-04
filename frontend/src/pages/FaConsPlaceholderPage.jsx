import { Construction } from 'lucide-react'

/**
 * FA & CONS — placeholder shell.
 *
 * The FA & CONS module (Project-Store & Consumables allocation) is specced in
 * the BRD/FSD dossier at public/docs/manual/fa_cons.md. The data foundation
 * (MBQ Master, SLOC Settings, Stock & MSA) is built; the allocation pages below
 * render this "under development" card until each is delivered.
 */
export default function FaConsPlaceholderPage({ title = 'FA & CONS', description }) {
  const blurb =
    description ||
    'This module is under development. The requirements are documented in the ARS Manual (FA & CONS dossier). The working page will be delivered in a later release.'

  return (
    <div className="flex items-center justify-center" style={{ minHeight: '60vh' }}>
      <div className="card max-w-md w-full text-center px-8 py-10">
        <div className="mx-auto mb-4 flex h-14 w-14 items-center justify-center rounded-full bg-primary-50 text-primary-600">
          <Construction size={28} />
        </div>
        <h1 className="text-lg font-semibold text-gray-900">{title}</h1>
        <p className="mt-1 text-xs font-semibold uppercase tracking-wide text-primary-600">FA &amp; CONS</p>
        <p className="mt-3 text-sm leading-relaxed text-gray-500">{blurb}</p>
      </div>
    </div>
  )
}
