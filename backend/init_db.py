"""
Database Initialization Script for Contribution Analysis
Run this once to set up all required tables
"""
import sys
import os
from sqlalchemy import create_engine

# Add parent directory to path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app.services.preset_manager import PresetManager


def initialize_database(database_url: str):
    """
    Initialize the contribution analysis database
    
    Args:
        database_url: SQLAlchemy database connection string
                     Example: 'mssql+pyodbc://username:password@server/database?driver=ODBC+Driver+18+for+SQL+Server'
    """
    try:
        print("🔧 Initializing Contribution Analysis Database...")
        
        # Create engine
        engine = create_engine(database_url)
        
        # Test connection
        with engine.connect() as conn:
            print("✅ Database connection successful")
        
        # Initialize PresetManager
        manager = PresetManager(engine)
        
        # Ensure table exists
        success, error = manager._ensure_table_exists()
        if not success:
            print(f"❌ Failed to create tables: {error}")
            return False
        
        print("✅ Preset table created/verified")
        
        # Ensure default preset
        presets, error = manager.ensure_default_preset()
        if error:
            print(f"❌ Failed to create default preset: {error}")
            return False
        
        print(f"✅ Default presets initialized ({len(presets)} presets)")
        
        # Get statistics
        stats = manager.get_statistics()
        print(f"\n📊 Database Statistics:")
        print(f"   Total Presets: {stats.get('total_presets', 0)}")
        print(f"   By Type: {stats.get('by_type', {})}")
        
        print(f"\n✅ Database initialization complete!")
        return True
        
    except Exception as e:
        print(f"❌ Initialization failed: {e}")
        import traceback
        traceback.print_exc()
        return False


if __name__ == '__main__':
    # Get database URL from environment or use default
    database_url = os.getenv(
        'SQLALCHEMY_DATABASE_URL',
        'mssql+pyodbc://localhost/ars_database?driver=ODBC+Driver+18+for+SQL+Server'
    )
    
    print(f"Database URL: {database_url}")
    success = initialize_database(database_url)
    sys.exit(0 if success else 1)
