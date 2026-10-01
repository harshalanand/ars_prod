import { Link } from 'react-router-dom'
import { FileSpreadsheet, PlugZap, Server, ArrowRight } from 'lucide-react'

const SOURCES = {
  excel: {
    icon: FileSpreadsheet, title: 'Excel', phase: 2,
    what: 'Load an Excel or CSV file into a local table — uploaded by hand, or picked up from a shared folder on a schedule.',
    plan: [
      'Same sync-job model as Snowflake: file → GD_XL_* table, full replace or append.',
      'Validate first: columns, types and row count are checked before anything is written.',
      'Runs land in the same Run History with AUTO / MANUAL and row counts.',
    ],
  },
  datav2: {
    icon: PlugZap, title: 'DataV2', phase: 3,
    what: 'Pull from the DataV2 source into local SQL on a schedule.',
    plan: [
      'The DataV2 source still has to be confirmed (the v2-data-api worker or another database).',
      'Once confirmed it plugs into the same jobs, schedule and history as Snowflake.',
    ],
  },
  sap: {
    icon: Server, title: 'SAP', phase: 4,
    what: 'SAP pulls (RFC tables and OData) will move here so every source is managed in one place.',
    plan: [
      'Until then, keep using SAP → Data Pulls. Nothing there changes.',
      'Existing SAP pulls and their run history will be carried over when this phase lands.',
    ],
    link: { to: '/sap/pulls', label: 'Open SAP → Data Pulls' },
  },
}

export default function GetDataComingSoonPage({ source }) {
  const s = SOURCES[source] || SOURCES.excel
  const Icon = s.icon
  return (
    <div className="space-y-6 max-w-3xl">
      <div>
        <h1 className="text-2xl font-bold text-gray-900 flex items-center gap-2"><Icon size={24} /> {s.title}
          <span className="text-xs font-medium px-2 py-0.5 rounded-full bg-gray-100 text-gray-500">phase {s.phase}</span>
        </h1>
        <p className="text-gray-500 text-sm mt-1">{s.what}</p>
      </div>
      <div className="card p-5 space-y-3">
        <div className="text-sm font-semibold text-gray-800">What's planned</div>
        <ul className="list-disc pl-5 text-sm text-gray-600 space-y-1">{s.plan.map((p, i) => <li key={i}>{p}</li>)}</ul>
        <div className="flex flex-wrap gap-2 pt-2">
          {s.link && <Link to={s.link.to} className="btn-secondary">{s.link.label} <ArrowRight size={15} /></Link>}
          <Link to="/get-data/snowflake/jobs" className="btn-secondary">Snowflake sync jobs <ArrowRight size={15} /></Link>
        </div>
      </div>
    </div>
  )
}
