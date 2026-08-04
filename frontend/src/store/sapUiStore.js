import { create } from 'zustand'

// Global SAP UI preference — how field columns are shown across every SAP
// screen: 'name' (technical field), 'label' (SAP label), or 'both'.
// The authoritative value is the SAP Connection config (DISPLAY_MODE); this
// store mirrors it and caches to localStorage so the choice survives reloads
// and is applied immediately everywhere.
const KEY = 'sap_display_mode'
const VALID = ['name', 'label', 'both']

const load = () => {
  try {
    const v = localStorage.getItem(KEY)
    return VALID.includes(v) ? v : 'name'
  } catch { return 'name' }
}

const useSapUiStore = create((set) => ({
  displayMode: load(),
  setDisplayMode: (m) => {
    const v = VALID.includes(m) ? m : 'name'
    try { localStorage.setItem(KEY, v) } catch { /* private mode */ }
    set({ displayMode: v })
  },
}))

export default useSapUiStore
