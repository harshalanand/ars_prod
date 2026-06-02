/**
 * AlcFixtureExecutePage — MSA-STK Allocation Engine
 * Trigger the end-to-end Stage A-H pipeline for the next allocation cycle.
 */
import { Cpu } from 'lucide-react'
import AlcFixturePlaceholder from '@/components/AlcFixturePlaceholder'

export default function AlcFixtureExecutePage() {
  return (
    <AlcFixturePlaceholder
      icon={Cpu}
      title="ALC_Fixture — Execute"
      blurb="Run the engine end-to-end per Store x Floor x Major-Category (~79K rows / ~326 stores). Loads inputs, applies the 8 stages, and persists every intermediate column for audit."
      stages={[
        { tag: 'Stage A', text: 'Lookups & derivations (ACC_DENSITY, DISP_Q/FIX, MIN/MAX FIX, REV_MAX_FIX, HH/VV bucket)' },
        { tag: 'Stage B', text: 'BGT & AUTO references (FINAL_BGT_FIX, FINAL_AUTO_FIX with UPC asymmetry)' },
        { tag: 'Stage C', text: 'Stock & sales aggregation, MBQ, MSA/PO pipeline math' },
        { tag: 'Stage D', text: 'Campaign-Article (C-ART) carve-out (ALGO-1 + ALGO-2)' },
        { tag: 'Stage E', text: 'Stock-based fix with banded growth cap + banded rounding' },
        { tag: 'Stage F', text: 'BGT reconciliation (FZ anchor)' },
        { tag: 'Stage G', text: 'MSA-backed redistribution (configurable rounds, default 3)' },
        { tag: 'Stage H', text: 'Final rounding + floor balance' },
      ]}
    />
  )
}
