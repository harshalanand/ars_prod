/**
 * AlcFixtureReviewPage — MSA-STK Allocation Engine
 * Per Store x Floor x Major-Category drill into the final ALLO FIX, with every
 * intermediate column (FE, FK, FN, FZ, GK, GL ...) exposed for audit.
 */
import { ClipboardCheck } from 'lucide-react'
import AlcFixturePlaceholder from '@/components/AlcFixturePlaceholder'

export default function AlcFixtureReviewPage() {
  return (
    <AlcFixturePlaceholder
      icon={ClipboardCheck}
      title="ALC_Fixture — Review"
      blurb='Drill into "why did store X major-cat Y get N fixtures?" Every intermediate column persisted by the engine is queryable here: STK_FIX_RAW, REV_MAX_FIX_CAP, STK_FIX_ROUNDED, FZ anchor, Round-1/2/N ADD_FIX, and final ALLO_FIX.'
      stages={[
        { tag: '§4',     text: 'Primary output: ALLO FIX per Store x Floor x MJ (integer or banded fraction)' },
        { tag: '§10.6',  text: 'Audit chain: every intermediate column persisted for "why this fixture count?" queries' },
        { tag: '§10.5',  text: 'MBQ x ALLO_FIX = order quantity surfaced to the replenishment system' },
      ]}
    />
  )
}
