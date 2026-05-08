"""
Database migrations for Contribution Percentage Analysis
Run this script to create the required tables
"""
from app.database.session import engine
from app.models.contribution import Base

def create_tables():
    """Create all contribution percentage tables"""
    print("Creating Contribution Percentage tables...")
    Base.metadata.create_all(bind=engine)
    print("✅ Tables created successfully!")
    print("\nCreated tables:")
    print("  - Cont_presets")
    print("  - Cont_mappings")
    print("  - Cont_mapping_assignments")

if __name__ == "__main__":
    create_tables()
