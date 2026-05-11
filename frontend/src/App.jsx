import { Routes, Route, Navigate } from 'react-router-dom'
import { useEffect, Component, lazy, Suspense } from 'react'
import useAuthStore from '@/store/authStore'
import Layout from '@/components/layout/Layout'

// Eager-load: login + dashboard (always needed on first paint)
import LoginPage from '@/pages/LoginPage'
import DashboardPage from '@/pages/DashboardPage'

// Lazy-load: all other pages (loaded on-demand, reduces initial bundle ~35%)
const TablesPage             = lazy(() => import('@/pages/TablesPage'))
const TableDataPage          = lazy(() => import('@/pages/TableDataPage'))
const CreateTablePage        = lazy(() => import('@/pages/CreateTablePage'))
const UploadPage             = lazy(() => import('@/pages/UploadPage'))
const ExportPage             = lazy(() => import('@/pages/ExportPage'))
const DataEditorPage         = lazy(() => import('@/pages/DataEditorPage'))
const AllocationsPage        = lazy(() => import('@/pages/AllocationsPage'))
const AllocationDetailPage   = lazy(() => import('@/pages/AllocationDetailPage'))
const NewAllocationPage      = lazy(() => import('@/pages/NewAllocationPage'))
const UsersPage              = lazy(() => import('@/pages/UsersPage'))
const RolesPage              = lazy(() => import('@/pages/RolesPage'))
const AuditPage              = lazy(() => import('@/pages/AuditPage'))
const RLSPage                = lazy(() => import('@/pages/RLSPage'))
const TableManagementPage    = lazy(() => import('@/pages/TableManagementPage'))
const SettingsPage           = lazy(() => import('@/pages/SettingsPage'))
const MSAStockCalculationPage= lazy(() => import('@/pages/MSAStockCalculationPage'))
const ContribPresetsPage     = lazy(() => import('@/pages/ContribPresetsPage'))
const ContribMappingsPage    = lazy(() => import('@/pages/ContribMappingsPage'))
const ContribExecutePage     = lazy(() => import('@/pages/ContribExecutePage'))
const ContribReviewPage      = lazy(() => import('@/pages/ContribReviewPage'))
const JobsDashboardPage      = lazy(() => import('@/pages/JobsDashboardPage'))
const BDCCreationPage        = lazy(() => import('@/pages/BDCCreationPage'))
const StoreStockPage         = lazy(() => import('@/pages/StoreStockPage'))
const GridBuilderPage        = lazy(() => import('@/pages/GridBuilderPage'))
const LookupArtMasterPage    = lazy(() => import('@/pages/LookupArtMasterPage'))
const ListingPage            = lazy(() => import('@/pages/ListingPage'))
const ListingLogsPage        = lazy(() => import('@/pages/ListingLogsPage'))
const PendAlcReportPage          = lazy(() => import('@/pages/PendAlcReportPage'))
const PendingAllocationPage      = lazy(() => import('@/pages/PendingAllocationPage'))
const PendingDeliveryOrderPage   = lazy(() => import('@/pages/PendingDeliveryOrderPage'))
const PendAlcRecoPage            = lazy(() => import('@/pages/PendAlcRecoPage'))
const StoreBdcSchedulePage       = lazy(() => import('@/pages/StoreBdcSchedulePage'))
const ScheduleAuditPage          = lazy(() => import('@/pages/ScheduleAuditPage'))
const PendAlcOperationsPage      = lazy(() => import('@/pages/PendAlcOperationsPage'))
const ManualPendAlcPage          = lazy(() => import('@/pages/ManualPendAlcPage'))
const HoldDashboardPage      = lazy(() => import('@/pages/HoldDashboardPage'))
const ChecklistPage          = lazy(() => import('@/pages/ChecklistPage'))
const TrendUploadPage        = lazy(() => import('@/pages/TrendUploadPage'))
const TrendReviewPage        = lazy(() => import('@/pages/TrendReviewPage'))
const TrendAdminPage         = lazy(() => import('@/pages/TrendAdminPage'))
const TrendDashboardPage     = lazy(() => import('@/pages/TrendDashboardPage'))
const ProcessPage            = lazy(() => import('@/pages/ProcessPage'))
const TempDBAdminPage        = lazy(() => import('@/pages/TempDBAdminPage'))
const DeveloperGuidePage     = lazy(() => import('@/pages/DeveloperGuidePage'))
// Project Tracker
const PTDashboardPage        = lazy(() => import('@/pages/pt/PTDashboardPage'))
const PTProjectsPage         = lazy(() => import('@/pages/pt/PTProjectsPage'))
const PTProjectDetailPage    = lazy(() => import('@/pages/pt/PTProjectDetailPage'))
const PTMyTasksPage          = lazy(() => import('@/pages/pt/PTMyTasksPage'))

function PageLoader() {
  return (
    <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'center', height: '60vh', color: '#94a3b8' }}>
      <div style={{ textAlign: 'center' }}>
        <div style={{ width: 32, height: 32, border: '3px solid #e2e8f0', borderTopColor: '#4f46e5', borderRadius: '50%', animation: 'spin 0.8s linear infinite', margin: '0 auto 12px' }} />
        Loading...
      </div>
      <style>{`@keyframes spin { to { transform: rotate(360deg) } }`}</style>
    </div>
  )
}

class ErrorBoundary extends Component {
  constructor(props) { super(props); this.state = { error: null } }
  static getDerivedStateFromError(error) { return { error } }
  componentDidCatch(error, info) { console.error('ErrorBoundary caught:', error, info) }
  render() {
    if (this.state.error) {
      return (
        <div style={{ padding: 40, color: '#dc2626' }}>
          <h2 style={{ marginBottom: 10 }}>Page Error</h2>
          <pre style={{ whiteSpace: 'pre-wrap', fontSize: 13, background: '#fef2f2', padding: 16, borderRadius: 8 }}>
            {this.state.error.message}{'\n'}{this.state.error.stack}
          </pre>
          <button onClick={() => this.setState({ error: null })} style={{ marginTop: 10, padding: '6px 16px', cursor: 'pointer' }}>
            Retry
          </button>
        </div>
      )
    }
    return this.props.children
  }
}

function ProtectedRoute({ children, permission }) {
  const { isAuthenticated, hasPermission } = useAuthStore()
  if (!isAuthenticated) return <Navigate to="/login" replace />
  if (permission && !hasPermission(permission)) {
    return <div className="p-10 text-center text-gray-500">Access denied. You don't have the required permission.</div>
  }
  return children
}

export default function App() {
  const { isAuthenticated, fetchUser } = useAuthStore()

  useEffect(() => {
    if (isAuthenticated) fetchUser()
  }, [])

  return (
    <Suspense fallback={<PageLoader />}>
    <Routes>
      <Route path="/login" element={<LoginPage />} />
      <Route path="/" element={<ProtectedRoute><Layout /></ProtectedRoute>}>
        <Route index element={<DashboardPage />} />
        {/* Data Management */}
        <Route path="tables" element={<ProtectedRoute permission="DATA_VIEW"><TablesPage /></ProtectedRoute>} />
        <Route path="tables/create" element={<ProtectedRoute permission="TABLE_CREATE"><CreateTablePage /></ProtectedRoute>} />
        <Route path="tables/:tableName" element={<ProtectedRoute permission="DATA_VIEW"><TableDataPage /></ProtectedRoute>} />
        <Route path="upload" element={<ProtectedRoute permission="DATA_UPLOAD"><UploadPage /></ProtectedRoute>} />
        <Route path="export" element={<ProtectedRoute permission="DATA_EXPORT"><ExportPage /></ProtectedRoute>} />
        <Route path="jobs" element={<ProtectedRoute permission="JOBS_VIEW"><JobsDashboardPage /></ProtectedRoute>} />
        <Route path="editor" element={<ProtectedRoute permission="DATA_EDITOR"><DataEditorPage /></ProtectedRoute>} />
        {/* Data Preparation */}
        <Route path="msa" element={<ProtectedRoute permission="MSA_VIEW"><MSAStockCalculationPage /></ProtectedRoute>} />
        <Route path="contribution/presets" element={<ProtectedRoute permission="CONTRIB_PRESETS"><ContribPresetsPage /></ProtectedRoute>} />
        <Route path="contribution/mappings" element={<ProtectedRoute permission="CONTRIB_MAPPINGS"><ContribMappingsPage /></ProtectedRoute>} />
        <Route path="contribution/execute" element={<ProtectedRoute permission="CONTRIB_EXECUTE"><ContribExecutePage /></ProtectedRoute>} />
        <Route path="contribution/review" element={<ProtectedRoute permission="CONTRIB_REVIEW"><ContribReviewPage /></ProtectedRoute>} />
        <Route path="bdc" element={<ProtectedRoute permission="BDC_VIEW"><BDCCreationPage /></ProtectedRoute>} />
        <Route path="data-validation/store-sloc" element={<ProtectedRoute permission="STORE_SLOC_VIEW"><StoreStockPage /></ProtectedRoute>} />
        <Route path="data-validation/checklist" element={<ProtectedRoute permission="CHECKLIST_VIEW"><ChecklistPage /></ProtectedRoute>} />
        {/* Data Preparation - Grid Builder */}
        <Route path="data-prep/store-stock" element={<ProtectedRoute permission="GRID_VIEW"><GridBuilderPage /></ProtectedRoute>} />
        <Route path="data-prep/lookup-art-master" element={<ProtectedRoute permission="LOOKUP_VIEW"><LookupArtMasterPage /></ProtectedRoute>} />
        <Route path="data-prep/listing" element={<ErrorBoundary><ListingPage /></ErrorBoundary>} />
        <Route path="data-prep/listing/logs" element={<ErrorBoundary><ListingLogsPage /></ErrorBoundary>} />
        <Route path="process" element={<ErrorBoundary><ProcessPage /></ErrorBoundary>} />
        <Route path="dev-guide" element={<ErrorBoundary><DeveloperGuidePage /></ErrorBoundary>} />
        {/* Project Tracker */}
        <Route path="pt"                   element={<ErrorBoundary><PTDashboardPage /></ErrorBoundary>} />
        <Route path="pt/projects"          element={<ErrorBoundary><PTProjectsPage /></ErrorBoundary>} />
        <Route path="pt/projects/:id"      element={<ErrorBoundary><PTProjectDetailPage /></ErrorBoundary>} />
        <Route path="pt/my-tasks"          element={<ErrorBoundary><PTMyTasksPage /></ErrorBoundary>} />
        {/* Trends */}
        <Route path="trends/dashboard" element={<ProtectedRoute permission="TRENDS_DASHBOARD"><ErrorBoundary><TrendDashboardPage /></ErrorBoundary></ProtectedRoute>} />
        <Route path="trends/upload" element={<ProtectedRoute permission="TRENDS_UPLOAD"><ErrorBoundary><TrendUploadPage /></ErrorBoundary></ProtectedRoute>} />
        <Route path="trends/review" element={<ProtectedRoute permission="TRENDS_REVIEW"><ErrorBoundary><TrendReviewPage /></ErrorBoundary></ProtectedRoute>} />
        <Route path="trends/admin" element={<ErrorBoundary><TrendAdminPage /></ErrorBoundary>} />
        {/* Reports */}
        <Route path="reports/pend-alc" element={<PendAlcReportPage />} />
        <Route path="reports/hold" element={<ErrorBoundary><HoldDashboardPage /></ErrorBoundary>} />
        {/* Pending Allocation Lifecycle */}
        <Route path="pend-alc/overview"      element={<ErrorBoundary><PendingAllocationPage /></ErrorBoundary>} />
        <Route path="pend-alc/manual-entry"  element={<ErrorBoundary><ManualPendAlcPage /></ErrorBoundary>} />
        <Route path="pend-alc/do-entry"      element={<ErrorBoundary><PendingDeliveryOrderPage /></ErrorBoundary>} />
        <Route path="pend-alc/reco"          element={<ErrorBoundary><PendAlcRecoPage /></ErrorBoundary>} />
        <Route path="pend-alc/schedule"        element={<ErrorBoundary><StoreBdcSchedulePage /></ErrorBoundary>} />
        <Route path="pend-alc/schedule-audit"  element={<ErrorBoundary><ScheduleAuditPage /></ErrorBoundary>} />
        <Route path="pend-alc/operations"      element={<ErrorBoundary><PendAlcOperationsPage /></ErrorBoundary>} />
        {/* Allocations */}
        <Route path="allocations" element={<ProtectedRoute permission="ALLOC_READ"><AllocationsPage /></ProtectedRoute>} />
        <Route path="allocations/new" element={<ProtectedRoute permission="ALLOC_CREATE"><NewAllocationPage /></ProtectedRoute>} />
        <Route path="allocations/:id" element={<ProtectedRoute permission="ALLOC_READ"><AllocationDetailPage /></ProtectedRoute>} />
        {/* Settings / Admin */}
        <Route path="settings" element={<ProtectedRoute permission="ADMIN_SETTINGS"><SettingsPage /></ProtectedRoute>} />
        <Route path="settings/tables" element={<ProtectedRoute permission="TABLE_CREATE"><TableManagementPage /></ProtectedRoute>} />
        <Route path="settings/users" element={<ProtectedRoute permission="ADMIN_USERS_READ"><UsersPage /></ProtectedRoute>} />
        <Route path="settings/roles" element={<ProtectedRoute permission="ADMIN_ROLES_MANAGE"><RolesPage /></ProtectedRoute>} />
        <Route path="settings/rls" element={<ProtectedRoute permission="ADMIN_RLS_MANAGE"><RLSPage /></ProtectedRoute>} />
        <Route path="settings/audit" element={<ProtectedRoute permission="ADMIN_AUDIT_READ"><AuditPage /></ProtectedRoute>} />
        <Route path="settings/tempdb" element={<ErrorBoundary><TempDBAdminPage /></ErrorBoundary>} />
        {/* Legacy routes - redirect to new paths */}
        <Route path="admin/users" element={<Navigate to="/settings/users" replace />} />
        <Route path="admin/roles" element={<Navigate to="/settings/roles" replace />} />
        <Route path="admin/rls" element={<Navigate to="/settings/rls" replace />} />
        <Route path="admin/audit" element={<Navigate to="/settings/audit" replace />} />
      </Route>
      <Route path="*" element={<Navigate to="/" replace />} />
    </Routes>
    </Suspense>
  )
}
