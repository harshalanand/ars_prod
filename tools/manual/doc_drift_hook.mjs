#!/usr/bin/env node
/**
 * doc_drift_hook — PostToolUse hook (Edit/Write/MultiEdit).
 * When an ARS backend source file is changed, remind Claude to update the
 * matching Training Manual dossier (the "ARS memory") and refresh the manual.
 * Silent for non-ARS edits. Never fails the tool (always exits 0).
 *
 * Wired in .claude/settings.json → hooks.PostToolUse.
 */
let raw = ''
process.stdin.setEncoding('utf8')
process.stdin.on('data', (c) => { raw += c })
process.stdin.on('end', () => {
  try {
    const evt = JSON.parse(raw || '{}')
    const fp = (evt.tool_input && (evt.tool_input.file_path || evt.tool_input.path)) || ''
    const p = String(fp).replace(/\\/g, '/').toLowerCase()
    if (!p) return done()

    // path fragment → { module, dossier, label }
    const RULES = [
      { re: /(msa_service|msa_job_service|msa_result_storage|endpoints\/msa)/, m: 'msa',     label: 'MSA Stock Calculation' },
      { re: /(grid_calculations|grid_builder|sec_cap_growth_matrix)/,          m: 'grid',    label: 'Grid Builder' },
      { re: /(merge_rules|derived_masters)/,                                    m: 'merge',   label: 'Merge Rules' },
      { re: /(rule_engine)/,                                                    m: 'listing', label: 'Allocation engine (also see rule_ars)' },
      { re: /(listing_allocator|listing_sessions|listing_job_manager|endpoints\/listing)/, m: 'listing', label: 'Listing & Allocation' },
      { re: /(hold_dashboard|parked_history)/,                                  m: 'hold',    label: 'Hold Process' },
      { re: /(pend_alc|alloc_queue|alloc_cancellation)/,                        m: 'pendalc', label: 'Pending Allocation' },
    ]
    const hit = RULES.find(r => r.re.test(p))
    if (!hit) return done()

    const msg =
      `📘 ARS memory reminder — you edited ${hit.label} source.\n` +
      `Update its dossier if the rules/formulas changed: frontend/public/docs/manual/${hit.m}.md ` +
      `(FSD + Recorded rules sections), and keep ARS_DATA_DICTIONARY in sync. ` +
      `To refresh screenshots + the column sweep, see tools/manual/REFRESH.md.`

    process.stdout.write(JSON.stringify({
      hookSpecificOutput: { hookEventName: 'PostToolUse', additionalContext: msg },
    }))
  } catch { /* never block the tool */ }
  done()
})
function done() { process.exit(0) }
