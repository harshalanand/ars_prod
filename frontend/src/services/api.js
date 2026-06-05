import axios from 'axios'
import toast from 'react-hot-toast'

const API_BASE = import.meta.env.VITE_API_URL || '/api/v1'

// Log actual API configuration
console.log(`🔌 API Configuration:
  Base URL: ${API_BASE}
  Environment: ${import.meta.env.MODE}
  Dev: ${import.meta.env.DEV}
  Prod: ${import.meta.env.PROD}
`);

const api = axios.create({ baseURL: API_BASE, timeout: 300000 }) // 5 minute timeout for complex calculations

// Request interceptor: attach JWT
api.interceptors.request.use((config) => {
  const token = localStorage.getItem('access_token')
  if (token) config.headers.Authorization = `Bearer ${token}`
  return config
})

// Response interceptor: handle 401, errors
api.interceptors.response.use(
  (res) => res,
  async (error) => {
    const status = error.response?.status
    if (status === 401) {
      // Try refresh
      const refresh = localStorage.getItem('refresh_token')
      if (refresh && !error.config._retry) {
        error.config._retry = true
        try {
          const { data } = await axios.post(`${API_BASE}/auth/refresh`, { refresh_token: refresh })
          localStorage.setItem('access_token', data.access_token)
          localStorage.setItem('refresh_token', data.refresh_token)
          error.config.headers.Authorization = `Bearer ${data.access_token}`
          return api(error.config)
        } catch {
          localStorage.clear()
          window.location.href = '/login'
        }
      } else {
        localStorage.clear()
        window.location.href = '/login'
      }
    }
    // Per-request opt-out: callers that poll in the background (active-job,
    // summary, config) pass { quiet: true } so transient timeouts/network
    // blips don't spam toasts. They retry on their own cadence.
    const quiet = error.config?.quiet === true
    const msg = error.response?.data?.detail || error.message
    if (status !== 401 && !quiet) toast.error(msg)
    return Promise.reject(error)
  }
)

// ============== Auth ==============
export const authAPI = {
  login: (username, password) => api.post('/auth/login', { username, password }),
  me: () => api.get('/auth/me'),
  updateProfile: (data) => api.put('/auth/profile', data),
  changePassword: (data) => api.post('/auth/change-password', data),
}

// ============== Users ==============
export const usersAPI = {
  list: (params) => api.get('/users', { params }),
  get: (id) => api.get(`/users/${id}`),
  create: (data) => api.post('/users', data),
  update: (id, data) => api.put(`/users/${id}`, data),
  unlock: (id) => api.post(`/users/${id}/unlock`),
  delete: (id) => api.delete(`/users/${id}`),
}

// ============== Roles ==============
export const rolesAPI = {
  list: () => api.get('/roles'),
  create: (data) => api.post('/roles', data),
  update: (id, data) => api.put(`/roles/${id}`, data),
  permissions: () => api.get('/roles/permissions'),
  assignPermissions: (id, data) => api.post(`/roles/${id}/permissions`, data),
}

// ============== RLS ==============
export const rlsAPI = {
  stores: () => api.get('/rls/stores'),
  storeAccess: (uid) => api.get(`/rls/store-access/${uid}`),
  addStoreAccess: (data) => api.post('/rls/store-access', data),
  deleteStoreAccess: (uid, code) => api.delete(`/rls/store-access/${uid}/${code}`),
  regionAccess: (uid) => api.get(`/rls/region-access/${uid}`),
  addRegionAccess: (data) => api.post('/rls/region-access', data),
  columnRestrictions: (table) => api.get(`/rls/column-restrictions/${table}`),
  myColumnRestrictions: (table) => api.get(`/rls/my-column-restrictions/${table}`),
  addColumnRestrictions: (data) => api.post('/rls/column-restrictions', data),
  bulkColumnRestrictions: (data) => api.post('/rls/column-restrictions/bulk', data),
  deleteColumnRestriction: (id) => api.delete(`/rls/column-restrictions/${id}`),
  tableAccess:          (table) => api.get(`/rls/table-access/${table}`),
  bulkTableAccess:      (data) => api.post('/rls/table-access/bulk', data),
  tableAccessByRole:    (roleId) => api.get(`/rls/table-access-by-role/${roleId}`),
}

// ============== Tables ==============
export const tablesAPI = {
  list: (params) => api.get('/tables', { params }),
  listAll: (params) => api.get('/tables/database/all', { params }),
  listAllVisible: () => api.get('/tables/database/all', { params: { visible_only: true } }),
  schema: (name) => api.get(`/tables/${name}/schema`),
  create: (data) => api.post('/tables', data),
  alter: (name, data) => api.put(`/tables/${name}/alter`, data),
  reorderColumns: (name, columns) => api.put(`/tables/${name}/reorder-columns`, { columns }),
  delete: (name) => api.delete(`/tables/${name}`),
  data: (name, params) => api.get(`/tables/${name}/data`, { params }),
  // Truncate now runs as a background job — returns { job_id } immediately.
  // Use truncateProgress(jobId) to poll a progress bar (TRUNCATE TABLE is
  // milliseconds; batched DELETE fallback reports per-batch progress).
  truncate: (name) => api.delete(`/tables/${name}/data`),
  truncateProgress: (jobId) => api.get(`/tables/truncate/progress/${jobId}`),
  rowCount: (name) => api.get(`/tables/${name}/row-count`),
  settings: (name) => api.get(`/tables/${name}/settings`),
  updateSettings: (name, params) => api.put(`/tables/settings/${name}`, null, { params }),
  allSettings: () => api.get('/tables/settings/all'),
  distinct: (name, column, params) => api.get(`/tables/${name}/distinct/${column}`, { params }),
  exportSettings: () => api.get('/tables/export/settings'),
  updateExportSettings: (settings) => api.post('/tables/export/settings', settings),
  // Table permissions
  tablePermissions: () => api.get('/tables/permissions'),
  allowedTables: (action) => api.get('/tables/permissions/allowed', { params: { action } }),
  saveTablePermissions: (permissions) => api.post('/tables/permissions', permissions),
  // Export jobs
  startExportJob: (data) => api.post('/tables/export/jobs/start', data),
  listExportJobs: (limit = 20) => api.get('/tables/export/jobs', { params: { limit } }),
  getExportJobStatus: (jobId) => api.get(`/tables/export/jobs/${jobId}`),
  deleteExportJob: (jobId) => api.delete(`/tables/export/jobs/${jobId}`),
  downloadExportJob: (jobId) => `/api/v1/tables/export/jobs/${jobId}/download`,
}

// ============== Data Operations ==============
export const dataAPI = {
  upsert: (data) => api.post('/data/upsert', data),
  update: (data) => api.put('/data/update', data),
  delete: (data) => api.post('/data/delete', data),
}

// ============== Upload ==============
export const uploadAPI = {
  upload: (formData, onProgress) =>
    api.post('/upload/', formData, {
      headers: { 'Content-Type': 'multipart/form-data' },
      onUploadProgress: onProgress,
      timeout: 300000,
    }),
  uploadAsync: (formData, onProgress) =>
    api.post('/upload/async', formData, {
      headers: { 'Content-Type': 'multipart/form-data' },
      onUploadProgress: onProgress,
      timeout: 60000,  // Shorter timeout since it's async
    }),
  preview: (formData) =>
    api.post('/upload/preview', formData, {
      headers: { 'Content-Type': 'multipart/form-data' },
    }),
  sheets: (formData) =>
    api.post('/upload/sheets', formData, {
      headers: { 'Content-Type': 'multipart/form-data' },
    }),
  // Job management
  listJobs: (limit = 20) => api.get('/upload/jobs', { params: { limit } }),
  listAllJobs: (limit = 50) => api.get('/upload/jobs/all', { params: { limit } }),
  getJob: (jobId) => api.get(`/upload/jobs/${jobId}`),
  cancelJob: (jobId, force = false) => api.post(`/upload/jobs/${jobId}/cancel`, null, { params: { force } }),
  deleteJob: (jobId) => api.delete(`/upload/jobs/${jobId}`),
  queueStatus: () => api.get('/upload/queue/status'),
}

// ============== MSA Analysis ==============
export const msaAPI = {
  // Configuration & Discovery
  getColumns: () => api.get('/msa/columns'),
  getDistinct: (column, date, filters) => api.get('/msa/distinct', { 
    params: { 
      column, 
      ...(date && { date }),
      ...(filters && { filters })
    } 
  }),
  loadConfig: (configName) => api.get(`/msa/load/${configName}`),
  saveConfig: (payload) => api.post('/msa/config', payload),
  
  // Filtering & Data Loading
  applyFilters: (payload) => api.post('/msa/filter', payload),
  
  // Debug endpoints
  debugTestDate: (date) => api.get(`/msa/debug/test-date`, { params: { date } }),
  
  // Calculation
  calculate: (payload) => api.post('/msa/calculate', payload),
  
  // Pivot Tables
  generatePivot: (payload) => api.post('/msa/pivot', payload),
  
  // Save Results
  save: (payload) => api.post('/msa/save', payload),
  
  // Stored Results Management (with Sequence Tracking)
  getStoredSequences: (limit = 10) => api.get('/msa/results/sequences', { params: { limit } }),
  getStoredResults: (sequenceId, table = 'msa') => api.get(`/msa/results/${sequenceId}`, { params: { table } }),
  getSequenceSummary: (sequenceId) => api.get(`/msa/results/${sequenceId}/summary`),
  
  // Legacy
  run: (payload) => api.post('/msa/run', payload),
}

// ============== Audit ==============
export const auditAPI = {
  list: (params) => api.get('/audit', { params }),
  get: (id) => api.get(`/audit/${id}`),
}

// ============== Settings ==============
export const settingsAPI = {
  getAll: () => api.get('/settings'),
  get: (category) => api.get(`/settings/${category}`),
  update: (category, settings) => api.put('/settings', { category, settings }),
  testConnection: (config) => api.post('/settings/test-connection', config || {}),
  // Database tab uses this instead of update(): tests, saves to JSON + .env,
  // and hot-reloads the live engines so the app reconnects to the new server
  // without a process restart.
  applyDatabase: (config) => api.post('/settings/database/apply', config || {}),
  testEmail: (to) => api.post('/settings/test-email', { to_address: to }),
  systemInfo: () => api.get('/settings/system/info'),
  // Backup
  listBackups: () => api.get('/settings/backup/list'),
  createBackup: (database) => api.post('/settings/backup/create', { database }),
  deleteBackup: (filename) => api.delete(`/settings/backup/${filename}`),
}



// ============== Store Stock (Data Preparation) ==============
// DB columns: kpi (NVARCHAR) and status ('Active' | 'Inactive')
export const storeStockAPI = {
  getSlocSettings: ()           => api.get('/store-stock/sloc-settings'),
  syncSlocs:       ()           => api.post('/store-stock/sync'),
  updateSloc:      (sloc, data) => api.put(`/store-stock/sloc-settings/${encodeURIComponent(sloc)}`, data),
  bulkUpdate:      (items)      => api.put('/store-stock/sloc-settings', { items }),
}

// ============== Grid Builder (Data Preparation > Store Stock) ==============
export const gridBuilderAPI = {
  getColumns:  ()           => api.get('/grid-builder/columns'),
  listGrids:   ()           => api.get('/grid-builder/grids'),
  createGrid:  (data)       => api.post('/grid-builder/grids', data),
  updateGrid:  (id, data)   => api.put(`/grid-builder/grids/${id}`, data),
  deleteGrid:  (id)         => api.delete(`/grid-builder/grids/${id}`),
  runGrid:     (id)         => api.post(`/grid-builder/grids/${id}/run`, null, { timeout: 600000 }),
  runAll:      ()           => api.post('/grid-builder/run-all', null, { timeout: 1800000 }),
  reorder:     (sequence)   => api.put('/grid-builder/reorder', { sequence }),
  calcPreview: ()           => api.get('/grid-builder/calculation-preview'),
  buildCalcTables: ()       => api.post('/grid-builder/build-calc-tables', null, { timeout: 600000 }),
  hierarchyGaps: (opts={})  => api.get('/grid-builder/hierarchy/gaps', { quiet: true, timeout: 30000, ...opts }),
}

// ============== Merge Rules (ARS_MERGE_RULES — drives MERGE_<col> derivation) ==============
export const mergeRulesAPI = {
  list:         ()             => api.get('/merge-rules'),
  sourceCols:   ()             => api.get('/merge-rules/source-cols'),
  create:       (data)         => api.post('/merge-rules', data),
  update:       (id, data)     => api.put(`/merge-rules/${id}`, data),
  remove:       (id)           => api.delete(`/merge-rules/${id}`),
  refresh:      (sourceCol)    => api.post(`/merge-rules/refresh/${encodeURIComponent(sourceCol)}`, null, { timeout: 300000 }),
  bulk:         (rules, refresh_after=true) =>
                                   api.post('/merge-rules/bulk', { rules, refresh_after }, { timeout: 600000 }),
}

// ============== Listing (Data Preparation) ==============
// Polling calls (config, summary, activeJob, allocProgress) pass quiet:true
// + a short timeout so transient backend pressure during a Generate run
// doesn't spam the user with timeout toasts.
const _POLL = { quiet: true, timeout: 30000 }
// Foreground (quiet:false) callers — initial page load, explicit refresh —
// run with no timeout: a cold Azure SQL connection can take well over 30s
// on the first config/summary query and we'd rather wait than fail.
const _FOREGROUND = { timeout: 0 }
const _pollOrForeground = (opts) => (opts?.quiet ? { ..._POLL, ...opts } : { ..._FOREGROUND, ...opts })
export const listingAPI = {
  // config / summary default to foreground (long timeout, error toast). Pass
  // { quiet: true } from background pollers to suppress the toast and use the
  // shorter polling timeout.
  config:       (opts={}) => api.get('/listing/config',  _pollOrForeground(opts)),
  generate:     (data, opts) => api.post('/listing/generate', data, { timeout: 600000, ...opts }),
  preview:      (params) => api.get('/listing/preview', { params }),
  summary:      (opts={}) => api.get('/listing/summary', _pollOrForeground(opts)),
  export:       (params) => api.get('/listing/export', { params, responseType: 'blob', timeout: 600000 }),
  createFinal:  (data)   => api.post('/listing/create-final', data || {}),
  storeRanking: (params) => api.get('/listing/store-ranking', { params }),
  allocPreview: (params) => api.get('/listing/alloc-preview', { params }),
  finalPreview: (params) => api.get('/listing/final/preview', { params }),
  saveSettings: (data)   => api.post('/listing/settings', data),
  // Parking-mode (Single / Multiple parked sessions).  Read any user;
  // write requires ADMIN / SUPER_ADMIN.  Surfaced in Settings → Application.
  getParkingMode: () => api.get('/listing/parking-mode'),
  setParkingMode: (allow_multi_parked) =>
    api.put('/listing/parking-mode', { allow_multi_parked: !!allow_multi_parked }),
  // Parallel allocation: live progress, manual retry, recent batches.
  allocProgress: (batchId) => api.get('/listing/alloc-progress', { params: { batch_id: batchId }, ..._POLL }),
  retryFailed:   (data)    => api.post('/listing/retry-failed', data, { timeout: 600000 }),
  allocBatches:  (limit=20) => api.get('/listing/alloc-batches', { params: { limit } }),
  // Live: detect any backend Python job currently running on the server,
  // so the UI can show its session/stage even if it was started elsewhere
  // or the local batch_id was lost (refresh, reopen, etc).
  activeJob:     ()       => api.get('/listing/active-job', _POLL),
  cancelBatch:   (batchId) => api.post('/listing/cancel-batch', { batch_id: batchId }),
  // RDC stock vs alloc, filtered by selected MAJ_CAT(s) — for the live
  // contribution chart on the listing page. Quiet because it's polled on
  // selection change.
  contribution:  (majCats) => api.get('/listing/contribution', {
    params: { maj_cats: (majCats || []).join(',') }, ..._POLL,
  }),
  // Per-store drill-down from the MAJ_CAT modal. rdc is optional —
  // omit to get all stores for the MAJ_CAT (TOTAL column drill-down).
  storeByMajCat: (majCat, rdc) => api.get('/listing/store-by-majcat', {
    params: rdc ? { maj_cat: majCat, rdc } : { maj_cat: majCat },
  }),
  // OPT-wise drill (per MAJ_CAT × RDC). Returns one row per (WERKS,
  // GEN_ART_NUMBER, CLR) with OPT-grain columns (OPT_MBQ, OPT_REQ,
  // MSA_FNL_Q_REM, ALLOC_REMARKS, OPT_PRIORITY_RANK, etc.). Optional
  // `werks` further narrows to one store.
  optSummary: (majCat, rdc, werks) => api.get('/listing/opt-summary', {
    params: {
      maj_cat: majCat,
      ...(rdc   ? { rdc }   : {}),
      ...(werks ? { werks } : {}),
    },
  }),
  // VAR_ART × SZ drill (per OPT). Returns one row per (VAR_ART, SZ)
  // from ARS_ALLOC_WORKING with per-size MBQ/REQ/SHIP/HOLD + audit trail.
  varSummary: (majCat, werks, genArt, clr, rdc) =>
    api.get('/listing/var-summary', {
      params: {
        maj_cat: majCat, werks, gen_art: genArt, clr: clr || '',
        ...(rdc ? { rdc } : {}),
      },
    }),
  // SLOC-wise inventory breakdown for the STORE_STOCK click — returns
  // the dynamic SLOC column sums for the selected (MAJ_CAT, RDC).
  slocBreakdown: (majCat, rdc, werks) => api.get('/listing/sloc-breakdown', {
    params: {
      maj_cat: majCat,
      ...(rdc   ? { rdc }   : {}),
      ...(werks ? { werks } : {}),
    },
  }),
  // Per-session log capture (Logs page).
  sessions:      (params)    => api.get('/listing/sessions', { params }),
  session:       (sid)       => api.get(`/listing/sessions/${sid}`),
  sessionLog:    (sid, tail) => api.get(`/listing/sessions/${sid}/log`, { params: tail ? { tail } : {} }),
  killSession:   (sid)       => api.post(`/listing/sessions/${sid}/kill`),
  deleteSession: (sid)       => api.delete(`/listing/sessions/${sid}`),
  // Park-then-promote alloc history: snapshot of ARS_ALLOC_WORKING per
  // run lands in ARS_ALLOC_PARKED awaiting review; on Approve it moves
  // to ARS_ALLOC_HISTORY (permanent record); on Reject it stays parked
  // with PARK_STATUS='REJECTED' for audit.
  parkedRuns:      (includeRejected=false) =>
                    api.get('/listing/parked-runs',
                            { params: { include_rejected: includeRejected }, ..._POLL }),
  // which: 'alloc' (ARS_ALLOC_PARKED) | 'listing' (ARS_LISTING_WORKING_PARKED)
  parkedRunDetail: (sid, params={}) =>
                    api.get(`/listing/parked-runs/${sid}`, { params }),
  approveParked:   (sid) => api.post(`/listing/parked-runs/${sid}/approve`),
  rejectParked:    (sid, note) =>
                    api.post(`/listing/parked-runs/${sid}/reject`, { note: note || '' }),
  allocHistory:    (params) => api.get('/listing/alloc-history', { params }),
  listingHistory:  (params) => api.get('/listing/listing-history', { params }),
}

// ============== ARS Dashboard (unified analytics) ==============
// All endpoints accept the standard scope filters as query params:
//   date, sid, mc (csv), werks (csv), rdc (csv), from, to
// The Product Drill tab reuses listingAPI.storeByMajCat / optSummary /
// varSummary directly — no wrapper here.
export const arsDashboardAPI = {
  summary:        (params)   => api.get('/ars-dashboard/summary',          { params }),
  breakdown:      (params)   => api.get('/ars-dashboard/breakdown',        { params }),
  dates:          (params)   => api.get('/ars-dashboard/dates',            { params }),
  sessionsByDate: (date)     => api.get('/ars-dashboard/sessions-by-date', { params: { date } }),
  sessions:       (params)   => api.get('/ars-dashboard/sessions',         { params }),
  sessionDetail:  (params)   => api.get('/ars-dashboard/session-detail',   { params }),
  trend:          (params)   => api.get('/ars-dashboard/trend',            { params }),
  trendSessions:  (params)   => api.get('/ars-dashboard/trend-sessions',   { params }),
  pending:        (params)   => api.get('/ars-dashboard/pending',          { params }),
  gap:            (params)   => api.get('/ars-dashboard/gap',              { params }),
  exportGap:      (params)   => api.get('/ars-dashboard/gap/export',       { params, responseType: 'blob', timeout: 300000 }),
  // Hierarchical drill — same endpoints power Date&Session deep drill and Product Drill tab
  drillMajCats:   (params)   => api.get('/ars-dashboard/drill/maj-cats',   { params }),
  drillStores:    (params)   => api.get('/ars-dashboard/drill/stores',     { params }),
  drillGenArts:   (params)   => api.get('/ars-dashboard/drill/gen-arts',   { params }),
  drillArticles:  (params)   => api.get('/ars-dashboard/drill/articles',   { params }),
  drillLevel:     (params)   => api.get('/ars-dashboard/drill/level',      { params }),
  sessionsLatest: (params)   => api.get('/ars-dashboard/sessions/latest',  { params }),
  sessionsReviewList: (params) => api.get('/ars-dashboard/sessions/review-list', { params }),
  sessionReview:  (params)   => api.get('/ars-dashboard/session-review',   { params }),
  configExtras:   ()         => api.get('/ars-dashboard/config-extras'),
  holdByRdc:      (params)   => api.get('/ars-dashboard/hold-by-rdc',      { params }),
  pivotMajCatRdc: (params)   => api.get('/ars-dashboard/pivot/maj-cat-rdc', { params }),
}

// ============== GAP Report (multi-category algorithm review) ==============
export const gapReportAPI = {
  summary:        (params) => api.get('/gap-report/summary',          { params }),
  excessStk:      (params) => api.get('/gap-report/excess-stk',       { params }),
  listedNotAlloc: (params) => api.get('/gap-report/listed-not-alloc', { params }),
  skipReason:     (params) => api.get('/gap-report/skip-reason',      { params }),
  holdAnomaly:    (params) => api.get('/gap-report/hold-anomaly',     { params }),
  mbqDeviation:   (params) => api.get('/gap-report/mbq-deviation',    { params }),
  pendAging:      (params) => api.get('/gap-report/pend-aging',       { params }),
  bdcDoReco:      (params) => api.get('/gap-report/bdc-do-reco',      { params }),
  parkedDrift:    (params) => api.get('/gap-report/parked-drift',     { params }),
  export:         (params) => api.get('/gap-report/export',           { params, responseType: 'blob', timeout: 300000 }),
}

// ============== Lookup Art Master (Data Preparation) ==============
export const lookupArtMasterAPI = {
  getColumns: () => api.get('/lookup-art-master/columns'),
  preview: (formData) =>
    api.post('/lookup-art-master/preview', formData, {
      headers: { 'Content-Type': 'multipart/form-data' },
      timeout: 300000,
    }),
  run: (formData) =>
    api.post('/lookup-art-master/run', formData, {
      headers: { 'Content-Type': 'multipart/form-data' },
      timeout: 300000,
    }),
  download: (formData) =>
    api.post('/lookup-art-master/download', formData, {
      headers: { 'Content-Type': 'multipart/form-data' },
      responseType: 'blob',
      timeout: 300000,
    }),
}

// ============== Contribution Percentage v2 ==============
export const contribAPI = {
  // Config
  getGroupingColumns: ()    => api.get('/contrib/config/grouping-columns'),
  getMonths:          ()    => api.get('/contrib/config/months'),
  getSsnValues:       ()    => api.get('/contrib/config/ssn-values'),
  getMajcats:    (gc)       => api.get('/contrib/config/majcats', { params: { grouping_column: gc } }),
  // Presets
  listPresets:   ()         => api.get('/contrib/presets'),
  savePreset:    (data)     => api.post('/contrib/presets', data),
  deletePreset:  (name)     => api.delete(`/contrib/presets/${encodeURIComponent(name)}`),
  reorderPresets:(seq)      => api.put('/contrib/presets/reorder', { sequence: seq }),
  // Mappings
  listMappings:  ()         => api.get('/contrib/mappings'),
  saveMapping:   (data)     => api.post('/contrib/mappings', data),
  deleteMapping: (name)     => api.delete(`/contrib/mappings/${encodeURIComponent(name)}`),
  // Assignments
  listAssignments: ()       => api.get('/contrib/assignments'),
  saveAssignment:  (data)   => api.post('/contrib/assignments', data),
  deleteAssignment:(id)     => api.delete(`/contrib/assignments/${id}`),
  // Execute (creates background job)
  execute: (data)           => api.post('/contrib/execute', data),
  // Jobs
  listJobs:    ()           => api.get('/contrib/jobs'),
  getJob:      (id)         => api.get(`/contrib/jobs/${id}`),
  cancelJob:   (id)         => api.post(`/contrib/jobs/${id}/cancel`),
  deleteJob:   (id)         => api.delete(`/contrib/jobs/${id}`),
  pauseJob:    (id)         => api.post(`/contrib/jobs/${id}/pause`),
  resumeJob:   (id)         => api.post(`/contrib/jobs/${id}/resume`),
  downloadJobResult: (id, type) => api.get(`/contrib/jobs/${id}/download/${type}`, { responseType: 'blob', timeout: 600000 }),
  // Review
  listTables:    ()         => api.get('/contrib/review/tables'),
  previewTable:  (name, limit=500, filters={}) => {
    const params = { limit }
    Object.entries(filters).forEach(([col, vals]) => { if (vals.length) params[`f_${col}`] = vals.join(',') })
    return api.get(`/contrib/review/preview/${encodeURIComponent(name)}`, { params })
  },
  downloadTable: (name)     => api.get(`/contrib/review/download/${encodeURIComponent(name)}`, { responseType: 'blob', timeout: 600000 }),
  deleteTable:   (name)     => api.delete(`/contrib/review/tables/${encodeURIComponent(name)}`),
  // Review Export Jobs (background download)
  startExport:      (name, filters={})  => api.post(`/contrib/review/export/${encodeURIComponent(name)}`, { filters }),
  listExports:      ()      => api.get('/contrib/review/exports'),
  getExport:        (id)    => api.get(`/contrib/review/exports/${id}`),
  downloadExport:   (id)    => api.get(`/contrib/review/exports/${id}/download`, { responseType: 'blob', timeout: 600000 }),
  deleteExport:     (id)    => api.delete(`/contrib/review/exports/${id}`),
}

// ============== Auto Cont % (SQL-direct pipeline) ==============
// Reuses Cont_presets / Cont_mappings via contribAPI above (those tables are
// shared with the pandas pipeline). Only compute, jobs, and table I/O are
// net-new here.
export const autoContAPI = {
  status:        ()       => api.get('/auto-cont/status'),
  // execute now creates a background JOB (returns { job_id }) — no longer
  // blocks the request. Poll /jobs/{id} for status.
  execute:       (data)   => api.post('/auto-cont/execute', data),
  // Jobs
  listJobs:      ()       => api.get('/auto-cont/jobs'),
  getJob:        (id)     => api.get(`/auto-cont/jobs/${id}`),
  cancelJob:     (id)     => api.post(`/auto-cont/jobs/${id}/cancel`),
  deleteJob:     (id)     => api.delete(`/auto-cont/jobs/${id}`),
  // Tables
  listTables:    ()       => api.get('/auto-cont/tables'),
  preview:       (name, limit=200) =>
    api.get(`/auto-cont/preview/${encodeURIComponent(name)}`, { params: { limit } }),
  dropTable:     (name)   => api.delete(`/auto-cont/tables/${encodeURIComponent(name)}`),
  downloadTable: (name)   => api.get(`/auto-cont/download/${encodeURIComponent(name)}`,
                                      { responseType: 'blob', timeout: 600000 }),
}

// ============== Data Checklist ==============
export const checklistAPI = {
  getItems:         ()           => api.get('/checklist/items'),
  getAvailableTables: ()         => api.get('/checklist/available-tables'),
  addItem:          (data)       => api.post('/checklist/items', data),
  updateItem:       (id, data)   => api.put(`/checklist/items/${id}`, data),
  reorder:          (items)      => api.put('/checklist/reorder', { items }),
  stamp:            (tableName)  => api.post(`/checklist/stamp/${encodeURIComponent(tableName)}`),
  deleteItem:       (id)         => api.delete(`/checklist/items/${id}`),
}

// ============== Trends ==============
export const trendsAPI = {
  listTables:      ()                    => api.get('/trends/tables'),
  getSchema:       (name)                => api.get(`/trends/tables/${encodeURIComponent(name)}/schema`),
  getDistinct:     (name, col)           => api.get(`/trends/tables/${encodeURIComponent(name)}/distinct/${encodeURIComponent(col)}`),
  uploadPreview:   (formData)            => api.post('/trends/upload/preview', formData, { headers: { 'Content-Type': 'multipart/form-data' } }),
  checkConflicts:  (data)                => api.post('/trends/upload/check-conflicts', data),
  upload:          (formData, onProgress) => api.post('/trends/upload', formData, { headers: { 'Content-Type': 'multipart/form-data' }, onUploadProgress: onProgress, timeout: 300000 }),
  review:          (data)                => api.post('/trends/review', data),
  downloadReview:  (name, params)        => api.get(`/trends/review/${encodeURIComponent(name)}/download`, { params, responseType: 'blob', timeout: 600000 }),
  createTable:     (formData)            => api.post('/trends/create-table', formData, { headers: { 'Content-Type': 'multipart/form-data' } }),
  truncateTable:   (name)                => api.post(`/trends/admin/truncate/${encodeURIComponent(name)}`),
  dropTable:       (name)                => api.delete(`/trends/admin/drop/${encodeURIComponent(name)}`),
  alterColumns:    (name, data)          => api.put(`/trends/admin/${encodeURIComponent(name)}/columns`, data),
}

// ============== Maintenance (superadmin only) ==============
export const maintenanceAPI = {
  tempdbStatus:    ()          => api.get('/maintenance/tempdb/status'),
  tempdbSize:      ()          => api.get('/maintenance/tempdb/size'),
  tempdbHistory:   ()          => api.get('/maintenance/tempdb/history'),
  tempdbSessions:  ()          => api.get('/maintenance/tempdb/sessions'),
  tempdbCleanup:   (dryRun=false) =>
    api.post('/maintenance/tempdb/cleanup', null, { params: { dry_run: dryRun } }),
  tempdbAggressiveShrink: ()   => api.post('/maintenance/tempdb/aggressive-shrink', null, { timeout: 600000 }),
  tempdbClearAlert: ()         => api.post('/maintenance/tempdb/alert/clear'),
  tempdbLongTransactions: ()   => api.get('/maintenance/tempdb/long-transactions'),
  tempdbKillSession: (sid)     => api.post(`/maintenance/tempdb/kill-session/${sid}`),

  // Database file maintenance (Rep_Data log + data files, Claude DB)
  dbFiles:           ()                         => api.get('/maintenance/db/files'),
  dbCheckpoint:      (db)                       => api.post(`/maintenance/db/${encodeURIComponent(db)}/checkpoint`),
  dbShrinkLog:       (db, targetMb=4096)        => api.post(`/maintenance/db/${encodeURIComponent(db)}/shrink-log`, null, { params: { target_mb: targetMb }, timeout: 600000 }),
  dbShrinkData:      (db, fileName, targetMb)   => api.post(`/maintenance/db/${encodeURIComponent(db)}/shrink-data`, null, { params: { file_name: fileName, target_mb: targetMb }, timeout: 900000 }),
  dbSetRecovery:     (db, model, confirm=true)  => api.post(`/maintenance/db/${encodeURIComponent(db)}/recovery`, null, { params: { model, confirm } }),
  dbBackupLog:       (db, backupPath)           => api.post(`/maintenance/db/${encodeURIComponent(db)}/backup-log`, null, { params: { backup_path: backupPath }, timeout: 600000 }),
  dbClearLogBackupWait: (db, targetMb=4096)     => api.post(`/maintenance/db/${encodeURIComponent(db)}/clear-log-backup-wait`, null, { params: { target_mb: targetMb }, timeout: 600000 }),
  dbSetLogMaxsize:   (db, maxMb)                => api.post(`/maintenance/db/${encodeURIComponent(db)}/set-log-maxsize`, null, { params: { max_mb: maxMb } }),
  diskSpace:         ()                         => api.get('/maintenance/disk'),
  reclaimAll:        ()                         => api.post('/maintenance/reclaim-all', null, { timeout: 900000 }),

  // Transactional-data reset (Settings → Danger Zone)
  resetPreview:      (includeMsaTracking=false) =>
    api.get('/maintenance/reset/preview', { params: { include_msa_tracking: includeMsaTracking } }),
  resetTransactionalData: (includeMsaTracking=false) =>
    api.post('/maintenance/reset/transactional-data',
      { confirm: 'RESET', include_msa_tracking: includeMsaTracking },
      { timeout: 900000 }),
}

// ============== Reports ==============
export const reportsAPI = {
  getPendAlc:         (limit=5000, filters={}) => {
    const params = { limit }
    Object.entries(filters).forEach(([col, vals]) => { if (vals.length) params[`f_${col}`] = vals.join(',') })
    return api.get('/reports/pend-alc', { params })
  },
  getDistinctValues:  (col)        => api.get(`/reports/pend-alc/distinct/${col}`),
  downloadPendAlc:    (filters={}) => {
    const params = {}
    Object.entries(filters).forEach(([col, vals]) => { if (vals.length) params[`f_${col}`] = vals.join(',') })
    return api.get('/reports/pend-alc/download', { params, responseType: 'blob', timeout: 600000 })
  },
}

// ============== Hold Dashboard (HOLD_QTY review across angles) ==============
export const holdDashboardAPI = {
  summary:        () => api.get('/hold-dashboard/summary'),
  byStore:        (params) => api.get('/hold-dashboard/by-store', { params }),
  byRdc:          (params) => api.get('/hold-dashboard/by-rdc', { params }),
  byArticle:      (params) => api.get('/hold-dashboard/by-article', { params }),
  byStatus:       () => api.get('/hold-dashboard/by-status'),
  byAge:          () => api.get('/hold-dashboard/by-age'),
  timeline:       (params) => api.get('/hold-dashboard/timeline', { params }),
  detail:         (params) => api.get('/hold-dashboard/detail', { params }),
  reconciliation: () => api.get('/hold-dashboard/reconciliation'),
}

// ============== Pending Allocation (ARS_PEND_ALC) ==============
export const pendAlcAPI = {
  summary:     ()               => api.get('/pend-alc/summary'),
  sessions:    ()               => api.get('/pend-alc/sessions'),
  detail:      (params = {})    => api.get('/pend-alc/detail', { params }),
  doHistory:   (limit = 100)    => api.get('/pend-alc/do-history', { params: { limit } }),
  doUpdate:    (payload)        => api.post('/pend-alc/do-update',
    // Accept either a raw rows array (legacy callers) or the full request
    // body { rows, session_id, is_first_chunk, is_last_chunk } (new callers).
    Array.isArray(payload) ? { rows: payload } : payload,
    // Per-chunk SQL is now sub-second after the set-based rewrite, but a
    // 10-min ceiling protects against cold Azure SQL connections and
    // unexpected lock waits on huge uploads.
    { timeout: 10 * 60 * 1000 }),
  bdcPreview:  (params = {})    => api.get('/pend-alc/bdc-preview', { params }),
  bdcGenerate: (params = {})    => api.post('/pend-alc/bdc-generate', null,
                                    { params, responseType: 'blob', timeout: 300000,
                                      paramsSerializer: { indexes: null } }),
  // Async BDC generate — returns { job_id } immediately. Caller polls
  // asyncJobStatus(job_id) and downloads via asyncJobDownload(job_id) when
  // status === 'completed'. Used by the Generate-BDC modal to escape the
  // 100s Cloudflare edge timeout on large batches.
  bdcGenerateAsync: (params = {}) => api.post('/pend-alc/bdc-generate-async', null,
                                      { params, paramsSerializer: { indexes: null } }),
  asyncJobStatus:   (job_id)      => api.get(`/pend-alc/async-jobs/${job_id}`),
  asyncJobDownload: (job_id)      => api.get(`/pend-alc/async-jobs/${job_id}/download`,
                                      { responseType: 'blob', timeout: 10 * 60 * 1000 }),
  // Async DO upload — same body as doUpdate(), returns { job_id, session_id }.
  doUpdateAsync: (payload)        => api.post('/pend-alc/do-update-async',
                                      Array.isArray(payload) ? { rows: payload } : payload,
                                      { timeout: 60 * 1000 }),
  bdcHistory:  (params = {})    => api.get('/pend-alc/bdc-history', { params }),
  // Re-download an old BDC's SAP-ready 9-column Excel from history.
  bdcHistoryRedownload: (allocation_number) => api.get('/pend-alc/bdc-history-redownload',
                                        { params: { allocation_number },
                                          responseType: 'blob', timeout: 5 * 60 * 1000 }),
  // Bulk export ARS_BDC_HISTORY rows (CSV) — default Open BDC Report view.
  bdcHistoryExport: (params = {}) => api.get('/pend-alc/bdc-history-export',
                                        { params, responseType: 'blob',
                                          timeout: 5 * 60 * 1000 }),
  // Distinct allocations summary — drives the "Re-download original SAP file"
  // chips on the Open BDC Report regardless of the row-cap on the table.
  bdcHistoryAllocations: (params = {}) => api.get('/pend-alc/bdc-history-allocations',
                                        { params }),
  manualUpload:(payload)        => api.post('/pend-alc/manual-upload',
    // Accept either a raw rows array (legacy callers) or the full request
    // body { rows, session_id, is_first_chunk, is_last_chunk } (new callers).
    Array.isArray(payload) ? { rows: payload } : payload,
    // The last chunk runs the deferred MSA+grid delta which can take 30-60
    // seconds on big uploads, so allow up to 5 minutes per request before we
    // bail. Avoids indefinite hangs while still catching truly stuck calls.
    { timeout: 5 * 60 * 1000, quiet: true }),
  reco:        (params = {})    => api.get('/pend-alc/reco', { params }),
  recoSummary: ()               => api.get('/pend-alc/reco-summary'),
  // Excel export — same filter params as /reco. Used by the per-tile export
  // buttons on the Reconciliation page.
  recoExport:  (params = {})    => api.get('/pend-alc/reco-export',
                                    { params, responseType: 'blob',
                                      timeout: 10 * 60 * 1000 }),

  // Store BDC schedule (Mon-Sat per store)
  scheduleList:        ()                    => api.get('/pend-alc/schedule'),
  scheduleStoresFor:   (date)                => api.get('/pend-alc/schedule/stores-for-date', { params: { date } }),
  scheduleUpsert:      (rows, opts = {})     => api.post('/pend-alc/schedule',
                                                  { rows, source: opts.source || 'API', note: opts.note || null }),
  scheduleDelete:      (st_cd, opts = {})    => api.delete(`/pend-alc/schedule/${encodeURIComponent(st_cd)}`,
                                                  { params: { source: opts.source || 'UI', ...(opts.note ? { note: opts.note } : {}) } }),
  scheduleAudit:       (params = {})         => api.get('/pend-alc/schedule/audit', { params }),

  // Operations log + revert (BDC / DO / MANUAL)
  operationsList:      (params = {})         => api.get('/pend-alc/operations', { params }),
  operationsPreview:   (op_id)               => api.post(`/pend-alc/operations/${op_id}/preview-revert`),
  operationsRevert:    (op_id, note)         => api.post(`/pend-alc/operations/${op_id}/revert`,
                                                  { note: note || null },
                                                  // 15-min cap — set-based revert is sub-second, but the
                                                  // post-revert grid + MSA resync passes can take a minute
                                                  // on huge ops, and the default 5-min axios timeout was
                                                  // killing the request mid-flight (backend finished, but
                                                  // the UI never saw the response so the success toast
                                                  // never fired).
                                                  { params: { confirm: true }, timeout: 15 * 60 * 1000 }),
  // Async revert — returns { job_id } immediately, frontend polls asyncJobStatus.
  operationsRevertAsync: (op_id, note)       => api.post(`/pend-alc/operations/${op_id}/revert-async`,
                                                  { note: note || null },
                                                  { params: { confirm: true }, timeout: 60 * 1000 }),
  operationsBackfillBdc: (confirm = false)   => api.post('/pend-alc/operations/backfill-bdc',
                                                  null, { params: { confirm } }),
  // One-shot cleanup of orphan OPEN BDC_HISTORY rows (those whose
  // PEND_ALC has already closed). confirm=false previews, confirm=true
  // applies. See close_orphan_open_bdc_history in pend_alc_service.py.
  operationsCloseOrphanBdc: (confirm = false) => api.post('/pend-alc/operations/close-orphan-bdc-history',
                                                  null, { params: { confirm },
                                                          timeout: 10 * 60 * 1000 }),

  // Adhoc Close — flip IS_CLOSED on PEND_ALC rows + CANCEL their open BDC
  // history. Revertable via the same operations log (OP_TYPE='ADHOC_CLOSE').
  closeRows:     (payload)         => api.post('/pend-alc/close-rows', payload,
                                        { timeout: 10 * 60 * 1000 }),
  closeRowsFile: (file, reason)    => {
    const fd = new FormData()
    fd.append('file', file)
    const params = reason ? { reason } : {}
    return api.post('/pend-alc/close-rows-file', fd, {
      params, headers: { 'Content-Type': 'multipart/form-data' },
      timeout: 10 * 60 * 1000,
    })
  },
}

// ============== Project Tracker ==============
export const ptAPI = {
  enums:        ()                       => api.get('/pt/enums'),
  list:         (params = {})            => api.get('/pt/projects',          { params }),
  tree:         (params = {})            => api.get('/pt/projects/tree',     { params }),
  get:          (id)                     => api.get(`/pt/projects/${id}`),
  create:       (data)                   => api.post('/pt/projects', data),
  update:       (id, data)               => api.put(`/pt/projects/${id}`, data),
  archive:      (id)                     => api.delete(`/pt/projects/${id}`),
  restore:      (id)                     => api.post(`/pt/projects/${id}/restore`),
  move:         (id, new_parent_id)      => api.post(`/pt/projects/${id}/move`,
                                                     { new_parent_id }),
  activity:     (id, limit = 100)        => api.get(`/pt/projects/${id}/activity`,
                                                    { params: { limit } }),
  dashboard:    ()                       => api.get('/pt/dashboard'),
  myTasks:      ()                       => api.get('/pt/my-tasks'),
}

export default api
