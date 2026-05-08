"""
Row-Level Security Models: Store Access, Region Access, Column Restrictions
"""
from datetime import datetime
from sqlalchemy import (
    Column, Integer, BigInteger, String, Boolean, DateTime, ForeignKey, UniqueConstraint
)
from sqlalchemy.orm import relationship
from app.database.session import Base


class Store(Base):
    __tablename__ = "rls_stores"

    id = Column(Integer, primary_key=True, autoincrement=True)
    store_code = Column(String(20), nullable=False, unique=True, index=True)
    store_name = Column(String(200), nullable=False)
    region = Column(String(100), index=True)
    hub = Column(String(100))
    division = Column(String(100), index=True)
    business_unit = Column(String(100))
    store_grade = Column(String(10))
    city = Column(String(100))
    state = Column(String(100))
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class UserStoreAccess(Base):
    __tablename__ = "rls_user_store_access"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("rbac_users.id"), nullable=False)
    store_code = Column(String(20), nullable=False, index=True)
    access_level = Column(String(50), default="READ")
    granted_at = Column(DateTime, default=datetime.utcnow)
    granted_by = Column(String(100))
    is_active = Column(Boolean, default=True)

    __table_args__ = (
        UniqueConstraint("user_id", "store_code", name="uq_user_store"),
    )

    user = relationship("User", back_populates="store_access")


class UserRegionAccess(Base):
    __tablename__ = "rls_user_region_access"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("rbac_users.id"), nullable=False)
    region = Column(String(100))
    hub = Column(String(100))
    division = Column(String(100))
    business_unit = Column(String(100))
    access_level = Column(String(50), default="READ")
    granted_at = Column(DateTime, default=datetime.utcnow)
    granted_by = Column(String(100))
    is_active = Column(Boolean, default=True)

    user = relationship("User", back_populates="region_access")


class UserCategoryAccess(Base):
    """
    Category-Level RLS: assigns specific Major Categories to each planner.
    
    Each planner gets a set of (division, sub_division, major_category)
    combinations. The MSA calculation, grid builder, allocation engine,
    and BDC creation all filter by these assignments.
    
    When is_exclusive=True, only this user should allocate for this category.
    Admins/SuperAdmins bypass this entirely.
    """
    __tablename__ = "rls_user_category_access"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("rbac_users.id"), nullable=False)
    division = Column(String(100))
    sub_division = Column(String(100))
    major_category = Column(String(100))
    access_level = Column(String(50), default="FULL")
    is_exclusive = Column(Boolean, default=True)
    granted_at = Column(DateTime, default=datetime.utcnow)
    granted_by = Column(String(100))
    is_active = Column(Boolean, default=True)
    notes = Column(String(500))

    __table_args__ = (
        UniqueConstraint("user_id", "division", "sub_division", "major_category",
                         name="uq_user_category"),
    )

    user = relationship("User", back_populates="category_access")


class ColumnRestriction(Base):
    __tablename__ = "rls_column_restrictions"

    id = Column(Integer, primary_key=True, autoincrement=True)
    table_name = Column(String(200), nullable=False)
    column_name = Column(String(200), nullable=False)
    role_id = Column(Integer, ForeignKey("rbac_roles.id"), nullable=False)
    is_visible = Column(Boolean, default=True)
    is_masked = Column(Boolean, default=False)
    mask_pattern = Column(String(100))
    can_edit = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    __table_args__ = (
        UniqueConstraint("table_name", "column_name", "role_id", name="uq_col_restriction"),
    )

    role = relationship("Role", back_populates="column_restrictions")


class TableRoleAccess(Base):
    """Per-table access control: which roles can read/write which tables."""
    __tablename__ = "rls_table_role_access"

    id = Column(Integer, primary_key=True, autoincrement=True)
    table_name = Column(String(200), nullable=False, index=True)
    role_id = Column(Integer, ForeignKey("rbac_roles.id"), nullable=False)
    can_read = Column(Boolean, default=True)
    can_write = Column(Boolean, default=False)
    can_upload = Column(Boolean, default=False)
    can_export = Column(Boolean, default=False)
    granted_at = Column(DateTime, default=datetime.utcnow)
    granted_by = Column(String(100))

    __table_args__ = (
        UniqueConstraint("table_name", "role_id", name="uq_table_role_access"),
    )

    role = relationship("Role")


class TableSettings(Base):
    """Configuration for table-specific settings like heavy table handling."""
    __tablename__ = "table_settings"

    id = Column(Integer, primary_key=True, autoincrement=True)
    table_name = Column(String(200), nullable=False, unique=True)
    is_heavy = Column(Boolean, default=False)  # Mark as heavy/large table
    row_threshold = Column(Integer, default=100000)  # Rows threshold for heavy
    require_filter = Column(Boolean, default=False)  # Require filter before loading
    visible_in_editor = Column(Boolean, default=True)  # Show this table in Data Editor
    filter_columns = Column(String(2000))  # JSON list of default filter columns
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
