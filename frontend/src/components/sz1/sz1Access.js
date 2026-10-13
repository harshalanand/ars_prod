import useAuthStore from '@/store/authStore'

// Which 1SZ ALC pages this user may open, and the links that reach them.
// No new permission codes: Prepare's route (/sz1-alloc/prepare) opens for any
// of its three tab codes (navRegistry `perms`), and each tab keeps its own code;
// Settings stays PAGE_1SZ_BASE. The old URLs (/base, /req, /gap) still work.
export const TAB_PERM = { supply: 'PAGE_1SZ_BASE', demand: 'PAGE_1SZ_REQ', gap: 'PAGE_1SZ_GAP' }
export const LEGACY_PATH = { supply: '/sz1-alloc/base', demand: '/sz1-alloc/req', gap: '/sz1-alloc/gap' }
const ORDER = ['supply', 'demand', 'gap']

export function useSz1Access() {
  const { hasPermission } = useAuthStore()
  const tabs = Object.fromEntries(ORDER.map(t => [t, !!hasPermission(TAB_PERM[t])]))
  const page = ORDER.some(t => tabs[t])                 // /sz1-alloc/prepare: any of the three
  const hrefFor = tab => (page ? `/sz1-alloc/prepare?tab=${tab}` : LEGACY_PATH[tab])
  const first = ORDER.find(t => tabs[t])
  return {
    tabs, page,
    settings: tabs.supply,                              // /sz1-alloc/settings — PAGE_1SZ_BASE
    run: !!hasPermission('PAGE_1SZ_RUN'),
    sessions: !!hasPermission('PAGE_1SZ_SESSIONS'),
    hrefFor,
    prepareHref: first ? hrefFor(first) : null,
  }
}
