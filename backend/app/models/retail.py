"""
Retail Models: Division, Category, Gen Article, Variant Article, Allocation
"""
from datetime import datetime, date
from sqlalchemy import (
    Column, Integer, BigInteger, String, Boolean, DateTime, Date,
    ForeignKey, Numeric, Computed, UniqueConstraint
)
from sqlalchemy.orm import relationship
from app.database.session import Base


# ============================================================================
# Product Hierarchy
# ============================================================================

class Division(Base):
    __tablename__ = "retail_division"

    id = Column(Integer, primary_key=True, autoincrement=True)
    division_code = Column(String(20), nullable=False, unique=True)
    division_name = Column(String(200), nullable=False)
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    sub_divisions = relationship("SubDivision", back_populates="division", lazy="selectin")


class SubDivision(Base):
    __tablename__ = "retail_sub_division"

    id = Column(Integer, primary_key=True, autoincrement=True)
    sub_division_code = Column(String(20), nullable=False, unique=True)
    sub_division_name = Column(String(200), nullable=False)
    division_id = Column(Integer, ForeignKey("retail_division.id"))
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    division = relationship("Division", back_populates="sub_divisions")
    categories = relationship("MajorCategory", back_populates="sub_division", lazy="selectin")


class MajorCategory(Base):
    __tablename__ = "retail_major_category"

    id = Column(Integer, primary_key=True, autoincrement=True)
    category_code = Column(String(20), nullable=False, unique=True)
    category_name = Column(String(200), nullable=False)
    sub_division_id = Column(Integer, ForeignKey("retail_sub_division.id"))
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    sub_division = relationship("SubDivision", back_populates="categories")


class SizeMaster(Base):
    __tablename__ = "retail_size_master"

    id = Column(Integer, primary_key=True, autoincrement=True)
    size_code = Column(String(20), nullable=False, unique=True)
    size_name = Column(String(50), nullable=False)
    size_order = Column(Integer, default=0)
    category = Column(String(50))
    is_active = Column(Boolean, default=True)


class ColorMaster(Base):
    __tablename__ = "retail_color_master"

    id = Column(Integer, primary_key=True, autoincrement=True)
    color_code = Column(String(20), nullable=False, unique=True)
    color_name = Column(String(100), nullable=False)
    color_hex = Column(String(10))
    is_active = Column(Boolean, default=True)


# ============================================================================
# Articles
# ============================================================================

class GenArticle(Base):
    __tablename__ = "retail_gen_article"

    id = Column(Integer, primary_key=True, autoincrement=True)
    gen_article_code = Column(String(50), nullable=False, unique=True, index=True)
    article_name = Column(String(300), nullable=False)
    division_id = Column(Integer, ForeignKey("retail_division.id"))
    sub_division_id = Column(Integer, ForeignKey("retail_sub_division.id"))
    category_id = Column(Integer, ForeignKey("retail_major_category.id"))
    mvgr = Column(String(100))
    fabric = Column(String(200))
    season = Column(String(100))
    brand = Column(String(100))
    mrp = Column(Numeric(12, 2))
    cost_price = Column(Numeric(12, 2))      # Column-level security
    margin_pct = Column(Numeric(8, 2))        # Column-level security
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    division = relationship("Division", lazy="selectin")
    sub_division = relationship("SubDivision", lazy="selectin")
    category = relationship("MajorCategory", lazy="selectin")
    variants = relationship("VariantArticle", back_populates="gen_article", lazy="selectin")


class VariantArticle(Base):
    __tablename__ = "retail_variant_article"

    id = Column(Integer, primary_key=True, autoincrement=True)
    variant_code = Column(String(50), nullable=False, unique=True, index=True)
    gen_article_id = Column(Integer, ForeignKey("retail_gen_article.id"), nullable=False)
    size_code = Column(String(20), nullable=False)
    size_name = Column(String(50))
    color_code = Column(String(20), nullable=False)
    color_name = Column(String(100))
    barcode = Column(String(50))
    mrp = Column(Numeric(12, 2))
    cost_price = Column(Numeric(12, 2))
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    gen_article = relationship("GenArticle", back_populates="variants")


# ============================================================================
# Allocation
# ============================================================================

class AllocationHeader(Base):
    __tablename__ = "alloc_header"

    id = Column(Integer, primary_key=True, autoincrement=True)
    allocation_code = Column(String(50), nullable=False, unique=True, index=True)
    allocation_name = Column(String(300))
    allocation_type = Column(String(50), nullable=False)  # INITIAL, REPLENISHMENT, TRANSFER
    division_id = Column(Integer, ForeignKey("retail_division.id"))
    season = Column(String(100))
    status = Column(String(50), default="DRAFT")  # DRAFT, IN_PROGRESS, APPROVED, EXECUTED, CANCELLED
    total_qty = Column(Integer, default=0)
    total_stores = Column(Integer, default=0)
    total_options = Column(Integer, default=0)
    created_by = Column(String(100), nullable=False)
    approved_by = Column(String(100))
    executed_at = Column(DateTime)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    division = relationship("Division", lazy="selectin")
    details = relationship("AllocationDetail", back_populates="allocation", lazy="dynamic")


class AllocationDetail(Base):
    __tablename__ = "alloc_detail"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    allocation_id = Column(Integer, ForeignKey("alloc_header.id"), nullable=False, index=True)
    store_code = Column(String(20), nullable=False, index=True)
    gen_article_id = Column(Integer, ForeignKey("retail_gen_article.id"))
    variant_id = Column(Integer, ForeignKey("retail_variant_article.id"))
    size_code = Column(String(20))
    color_code = Column(String(20))
    allocated_qty = Column(Integer, default=0)
    override_qty = Column(Integer)           # Column-level security
    final_qty = Column(Integer, default=0)
    store_grade = Column(String(10))
    allocation_basis = Column(String(50))    # STOCK, SALES, RATIO, MANUAL
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    allocation = relationship("AllocationHeader", back_populates="details")


# ============================================================================
# Stock & Sales
# ============================================================================

class StoreStock(Base):
    __tablename__ = "store_stock"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    store_code = Column(String(20), nullable=False, index=True)
    variant_code = Column(String(50), nullable=False, index=True)
    stock_qty = Column(Integer, default=0)
    in_transit_qty = Column(Integer, default=0)
    reserved_qty = Column(Integer, default=0)
    last_updated = Column(DateTime, default=datetime.utcnow)

    __table_args__ = (
        UniqueConstraint("store_code", "variant_code", name="uq_store_variant_stock"),
    )


class StoreSales(Base):
    __tablename__ = "store_sales"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    store_code = Column(String(20), nullable=False, index=True)
    variant_code = Column(String(50), nullable=False)
    sale_date = Column(Date, nullable=False, index=True)
    qty_sold = Column(Integer, default=0)
    sale_value = Column(Numeric(12, 2), default=0)

    __table_args__ = (
        UniqueConstraint("store_code", "variant_code", "sale_date", name="uq_store_variant_sale"),
    )


class WarehouseStock(Base):
    __tablename__ = "warehouse_stock"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    warehouse_code = Column(String(20), nullable=False, index=True)
    variant_code = Column(String(50), nullable=False)
    stock_qty = Column(Integer, default=0)
    reserved_qty = Column(Integer, default=0)
    last_updated = Column(DateTime, default=datetime.utcnow)

    __table_args__ = (
        UniqueConstraint("warehouse_code", "variant_code", name="uq_wh_variant"),
    )
