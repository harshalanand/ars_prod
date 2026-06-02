/**
 * AlcFixtureJobsPage — MSA-STK Allocation Engine
 * Run history with per-stage timing, row counts, and tunables snapshot.
 */
import { Activity } from 'lucide-react'
import AlcFixturePlaceholder from '@/components/AlcFixturePlaceholder'

export default function AlcFixtureJobsPage() {
  return (
    <AlcFixturePlaceholder
      icon={Activity}
      title="ALC_Fixture — Jobs"
      blurb="Per-run history: who ran it, when, the tunables snapshot used, per-stage timings, row counts, and links to the per-row review for that run."
      stages={[
        { tag: '§12 SLA',  text: 'Acceptance criterion: < 30 s end-to-end for ~326 stores / ~79K rows' },
        { tag: '§10.4',    text: 'Per-stage timing logged for performance regression watch' },
        { tag: '§10.6',    text: 'Tunables snapshot stored against each run (msa_wt, po_wt, GE/GQ rounds...)' },
      ]}
    />
  )
}
