import React, { useState, useEffect, useRef } from 'react';
import { msaAPI } from '../../services/api';

/**
 * CascadingFilters Component
 * 
 * Multi-level cascading filter with dependencies between filter levels.
 * Example: ST_CD → SLOC → DIV
 * 
 * When a parent level changes, child level values are fetched filtered by the parent selection.
 * When parent changes, all child selections are cleared.
 * 
 * Props:
 *   - filterHierarchy: Array of {name: string, label: string} determining filter levels
 *   - date: Selected date for filtering (passed to backend)
 *   - selectedFilters: Current selected values: {ST_CD: ['val1'], SLOC: ['val2']}
 *   - onSelectionChange: Callback(updatedFilters) called when any selection changes
 *   - className: Optional CSS class for root container
 */
const CascadingFilters = ({
  filterHierarchy = [],
  date = null,
  selectedFilters = {},
  onSelectionChange = () => {},
  className = ''
}) => {
  const [distinctValues, setDistinctValues] = useState({});
  const [loading, setLoading] = useState({});
  const [errors, setErrors] = useState({});
  
  // Track which levels have been fetched to prevent duplicate requests
  const fetchedLevels = useRef({});
  
  // Debounce timer for fetch requests
  const fetchTimers = useRef({});

  // Load values for a specific filter level (with cascading parent filters and debouncing)
  const loadFilterValues = async (filterLevel) => {
    const levelName = filterLevel.name;
    
    // Build a key to identify this exact fetch request
    const parentFilters = {};
    const levelIndex = filterHierarchy.findIndex(f => f.name === levelName);
    
    for (let i = 0; i < levelIndex; i++) {
      const parentFilterName = filterHierarchy[i].name;
      const parentSelection = selectedFilters[parentFilterName];
      if (parentSelection && Array.isArray(parentSelection) && parentSelection.length > 0) {
        parentFilters[parentFilterName] = parentSelection.sort().join(','); // Create deterministic key
      }
    }
    
    const fetchKey = `${levelName}|${date}|${JSON.stringify(parentFilters)}`;
    
    // If this exact request was just made, skip it (debounce)
    if (fetchedLevels.current[fetchKey]) {
      console.log(`⏭️  Skipping duplicate fetch for ${levelName}`);
      return;
    }
    
    // Cancel previous timer for this level if it exists
    if (fetchTimers.current[levelName]) {
      clearTimeout(fetchTimers.current[levelName]);
    }
    
    // Debounce fetch by 300ms
    fetchTimers.current[levelName] = setTimeout(async () => {
      try {
        setLoading(prev => ({ ...prev, [levelName]: true }));
        setErrors(prev => ({ ...prev, [levelName]: null }));

        // Build cascade filters from parent selections
        const cascadeFilters = {};
        
        for (let i = 0; i < levelIndex; i++) {
          const parentFilterName = filterHierarchy[i].name;
          const parentSelection = selectedFilters[parentFilterName];
          if (parentSelection && Array.isArray(parentSelection) && parentSelection.length > 0) {
            cascadeFilters[parentFilterName] = parentSelection;
          }
        }

        console.log(`📍 Fetching ${levelName} (debounced)`, {
          date,
          parentFilters: cascadeFilters
        });

        // Call API with cascading filters
        const filtersParam = Object.keys(cascadeFilters).length > 0 
          ? JSON.stringify(cascadeFilters) 
          : null;

        const response = await msaAPI.getDistinct(levelName, date, filtersParam);
        const values = response.data?.data?.values || [];

        setDistinctValues(prev => ({
          ...prev,
          [levelName]: values
        }));

        // Mark this fetch as completed to prevent duplicates
        fetchedLevels.current[fetchKey] = true;
        
        console.log(`✅ Loaded ${values.length} values for ${levelName}`);
      } catch (error) {
        console.error(`❌ Error loading ${levelName}:`, error.message);
        
        if (!error.response) {
          setErrors(prev => ({
            ...prev,
            [levelName]: 'Network error - check API connection'
          }));
        } else {
          setErrors(prev => ({
            ...prev,
            [levelName]: error.response?.data?.detail || error.message
          }));
        }
        
        setDistinctValues(prev => ({
          ...prev,
          [levelName]: []
        }));
      } finally {
        setLoading(prev => ({ ...prev, [levelName]: false }));
      }
    }, 300); // 300ms debounce
  };

  // Load initial values for first level on mount and date change
  useEffect(() => {
    if (filterHierarchy.length === 0) return;
    
    console.log(`🚀 Initializing cascading filters with date: ${date}`);
    
    // Clear fetched cache when date changes
    fetchedLevels.current = {};
    
    // Load first level (no parent dependencies)
    loadFilterValues(filterHierarchy[0]);
  }, [date, filterHierarchy.length]); // Only re-run on date or hierarchy change

  // When parent filters change, reload only the immediate child level
  useEffect(() => {
    if (filterHierarchy.length <= 1) return;

    // Find which parent changed by checking each level
    for (let i = 1; i < filterHierarchy.length; i++) {
      const currentLevel = filterHierarchy[i];
      const parentLevel = filterHierarchy[i - 1];
      const parentSelection = selectedFilters[parentLevel.name];
      
      // Only load this level if the immediate parent has selections
      if (parentSelection && Array.isArray(parentSelection) && parentSelection.length > 0) {
        console.log(`🔄 Parent ${parentLevel.name} changed, reloading ${currentLevel.name}`);
        loadFilterValues(currentLevel);
      }
    }
    
    // Clear selections for child levels whenever parent changes
    // (This needs to happen via callback to parent, not here)
  }, [selectedFilters, filterHierarchy]);

  // Handle selection toggle for a filter value
  const handleToggleValue = (filterName, value) => {
    const currentSelection = selectedFilters[filterName] || [];
    let updatedSelection;

    if (currentSelection.includes(value)) {
      updatedSelection = currentSelection.filter(v => v !== value);
    } else {
      updatedSelection = [...currentSelection, value];
    }

    const updatedFilters = { ...selectedFilters, [filterName]: updatedSelection };

    // Clear child selections when parent changes
    const filterIndex = filterHierarchy.findIndex(f => f.name === filterName);
    if (filterIndex >= 0 && filterIndex < filterHierarchy.length - 1) {
      for (let i = filterIndex + 1; i < filterHierarchy.length; i++) {
        updatedFilters[filterHierarchy[i].name] = [];
      }
    }

    onSelectionChange(updatedFilters);
  };

  // Select ALL available values for a filter level
  const handleSelectAll = (filterName) => {
    const values = distinctValues[filterName] || [];
    const updatedFilters = { ...selectedFilters, [filterName]: [...values] };
    onSelectionChange(updatedFilters);
  };

  // Clear all selections for a filter level
  const handleClearAll = (filterName) => {
    const updatedFilters = { ...selectedFilters, [filterName]: [] };
    // Also clear children
    const filterIndex = filterHierarchy.findIndex(f => f.name === filterName);
    if (filterIndex >= 0) {
      for (let i = filterIndex + 1; i < filterHierarchy.length; i++) {
        updatedFilters[filterHierarchy[i].name] = [];
      }
    }
    onSelectionChange(updatedFilters);
  };

  // Render a single filter level with multi-select buttons
  const renderFilterLevel = (filterLevel) => {
    const levelIndex = filterHierarchy.findIndex(f => f.name === filterLevel.name);
    const values = distinctValues[filterLevel.name] || [];
    const selected = selectedFilters[filterLevel.name] || [];
    const isLoading = loading[filterLevel.name];
    const error = errors[filterLevel.name];

    // Determine if this level is disabled (parent has no selection)
    let isDisabled = false;
    let disabledReason = '';
    
    if (levelIndex > 0) {
      const parentFilterName = filterHierarchy[levelIndex - 1].name;
      const parentSelection = selectedFilters[parentFilterName] || [];
      if (!Array.isArray(parentSelection) || parentSelection.length === 0) {
        isDisabled = true;
        disabledReason = `Select ${filterHierarchy[levelIndex - 1].label} first`;
      }
    }

    const allSelected = values.length > 0 && values.every(v => selected.includes(v))

    return (
      <div key={filterLevel.name} className="mb-6">
        <div className="flex items-center justify-between mb-2">
          <label className="block text-sm font-semibold text-gray-700">
            {filterLevel.label}
            {isLoading && <span className="ml-2 text-blue-500 text-xs">Loading...</span>}
          </label>

          {/* Select All / Clear All buttons — hidden when disabled or no values */}
          {!isDisabled && values.length > 0 && (
            <div className="flex gap-2">
              <button
                onClick={() => handleSelectAll(filterLevel.name)}
                disabled={isLoading || allSelected}
                className="text-[10px] px-2 py-0.5 rounded border border-blue-400 text-blue-600 hover:bg-blue-50 disabled:opacity-40 disabled:cursor-not-allowed font-medium"
                type="button"
              >
                Select All ({values.length})
              </button>
              <button
                onClick={() => handleClearAll(filterLevel.name)}
                disabled={isLoading || selected.length === 0}
                className="text-[10px] px-2 py-0.5 rounded border border-gray-300 text-gray-500 hover:bg-gray-100 disabled:opacity-40 disabled:cursor-not-allowed font-medium"
                type="button"
              >
                Clear
              </button>
            </div>
          )}
        </div>

        {error && (
          <div className="mb-3 p-2 bg-red-100 border border-red-400 text-red-700 text-xs rounded">
            {error}
          </div>
        )}

        <div className="flex flex-wrap gap-2">
          {isDisabled && !isLoading ? (
            <span className="text-gray-400 text-sm italic">{disabledReason}</span>
          ) : values.length === 0 ? (
            <span className="text-gray-400 text-sm italic">
              {isLoading ? 'Loading values...' : 'No values available'}
            </span>
          ) : (
            values.map(value => (
              <button
                key={value}
                onClick={() => handleToggleValue(filterLevel.name, value)}
                disabled={isDisabled || isLoading}
                className={`px-3 py-1 rounded-full text-sm font-medium transition-all ${
                  selected.includes(value)
                    ? 'bg-blue-500 text-white'
                    : 'bg-gray-200 text-gray-700 hover:bg-gray-300'
                } ${(isDisabled || isLoading) ? 'opacity-50 cursor-not-allowed' : 'cursor-pointer'}`}
              >
                {value}
              </button>
            ))
          )}
        </div>

        {selected.length > 0 && (
          <div className="mt-2 text-xs text-gray-500">
            {selected.length} of {values.length} selected: {selected.join(', ')}
          </div>
        )}
      </div>
    );
  };

  return (
    <div className={`cascading-filters p-4 bg-gray-50 rounded-lg border border-gray-200 ${className}`}>
      <h3 className="text-lg font-bold text-gray-800 mb-4">Filters</h3>
      
      {filterHierarchy.length === 0 ? (
        <p className="text-gray-500 text-sm">No filters configured</p>
      ) : (
        <div className="space-y-6">
          {filterHierarchy.map(filterLevel => renderFilterLevel(filterLevel))}
        </div>
      )}
    </div>
  );
};

export default CascadingFilters;
