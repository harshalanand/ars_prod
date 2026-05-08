import React, { useState, useEffect, useRef } from 'react';
import { Calculator, Filter, Calendar, Save, Plus, Trash2, Download, X, ChevronDown, CheckCircle2, AlertTriangle } from 'lucide-react';
import { msaAPI } from '../services/api';
import toast from 'react-hot-toast';
import CascadingFilters from '../components/filters/CascadingFilters';

function AddFilterColumnModal({ columns, existingFilters, onAdd, onClose }) {
  const [search, setSearch] = useState('');
  const availableColumns = columns.filter(
    c => !existingFilters.includes(c) && c.toLowerCase().includes(search.toLowerCase())
  );
  return (
    <div className="fixed inset-0 bg-black/50 flex items-center justify-center z-50" onClick={onClose}>
      <div className="bg-white rounded-xl shadow-2xl w-full max-w-md animate-fade-in" onClick={e => e.stopPropagation()}>
        <div className="card-header">
          <h3 className="font-semibold text-[13px] text-gray-900">Add Filter Column</h3>
        </div>
        <div className="p-3 border-b">
          <input
            type="text"
            value={search}
            onChange={e => setSearch(e.target.value)}
            placeholder="Search columns..."
            className="input"
            autoFocus
          />
        </div>
        <div className="max-h-64 overflow-y-auto">
          {availableColumns.length === 0 ? (
            <div className="p-6 text-center text-gray-400 text-[11px]">No more columns available</div>
          ) : (
            availableColumns.map(col => (
              <button
                key={col}
                onClick={() => { onAdd(col); onClose(); }}
                className="w-full text-left px-4 py-2.5 text-[11px] hover:bg-primary-50 flex items-center justify-between transition-colors"
              >
                <span>{col}</span>
              </button>
            ))
          )}
        </div>
        <div className="p-3 border-t bg-gray-50">
          <button onClick={onClose} className="btn-secondary btn-sm w-full">Cancel</button>
        </div>
      </div>
    </div>
  );
}

export default function MSAStockCalculationPage() {
  const [date, setDate] = useState('');
  const [rdc, setRdc] = useState('');
  const [sloc, setSloc] = useState('');
  const [table, setTable] = useState('');
  const [tableList, setTableList] = useState([]);
  const [filterColumns, setFilterColumns] = useState([]);
  const [filters, setFilters] = useState({});
  const [loading, setLoading] = useState(false);
  const [token, setToken] = useState('');
  const [saveStatus, setSaveStatus] = useState('');
  const [showAddColumnModal, setShowAddColumnModal] = useState(false);
  const [allAvailableColumns, setAllAvailableColumns] = useState([]);
  const [availableDates, setAvailableDates] = useState([]);
  const [dataDate, setDataDate] = useState(null);
  const [distinctValues, setDistinctValues] = useState({});
  const [presetName, setPresetName] = useState('msa_filter');
  const [savedPresets, setSavedPresets] = useState({});
  const [selectedPreset, setSelectedPreset] = useState('msa_filter');
  const [calculationResults, setCalculationResults] = useState(null);
  const [expandResults, setExpandResults] = useState(false);
  const [sequenceId, setSequenceId] = useState(null);
  const [sequencesList, setSequencesList] = useState([]);
  const [storageJob, setStorageJob] = useState(null); // Background job info
  
  const [cascadingFilterSelection, setCascadingFilterSelection] = useState({});
  const [autoStoreResults, setAutoStoreResults] = useState(true); // Auto-store checkbox
  const [missingRdcs, setMissingRdcs] = useState([]); // RDCs with no data for chosen SLOCs
  
  // Ref to track if initialization is complete (prevents auto-load on mount)
  const isInitializedRef = useRef(false);
  
  // Define cascading hierarchy column names
  const cascadingColumnHierarchy = ['ST_CD', 'SLOC', 'DIV'];
  
  // Check if any cascading columns are selected
  const hasCascadingColumns = filterColumns && filterColumns.some(col => 
    cascadingColumnHierarchy.includes(col)
  );
  
  // Get only cascading columns that are selected
  const selectedCascadingColumns = filterColumns?.filter(col => 
    cascadingColumnHierarchy.includes(col)
  ) || [];
  
  // Get non-cascading columns (any column NOT in the cascade hierarchy)
  const nonCascadingColumns = filterColumns?.filter(col => 
    !cascadingColumnHierarchy.includes(col)
  ) || [];
  
  // Build cascading filter hierarchy for selected columns
  const activeCascadingHierarchy = cascadingColumnHierarchy
    .filter(col => selectedCascadingColumns.includes(col))
    .map(col => {
      const labels = { ST_CD: 'Store Code', SLOC: 'Store Location', DIV: 'Division' };
      return { name: col, label: labels[col] };
    });

  // Helper function to download JSON as CSV
  const downloadCSV = (data, filename) => {
    if (!data || data.length === 0) return;
    const columns = Object.keys(data[0]);
    const csvRows = [
      columns.join(','),
      ...data.map(row => columns.map(col => `"${String(row[col] ?? '').replace(/"/g, '""')}"`).join(','))
    ].join('\n');
    const blob = new Blob([csvRows], { type: 'text/csv;charset=utf-8;' });
    const link = document.createElement('a');
    if (link.download !== undefined) {
      const url = URL.createObjectURL(blob);
      link.setAttribute('href', url);
      link.setAttribute('download', filename);
      link.style.visibility = 'hidden';
      document.body.appendChild(link);
      link.click();
      document.body.removeChild(link);
    }
  };

  // Helper function to export all calculation results
  const exportAllResults = () => {
    if (!calculationResults) {
      toast.error('No calculation results found. Please run calculation first.');
      return;
    }
    
    // Export each table
    if (calculationResults.msa) downloadCSV(calculationResults.msa, `ARS_MSA_TOTAL_${date}.csv`);
    if (calculationResults.msa_gen_clr) downloadCSV(calculationResults.msa_gen_clr, `ARS_MSA_GEN_ART_${date}.csv`);
    if (calculationResults.msa_gen_clr_var) downloadCSV(calculationResults.msa_gen_clr_var, `ARS_MSA_VAR_ART_${date}.csv`);
  };

  // Export specific table by container ID
  const exportIndividualTable = (containerId) => {
    if (!calculationResults) return;
    
    let tableData = [];
    let fileName = 'export.csv';
    
    if (containerId === 'msa-results') {
      tableData = calculationResults.msa;
      fileName = `ARS_MSA_TOTAL_${date}.csv`;
    } else if (containerId === 'gen-clr-results') {
      tableData = calculationResults.msa_gen_clr;
      fileName = `ARS_MSA_GEN_ART_${date}.csv`;
    } else if (containerId === 'variants-results') {
      tableData = calculationResults.msa_gen_clr_var;
      fileName = `ARS_MSA_VAR_ART_${date}.csv`;
    }
    
    downloadCSV(tableData, fileName);
  };

  // Attach export function to window so it can be called from dynamic HTML
  useEffect(() => {
    window.exportIndividualTable = exportIndividualTable;
    return () => {
      delete window.exportIndividualTable;
    };
  }, [calculationResults, date]);

  // Populate tables when calculation results are available and drawer is expanded
  useEffect(() => {
    if (calculationResults && expandResults) {
      console.log('📊 Populating tables now that they are in DOM...');
      // Use setTimeout to ensure DOM is fully updated
      setTimeout(() => {
        displayTable('msa-results', calculationResults.msa, 'MSA Total (ARS_MSA_TOTAL)');
        displayTable('gen-clr-results', calculationResults.msa_gen_clr, 'Generated Articles (ARS_MSA_GEN_ART)');
        displayTable('variants-results', calculationResults.msa_gen_clr_var, 'Variant Articles (ARS_MSA_VAR_ART)');
      }, 0);
    }
  }, [calculationResults, expandResults]);

  // Initialize: Load columns and dates from MSA view + Load presets from localStorage
  useEffect(() => {
    console.log('🚀 Initializing MSA page...');
    
    // Load filter configs from API
    msaAPI.getColumns()
      .then(res => {
        console.log('✅ Full API Response:', res);
        
        // Extract data from nested response structure
        let datesList = [];
        let columnsList = [];
        let configsList = [];
        
        if (res.data && res.data.data) {
          datesList = res.data.data.dates || [];
          columnsList = res.data.data.columns || [];
          configsList = res.data.data.filter_configs || [];
          if (res.data.data.data_date) setDataDate(res.data.data.data_date);
        } else if (res.data) {
          console.warn('⚠️ Unexpected response structure:', res.data);
        }
        
        console.log('📅 Dates from API:', datesList);
        console.log('📊 Columns from API:', columnsList);
        console.log('📋 Filter Configs from API:', configsList);
        
        // Load filter configs from API
        if (configsList && configsList.length > 0) {
          const presetsObj = {};
          configsList.forEach(config => {
            presetsObj[config.name] = {
              id: config.id,
              name: config.name,
              created_at: config.created_at
            };
          });
          setSavedPresets(presetsObj);
          console.log('✅ Loaded filter configs:', Object.keys(presetsObj));
          
          // Auto-select first config
          if (configsList.length > 0) {
            setSelectedPreset(configsList[0].name);
            console.log('🎯 Auto-selected first config:', configsList[0].name);
          }
        }
        
        // Set columns
        if (columnsList.length > 0) {
          setAllAvailableColumns(columnsList);
          console.log('✅ Set columns:', columnsList.length);
        } else {
          console.warn('⚠️ No columns returned');
        }
        
        // Set dates and auto-select previous day (yesterday)
        if (datesList.length > 0) {
          setAvailableDates(datesList);
          // Calculate yesterday (previous day)
          const now = new Date();
          now.setDate(now.getDate() - 1); // Set to previous day
          const yesterday = `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, '0')}-${String(now.getDate()).padStart(2, '0')}`;
          
          // Check if yesterday exists in the dates list, if not use first available date
          const selectedDate = datesList.includes(yesterday) ? yesterday : datesList[0];
          setDate(selectedDate);
          console.log(`✅ Set dates (${datesList.length}), auto-selected yesterday:`, selectedDate);
        } else {
          console.warn('⚠️ No dates returned, using fallback');
          // Fallback: use yesterday's date (previous day)
          const now = new Date();
          now.setDate(now.getDate() - 1); // Set to previous day
          const yesterday = `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, '0')}-${String(now.getDate()).padStart(2, '0')}`;
          setAvailableDates([yesterday]);
          setDate(yesterday);
          console.log('📅 Using fallback date (yesterday):', yesterday);
        }
      })
      .catch(err => {
        console.error('❌ Error loading MSA columns:', err);
        console.error('Error details:', err.response?.data || err.message);
        
        // Fallback behavior: use yesterday (previous day)
        const now = new Date();
        now.setDate(now.getDate() - 1); // Set to previous day
        const fallbackDate = `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, '0')}-${String(now.getDate()).padStart(2, '0')}`;
        setAvailableDates([fallbackDate]);
        setDate(fallbackDate);
        
        // Try to show error to user
        console.warn('⚠️ Using fallback date due to API error');
        
        // Mark initialization as complete
        isInitializedRef.current = true;
        console.log('✅ Initialization complete, will now auto-load presets');
      })
      .then(() => {
        // Mark initialization as complete (success path)
        if (!isInitializedRef.current) {
          isInitializedRef.current = true;
          console.log('✅ Initialization complete (success), will now auto-load presets');
        }
      });
  }, []);

  // Handle filter column selection
  const handleFilterChange = (col, value) => {
    setFilters(f => {
      const current = f[col] || [];
      if (!Array.isArray(current)) {
        return { ...f, [col]: [value] };
      }
      // Toggle: add if not present, remove if present
      if (current.includes(value)) {
        return { ...f, [col]: current.filter(v => v !== value) };
      } else {
        return { ...f, [col]: [...current, value] };
      }
    });
  };

  // Toggle filter value (for tags)
  const handleToggleFilterValue = (col, value) => {
    handleFilterChange(col, value);
  };

  // Handle cascading filter changes
  const handleCascadingFilterSelectionChange = (updatedSelection) => {
    console.log('🔗 Cascading filter selection changed:', updatedSelection);
    setCascadingFilterSelection(updatedSelection);
    
    // Merge cascading filters into the main filter state
    const mergedFilters = { ...filters };
    
    // Add cascading filter selections
    Object.entries(updatedSelection).forEach(([col, values]) => {
      if (values && values.length > 0) {
        mergedFilters[col] = values;
      } else {
        // Remove if empty
        delete mergedFilters[col];
      }
    });
    
    setFilters(mergedFilters);
    console.log('📦 Merged filters:', mergedFilters);
  };

  // Save filter preset
  const handleSavePreset = () => {
    if (!presetName.trim()) {
      toast.error('Please enter a preset name');
      return;
    }
    
    console.log(`💾 Saving preset to backend: ${presetName}`);
    setLoading(true);
    
    const threshold = parseInt(document.getElementById('msa-threshold')?.value || 25);
    
    // Call backend to save config
    msaAPI.saveConfig({
      name: presetName,
      filter_columns: filterColumns,
      filters: filters,
      sql_agg: threshold
    })
      .then(res => {
        console.log(`✅ Saved config to backend:`, res.data.data);
        
        // Update local presets list
        const updatedPresets = { 
          ...savedPresets, 
          [presetName]: {
            id: res.data.data.id || Math.random(),
            name: presetName,
            filter_columns: filterColumns,
            filters: filters,
            created_at: new Date().toISOString()
          }
        };
        setSavedPresets(updatedPresets);
        
        // Auto-select the newly saved preset
        setSelectedPreset(presetName);
        
        console.log(`📦 All presets now:`, Object.keys(updatedPresets));
        toast.success(`Preset "${presetName}" saved successfully`);
        setLoading(false);
      })
      .catch(err => {
        console.error(`❌ Error saving preset "${presetName}":`, err);
        setLoading(false);
        toast.error(`Error saving preset: ${err.response?.data?.detail || err.message}`);
      });
  };

  // Auto-load preset when selectedPreset changes after initialization is complete
  useEffect(() => {
    // Only auto-load if:
    // 1. Initialization is complete (isInitializedRef.current = true)
    // 2. selectedPreset is set
    // 3. savedPresets are loaded  
    if (isInitializedRef.current && selectedPreset && Object.keys(savedPresets).length > 0) {
      console.log(`🔄 Auto-loading preset: "${selectedPreset}"`);
      handleLoadPreset();
    }
  }, [selectedPreset]); // Only run when selectedPreset changes

  // Load filter preset from backend data
  const handleLoadPreset = () => {
    if (!selectedPreset || !savedPresets[selectedPreset]) {
      toast.error('Please select a valid preset');
      return;
    }
    
    console.log(`📂 Loading preset from backend: ${selectedPreset}`);
    setLoading(true);
    
    // Call backend to get full config
    msaAPI.loadConfig(selectedPreset)
      .then(res => {
        const configData = res.data.data;
        
        console.log(`✅ Loaded from backend:`, configData);
        console.log(`   filter_columns: ${JSON.stringify(configData.filter_columns)}`);
        console.log(`   filters: ${JSON.stringify(configData.filters)}`);
        
        // Store loaded config in a temporary variable to check later
        const loadedColumns = configData.filter_columns || [];
        const loadedFilters = configData.filters || {};
        const cascadingColumnHierarchy = ['ST_CD', 'SLOC', 'DIV'];
        
        // Separate cascading and normal filters
        const loadedCascadingSelection = {};
        const loadedNormalFilters = {};
        
        Object.entries(loadedFilters).forEach(([col, values]) => {
          if (cascadingColumnHierarchy.includes(col)) {
            loadedCascadingSelection[col] = values || [];
          } else {
            loadedNormalFilters[col] = values || [];
          }
        });
        
        console.log(`🔗 Cascading selections:`, loadedCascadingSelection);
        console.log(`📝 Normal filters:`, loadedNormalFilters);
        
        // Update state - restore both cascading and normal filters
        setFilterColumns(loadedColumns);
        setFilters(loadedFilters); // Keep all filters for Apply Filters button
        setCascadingFilterSelection(loadedCascadingSelection); // Restore cascading state
        
        // Also update the window object for debugging
        window.__loadedPresetConfig = { 
          columns: loadedColumns, 
          filters: loadedFilters,
          cascadingSelection: loadedCascadingSelection 
        };
        console.log(`💾 Stored in window.__loadedPresetConfig`);
        
        // Fetch distinct values for each loaded column
        if (loadedColumns && Array.isArray(loadedColumns)) {
          console.log(`🔄 Fetching distinct values for ${loadedColumns.length} columns...`);
          
          setDistinctValues(prev => {
            const updated = { ...prev };
            loadedColumns.forEach(col => {
              updated[col] = []; // Initialize with empty array
            });
            return updated;
          });
          
          // Fetch distinct values for each column
          const distinctPromises = loadedColumns.map(col =>
            // For cascading columns, pass parent filters
            (() => {
              const parentFilters = {};
              const colIndex = cascadingColumnHierarchy.indexOf(col);
              
              if (colIndex > 0 && loadedCascadingSelection) {
                for (let i = 0; i < colIndex; i++) {
                  const parentCol = cascadingColumnHierarchy[i];
                  if (loadedCascadingSelection[parentCol]?.length > 0) {
                    parentFilters[parentCol] = loadedCascadingSelection[parentCol];
                  }
                }
              }
              
              const filtersParam = Object.keys(parentFilters).length > 0 
                ? JSON.stringify(parentFilters) 
                : null;
              
              return msaAPI.getDistinct(col, date, filtersParam);
            })()
              .then(res => {
                const values = res.data?.data?.values || [];
                console.log(`  ✅ ${col}: ${values.length} values`);
                setDistinctValues(prev => ({ ...prev, [col]: values }));
              })
              .catch(err => {
                console.error(`  ❌ ${col}: Error - ${err.message}`);
                setDistinctValues(prev => ({ ...prev, [col]: [] }));
              })
          );
          
          // Wait for all distinct values to load
          Promise.all(distinctPromises).then(() => {
            console.log(`✅ All distinct values loaded!`);
          });
        }
        
        if (configData.sql_agg) {
          const thresholdInput = document.getElementById('msa-threshold');
          if (thresholdInput) {
            thresholdInput.value = configData.sql_agg;
            console.log(`⚙️ Threshold set to: ${configData.sql_agg}`);
          }
        }
        
        setLoading(false);
        
        // Verify state was updated
        setTimeout(() => {
          console.log(`✅ State verification after load:`);
          console.log(`   filterColumns: ${JSON.stringify(loadedColumns)}`);
          console.log(`   filters: ${JSON.stringify(loadedFilters)}`);
          console.log(`   cascadingFilterSelection: ${JSON.stringify(loadedCascadingSelection)}`);
          toast.success(`Preset loaded: ${loadedColumns.length} columns, ${Object.keys(loadedFilters).length} filters applied.`, { duration: 4000 });
        }, 100);
      })
      .catch(err => {
        console.error(`❌ Error loading preset:`, err);
        setLoading(false);
        toast.error(`Error: ${err.response?.data?.message || err.message}`);
      });
  };

  // Delete filter preset from backend
  const handleDeletePreset = () => {
    if (!selectedPreset || !savedPresets[selectedPreset]) {
      toast.error('Please select a valid preset');
      return;
    }
    if (!window.confirm(`Delete preset "${selectedPreset}"?`)) return;
    
    // TODO: Delete from backend
    const updatedPresets = { ...savedPresets };
    delete updatedPresets[selectedPreset];
    setSavedPresets(updatedPresets);
    
    setSelectedPreset('');
    console.log(`🗑️ Deleted preset: ${selectedPreset}`);
    toast.success(`Preset "${selectedPreset}" deleted`);
  };

  const handleAddFilterColumn = (col) => {
    if (!filterColumns.includes(col)) {
      setFilterColumns(prev => [...prev, col]);
      console.log(`📋 Added filter column: ${col}`);
      
      // Initialize with "Loading..." state
      setDistinctValues(prev => ({ ...prev, [col]: [] }));
      
      // Fetch distinct values for this column with timeout
      console.log(`🔄 Fetching distinct values for ${col}...`);
      const timeoutId = setTimeout(() => {
        console.warn(`⏱️ Timeout loading values for ${col}`);
        setDistinctValues(prev => ({ ...prev, [col]: [] }));
      }, 10000); // 10 second timeout
      
      msaAPI.getDistinct(col, date)
        .then(res => {
          clearTimeout(timeoutId);
          console.log(`📨 Full API response for ${col}:`, res);
          console.log(`📦 Response data structure:`, res.data);
          
          const values = res.data?.data?.values || res.data?.values || [];
          console.log(`✅ Loaded ${values.length} distinct values for ${col}:`, values);
          
          if (values.length === 0) {
            console.warn(`⚠️ No values returned for ${col}, checking response...`);
          }
          
          // Store values in state for dropdown
          setDistinctValues(prev => ({ ...prev, [col]: values }));
        })
        .catch(err => {
          clearTimeout(timeoutId);
          console.error(`❌ Error loading values for ${col}:`, err.message);
          console.error(`Error response:`, err.response?.data);
          // On error, set empty array so dropdown still works
          setDistinctValues(prev => ({ ...prev, [col]: [] }));
        });
    }
  };

  const handleRemoveFilterColumn = (col) => {
    setFilterColumns(prev => prev.filter(c => c !== col));
    setFilters(f => { const n = { ...f }; delete n[col]; return n; });
    setDistinctValues(prev => { const n = { ...prev }; delete n[col]; return n; });
  };

  // Calculate MSA - call new calculate endpoint
  const handleCalculateMSA = () => {
    // Use SLOCs from filters (auto-apply all filtered SLOCs)
    const slocList = filters['SLOC'] || cascadingFilterSelection['SLOC'] || [];

    if (!slocList || slocList.length === 0) {
      toast.error('Please select SLOC in filters first');
      return;
    }

    setLoading(true);
    setMissingRdcs([]); // Clear previous RDC coverage warning
    const threshold = parseInt(document.getElementById('msa-threshold')?.value || 25);

    console.log('🧮 Calculating MSA:', { slocs: slocList, threshold, date, filters, autoStore: autoStoreResults });

    msaAPI.calculate({
      slocs: slocList,
      threshold,
      date,
      filters,
      auto_store_results: autoStoreResults
    })
      .then(res => {
        console.log('✅ MSA Calculated:', res.data);
        const result = res.data.data;
        setCalculationResults(result); // Store for export
        setExpandResults(true); // Auto-expand results wrapper
        
        // Extract and display sequence ID (if auto-store enabled)
        const seqId = result.sequence_id;
        
        if (autoStoreResults && seqId) {
          // ✅ AUTO-STORE ENABLED: Show sequence ID and update list
          setSequenceId(seqId);
          
          // Store job info if present
          if (result.storage_job) {
            setStorageJob(result.storage_job);
            console.log(`📋 Storage job queued: ${result.storage_job.job_id} (Position: ${result.storage_job.position_in_queue})`);
          }
          
          console.log(`📦 Auto-stored with sequence ID: ${seqId}`);
          toast.success(`✅ Auto-saved! (Sequence: ${seqId}) - Storage in progress...`, { duration: 4000 });
          
          // Load the latest sequences list
          msaAPI.getStoredSequences(10)
            .then(seqRes => {
              setSequencesList(seqRes.data.data.sequences || []);
              console.log(`📋 Loaded ${seqRes.data.data.sequences.length} stored sequences`);
            })
            .catch(err => console.warn('Could not load sequences list:', err));
          
          setSaveStatus(`✅ Calculation auto-saved! Sequence: ${seqId} (Data storage in progress...)`);
        } else if (!autoStoreResults) {
          // ❌ AUTO-STORE DISABLED: Don't show sequence, user must click Save button
          setSequenceId(null);
          setStorageJob(null);
          console.log(`📋 Auto-store disabled - user must click "Save to Database" button`);
          toast.success(`✅ Calculation complete! Click "💾 Save to Database" to store.`, { duration: 5000 });
          setSaveStatus(`✅ Calculation complete! Click "Save to Database" to store results.`);
        } else {
          // Fallback
          toast.success('✅ Calculation complete!', { duration: 4000 });
          setSaveStatus(`✅ Calculation complete! Rows: ${result.row_counts.msa}`);
        }
        
        // Warn about RDCs that had no data for the selected SLOCs
        const newMissing = result.missing_rdcs || [];
        setMissingRdcs(newMissing);
        if (newMissing.length > 0) {
          const missingList = newMissing.join(', ');
          const coveredList = (result.covered_rdcs || []).join(', ') || 'none';
          toast.error(
            `RDC(s) ${missingList} have NO data for the selected SLOC(s) — they are absent from MSA results. Only ${coveredList} contributed data. Select SLOCs that exist for all RDCs.`,
            { duration: 8000 }
          );
          console.warn(`[msa] Missing RDCs: ${missingList} | Covered: ${coveredList}`);
        }

        setLoading(false);

        // Scroll to results with a small delay to ensure DOM is updated
        setTimeout(() => {
          const resultsElement = document.querySelector('[id*="msa-results"]');
          if (resultsElement) {
            resultsElement.scrollIntoView({ behavior: 'smooth', block: 'start' });
            console.log('📍 Scrolled to results');
          }
        }, 300);
      })
      .catch(err => {
        console.error('❌ Error calculating MSA:', err);
        const timeoutMsg = err.code === 'ECONNABORTED'
          ? 'Calculation timed out. Try with fewer filters or a smaller date range.'
          : err.response?.data?.detail || err.message;
        setSaveStatus(`❌ Error: ${timeoutMsg}`);
        setLoading(false);
      });
  };

  // Display table results with proper framing
  const displayTable = (containerId, data, title) => {
    const container = document.getElementById(containerId);
    if (!container || !data || data.length === 0) {
      console.warn(`⚠️ Cannot display table ${containerId}:`, !container ? 'container not found' : 'no data');
      return;
    }

    const columns = data.length > 0 ? Object.keys(data[0]) : [];
    console.log(`📊 Displaying table ${containerId} with ${data.length} rows and ${columns.length} columns`);
    
    // Set inline style to ensure visibility, even if parent is hidden
    container.style.display = 'block';
    container.style.visibility = 'visible';
    container.className = 'card mb-4 animate-fade-in';
    
    let html = `
      <div class="card-header flex justify-between items-center">
        <div>
          <h2 class="font-semibold text-[13px] text-gray-900">${title}</h2>
          <p class="text-[10px] text-gray-500 mt-0.5">Total records: ${data.length}</p>
        </div>
        <button 
          onclick="window.exportIndividualTable('${containerId}')"
          class="btn-secondary btn-sm"
        >
          <svg xmlns="http://www.w3.org/2000/svg" width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v4"/><polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/></svg>
          Export CSV
        </button>
      </div>
      <div class="card-body">
        <div class="overflow-x-auto border border-gray-200 rounded-lg">
          <table class="w-full text-[11px]">
            <thead class="bg-gray-50 border-b-2 border-gray-200">
              <tr>
                ${columns.map(col => `<th class="px-3 py-2 text-left font-semibold text-gray-700 whitespace-nowrap">${col}</th>`).join('')}
              </tr>
            </thead>
            <tbody>
              ${data.slice(0, 100).map((row, idx) => `
                <tr class="border-b border-gray-100 hover:bg-gray-50 transition-colors ${idx % 2 === 0 ? 'bg-white' : 'bg-gray-50/30'}">
                  ${columns.map(col => `<td class="px-3 py-2 text-gray-700">${row[col] !== null && row[col] !== undefined ? row[col] : '-'}</td>`).join('')}
                </tr>
              `).join('')}
            </tbody>
          </table>
        </div>
        ${data.length > 100 ? `<div class="mt-3 p-2.5 bg-primary-50 border-l-4 border-primary-500 rounded text-[10px] text-primary-800"><strong>Showing 100 of ${data.length} rows</strong> - Export to see all</div>` : ''}
      </div>
    `;
    
    container.innerHTML = html;
  };

  // Save MSA results to database
  const handleSave = () => {
    if (!calculationResults) {
      toast.error('Please calculate MSA first');
      return;
    }

    console.log('💾 Saving MSA results to database...');
    setLoading(true);

    const threshold = parseInt(document.getElementById('msa-threshold')?.value || 25);

    msaAPI.save({
      // Calculation results
      msa: calculationResults.msa || [],
      msa_gen_clr: calculationResults.msa_gen_clr || [],
      msa_gen_clr_var: calculationResults.msa_gen_clr_var || [],
      row_counts: calculationResults.row_counts || {},
      
      // Filter information
      date_filter: date || '',
      filter_columns: filterColumns || [],
      filters: filters || {},
      threshold: threshold,
      slocs: Array.isArray(sloc) ? sloc : [sloc]
    })
      .then(res => {
        console.log('✅ Saved to database:', res.data);
        const seqId = res.data.data.sequence_id;
        setSequenceId(seqId);
        setSaveStatus(`✅ Saved to database! Sequence ID: ${seqId}`);
        toast.success(`✅ Results saved! (Sequence: ${seqId})`, { duration: 4000 });
        
        // Reload sequences list
        msaAPI.getStoredSequences(10)
          .then(seqRes => {
            setSequencesList(seqRes.data.data.sequences || []);
            console.log(`📋 Reloaded ${seqRes.data.data.sequences.length} sequences`);
          })
          .catch(err => console.warn('Could not reload sequences:', err));
        
        setLoading(false);
      })
      .catch(err => {
        console.error('❌ Error saving:', err);
        const errMsg = err.response?.data?.detail || err.message;
        setSaveStatus(`❌ Error: ${errMsg}`);
        toast.error(`Error saving: ${errMsg}`);
        setLoading(false);
      });
  };

  return (
    <div className="space-y-4">
      {/* Header */}
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-3">
          <div className="p-2 bg-primary-100 rounded-lg">
            <Calculator size={20} className="text-primary-600" />
          </div>
          <div>
            <h1 className="page-title">MSA Stock Calculation</h1>
            <p className="page-subtitle">Configure filters, select SLOC codes, and calculate MSA analysis</p>
          </div>
        </div>
      </div>

      {/* Data freshness alert */}
      {dataDate && (() => {
        const diffDays = (Date.now() - new Date(dataDate).getTime()) / 86_400_000
        const isOk = diffDays < 2
        const dateFmt = new Date(dataDate).toLocaleDateString('en-IN',{day:'2-digit',month:'short',year:'numeric'})
        return (
          <div className={`flex items-center gap-2.5 px-3.5 py-2 rounded-lg text-xs font-medium border ${isOk ? 'bg-emerald-50 border-emerald-200 text-emerald-700' : 'bg-red-50 border-red-200 text-red-700'}`}>
            {isOk ? <CheckCircle2 size={14}/> : <AlertTriangle size={14}/>}
            <span>
              <strong>ET_MSA_STK</strong> data date: <strong>{dateFmt}</strong>
              {isOk
                ? ' — Data is up to date.'
                : ` — Data is ${Math.floor(diffDays)} day${Math.floor(diffDays)>1?'s':''} old. Please update the source data.`}
            </span>
          </div>
        )
      })()}

      {/* Filter Configuration Card */}
      <div className="card">
        <div className="card-header flex items-center gap-2">
          <Filter size={16} className="text-primary-600" />
          <h2 className="font-semibold text-[13px] text-gray-900">Filter Configuration</h2>
        </div>
        
        <div className="card-body space-y-4">
          {/* Select filter columns */}
          <div>
            <label className="label mb-2">Select Filter Columns</label>
            <div className="flex flex-wrap gap-1.5 mb-3">
              {filterColumns && filterColumns.length > 0 ? (
                filterColumns.map(col => (
                  <button
                    key={col}
                    onClick={() => handleRemoveFilterColumn(col)}
                    className="inline-flex items-center gap-1 px-2.5 py-1 bg-primary-100 text-primary-700 rounded-full text-[10px] font-medium hover:bg-primary-200 transition-all"
                    type="button"
                  >
                    <span>{col}</span>
                    <X size={12} />
                  </button>
                ))
              ) : (
                <p className="text-gray-400 italic text-[11px]">No filter columns selected</p>
              )}
            </div>
            <button 
              className="btn-secondary btn-sm"
              onClick={() => setShowAddColumnModal(true)}
              disabled={!allAvailableColumns || allAvailableColumns.length === filterColumns.length}
              type="button"
            >
              <Plus size={12} /> Add Column
            </button>
            {showAddColumnModal && (
              <AddFilterColumnModal
                columns={allAvailableColumns || []}
                existingFilters={filterColumns || []}
                onAdd={handleAddFilterColumn}
                onClose={() => setShowAddColumnModal(false)}
              />
            )}
          </div>

          {/* Cascading Filters (if cascade columns selected) */}
          {hasCascadingColumns && (
            <div className="border-t pt-3">
              <p className="label mb-3">Cascading Filters (Multi-select)</p>
              <CascadingFilters
                filterHierarchy={activeCascadingHierarchy}
                date={date}
                selectedFilters={cascadingFilterSelection}
                onSelectionChange={handleCascadingFilterSelectionChange}
                className="w-full"
              />
            </div>
          )}

          {/* Normal Filters (for non-cascading columns) */}
          {nonCascadingColumns.length > 0 && (
            <div className={`space-y-3 ${hasCascadingColumns ? 'border-t pt-3' : 'border-t pt-3'}`}>
              {nonCascadingColumns.map(col => (
                <div key={col}>
                  <p className="label mb-1.5">Filter by {col}</p>
                  <div className="flex flex-wrap gap-1.5">
                    {distinctValues[col] && distinctValues[col].length > 0 ? (
                      distinctValues[col].map(val => (
                        <button
                          key={val}
                          onClick={() => handleToggleFilterValue(col, val)}
                          className={`inline-flex items-center gap-1 px-2.5 py-1 rounded-md text-[10px] font-medium transition-all ${
                            filters[col]?.includes(val)
                              ? 'bg-red-500 hover:bg-red-600 text-white shadow-sm'
                              : 'bg-gray-100 hover:bg-gray-200 text-gray-700 border border-gray-200'
                          }`}
                          type="button"
                        >
                          <span>{val}</span>
                          {filters[col]?.includes(val) && <X size={10} />}
                        </button>
                      ))
                    ) : (
                      <p className="text-gray-400 italic text-[11px]">Loading values...</p>
                    )}
                  </div>
                </div>
              ))}
            </div>
          )}
        </div>
      </div>

      {/* Date, Threshold & Presets Grid */}
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
        {/* Date & Threshold Selection Card */}
        <div className="card">
          <div className="card-header flex items-center gap-2">
            <Calendar size={16} className="text-primary-600" />
            <h2 className="font-semibold text-[13px] text-gray-900">Date & Threshold</h2>
          </div>

          <div className="card-body space-y-3">
            <div>
              <label className="label mb-2">Select Date</label>
              <input
                type="date"
                className="input"
                value={date}
                onChange={e => {
                  const newDate = e.target.value;
                  console.log('📅 Date selected:', newDate);
                  setDate(newDate);
                }}
              />
            </div>
            <div>
              <label className="label mb-2">Threshold (%)</label>
              <input
                type="number"
                id="msa-threshold"
                className="input"
                defaultValue={25}
                min={0}
                max={100}
                placeholder="25"
              />
            </div>
          </div>
        </div>

        {/* Filter Presets Card */}
        <div className="card">
          <div className="card-header flex items-center gap-2">
            <Save size={16} className="text-primary-600" />
            <h2 className="font-semibold text-[13px] text-gray-900">Filter Presets</h2>
          </div>
          
          <div className="card-body space-y-3">
            <div>
              <label className="label mb-1.5">Select Existing Config</label>
              <select 
                className="input"
                value={selectedPreset}
                onChange={e => {
                  console.log('📝 Preset selected:', e.target.value);
                  setSelectedPreset(e.target.value);
                }}
              >
                <option value="">-- No Preset --</option>
                {Object.keys(savedPresets).length > 0 ? (
                  Object.keys(savedPresets).map(name => (
                    <option key={name} value={name}>{name}</option>
                  ))
                ) : (
                  <option disabled>No presets saved yet</option>
                )}
              </select>
              {Object.keys(savedPresets).length > 0 && (
                <p className="text-[10px] text-gray-500 mt-1">Saved: <strong>{Object.keys(savedPresets).join(', ')}</strong></p>
              )}
            </div>

            <div>
              <label className="label mb-1.5">Config Name</label>
              <input 
                type="text"
                className="input"
                value={presetName}
                onChange={e => setPresetName(e.target.value)}
                placeholder="e.g., msa_filter"
              />
            </div>

            <div className="flex gap-1.5 pt-1">
              <button 
                className="btn-primary btn-sm flex-1"
                onClick={handleSavePreset}
                type="button"
              >
                <Save size={12} /> Save
              </button>
              <button 
                className="btn-secondary btn-sm flex-1"
                onClick={handleLoadPreset}
                disabled={!selectedPreset}
                type="button"
              >
                <Download size={12} /> Load
              </button>
              <button 
                className="btn-danger btn-sm"
                onClick={handleDeletePreset}
                disabled={!selectedPreset}
                type="button"
              >
                <Trash2 size={12} />
              </button>
            </div>
          </div>
        </div>
      </div>

      {/* Configuration Summary Section - Shows when filters are selected */}
      {filterColumns && filterColumns.length > 0 && Object.keys(filters).some(k => filters[k]?.length > 0) && (
        <div className="card border-l-4 border-l-primary-500">
          {/* <div className="card-header">
            <h2 className="font-semibold text-[13px] text-gray-900">📋 Active Filter Configuration</h2>
          </div> */}

          {/* <div className="card-body">
            <div className="grid grid-cols-1 md:grid-cols-3 gap-4 mb-4">
              {filterColumns.map(col => (
                <div key={col}>
                  <div className="text-[10px] font-semibold text-gray-600 uppercase tracking-wide mb-1.5">{col}</div>
                  {filters[col] && filters[col].length > 0 ? (
                    <div className="flex flex-wrap gap-1">
                      {filters[col].map((val, idx) => (
                        <span
                          key={idx}
                          className="inline-flex items-center bg-primary-500 text-white px-2 py-0.5 rounded-full text-[9px] font-medium"
                        >
                          {val}
                        </span>
                      ))}
                    </div>
                  ) : (
                    <span className="text-gray-400 italic text-[10px]">No selection</span>
                  )}
                </div>
              ))}
            </div>

            <div className="pt-3 border-t border-gray-100 flex items-center justify-between">
              <div className="flex items-center gap-2">
                <span className="inline-flex items-center justify-center w-5 h-5 rounded-full bg-emerald-500 text-white text-[9px] font-bold">✓</span>
                <span className="text-[11px] text-gray-600">Status: <span className="text-emerald-600 font-semibold">Ready for Calculation</span></span>
              </div>
              <p className="text-[10px] text-gray-500">📊 <strong>{Object.values(filters).filter(v => v?.length > 0).length}</strong> filters active</p>
            </div>
          </div> */}
        </div>
      )}

      {/* MSA Calculation Section - Calculate Button & Auto-save side by side */}
      <div className="card">
        <div className="card-header">
          <h2 className="font-semibold text-[13px] text-gray-900">🧮 MSA Calculation</h2>
        </div>

        <div className="card-body">
          {/* Show selected SLOCs from filter */}
          {(filters['SLOC']?.length > 0 || cascadingFilterSelection['SLOC']?.length > 0) && (() => {
            const selectedSlocs = filters['SLOC'] || cascadingFilterSelection['SLOC'] || []
            const fewSlocs = selectedSlocs.length < 3
            return (
              <div className={`mb-4 p-3 rounded-lg border ${fewSlocs ? 'bg-amber-50 border-amber-300' : 'bg-primary-50 border-primary-200'}`}>
                <div className="flex items-center justify-between mb-1.5">
                  <div className={`text-[10px] font-semibold uppercase ${fewSlocs ? 'text-amber-700' : 'text-primary-700'}`}>
                    SLOCs to Calculate ({selectedSlocs.length})
                  </div>
                  {fewSlocs && (
                    <span className="text-[9px] font-bold text-amber-700 bg-amber-100 px-2 py-0.5 rounded-full border border-amber-300">
                      ⚠ Only {selectedSlocs.length} SLOC{selectedSlocs.length > 1 ? 's' : ''} — MSA will cover limited warehouses. Use "Select All" to include all SLOCs.
                    </span>
                  )}
                </div>
                <div className="flex flex-wrap gap-1.5">
                  {selectedSlocs.map(slocCode => (
                    <span key={slocCode} className={`inline-flex items-center px-2 py-0.5 rounded-full text-[10px] font-medium ${fewSlocs ? 'bg-amber-500 text-white' : 'bg-primary-500 text-white'}`}>
                      {slocCode}
                    </span>
                  ))}
                </div>
              </div>
            )
          })()}

          {/* Missing RDC warning banner */}
          {missingRdcs.length > 0 && (
            <div className="mb-4 p-3 rounded-lg border bg-red-50 border-red-300 flex items-start gap-2">
              <AlertTriangle size={14} className="text-red-600 mt-0.5 shrink-0" />
              <div>
                <div className="text-[10px] font-bold text-red-700 uppercase mb-0.5">RDC Coverage Warning</div>
                <div className="text-[11px] text-red-700">
                  <strong>{missingRdcs.join(', ')}</strong> {missingRdcs.length === 1 ? 'has' : 'have'} no data for the selected SLOC(s) and {missingRdcs.length === 1 ? 'is' : 'are'} absent from MSA results.
                  Select SLOCs that exist in all chosen RDCs, or use "Select All" SLOCs.
                </div>
              </div>
            </div>
          )}

          {/* Calculate Button & Auto-save side by side */}
          <div className="flex items-center gap-3">
            <button
              className="btn-primary flex-1"
              onClick={() => {
                const selectedSlocs = filters['SLOC'] || cascadingFilterSelection['SLOC'] || [];
                console.log('🧮 Calculate MSA clicked. SLOCs:', selectedSlocs, 'Threshold:', document.getElementById('msa-threshold')?.value);
                if (!selectedSlocs || selectedSlocs.length === 0) {
                  console.warn('⚠️ No SLOC selected in filters');
                  toast.error('Please select SLOC in filters first');
                  return;
                }
                // Set sloc state and trigger calculation
                setSloc(selectedSlocs);
                handleCalculateMSA();
              }}
              disabled={loading || (!filters['SLOC']?.length && !cascadingFilterSelection['SLOC']?.length)}
              type="button"
            >
              {loading ? '⏳ Calculating...' : '🧮 Calculate MSA'}
            </button>

            <label className="flex items-center gap-2 p-3 bg-gray-50 rounded-lg hover:bg-gray-100 cursor-pointer transition-colors border border-gray-200 whitespace-nowrap">
              <input
                type="checkbox"
                checked={autoStoreResults}
                onChange={(e) => setAutoStoreResults(e.target.checked)}
                className="w-4 h-4 cursor-pointer"
              />
              <span className="text-[11px] font-medium text-gray-700">
                Auto-save
              </span>
            </label>
          </div>
        </div>
      </div>

<div className="space-y-4">
        {/* Results Sections Wrapper */}
        {calculationResults && (
          <div className="space-y-4">
            {/* Results Header */}
            <div className="card border-l-4 border-l-emerald-500 bg-emerald-50/50">
              <div className="card-header bg-gradient-to-r from-emerald-50 to-white flex items-center justify-between cursor-pointer hover:bg-emerald-100/30 transition-colors" onClick={() => setExpandResults(!expandResults)}>
                <h2 className="font-semibold text-[13px] text-emerald-900">📊 Calculation Results</h2>
                <ChevronDown size={16} className={`text-emerald-600 transition-transform ${expandResults ? 'rotate-180' : ''}`} />
              </div>
              <div className="card-body space-y-3">
                {/* Sequence ID Section */}
                {sequenceId && (
                  <div className="p-3 bg-blue-50 rounded-lg border-l-4 border-l-blue-500">
                    <div className="flex items-center justify-between">
                      <div>
                        <div className="text-[10px] font-semibold text-blue-700 uppercase">Database Sequence ID</div>
                        <div className="text-[15px] font-bold text-blue-900 font-mono mt-0.5">#{sequenceId}</div>
                        <p className="text-[9px] text-blue-600 mt-1">Results stored in database and accessible via API</p>
                      </div>
                      <button
                        onClick={() => {
                          const link = document.createElement('a');
                          link.href = `#sequence-${sequenceId}`;
                          link.textContent = 'View';
                          console.log(`📂 Sequence ${sequenceId} - View stored data`);
                          toast.info(`View sequence ${sequenceId} results in the stored sequences list below`);
                        }}
                        className="btn-primary btn-sm"
                      >
                        📋 View in Database
                      </button>
                    </div>
                  </div>
                )}
                
                {/* Storage Job Status Section */}
                {storageJob && (
                  <div className="p-3 bg-amber-50 rounded-lg border-l-4 border-l-amber-500">
                    <div className="flex items-center justify-between">
                      <div>
                        <div className="text-[10px] font-semibold text-amber-700 uppercase">Background Data Storage</div>
                        <div className="text-[13px] font-medium text-amber-900 mt-0.5">
                          🔄 {storageJob.status === 'queued' ? `Queued (Position: ${storageJob.position_in_queue})` : storageJob.status.toUpperCase()}
                        </div>
                        <p className="text-[9px] text-amber-600 mt-1">
                          {storageJob.total_rows ? `${storageJob.total_rows.toLocaleString()} rows queued for storage` : 'Processing...'}
                        </p>
                      </div>
                      <button
                        onClick={() => {
                          console.log(`📋 Checking job status: ${storageJob.job_id}`);
                          toast.info(`Job ID: ${storageJob.job_id}\nStatus: ${storageJob.status}`);
                        }}
                        className="btn-secondary btn-sm"
                      >
                        🔍 Job Status
                      </button>
                    </div>
                  </div>
                )}
                
                {/* Results Summary Grid */}
                <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
                  {calculationResults.msa && calculationResults.msa.length > 0 && (
                    <div className="p-3 bg-white rounded-lg border border-emerald-200">
                      <div className="text-[10px] font-semibold text-gray-600 uppercase">MSA Total</div>
                      <div className="text-[16px] font-bold text-emerald-600 mt-1">{calculationResults.msa.length}</div>
                      <p className="text-[10px] text-gray-500 mt-1">records processed</p>
                    </div>
                  )}
                  {calculationResults.msa_gen_clr && calculationResults.msa_gen_clr.length > 0 && (() => {
                    const rows = calculationResults.msa_gen_clr;
                    const optCount = new Set(rows.map(r =>
                      `${r.MAJ_CAT ?? r.maj_cat ?? ''}|${r.GEN_ART_NUMBER ?? r.gen_art_number ?? ''}|${r.CLR ?? r.clr ?? ''}`
                    )).size;
                    const qtySum = rows.reduce((s, r) => s + (parseFloat(r.FNL_Q ?? r.fnl_q ?? 0) || 0), 0);
                    return (
                      <div className="p-3 bg-white rounded-lg border border-blue-200">
                        <div className="text-[10px] font-semibold text-gray-600 uppercase">Generated Articles (ARS_MSA_GEN_ART)</div>
                        <div className="text-[16px] font-bold text-blue-600 mt-1">
                          {optCount.toLocaleString()} <span className="text-[10px] font-normal text-gray-500">OPTs</span>
                          <span className="mx-2 text-gray-300">·</span>
                          {Math.round(qtySum).toLocaleString()} <span className="text-[10px] font-normal text-gray-500">qty</span>
                        </div>
                        <p className="text-[10px] text-gray-500 mt-1">
                          {rows.length.toLocaleString()} rows · distinct MAJ+GEN+CLR · Σ FNL_Q
                        </p>
                      </div>
                    );
                  })()}
                  {calculationResults.msa_gen_clr_var && calculationResults.msa_gen_clr_var.length > 0 && (
                    <div className="p-3 bg-white rounded-lg border border-purple-200">
                      <div className="text-[10px] font-semibold text-gray-600 uppercase">Variant Articles</div>
                      <div className="text-[16px] font-bold text-purple-600 mt-1">{calculationResults.msa_gen_clr_var.length}</div>
                      <p className="text-[10px] text-gray-500 mt-1">variant article records</p>
                    </div>
                  )}
                </div>
              </div>
            </div>
          </div>
        )}

        {/* Result Tables Container - ALWAYS in DOM (outside conditional), shown/hidden with inline styles */}
        <div style={{ display: calculationResults && expandResults ? 'block' : 'none' }} className="border-t-2 border-gray-100 pt-4 space-y-4 transition-all duration-300">
          <div id="msa-results"></div>
          <div id="gen-clr-results"></div>
          <div id="variants-results"></div>
        </div>

        {/* Export & Save Actions */}
        <div className="flex gap-2 sticky bottom-4">
          {calculationResults && (
            <button 
              className="btn-secondary flex-1"
              onClick={exportAllResults}
              type="button"
            >
              <Download size={14} /> Export All Results as CSV
            </button>
          )}
          {/* Conditional Save Button: Only show when auto-save is DISABLED */}
          {!autoStoreResults && (
            <button 
              className="btn-success flex-1"
              onClick={() => {
                console.log('💾 Save to Database clicked');
                if (!calculationResults) {
                  console.warn('⚠️ No calculation results');
                  toast.error('Please calculate MSA first');
                  return;
                }
                handleSave();
              }}
              disabled={!calculationResults || loading}
              type="button"
            >
              {loading ? '⏳ Saving...' : '💾 Save to Database'}
            </button>
          )}
        </div>
      </div>

      {/* Status Messages */}
      {saveStatus && (
        <div className={`card border-l-4 ${saveStatus.includes('✅') || saveStatus.includes('Successfully') 
          ? 'border-l-emerald-500 bg-emerald-50/50' 
          : 'border-l-red-500 bg-red-50/50'
        }`}>
          <div className="card-body">
            <div className={`font-semibold text-[11px] ${saveStatus.includes('✅') || saveStatus.includes('Successfully')
              ? 'text-emerald-700' 
              : 'text-red-700'
            }`}>
              {saveStatus}
            </div>
            {token && <div className="text-[10px] text-emerald-600 mt-1.5">✅ Token: {token}</div>}
          </div>
        </div>
      )}

      {/* Stored Sequences History Section */}
      <div className="card border-l-4 border-l-purple-500 bg-purple-50/50">
        <div className="card-header bg-gradient-to-r from-purple-50 to-white flex items-center justify-between cursor-pointer hover:bg-purple-100/30 transition-colors">
          <h2 className="font-semibold text-[13px] text-purple-900">📦 Stored Calculation Sequences</h2>
          <button
            onClick={() => {
              msaAPI.getStoredSequences(10)
                .then(res => {
                  setSequencesList(res.data.data.sequences || []);
                  console.log('✅ Refreshed sequences list');
                  toast.success('Sequences list updated');
                })
                .catch(err => {
                  console.error('Error loading sequences:', err);
                  toast.error('Could not load sequences');
                });
            }}
            className="btn-secondary btn-sm text-[10px] px-2 py-1"
            title="Refresh stored sequences list"
          >
            🔄 Refresh
          </button>
        </div>
        <div className="card-body">
          {sequencesList && sequencesList.length > 0 ? (
            <div className="space-y-2 max-h-60 overflow-y-auto">
              {sequencesList.map((seq, idx) => (
                <div key={seq.sequence_id || idx} className="p-2 bg-white rounded border border-purple-200 hover:border-purple-400 transition-colors">
                  <div className="flex items-center justify-between">
                    <div className="flex-1">
                      <div className="flex items-center gap-2">
                        <span className="font-mono font-bold text-purple-700">#{seq.sequence_id}</span>
                        <span className="text-[10px] bg-purple-100 text-purple-700 px-1.5 py-0.5 rounded">
                          {new Date(seq.calculation_date).toLocaleString()}
                        </span>
                      </div>
                      <div className="text-[10px] text-gray-600 mt-1">
                        📊 {seq.msa_row_count} Total | 🎨 {seq.gen_color_row_count} Gen Articles | 🔸 {seq.color_variant_row_count} Var Articles
                      </div>
                      {seq.created_by && (
                        <div className="text-[9px] text-gray-500 mt-0.5">By: {seq.created_by}</div>
                      )}
                    </div>
                    <button
                      onClick={() => {
                        msaAPI.getSequenceSummary(seq.sequence_id)
                          .then(res => {
                            const summary = res.data.data;
                            console.log('📋 Sequence summary:', summary);
                            toast.success(`Sequence ${seq.sequence_id}: ${summary.row_counts.msa} MSA | ${summary.row_counts.msa_gen_clr} Colors`, { duration: 4000 });
                          })
                          .catch(err => {
                            console.error('Error loading summary:', err);
                            toast.error('Could not load sequence summary');
                          });
                      }}
                      className="btn-secondary btn-sm text-[9px] px-1.5 py-0.5 whitespace-nowrap"
                    >
                      📋 Details
                    </button>
                  </div>
                </div>
              ))}
            </div>
          ) : (
            <div className="p-4 text-center text-gray-400 text-[11px]">
              No stored sequences yet. Run a calculation to create a new sequence.
            </div>
          )}
        </div>
      </div>
    </div>
  );
}

