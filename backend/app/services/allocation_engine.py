"""
Retail Allocation Engine Service
==================================
Core business logic for allocating products from warehouse to 400+ stores.

Supports:
- Store grade-based allocation (A/B/C/D ratios)
- Size × Color grid distribution
- Stock-based allocation (fill gaps)
- Sales-based allocation (proportional to history)
- Manual ratio allocation
- Warehouse availability enforcement
- Per-store min/max constraints
- Manual overrides with audit

This module is designed to be refactored from existing Python allocation logic.
Business logic is preserved; infrastructure is FastAPI-native.
"""
import uuid
import time
from datetime import datetime, timezone, timedelta
from typing import List, Dict, Any, Optional, Tuple
from collections import defaultdict

import pandas as pd
import numpy as np
from sqlalchemy import text, func
from sqlalchemy.orm import Session
from loguru import logger

from app.models.retail import (
    AllocationHeader, AllocationDetail, GenArticle, VariantArticle,
    StoreStock, StoreSales, WarehouseStock,
)
from app.models.rls import Store
from app.audit.service import AuditService
from app.database.session import get_data_engine


class AllocationEngine:
    """
    Multi-level retail allocation engine.

    Flow:
    1. Create allocation header (DRAFT)
    2. Resolve eligible stores and products
    3. Fetch warehouse availability
    4. Calculate allocation per store × variant based on chosen basis
    5. Apply size curve distribution
    6. Apply store grade ratios
    7. Enforce min/max constraints
    8. Cap at warehouse availability
    9. Save allocation details
    10. Support manual overrides
    11. Approve → Execute (lock)
    """

    DEFAULT_GRADE_RATIOS = {"A": 1.0, "B": 0.7, "C": 0.4, "D": 0.2}
    DEFAULT_SIZE_CURVE = {"XS": 0.05, "S": 0.15, "M": 0.30, "L": 0.30, "XL": 0.15, "XXL": 0.05}

    def __init__(self, db: Session):
        self.db = db
        self.engine = get_data_engine()  # Use Data DB for business data
        self.audit = AuditService(db)

    # ========================================================================
    # RUN ALLOCATION
    # ========================================================================

    def run_allocation(
        self,
        allocation_name: str,
        allocation_type: str,
        created_by: str,
        division_id: Optional[int] = None,
        season: Optional[str] = None,
        basis: str = "RATIO",
        gen_article_ids: Optional[List[int]] = None,
        gen_article_codes: Optional[List[str]] = None,
        store_codes: Optional[List[str]] = None,
        store_grades: Optional[List[str]] = None,
        warehouse_code: str = "WH001",
        grade_ratios: Optional[Dict[str, float]] = None,
        total_qty_limit: Optional[int] = None,
        per_store_max: Optional[int] = None,
        per_store_min: Optional[int] = None,
        size_curve: Optional[Dict[str, float]] = None,
        sales_lookback_days: int = 30,
        ip_address: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Execute a full allocation run.
        """
        start_time = time.time()
        alloc_code = f"ALLOC_{datetime.now().strftime('%Y%m%d')}_{uuid.uuid4().hex[:6].upper()}"

        grade_ratios = grade_ratios or self.DEFAULT_GRADE_RATIOS
        size_curve = size_curve or {}

        logger.info(f"[{alloc_code}] Starting allocation: {allocation_name} | basis={basis}")

        # 1. Create allocation header
        header = AllocationHeader(
            allocation_code=alloc_code,
            allocation_name=allocation_name,
            allocation_type=allocation_type,
            division_id=division_id,
            season=season,
            status="IN_PROGRESS",
            created_by=created_by,
        )
        self.db.add(header)
        self.db.flush()

        try:
            # 2. Resolve eligible stores
            stores_df = self._get_eligible_stores(store_codes, store_grades, division_id)
            if stores_df.empty:
                raise ValueError("No eligible stores found for the given criteria")
            logger.info(f"[{alloc_code}] Eligible stores: {len(stores_df)}")

            # 3. Resolve eligible products (gen articles + variants)
            variants_df = self._get_eligible_variants(
                gen_article_ids, gen_article_codes, division_id, season
            )
            if variants_df.empty:
                raise ValueError("No eligible products/variants found")
            logger.info(f"[{alloc_code}] Eligible variants: {len(variants_df)}")

            # 4. Get warehouse availability
            warehouse_df = self._get_warehouse_stock(warehouse_code, variants_df["variant_code"].tolist())
            logger.info(f"[{alloc_code}] Warehouse SKUs with stock: {len(warehouse_df)}")

            # 5. Calculate base allocation
            if basis == "SALES":
                alloc_df = self._allocate_by_sales(
                    stores_df, variants_df, warehouse_df,
                    sales_lookback_days, grade_ratios,
                )
            elif basis == "STOCK":
                alloc_df = self._allocate_by_stock(
                    stores_df, variants_df, warehouse_df, grade_ratios,
                )
            else:  # RATIO (default)
                alloc_df = self._allocate_by_ratio(
                    stores_df, variants_df, warehouse_df,
                    grade_ratios, size_curve,
                )

            if alloc_df.empty:
                header.status = "DRAFT"
                header.total_qty = 0
                self.db.commit()
                return self._build_response(header, alloc_df, start_time)

            # 6. Apply constraints
            alloc_df = self._apply_constraints(
                alloc_df, per_store_min, per_store_max, total_qty_limit
            )

            # 7. Cap at warehouse availability
            alloc_df = self._cap_at_warehouse(alloc_df, warehouse_df)

            # 8. Set final_qty
            alloc_df["final_qty"] = alloc_df["allocated_qty"]

            # 9. Save allocation details
            self._save_allocation_details(header.id, alloc_df, alloc_code)

            # 10. Update header summary
            header.status = "DRAFT"
            header.total_qty = int(alloc_df["final_qty"].sum())
            header.total_stores = int(alloc_df["store_code"].nunique())
            header.total_options = int(alloc_df["variant_code"].nunique() if "variant_code" in alloc_df.columns else 0)
            self.db.commit()

            # Audit
            self.audit.log(
                table_name="alloc_header",
                action_type="INSERT",
                changed_by=created_by,
                record_primary_key=str(header.id),
                new_data={
                    "allocation_code": alloc_code,
                    "type": allocation_type,
                    "basis": basis,
                    "total_qty": header.total_qty,
                    "total_stores": header.total_stores,
                },
                ip_address=ip_address,
            )
            self.db.commit()

            logger.info(
                f"[{alloc_code}] Allocation complete: "
                f"{header.total_qty} units → {header.total_stores} stores | "
                f"{int((time.time() - start_time) * 1000)}ms"
            )

            return self._build_response(header, alloc_df, start_time)

        except Exception as e:
            header.status = "CANCELLED"
            self.db.commit()
            logger.error(f"[{alloc_code}] Allocation failed: {e}")
            raise

    # ========================================================================
    # ALLOCATION STRATEGIES
    # ========================================================================

    def _allocate_by_ratio(
        self,
        stores_df: pd.DataFrame,
        variants_df: pd.DataFrame,
        warehouse_df: pd.DataFrame,
        grade_ratios: Dict[str, float],
        size_curve: Dict[str, float],
    ) -> pd.DataFrame:
        """
        Ratio-based allocation:
        - Distribute warehouse stock proportionally across stores by grade
        - Apply size curve if available
        """
        # Cross join stores × gen_articles
        gen_articles = variants_df[["gen_article_id", "gen_article_code"]].drop_duplicates()

        allocations = []

        for _, ga in gen_articles.iterrows():
            ga_variants = variants_df[variants_df["gen_article_id"] == ga["gen_article_id"]]

            for _, variant in ga_variants.iterrows():
                variant_code = variant["variant_code"]
                size_code = variant.get("size_code", "")

                # Warehouse available qty
                wh_row = warehouse_df[warehouse_df["variant_code"] == variant_code]
                if wh_row.empty:
                    continue
                available_qty = int(wh_row.iloc[0]["available_qty"])
                if available_qty <= 0:
                    continue

                # Size curve factor
                size_factor = size_curve.get(size_code, 1.0) if size_curve else 1.0

                # Calculate grade weights
                total_weight = 0
                store_weights = []
                for _, store in stores_df.iterrows():
                    grade = store.get("store_grade", "C")
                    weight = grade_ratios.get(grade, 0.3) * size_factor
                    store_weights.append((store, weight))
                    total_weight += weight

                if total_weight == 0:
                    continue

                # Distribute proportionally
                remaining = available_qty
                for store, weight in store_weights:
                    if remaining <= 0:
                        break
                    raw_qty = (weight / total_weight) * available_qty
                    qty = max(0, int(round(raw_qty)))
                    qty = min(qty, remaining)

                    if qty > 0:
                        allocations.append({
                            "store_code": store["store_code"],
                            "store_grade": store.get("store_grade"),
                            "gen_article_id": int(ga["gen_article_id"]),
                            "gen_article_code": ga["gen_article_code"],
                            "variant_id": int(variant.get("variant_id", 0)),
                            "variant_code": variant_code,
                            "size_code": size_code,
                            "color_code": variant.get("color_code", ""),
                            "allocated_qty": qty,
                            "allocation_basis": "RATIO",
                        })
                        remaining -= qty

        return pd.DataFrame(allocations) if allocations else pd.DataFrame()

    def _allocate_by_sales(
        self,
        stores_df: pd.DataFrame,
        variants_df: pd.DataFrame,
        warehouse_df: pd.DataFrame,
        sales_lookback_days: int,
        grade_ratios: Dict[str, float],
    ) -> pd.DataFrame:
        """
        Sales-based allocation:
        - Proportion based on each store's historical sales of the variant
        - Stores with higher sales get more allocation
        """
        cutoff_date = (datetime.now() - timedelta(days=sales_lookback_days)).date()
        store_codes = stores_df["store_code"].tolist()
        variant_codes = variants_df["variant_code"].tolist()

        # Fetch aggregated sales
        sales_sql = text("""
            SELECT store_code, variant_code, SUM(qty_sold) as total_sold
            FROM store_sales
            WHERE sale_date >= :cutoff
              AND store_code IN :stores
              AND variant_code IN :variants
            GROUP BY store_code, variant_code
        """)

        # Use pandas for the aggregation since IN clause with many params
        # is cleaner via raw connection
        sales_query = f"""
            SELECT store_code, variant_code, SUM(qty_sold) as total_sold
            FROM store_sales
            WHERE sale_date >= '{cutoff_date}'
            GROUP BY store_code, variant_code
        """
        try:
            with self.engine.connect() as conn:
                sales_df = pd.read_sql(sales_query, conn)
        except Exception:
            sales_df = pd.DataFrame(columns=["store_code", "variant_code", "total_sold"])

        # Filter to eligible stores/variants
        if not sales_df.empty:
            sales_df = sales_df[
                sales_df["store_code"].isin(store_codes) &
                sales_df["variant_code"].isin(variant_codes)
            ]

        allocations = []

        for variant_code in variant_codes:
            wh_row = warehouse_df[warehouse_df["variant_code"] == variant_code]
            if wh_row.empty:
                continue
            available_qty = int(wh_row.iloc[0]["available_qty"])
            if available_qty <= 0:
                continue

            # Get sales by store for this variant
            var_sales = sales_df[sales_df["variant_code"] == variant_code] if not sales_df.empty else pd.DataFrame()

            variant_info = variants_df[variants_df["variant_code"] == variant_code].iloc[0]

            if var_sales.empty or var_sales["total_sold"].sum() == 0:
                # Fall back to ratio-based for variants with no sales history
                for _, store in stores_df.iterrows():
                    grade = store.get("store_grade", "C")
                    ratio = grade_ratios.get(grade, 0.3)
                    qty = max(0, int(round(ratio * available_qty / len(stores_df))))
                    if qty > 0:
                        allocations.append(self._make_alloc_row(
                            store, variant_info, qty, "SALES_FALLBACK"
                        ))
                continue

            total_sales = var_sales["total_sold"].sum()
            remaining = available_qty

            for _, sale_row in var_sales.iterrows():
                if remaining <= 0:
                    break
                store_code = sale_row["store_code"]
                store_row = stores_df[stores_df["store_code"] == store_code]
                if store_row.empty:
                    continue

                proportion = sale_row["total_sold"] / total_sales
                qty = max(0, int(round(proportion * available_qty)))
                qty = min(qty, remaining)

                if qty > 0:
                    allocations.append(self._make_alloc_row(
                        store_row.iloc[0], variant_info, qty, "SALES"
                    ))
                    remaining -= qty

        return pd.DataFrame(allocations) if allocations else pd.DataFrame()

    def _allocate_by_stock(
        self,
        stores_df: pd.DataFrame,
        variants_df: pd.DataFrame,
        warehouse_df: pd.DataFrame,
        grade_ratios: Dict[str, float],
    ) -> pd.DataFrame:
        """
        Stock-based allocation:
        - Fill stores that have LOW stock relative to their grade target
        - Stores with zero stock get priority
        """
        store_codes = stores_df["store_code"].tolist()
        variant_codes = variants_df["variant_code"].tolist()

        # Fetch current store stock
        stock_query = f"""
            SELECT store_code, variant_code, stock_qty, reserved_qty
            FROM store_stock
        """
        try:
            with self.engine.connect() as conn:
                stock_df = pd.read_sql(stock_query, conn)
        except Exception:
            stock_df = pd.DataFrame(columns=["store_code", "variant_code", "stock_qty", "reserved_qty"])

        if not stock_df.empty:
            stock_df = stock_df[
                stock_df["store_code"].isin(store_codes) &
                stock_df["variant_code"].isin(variant_codes)
            ]
            stock_df["available"] = stock_df["stock_qty"] - stock_df.get("reserved_qty", 0)

        allocations = []

        for variant_code in variant_codes:
            wh_row = warehouse_df[warehouse_df["variant_code"] == variant_code]
            if wh_row.empty:
                continue
            available_qty = int(wh_row.iloc[0]["available_qty"])
            if available_qty <= 0:
                continue

            variant_info = variants_df[variants_df["variant_code"] == variant_code].iloc[0]

            # Calculate target stock per store based on grade
            store_needs = []
            for _, store in stores_df.iterrows():
                grade = store.get("store_grade", "C")
                target = grade_ratios.get(grade, 0.3) * 10  # base target * ratio

                current_stock = 0
                if not stock_df.empty:
                    ss = stock_df[
                        (stock_df["store_code"] == store["store_code"]) &
                        (stock_df["variant_code"] == variant_code)
                    ]
                    if not ss.empty:
                        current_stock = max(0, int(ss.iloc[0]["available"]))

                need = max(0, int(target - current_stock))
                if need > 0:
                    store_needs.append((store, need))

            # Sort by need descending (most needy first)
            store_needs.sort(key=lambda x: x[1], reverse=True)

            remaining = available_qty
            for store, need in store_needs:
                if remaining <= 0:
                    break
                qty = min(need, remaining)
                if qty > 0:
                    allocations.append(self._make_alloc_row(
                        store, variant_info, qty, "STOCK"
                    ))
                    remaining -= qty

        return pd.DataFrame(allocations) if allocations else pd.DataFrame()

    # ========================================================================
    # CONSTRAINTS & CAPPING
    # ========================================================================

    def _apply_constraints(
        self,
        alloc_df: pd.DataFrame,
        per_store_min: Optional[int],
        per_store_max: Optional[int],
        total_qty_limit: Optional[int],
    ) -> pd.DataFrame:
        """Apply min/max constraints."""
        if alloc_df.empty:
            return alloc_df

        if per_store_min is not None:
            # Remove allocations below minimum
            alloc_df = alloc_df[alloc_df["allocated_qty"] >= per_store_min]

        if per_store_max is not None:
            alloc_df["allocated_qty"] = alloc_df["allocated_qty"].clip(upper=per_store_max)

        if total_qty_limit is not None:
            total = alloc_df["allocated_qty"].sum()
            if total > total_qty_limit:
                # Scale down proportionally
                scale_factor = total_qty_limit / total
                alloc_df["allocated_qty"] = (
                    alloc_df["allocated_qty"] * scale_factor
                ).round().astype(int)

        return alloc_df

    def _cap_at_warehouse(
        self,
        alloc_df: pd.DataFrame,
        warehouse_df: pd.DataFrame,
    ) -> pd.DataFrame:
        """Ensure total allocation per variant doesn't exceed warehouse stock."""
        if alloc_df.empty or "variant_code" not in alloc_df.columns:
            return alloc_df

        for variant_code in alloc_df["variant_code"].unique():
            wh_row = warehouse_df[warehouse_df["variant_code"] == variant_code]
            if wh_row.empty:
                alloc_df.loc[alloc_df["variant_code"] == variant_code, "allocated_qty"] = 0
                continue

            available = int(wh_row.iloc[0]["available_qty"])
            mask = alloc_df["variant_code"] == variant_code
            total_alloc = alloc_df.loc[mask, "allocated_qty"].sum()

            if total_alloc > available:
                scale = available / total_alloc
                alloc_df.loc[mask, "allocated_qty"] = (
                    alloc_df.loc[mask, "allocated_qty"] * scale
                ).round().astype(int)

        # Remove zero allocations
        alloc_df = alloc_df[alloc_df["allocated_qty"] > 0]
        return alloc_df

    # ========================================================================
    # PERSISTENCE
    # ========================================================================

    def _save_allocation_details(
        self, allocation_id: int, alloc_df: pd.DataFrame, alloc_code: str
    ):
        """Bulk save allocation detail rows."""
        if alloc_df.empty:
            return

        records = []
        for _, row in alloc_df.iterrows():
            detail = AllocationDetail(
                allocation_id=allocation_id,
                store_code=str(row.get("store_code", "")),
                gen_article_id=int(row["gen_article_id"]) if pd.notna(row.get("gen_article_id")) else None,
                variant_id=int(row["variant_id"]) if pd.notna(row.get("variant_id")) else None,
                size_code=str(row.get("size_code", "")),
                color_code=str(row.get("color_code", "")),
                allocated_qty=int(row.get("allocated_qty", 0)),
                final_qty=int(row.get("final_qty", row.get("allocated_qty", 0))),
                store_grade=str(row.get("store_grade", "")),
                allocation_basis=str(row.get("allocation_basis", "")),
            )
            records.append(detail)

        # Bulk add
        self.db.bulk_save_objects(records)
        self.db.flush()

        logger.info(f"[{alloc_code}] Saved {len(records)} allocation detail rows")

    # ========================================================================
    # OVERRIDES
    # ========================================================================

    def apply_overrides(
        self,
        allocation_id: int,
        overrides: List[Dict[str, Any]],
        changed_by: str,
        ip_address: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Apply manual overrides to allocation details."""
        header = self.db.query(AllocationHeader).filter(
            AllocationHeader.id == allocation_id
        ).first()
        if not header:
            raise ValueError("Allocation not found")
        if header.status not in ("DRAFT", "IN_PROGRESS"):
            raise ValueError(f"Cannot override allocation in '{header.status}' status")

        applied = 0
        for override in overrides:
            store_code = override.get("store_code")
            variant_id = override.get("variant_id")
            override_qty = override.get("override_qty")

            if not all([store_code, variant_id is not None, override_qty is not None]):
                continue

            detail = self.db.query(AllocationDetail).filter(
                AllocationDetail.allocation_id == allocation_id,
                AllocationDetail.store_code == store_code,
                AllocationDetail.variant_id == variant_id,
            ).first()

            if detail:
                old_qty = detail.final_qty
                detail.override_qty = override_qty
                detail.final_qty = override_qty

                self.audit.log_update(
                    table_name="alloc_detail",
                    changed_by=changed_by,
                    record_pk=str(detail.id),
                    old_data={"final_qty": old_qty, "override_qty": None},
                    new_data={"final_qty": override_qty, "override_qty": override_qty},
                    changed_columns=["override_qty", "final_qty"],
                    ip_address=ip_address,
                )
                applied += 1

        # Update header totals
        header.total_qty = (
            self.db.query(func.sum(AllocationDetail.final_qty))
            .filter(AllocationDetail.allocation_id == allocation_id)
            .scalar() or 0
        )
        self.db.commit()

        return {"applied": applied, "total_qty": header.total_qty}

    # ========================================================================
    # STATUS MANAGEMENT
    # ========================================================================

    def approve_allocation(self, allocation_id: int, approved_by: str) -> Dict[str, Any]:
        """Approve allocation for execution."""
        header = self.db.query(AllocationHeader).filter(
            AllocationHeader.id == allocation_id
        ).first()
        if not header:
            raise ValueError("Allocation not found")
        if header.status != "DRAFT":
            raise ValueError(f"Only DRAFT allocations can be approved (current: {header.status})")

        header.status = "APPROVED"
        header.approved_by = approved_by

        self.audit.log(
            table_name="alloc_header", action_type="UPDATE",
            changed_by=approved_by, record_primary_key=str(allocation_id),
            new_data={"status": "APPROVED"}, notes="Allocation approved",
        )
        self.db.commit()

        return {"allocation_id": allocation_id, "status": "APPROVED"}

    def execute_allocation(self, allocation_id: int, executed_by: str) -> Dict[str, Any]:
        """Execute allocation (lock and mark as EXECUTED)."""
        header = self.db.query(AllocationHeader).filter(
            AllocationHeader.id == allocation_id
        ).first()
        if not header:
            raise ValueError("Allocation not found")
        if header.status != "APPROVED":
            raise ValueError(f"Only APPROVED allocations can be executed (current: {header.status})")

        header.status = "EXECUTED"
        header.executed_at = datetime.now(timezone.utc)

        # TODO: Here you would integrate with warehouse management system
        # to actually reserve/ship the stock

        self.audit.log(
            table_name="alloc_header", action_type="UPDATE",
            changed_by=executed_by, record_primary_key=str(allocation_id),
            new_data={"status": "EXECUTED"}, notes="Allocation executed",
        )
        self.db.commit()

        return {"allocation_id": allocation_id, "status": "EXECUTED"}

    def cancel_allocation(self, allocation_id: int, cancelled_by: str) -> Dict[str, Any]:
        """Cancel an allocation."""
        header = self.db.query(AllocationHeader).filter(
            AllocationHeader.id == allocation_id
        ).first()
        if not header:
            raise ValueError("Allocation not found")
        if header.status == "EXECUTED":
            raise ValueError("Cannot cancel an executed allocation")

        header.status = "CANCELLED"
        self.audit.log(
            table_name="alloc_header", action_type="UPDATE",
            changed_by=cancelled_by, record_primary_key=str(allocation_id),
            new_data={"status": "CANCELLED"}, notes="Allocation cancelled",
        )
        self.db.commit()
        return {"allocation_id": allocation_id, "status": "CANCELLED"}

    # ========================================================================
    # QUERIES
    # ========================================================================

    def get_allocation_details(
        self, allocation_id: int, page: int = 1, page_size: int = 100,
        store_code: Optional[str] = None, size_code: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Get allocation details with pagination."""
        query = self.db.query(AllocationDetail).filter(
            AllocationDetail.allocation_id == allocation_id
        )
        if store_code:
            query = query.filter(AllocationDetail.store_code == store_code)
        if size_code:
            query = query.filter(AllocationDetail.size_code == size_code)

        total = query.count()
        details = query.offset((page - 1) * page_size).limit(page_size).all()

        return {
            "allocation_id": allocation_id,
            "details": [
                {
                    "id": d.id,
                    "store_code": d.store_code,
                    "store_grade": d.store_grade,
                    "gen_article_id": d.gen_article_id,
                    "variant_id": d.variant_id,
                    "size_code": d.size_code,
                    "color_code": d.color_code,
                    "allocated_qty": d.allocated_qty,
                    "override_qty": d.override_qty,
                    "final_qty": d.final_qty,
                    "allocation_basis": d.allocation_basis,
                }
                for d in details
            ],
            "total": total,
            "page": page,
            "page_size": page_size,
        }

    def get_allocation_summary(self, allocation_id: int) -> Dict[str, Any]:
        """Generate allocation summary with breakdowns."""
        details = self.db.query(AllocationDetail).filter(
            AllocationDetail.allocation_id == allocation_id
        ).all()

        if not details:
            return {"total_qty": 0, "total_stores": 0, "total_variants": 0}

        df = pd.DataFrame([{
            "store_code": d.store_code,
            "store_grade": d.store_grade or "",
            "variant_id": d.variant_id,
            "size_code": d.size_code or "",
            "color_code": d.color_code or "",
            "final_qty": d.final_qty or 0,
        } for d in details])

        qty_by_grade = df.groupby("store_grade")["final_qty"].sum().to_dict()
        qty_by_size = df.groupby("size_code")["final_qty"].sum().to_dict()
        qty_by_color = df.groupby("color_code")["final_qty"].sum().to_dict()
        top_stores = (
            df.groupby("store_code")["final_qty"].sum()
            .sort_values(ascending=False)
            .head(10)
            .reset_index()
            .rename(columns={"final_qty": "total_qty"})
            .to_dict(orient="records")
        )

        return {
            "total_qty": int(df["final_qty"].sum()),
            "total_stores": int(df["store_code"].nunique()),
            "total_variants": int(df["variant_id"].nunique()),
            "qty_by_grade": {str(k): int(v) for k, v in qty_by_grade.items()},
            "qty_by_size": {str(k): int(v) for k, v in qty_by_size.items()},
            "qty_by_color": {str(k): int(v) for k, v in qty_by_color.items()},
            "top_stores": top_stores,
        }

    # ========================================================================
    # DATA FETCHING HELPERS
    # ========================================================================

    def _get_eligible_stores(
        self, store_codes: Optional[List[str]], store_grades: Optional[List[str]],
        division_id: Optional[int],
    ) -> pd.DataFrame:
        """Fetch eligible stores based on filters."""
        query = self.db.query(Store).filter(Store.is_active == True)
        if store_codes:
            query = query.filter(Store.store_code.in_(store_codes))
        if store_grades:
            query = query.filter(Store.store_grade.in_(store_grades))
        if division_id:
            from app.models.retail import Division
            div = self.db.query(Division).filter(Division.id == division_id).first()
            if div:
                query = query.filter(Store.division == div.division_name)

        stores = query.all()
        if not stores:
            return pd.DataFrame()

        return pd.DataFrame([{
            "store_code": s.store_code,
            "store_name": s.store_name,
            "store_grade": s.store_grade or "C",
            "region": s.region,
            "hub": s.hub,
            "division": s.division,
        } for s in stores])

    def _get_eligible_variants(
        self, gen_article_ids: Optional[List[int]],
        gen_article_codes: Optional[List[str]],
        division_id: Optional[int], season: Optional[str],
    ) -> pd.DataFrame:
        """Fetch eligible product variants."""
        query = (
            self.db.query(VariantArticle)
            .join(GenArticle)
            .filter(VariantArticle.is_active == True, GenArticle.is_active == True)
        )
        if gen_article_ids:
            query = query.filter(GenArticle.id.in_(gen_article_ids))
        if gen_article_codes:
            query = query.filter(GenArticle.gen_article_code.in_(gen_article_codes))
        if division_id:
            query = query.filter(GenArticle.division_id == division_id)
        if season:
            query = query.filter(GenArticle.season == season)

        variants = query.all()
        if not variants:
            return pd.DataFrame()

        return pd.DataFrame([{
            "variant_id": v.id,
            "variant_code": v.variant_code,
            "gen_article_id": v.gen_article_id,
            "gen_article_code": v.gen_article.gen_article_code if v.gen_article else "",
            "size_code": v.size_code,
            "color_code": v.color_code,
        } for v in variants])

    def _get_warehouse_stock(
        self, warehouse_code: str, variant_codes: List[str]
    ) -> pd.DataFrame:
        """Fetch warehouse stock for given variants."""
        if not variant_codes:
            return pd.DataFrame(columns=["variant_code", "available_qty"])

        stocks = (
            self.db.query(WarehouseStock)
            .filter(
                WarehouseStock.warehouse_code == warehouse_code,
                WarehouseStock.variant_code.in_(variant_codes),
            )
            .all()
        )

        if not stocks:
            return pd.DataFrame(columns=["variant_code", "available_qty"])

        return pd.DataFrame([{
            "variant_code": s.variant_code,
            "stock_qty": s.stock_qty,
            "reserved_qty": s.reserved_qty,
            "available_qty": max(0, (s.stock_qty or 0) - (s.reserved_qty or 0)),
        } for s in stocks])

    # ========================================================================
    # HELPERS
    # ========================================================================

    def _make_alloc_row(self, store, variant_info, qty: int, basis: str) -> Dict:
        """Build a single allocation row dict."""
        return {
            "store_code": store["store_code"] if isinstance(store, dict) else store.get("store_code", ""),
            "store_grade": store.get("store_grade", ""),
            "gen_article_id": variant_info.get("gen_article_id"),
            "gen_article_code": variant_info.get("gen_article_code", ""),
            "variant_id": variant_info.get("variant_id"),
            "variant_code": variant_info.get("variant_code", ""),
            "size_code": variant_info.get("size_code", ""),
            "color_code": variant_info.get("color_code", ""),
            "allocated_qty": qty,
            "allocation_basis": basis,
        }

    def _build_response(
        self, header: AllocationHeader, alloc_df: pd.DataFrame, start_time: float
    ) -> Dict[str, Any]:
        duration_ms = int((time.time() - start_time) * 1000)

        summary = {}
        if not alloc_df.empty:
            summary = {
                "total_qty": int(alloc_df.get("final_qty", alloc_df.get("allocated_qty", pd.Series([0]))).sum()),
                "total_stores": int(alloc_df["store_code"].nunique()) if "store_code" in alloc_df.columns else 0,
                "total_variants": int(alloc_df["variant_code"].nunique()) if "variant_code" in alloc_df.columns else 0,
            }

        return {
            "allocation_id": header.id,
            "allocation_code": header.allocation_code,
            "status": header.status,
            "summary": summary,
            "duration_ms": duration_ms,
        }
