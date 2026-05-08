"""
Dynamic Table Management Models
"""
from datetime import datetime
from sqlalchemy import Column, Integer, String, Boolean, DateTime, BigInteger, ForeignKey
from sqlalchemy.orm import relationship
from app.database.session import Base


class TableRegistry(Base):
    __tablename__ = "sys_table_registry"

    id = Column(Integer, primary_key=True, autoincrement=True)
    table_name = Column(String(200), nullable=False, unique=True)
    display_name = Column(String(200))
    description = Column(String(1000))
    module = Column(String(100))
    primary_key_columns = Column(String(500))  # JSON array
    is_system_table = Column(Boolean, default=False)
    is_active = Column(Boolean, default=True)
    row_count = Column(BigInteger, default=0)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    created_by = Column(String(100))

    columns = relationship("ColumnRegistry", back_populates="table", lazy="selectin")


class ColumnRegistry(Base):
    __tablename__ = "sys_column_registry"

    id = Column(Integer, primary_key=True, autoincrement=True)
    table_id = Column(Integer, ForeignKey("sys_table_registry.id"), nullable=False)
    column_name = Column(String(200), nullable=False)
    display_name = Column(String(200))
    data_type = Column(String(100), nullable=False)
    max_length = Column(Integer)
    is_nullable = Column(Boolean, default=True)
    is_primary_key = Column(Boolean, default=False)
    default_value = Column(String(500))
    column_order = Column(Integer, default=0)
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    table = relationship("TableRegistry", back_populates="columns")
