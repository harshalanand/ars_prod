"""
Migration script: Create data_change_log table
Run from: backend directory
Usage: python scripts/run_010_data_change_log.py
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import text
from app.database.session import SessionLocal
from app.core.config import get_settings

settings = get_settings()

def run_migration():
    db = SessionLocal()
    try:
        # Create data_change_log table if not exists
        db.execute(text("""
            IF NOT EXISTS (SELECT * FROM sys.tables WHERE name = 'data_change_log')
            BEGIN
                CREATE TABLE data_change_log (
                    id BIGINT IDENTITY(1,1) PRIMARY KEY,
                    audit_log_id BIGINT NULL,
                    table_name NVARCHAR(200) NOT NULL,
                    action_type NVARCHAR(20) NOT NULL,
                    record_key NVARCHAR(500) NOT NULL,
                    column_name NVARCHAR(200) NULL,
                    old_value NVARCHAR(MAX) NULL,
                    new_value NVARCHAR(MAX) NULL,
                    data_type NVARCHAR(50) NULL,
                    changed_by NVARCHAR(100) NOT NULL,
                    changed_at DATETIME DEFAULT GETUTCDATE(),
                    source NVARCHAR(50) DEFAULT 'UI',
                    batch_id NVARCHAR(100) NULL,
                    row_index INT NULL
                );
            END
        """))
        db.commit()
        print("✅ Created data_change_log table")
        
        # Create indexes
        indexes = [
            ("IX_data_change_log_table_name", "table_name"),
            ("IX_data_change_log_changed_at", "changed_at DESC"),
            ("IX_data_change_log_changed_by", "changed_by"),
        ]
        
        for idx_name, cols in indexes:
            try:
                db.execute(text(f"""
                    IF NOT EXISTS (SELECT * FROM sys.indexes WHERE name = '{idx_name}')
                    CREATE INDEX {idx_name} ON data_change_log({cols})
                """))
                db.commit()
                print(f"✅ Created index {idx_name}")
            except Exception as e:
                print(f"⚠️ Index {idx_name}: {e}")
        
        # Create filtered indexes
        try:
            db.execute(text("""
                IF NOT EXISTS (SELECT * FROM sys.indexes WHERE name = 'IX_data_change_log_batch_id')
                CREATE INDEX IX_data_change_log_batch_id ON data_change_log(batch_id) WHERE batch_id IS NOT NULL
            """))
            db.commit()
            print("✅ Created filtered index IX_data_change_log_batch_id")
        except Exception as e:
            print(f"⚠️ batch_id index: {e}")
            
        try:
            db.execute(text("""
                IF NOT EXISTS (SELECT * FROM sys.indexes WHERE name = 'IX_data_change_log_audit_log_id')
                CREATE INDEX IX_data_change_log_audit_log_id ON data_change_log(audit_log_id) WHERE audit_log_id IS NOT NULL
            """))
            db.commit()
            print("✅ Created filtered index IX_data_change_log_audit_log_id")
        except Exception as e:
            print(f"⚠️ audit_log_id index: {e}")
        
        print("\n✅ Migration completed successfully!")
        
    except Exception as e:
        print(f"❌ Migration failed: {e}")
        db.rollback()
        raise
    finally:
        db.close()


if __name__ == "__main__":
    run_migration()
