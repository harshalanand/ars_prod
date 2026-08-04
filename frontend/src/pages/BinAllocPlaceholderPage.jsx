import { Construction } from 'lucide-react'

/**
 * Bin Allocation — placeholder shell.
 *
 * The Bin Allocation module assigns pre-packed DC/vendor bins to the best-fit
 * store without splitting a bin, using a greedy eligibility-threshold engine
 * (fixed or cascading) at MAJ_CAT / SIZE / MAJ_CAT+SIZE level. Specced in the
 * BRD/FSD (Bin-Allocation). The ARS-native (server-side, job-driven, audited)
 * design supersedes the client-only draft — see the feasibility note in the
 * dossier. These pages render this "under development" card until delivered.
 */
export default function BinAllocPlaceholderPage({ title = 'Bin Allocation', description }) {
  const blurb =
    description ||
    'This module is under development. The requirements are documented in the ARS Manual (Bin Allocation dossier). The working page will be delivered in a later release.'

  return (
    <div className="flex items-center justify-center" style={{ minHeight: '60vh' }}>
      <div className="card max-w-md w-full text-center px-8 py-10">
        <div className="mx-auto mb-4 flex h-14 w-14 items-center justify-center rounded-full bg-primary-50 text-primary-600">
          <Construction size={28} />
        </div>
        <h1 className="text-lg font-semibold text-gray-900">{title}</h1>
        <p className="mt-1 text-xs font-semibold uppercase tracking-wide text-primary-600">Bin Allocation</p>
        <p className="mt-3 text-sm leading-relaxed text-gray-500">{blurb}</p>
      </div>
    </div>
  )
}
