/**
 * AlcFixtureDashboardPage — MSA-STK Allocation Engine
 * Division x Gender roll-ups with UPC-D variants, vs DEC BGT FIX and vs prior run.
 */
import { LayoutDashboard } from 'lucide-react'
import AlcFixturePlaceholder from '@/components/AlcFixturePlaceholder'

export default function AlcFixtureDashboardPage() {
  return (
    <AlcFixturePlaceholder
      icon={LayoutDashboard}
      title="ALC_Fixture — Dashboard"
      blurb="Roll-ups by Division and Gender, with All vs UPC-D-only side-by-side variants. Compares ALLO FIX against the planner-entered DEC BGT FIX and against the prior-run snapshot."
      stages={[
        { tag: '§9 row 1-3', text: 'DEC BGT FIX (manual) vs ALLO FIX vs DIFF — by Division (A/S/GM/PW/W/OC/SSNL)' },
        { tag: '§9 row 4-6', text: 'PREVIOUS run vs current ALLO FIX vs DIFF' },
        { tag: '§9 slices',  text: 'Gender cut (MENS / LADIES / KIDS / GM) and UPC-D-only filter variants' },
      ]}
    />
  )
}
