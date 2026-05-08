-- ============================================================
-- Contribution Percentage Analysis - Database Schema
-- ============================================================

-- Cont_presets: Preset configuration table
CREATE TABLE Cont_presets (
    preset_name NVARCHAR(255) PRIMARY KEY,
    preset_type NVARCHAR(50) NOT NULL,           -- 'formula' or 'standard'
    description NVARCHAR(MAX),
    config_json NVARCHAR(MAX) NOT NULL,          -- JSON format configuration
    sequence_order INT DEFAULT 9999,
    created_date DATETIME DEFAULT GETDATE(),
    modified_date DATETIME DEFAULT GETDATE()
);

-- Index for sequence ordering
CREATE INDEX idx_cont_presets_sequence ON Cont_presets(sequence_order);

-- Cont_mappings: Suffix mapping table
CREATE TABLE Cont_mappings (
    mapping_name NVARCHAR(255) PRIMARY KEY,
    mapping_json NVARCHAR(MAX) NOT NULL,         -- JSON with suffix mappings
    fallback_json NVARCHAR(MAX),                 -- JSON with fallback values
    description NVARCHAR(MAX),
    created_date DATETIME DEFAULT GETDATE(),
    modified_date DATETIME DEFAULT GETDATE()
);

-- Cont_mapping_assignments: Links columns to mapping rules
CREATE TABLE Cont_mapping_assignments (
    id INT PRIMARY KEY IDENTITY(1,1),
    col_name NVARCHAR(255) NOT NULL,
    mapping_name NVARCHAR(255) NOT NULL,
    prefix NVARCHAR(255),
    target NVARCHAR(20) DEFAULT 'Both',          -- 'Both', 'Store', 'Company'
    created_date DATETIME DEFAULT GETDATE(),
    modified_date DATETIME DEFAULT GETDATE(),
    
    -- Foreign key to mappings
    CONSTRAINT fk_assignment_mapping 
        FOREIGN KEY (mapping_name) 
        REFERENCES Cont_mappings(mapping_name) ON DELETE CASCADE
);

-- Indexes for better query performance
CREATE INDEX idx_assignment_col ON Cont_mapping_assignments(col_name);
CREATE INDEX idx_assignment_mapping ON Cont_mapping_assignments(mapping_name);
CREATE INDEX idx_assignment_target ON Cont_mapping_assignments(target);

-- ============================================================
-- Sample Data (Optional)
-- ============================================================

-- Insert sample preset
INSERT INTO Cont_presets (preset_name, preset_type, description, config_json, sequence_order)
VALUES (
    'Sample_Analysis',
    'standard',
    'Sample preset for testing contribution percentage analysis',
    '{"filters": {"kpi": ["SALE_V", "GM_V"]}, "groupby": "ST_CD", "period": "all"}',
    1
);

-- Insert sample mapping
INSERT INTO Cont_mappings (mapping_name, mapping_json, fallback_json, description)
VALUES (
    'Sample_Mapping',
    '{"suffix_mapping": {"SKU_001": "ELECTRONICS", "SKU_002": "CLOTHING"}}',
    '{"default": "OTHER"}',
    'Sample SKU to category mapping'
);

-- Insert sample assignment
INSERT INTO Cont_mapping_assignments (col_name, mapping_name, prefix, target)
VALUES (
    'Product_SKU',
    'Sample_Mapping',
    'PROD_',
    'Both'
);

PRINT 'Contribution Percentage tables created successfully!';
