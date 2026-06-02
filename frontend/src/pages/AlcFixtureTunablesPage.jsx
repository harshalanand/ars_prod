/**
 * AlcFixtureTunablesPage — MSA-STK Allocation Engine
 * Planning-team tunables ($1003/$1004 cells in source workbook).
 */
import { Sliders } from 'lucide-react'
import AlcFixturePlaceholder from '@/components/AlcFixturePlaceholder'

export default function AlcFixtureTunablesPage() {
  return (
    <AlcFixturePlaceholder
      icon={Sliders}
      title="ALC_Fixture — Tunables"
      blurb="Manage per-run planning knobs: MSA/PO weights, shortage-cap toggle, old-alloc toggle, MSA cap factor, redistribution-round BGT multipliers, days-cover, and DEC BGT FIX manual inputs."
      stages={[
        { tag: '§3.3', text: 'Planning-team tunables (msa_weight, po_weight, shortage_cap_toggle, msa_cap_factor)' },
        { tag: '§7',   text: 'Redistribution rounds (GE/GQ BGT multipliers per round)' },
        { tag: '§9',   text: 'DEC BGT FIX manual-entry table for dashboard roll-ups' },
      ]}
    />
  )
}
