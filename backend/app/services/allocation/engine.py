"""
Allocation Engine Orchestrator
Ties all 5 engines together for a complete allocation run.
Processes MAJCATs in parallel using ThreadPoolExecutor.

Pipeline:
  Engine 1 (Budget Cascade) → Engine 2 (Article Scoring) →
  Engine 3 (Global Greedy Fill) → Engine 4 (Size Allocation) →
  Engine 5 (Output/DO Generation)
"""
import logging
import uuid
import time
import json
from typing import Dict, List, Optional
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed
import pandas as pd
from sqlalchemy import text
from sqlalchemy.orm import Session

from .budget_cascade import BudgetCascade
from .article_scorer import ArticleScorer
from .option_filler import GlobalGreedyFiller
from .size_allocator import SizeAllocator

logger = logging.getLogger(__name__)


class AllocationEngine:
    """
    Complete allocation engine orchestrator.
    Replaces the entire Excel-based 29-step allocation system.
    """

    def __init__(self, system_db: Session, data_db: Session):
        self.system_db = system_db
        self.data_db = data_db
        self.settings = {}
        self.weights = {}
        self._load_config()

    def _load_config(self):
        """Load score weights and engine settings from DB. Auto-creates tables if needed."""
        self._ensure_tables()
        try:
            # Load score weights
            rows = self.data_db.execute(
                text("SELECT attribute_name, score_weight FROM alloc_score_config WHERE is_active = 1 AND config_name = :cfg"),
                {'cfg': 'default'}
            ).fetchall()
            self.weights = {r[0]: r[1] for r in rows}
            logger.info(f"Loaded {len(self.weights)} score weights")

            # Load engine settings
            rows = self.data_db.execute(
                text("SELECT setting_key, setting_value FROM alloc_engine_settings")
            ).fetchall()
            self.settings = {r[0]: r[1] for r in rows}
            logger.info(f"Loaded {len(self.settings)} engine settings")
        except Exception as e:
            logger.warning(f"Could not load allocation config: {e}")
            # Use defaults
            self.weights = {
                'ST_SPECIFIC': 9999, 'NATIONAL_HERO': 100, 'CORE_FOCUS': 60,
                'ASSORTED': 30, 'SEG': 30, 'MACRO_MVGR': 25, 'VENDOR': 20,
                'MRP_RANGE': 15, 'FABRIC': 10, 'COLOR': 10, 'SEASON': 10,
                'NECK': 5, 'MVGR1': 15, 'GP_PSF_RANK': 10,
            }
            self.settings = {
                'min_score_threshold': '0', 'multi_option_enabled': 'true',
                'multi_option_min_score': '150', 'multi_option_max_slots': '3',
                'max_colors_per_store': '5', 'parallel_workers': '8',
            }

    def _ensure_tables(self):
        """Auto-create allocation engine tables if they don't exist."""
        try:
            self.data_db.execute(text("SELECT TOP 1 1 FROM alloc_score_config"))
            return  # Tables exist
        except Exception:
            pass

        logger.info("Creating allocation engine tables...")
        table_ddls = [
            """IF NOT EXISTS (SELECT * FROM sys.objects WHERE object_id = OBJECT_ID(N'alloc_score_config') AND type='U')
            CREATE TABLE alloc_score_config (
                id INT IDENTITY(1,1) PRIMARY KEY, config_name NVARCHAR(100) NOT NULL DEFAULT 'default',
                attribute_name NVARCHAR(50) NOT NULL, score_weight INT NOT NULL DEFAULT 0,
                is_active BIT NOT NULL DEFAULT 1, description NVARCHAR(500),
                created_at DATETIME2 NOT NULL DEFAULT SYSUTCDATETIME(),
                updated_at DATETIME2 NOT NULL DEFAULT SYSUTCDATETIME(), updated_by NVARCHAR(100),
                CONSTRAINT UQ_score_config UNIQUE (config_name, attribute_name))""",
            """IF NOT EXISTS (SELECT * FROM sys.objects WHERE object_id = OBJECT_ID(N'alloc_engine_settings') AND type='U')
            CREATE TABLE alloc_engine_settings (
                id INT IDENTITY(1,1) PRIMARY KEY, setting_key NVARCHAR(100) NOT NULL UNIQUE,
                setting_value NVARCHAR(MAX), data_type NVARCHAR(20) DEFAULT 'string',
                description NVARCHAR(500), updated_at DATETIME2 NOT NULL DEFAULT SYSUTCDATETIME(),
                updated_by NVARCHAR(100))""",
            """IF NOT EXISTS (SELECT * FROM sys.objects WHERE object_id = OBJECT_ID(N'alloc_runs') AND type='U')
            CREATE TABLE alloc_runs (
                id INT IDENTITY(1,1) PRIMARY KEY, run_id NVARCHAR(50) NOT NULL UNIQUE,
                run_date DATE NOT NULL DEFAULT GETDATE(), status NVARCHAR(20) NOT NULL DEFAULT 'pending',
                majcats NVARCHAR(MAX), rdc_code NVARCHAR(20), total_majcats INT DEFAULT 0,
                processed_majcats INT DEFAULT 0, total_articles_scored INT DEFAULT 0,
                total_slots_filled INT DEFAULT 0, total_variants_allocated INT DEFAULT 0,
                total_dos_generated INT DEFAULT 0, total_stores INT DEFAULT 0,
                score_config NVARCHAR(100) DEFAULT 'default', settings_json NVARCHAR(MAX),
                error_message NVARCHAR(MAX), created_by NVARCHAR(100),
                created_at DATETIME2 NOT NULL DEFAULT SYSUTCDATETIME(),
                started_at DATETIME2, completed_at DATETIME2, duration_ms INT)""",
            """IF NOT EXISTS (SELECT * FROM sys.objects WHERE object_id = OBJECT_ID(N'alloc_budget_cascade') AND type='U')
            CREATE TABLE alloc_budget_cascade (
                id BIGINT IDENTITY(1,1) PRIMARY KEY, run_id NVARCHAR(50) NOT NULL,
                st_cd NVARCHAR(20) NOT NULL, majcat NVARCHAR(50) NOT NULL, seg NVARCHAR(10),
                macro_mvgr NVARCHAR(50), bgt_disp_q DECIMAL(12,2) DEFAULT 0,
                opt_density INT DEFAULT 0, opt_count INT DEFAULT 0,
                bgt_sales_per_day DECIMAL(10,4) DEFAULT 0, mbq INT DEFAULT 0)""",
            """IF NOT EXISTS (SELECT * FROM sys.objects WHERE object_id = OBJECT_ID(N'alloc_article_scores') AND type='U')
            CREATE TABLE alloc_article_scores (
                id BIGINT IDENTITY(1,1) PRIMARY KEY, run_id NVARCHAR(50) NOT NULL,
                st_cd NVARCHAR(20) NOT NULL, majcat NVARCHAR(50) NOT NULL,
                gen_art_color NVARCHAR(50) NOT NULL, gen_art NVARCHAR(30), color NVARCHAR(20),
                seg NVARCHAR(10), total_score INT NOT NULL DEFAULT 0,
                score_st_specific INT DEFAULT 0, score_hero INT DEFAULT 0, score_focus INT DEFAULT 0,
                score_seg INT DEFAULT 0, score_mvgr INT DEFAULT 0, score_vendor INT DEFAULT 0,
                score_mrp INT DEFAULT 0, score_fabric INT DEFAULT 0, score_color INT DEFAULT 0,
                score_season INT DEFAULT 0, score_neck INT DEFAULT 0, score_gp_psf INT DEFAULT 0,
                dc_stock_qty INT DEFAULT 0, mrp DECIMAL(10,2), vendor_code NVARCHAR(20),
                fabric NVARCHAR(50), season NVARCHAR(10), is_st_specific BIT DEFAULT 0,
                priority_type NVARCHAR(20))""",
            """IF NOT EXISTS (SELECT * FROM sys.objects WHERE object_id = OBJECT_ID(N'alloc_option_assignments') AND type='U')
            CREATE TABLE alloc_option_assignments (
                id BIGINT IDENTITY(1,1) PRIMARY KEY, run_id NVARCHAR(50) NOT NULL,
                st_cd NVARCHAR(20) NOT NULL, majcat NVARCHAR(50) NOT NULL, seg NVARCHAR(10),
                opt_no INT NOT NULL, gen_art_color NVARCHAR(50) NOT NULL, gen_art NVARCHAR(30),
                color NVARCHAR(20), total_score INT DEFAULT 0, art_status NVARCHAR(10),
                is_multi_opt BIT DEFAULT 0, disp_q INT DEFAULT 0, mbq INT DEFAULT 0,
                bgt_sales_per_day DECIMAL(10,4) DEFAULT 0, dc_stock_before INT DEFAULT 0,
                dc_stock_after INT DEFAULT 0)""",
            """IF NOT EXISTS (SELECT * FROM sys.objects WHERE object_id = OBJECT_ID(N'alloc_variant_assignments') AND type='U')
            CREATE TABLE alloc_variant_assignments (
                id BIGINT IDENTITY(1,1) PRIMARY KEY, run_id NVARCHAR(50) NOT NULL,
                st_cd NVARCHAR(20) NOT NULL, gen_art_color NVARCHAR(50) NOT NULL,
                var_art NVARCHAR(30) NOT NULL, sz NVARCHAR(20), alloc_qty INT DEFAULT 0,
                hold_qty INT DEFAULT 0, bgt_sz_cont_pct DECIMAL(8,4) DEFAULT 0,
                dc_sz_stock INT DEFAULT 0, st_sz_stock INT DEFAULT 0,
                fill_rate_pct DECIMAL(5,2) DEFAULT 0, short_qty INT DEFAULT 0, excess_qty INT DEFAULT 0)""",
            """IF NOT EXISTS (SELECT * FROM sys.objects WHERE object_id = OBJECT_ID(N'alloc_delivery_orders') AND type='U')
            CREATE TABLE alloc_delivery_orders (
                id BIGINT IDENTITY(1,1) PRIMARY KEY, run_id NVARCHAR(50) NOT NULL,
                do_number NVARCHAR(50), rdc_code NVARCHAR(20), st_cd NVARCHAR(20) NOT NULL,
                majcat NVARCHAR(50), gen_art NVARCHAR(30), gen_art_color NVARCHAR(50),
                var_art NVARCHAR(30), sz NVARCHAR(20), alloc_qty INT DEFAULT 0,
                status NVARCHAR(20) DEFAULT 'PENDING', posted_at DATETIME2, sap_doc_number NVARCHAR(50))""",
            """IF NOT EXISTS (SELECT * FROM sys.objects WHERE object_id = OBJECT_ID(N'alloc_run_summary') AND type='U')
            CREATE TABLE alloc_run_summary (
                id BIGINT IDENTITY(1,1) PRIMARY KEY, run_id NVARCHAR(50) NOT NULL,
                level NVARCHAR(10) NOT NULL, st_cd NVARCHAR(20), majcat NVARCHAR(50),
                seg NVARCHAR(10), bgt_disp_q INT DEFAULT 0, bgt_opt INT DEFAULT 0,
                filled_opt INT DEFAULT 0, unfilled_opt INT DEFAULT 0, l_art_opt INT DEFAULT 0,
                mix_art_opt INT DEFAULT 0, fallback_opt INT DEFAULT 0, total_alloc_qty INT DEFAULT 0,
                total_do_qty INT DEFAULT 0, avg_score DECIMAL(8,2) DEFAULT 0, min_score INT DEFAULT 0,
                max_score INT DEFAULT 0, fill_rate_pct DECIMAL(5,2) DEFAULT 0)""",
        ]
        try:
            for ddl in table_ddls:
                self.data_db.execute(text(ddl))
            self.data_db.commit()

            # Seed default weights
            count = self.data_db.execute(text("SELECT COUNT(*) FROM alloc_score_config")).scalar()
            if count == 0:
                defaults = [
                    ('ST_SPECIFIC', 9999), ('NATIONAL_HERO', 100), ('CORE_FOCUS', 60),
                    ('ASSORTED', 30), ('SEG', 30), ('MACRO_MVGR', 25), ('VENDOR', 20),
                    ('MRP_RANGE', 15), ('FABRIC', 10), ('COLOR', 10), ('SEASON', 10),
                    ('NECK', 5), ('MVGR1', 15), ('GP_PSF_RANK', 10),
                ]
                for attr, w in defaults:
                    self.data_db.execute(text(
                        "INSERT INTO alloc_score_config(config_name,attribute_name,score_weight) VALUES('default',:a,:w)"
                    ), {'a': attr, 'w': w})

            # Seed settings
            count = self.data_db.execute(text("SELECT COUNT(*) FROM alloc_engine_settings")).scalar()
            if count == 0:
                defaults = [
                    ('min_score_threshold','0'),('multi_option_enabled','true'),
                    ('multi_option_min_score','150'),('multi_option_max_slots','3'),
                    ('max_colors_per_store','5'),('fallback_level','MAJCAT'),
                    ('mbq_accessory_density','3'),('mbq_sales_cover_days','14'),
                    ('mbq_intransit_days','3'),('mbq_scan_days','2'),
                    ('mbq_type','DISP'),  # 7 types: DISP, B_MTH, SSN, DISP+B_MTH, DISP+SSN, DISP/B_MTH, DISP/SSN
                    ('parallel_workers','8'),('score_config_name','default'),
                    ('supabase_url',''),('supabase_key',''),
                    ('supabase_budget_table','v2srm'),
                    ('budget_source','sql'),  # 'sql' or 'supabase'
                ]
                for k, v in defaults:
                    self.data_db.execute(text(
                        "INSERT INTO alloc_engine_settings(setting_key,setting_value) VALUES(:k,:v)"
                    ), {'k': k, 'v': v})

            self.data_db.commit()
            logger.info("✓ Allocation engine tables created and seeded")
        except Exception as e:
            logger.error(f"Failed to create allocation tables: {e}")
            self.data_db.rollback()

    def run(
        self,
        majcats: Optional[List[str]] = None,
        rdc_code: str = 'DH24',
        created_by: str = 'system',
        current_month: int = 4,  # April
    ) -> Dict:
        """
        Run the complete allocation engine.

        Args:
            majcats: List of MAJCATs to process (None = all available)
            rdc_code: Which DC to run for
            created_by: User who triggered the run
            current_month: Current month (1-12)

        Returns:
            Dict with run_id, status, stats
        """
        run_id = str(uuid.uuid4())[:12]
        start_time = time.time()

        logger.info(f"=== ALLOCATION RUN {run_id} START ===")
        logger.info(f"RDC: {rdc_code}, Month: {current_month}, MAJCATs: {majcats or 'ALL'}")

        # Create run record
        try:
            self.data_db.execute(text("""
                INSERT INTO alloc_runs (run_id, run_date, status, majcats, rdc_code, score_config,
                                        settings_json, created_by, started_at)
                VALUES (:run_id, GETDATE(), 'running', :majcats, :rdc, :cfg, :settings, :by, SYSUTCDATETIME())
            """), {
                'run_id': run_id,
                'majcats': json.dumps(majcats) if majcats else None,
                'rdc': rdc_code,
                'cfg': self.settings.get('score_config_name', 'default'),
                'settings': json.dumps(self.settings),
                'by': created_by,
            })
            self.data_db.commit()
        except Exception as e:
            logger.error(f"Failed to create run record: {e}")

        try:
            # ── Discover available MAJCATs ──
            if not majcats:
                majcats = self._get_available_majcats()
            logger.info(f"Processing {len(majcats)} MAJCATs")

            # ── Load shared data (once, not per MAJCAT) ──
            shared_data = self._load_shared_data(rdc_code, current_month, majcats)

            # ── Process MAJCATs ──
            workers = int(self.settings.get('parallel_workers', 8))
            all_results = {}

            if len(majcats) == 1:
                # Single MAJCAT — run directly
                result = self._process_majcat(
                    run_id, majcats[0], rdc_code, current_month, shared_data
                )
                all_results[majcats[0]] = result
            else:
                # Parallel processing
                with ThreadPoolExecutor(max_workers=min(workers, len(majcats))) as executor:
                    futures = {
                        executor.submit(
                            self._process_majcat, run_id, mc, rdc_code, current_month, shared_data
                        ): mc for mc in majcats
                    }
                    for future in as_completed(futures):
                        mc = futures[future]
                        try:
                            result = future.result()
                            all_results[mc] = result
                        except Exception as e:
                            logger.error(f"[{mc}] Failed: {e}")
                            all_results[mc] = {'status': 'failed', 'error': str(e)}

            # ── Aggregate stats ──
            duration_ms = int((time.time() - start_time) * 1000)
            total_scored = sum(r.get('articles_scored', 0) for r in all_results.values())
            total_filled = sum(r.get('slots_filled', 0) for r in all_results.values())
            total_variants = sum(r.get('variants_allocated', 0) for r in all_results.values())
            failed = sum(1 for r in all_results.values() if r.get('status') == 'failed')

            status = 'completed' if failed == 0 else ('partial' if failed < len(majcats) else 'failed')

            # Update run record
            try:
                self.data_db.execute(text("""
                    UPDATE alloc_runs SET
                        status = :status,
                        total_majcats = :total,
                        processed_majcats = :processed,
                        total_articles_scored = :scored,
                        total_slots_filled = :filled,
                        total_variants_allocated = :variants,
                        completed_at = SYSUTCDATETIME(),
                        duration_ms = :dur
                    WHERE run_id = :run_id
                """), {
                    'status': status,
                    'total': len(majcats),
                    'processed': len(majcats) - failed,
                    'scored': total_scored,
                    'filled': total_filled,
                    'variants': total_variants,
                    'dur': duration_ms,
                    'run_id': run_id,
                })
                self.data_db.commit()
            except Exception as e:
                logger.error(f"Failed to update run record: {e}")

            logger.info(f"=== ALLOCATION RUN {run_id} {status.upper()} in {duration_ms}ms ===")
            logger.info(f"  MAJCATs: {len(majcats) - failed}/{len(majcats)} succeeded")
            logger.info(f"  Articles scored: {total_scored}")
            logger.info(f"  Slots filled: {total_filled}")
            logger.info(f"  Variants allocated: {total_variants}")

            return {
                'run_id': run_id,
                'status': status,
                'duration_ms': duration_ms,
                'majcats_total': len(majcats),
                'majcats_processed': len(majcats) - failed,
                'articles_scored': total_scored,
                'slots_filled': total_filled,
                'variants_allocated': total_variants,
                'per_majcat': all_results,
            }

        except Exception as e:
            logger.error(f"Allocation run failed: {e}", exc_info=True)
            try:
                self.data_db.execute(text("""
                    UPDATE alloc_runs SET status='failed', error_message=:err,
                    completed_at=SYSUTCDATETIME() WHERE run_id=:rid
                """), {'err': str(e), 'rid': run_id})
                self.data_db.commit()
            except:
                pass
            return {'run_id': run_id, 'status': 'failed', 'error': str(e)}

    def _process_majcat(
        self, run_id: str, majcat: str, rdc_code: str,
        current_month: int, shared_data: Dict
    ) -> Dict:
        """Process a single MAJCAT through all 5 engines."""
        mc_start = time.time()
        logger.info(f"[{majcat}] ──── Processing ────")

        try:
            # Load MAJCAT-specific data
            msa_articles = self._get_msa_articles(majcat, rdc_code)
            dc_stock = self._get_dc_stock(majcat, rdc_code)
            dc_variant_stock = self._get_dc_variant_stock(majcat, rdc_code)

            if msa_articles.empty:
                logger.warning(f"[{majcat}] No MSA articles found")
                return {'status': 'skipped', 'reason': 'no_msa_articles'}

            logger.info(f"[{majcat}] DIAG: articles={len(msa_articles)}, dc_stock={len(dc_stock)} (sum={dc_stock['stock_qty'].sum() if not dc_stock.empty else 0})")
            logger.info(f"[{majcat}] DIAG: stores={len(shared_data['stores'])}, bgt_majcat={len(shared_data['bgt_majcat'])}")

            # ── Engine 1: Budget Cascade ──
            cascade = BudgetCascade(self.settings)
            budget_result = cascade.cascade(
                bgt_majcat=shared_data['bgt_majcat'],
                bgt_seg=shared_data['bgt_seg'],
                bgt_mvgr=shared_data.get('bgt_mvgr', pd.DataFrame()),
                store_priority=shared_data['store_priority'],
                majcat=majcat,
                current_month=current_month,
            )

            if budget_result.empty:
                # Derive budget from DC stock + fixture density (test mode)
                logger.info(f"[{majcat}] No budget data — deriving from DC stock + fixture density")
                if not dc_stock.empty:
                    # Get fixture density from Supabase
                    density = 16  # default
                    supabase_url = self.settings.get('supabase_url', '')
                    supabase_key = self.settings.get('supabase_key', '')
                    if supabase_url and supabase_key:
                        try:
                            import requests as req_lib
                            r = req_lib.get(
                                f"{supabase_url}/rest/v1/major_category_fixture_density?select=fixture_density&maj_cat=eq.{majcat}",
                                headers={'apikey': s_key, 'Authorization': f'Bearer {s_key}'}, timeout=10
                            )
                            fd = r.json()
                            if fd and len(fd) > 0:
                                density = int(fd[0].get('fixture_density', 16) or 16)
                        except:
                            pass

                    # Get stores with inventory from Supabase
                    stores_with_inv = []
                    if supabase_url and supabase_key:
                        try:
                            r = req_lib.get(
                                f"{supabase_url}/rest/v1/inventory_store_major_category?select=store_code,today_stock_qty&major_category=eq.{majcat}&today_stock_qty=gt.0&limit=500",
                                headers={'apikey': s_key, 'Authorization': f'Bearer {s_key}'}, timeout=15
                            )
                            stores_with_inv = r.json() or []
                        except:
                            pass

                    if not stores_with_inv:
                        # Use all stores from system DB
                        try:
                            stores_df = pd.read_sql("SELECT store_code as st_cd FROM rls_stores WHERE is_active=1", self.system_db.bind)
                            stores_with_inv = [{'store_code': r['st_cd'], 'today_stock_qty': 100} for _, r in stores_df.iterrows()]
                        except:
                            stores_with_inv = [{'store_code': 'TEST01', 'today_stock_qty': 100}]

                    total_dc = dc_stock['stock_qty'].sum()
                    n_stores = len(stores_with_inv)
                    per_store_disp = max(1, int(total_dc / max(n_stores, 1) * 0.3))  # ~30% of equal share

                    synth_rows = []
                    for s in stores_with_inv:
                        st_cd = s.get('store_code', '')
                        stk = s.get('today_stock_qty', 100)
                        synth_rows.append({
                            'st_cd': st_cd, 'majcat': majcat, 'seg': 'ALL', 'macro_mvgr': '',
                            'bgt_disp_q': per_store_disp, 'opt_density': density,
                            'opt_count': density, 'bgt_sales_per_day': round(per_store_disp / 30, 4),
                            'mbq': max(3, int(per_store_disp / density)),
                        })
                    budget_result = pd.DataFrame(synth_rows)
                    logger.info(f"[{majcat}] Derived budget: {len(budget_result)} stores, density={density}, disp/store={per_store_disp}")

            if budget_result.empty:
                return {
                    'status': 'skipped', 'reason': 'no_budget',
                    'diag': {
                        'msa_articles': len(msa_articles),
                        'dc_stock_articles': len(dc_stock),
                        'dc_stock_total': int(dc_stock['stock_qty'].sum()) if not dc_stock.empty else 0,
                        'bgt_majcat_total': len(shared_data['bgt_majcat']),
                    }
                }

            # ══════════════════════════════════════════════════════
            # UNIFIED ARCHITECTURE: Read from Snowflake directly
            # Snowflake has 246M pre-scored pairs + 4.97M store stock
            # Azure only runs Engines 3-5 (fill → size → DO)
            # ══════════════════════════════════════════════════════
            scored = pd.DataFrame()
            store_stock_gencolor = pd.DataFrame()
            
            try:
                from . import snowflake_loader as sf
                
                # Engine 1+2: Get pre-computed scores from Snowflake (246M pairs)
                scored = sf.get_scored_pairs(majcat, top_n=200)
                
                if not scored.empty:
                    # Get store stock for L-ART waterfall
                    scored_gacs = scored['gen_art_color'].unique().tolist()
                    store_stock_gencolor = sf.get_store_stock(scored_gacs, majcat)
                    
                    # Get budget from Snowflake if not already loaded
                    if budget_result.empty:
                        budget_result = sf.get_budget_cascade(majcat)
                        logger.info(f"[{majcat}] Budget from Snowflake: {len(budget_result)} stores")
                    
                    # Get DC variant stock for size allocation
                    dc_variant_stock = sf.get_dc_variant_stock(scored_gacs, majcat)
                    
            except ImportError:
                logger.warning(f"[{majcat}] snowflake_loader not available")
            except Exception as e:
                logger.warning(f"[{majcat}] Snowflake query failed: {e}")
            
            # Fallback: local scorer if Snowflake unavailable
            if scored.empty:
                logger.info(f"[{majcat}] Fallback: using local scorer")
                scorer = ArticleScorer(self.weights, self.settings)
                scored = scorer.score(
                    msa_articles=msa_articles, stores=shared_data['stores'],
                    dc_stock=dc_stock, priority_list=shared_data['priority_list'],
                    st_specific=shared_data['st_specific'], budget_cascade=budget_result,
                    majcat=majcat,
                )

            if scored.empty:
                return {
                    'status': 'skipped', 'reason': 'no_scores',
                    'diag': {'msa_articles': len(msa_articles), 'budget_rows': len(budget_result)}
                }

            # ── Engine 3: Global Greedy Fill ──
            filler = GlobalGreedyFiller(self.settings)
            assignments = filler.fill(
                scored_pairs=scored,
                budget_cascade=budget_result,
                majcat=majcat,
                store_stock_gencolor=store_stock_gencolor,
            )

            if assignments.empty:
                return {
                    'status': 'completed', 'slots_filled': 0,
                    'articles_scored': len(scored),
                    'diag': {
                        'msa_articles': len(msa_articles),
                        'dc_stock_articles': len(dc_stock),
                        'dc_stock_total': int(dc_stock['stock_qty'].sum()) if not dc_stock.empty else 0,
                        'stores': len(shared_data['stores']),
                        'budget_rows': len(budget_result),
                        'budget_opt_total': int(budget_result['opt_count'].sum()) if not budget_result.empty and 'opt_count' in budget_result.columns else 0,
                        'scored_pairs': len(scored),
                        'scored_sample': scored[['st_cd','gen_art_color','total_score']].head(3).to_dict('records') if not scored.empty else [],
                        'budget_sample': budget_result[['st_cd','opt_count','mbq']].head(3).to_dict('records') if not budget_result.empty and 'opt_count' in budget_result.columns else [],
                    }
                }

            # ── Merge MBQ from budget cascade into assignments ──
            if not budget_result.empty and 'mbq' in budget_result.columns:
                bgt_lookup = budget_result.groupby('st_cd').first()[['mbq', 'bgt_disp_q', 'bgt_sales_per_day']].reset_index()
                assignments = assignments.merge(
                    bgt_lookup.rename(columns={'mbq': '_mbq', 'bgt_disp_q': '_disp_q', 'bgt_sales_per_day': '_spd'}),
                    on='st_cd', how='left'
                )
                assignments['mbq'] = assignments['_mbq'].fillna(0).astype(int)
                assignments['disp_q'] = assignments['_disp_q'].fillna(0).astype(int)
                assignments['bgt_sales_per_day'] = assignments['_spd'].fillna(0)
                assignments.drop(columns=['_mbq', '_disp_q', '_spd'], inplace=True, errors='ignore')

            # ── Engine 4: Size Allocation ──
            size_alloc = SizeAllocator(self.settings)
            variants = size_alloc.allocate(
                option_assignments=assignments,
                dc_variant_stock=dc_variant_stock,
                bgt_size=shared_data.get('bgt_size', pd.DataFrame()),
                store_stock=shared_data.get('store_stock', pd.DataFrame()),
                majcat=majcat,
            )

            # ── Engine 5: Save Results ──
            self._save_results(run_id, majcat, budget_result, scored, assignments, variants)

            mc_dur = int((time.time() - mc_start) * 1000)
            result = {
                'status': 'completed',
                'duration_ms': mc_dur,
                'articles_scored': len(scored),
                'slots_filled': len(assignments),
                'variants_allocated': len(variants) if not variants.empty else 0,
                'avg_score': round(assignments['total_score'].mean(), 1) if not assignments.empty else 0,
            }
            logger.info(f"[{majcat}] ──── Done in {mc_dur}ms ────")
            return result

        except Exception as e:
            logger.error(f"[{majcat}] Failed: {e}", exc_info=True)
            return {'status': 'failed', 'error': str(e)}

    # ── Data Loading Methods ──

    def _load_shared_data(self, rdc_code: str, current_month: int, majcats: List[str] = None) -> Dict:
        """Load data shared across all MAJCATs (loaded once)."""
        logger.info("Loading shared data...")
        data = {}

        # Stores
        try:
            data['stores'] = pd.read_sql(
                "SELECT store_code as st_cd, store_name as st_nm FROM rls_stores WHERE is_active = 1",
                self.system_db.bind
            )
            logger.info(f"  Stores: {len(data['stores'])}")
        except:
            data['stores'] = pd.DataFrame(columns=['st_cd', 'st_nm'])

        # Fallback: derive stores from ALLOCATION_MRDC_RAW_DATA if rls_stores is empty
        if data['stores'].empty:
            try:
                data['stores'] = pd.read_sql(
                    "SELECT DISTINCT STORE_CODE as st_cd, STORE_CODE as st_nm FROM ALLOCATION_MRDC_RAW_DATA WHERE STORE_CODE IS NOT NULL",
                    self.data_db.bind
                )
                logger.info(f"  Stores (from ALLOCATION_MRDC): {len(data['stores'])}")
            except:
                data['stores'] = pd.DataFrame(columns=['st_cd', 'st_nm'])

        # If still empty or only DC codes, derive from budget data after it's loaded
        # (will be populated after budget loading below)

        # Store priority
        try:
            data['store_priority'] = pd.read_sql(
                "SELECT * FROM store_priority", self.data_db.bind
            )
        except:
            data['store_priority'] = pd.DataFrame(columns=['st_cd', 'priority_rank', 'opt_density'])

        # Budget tables — from Supabase or SQL Server
        budget_source = self.settings.get('budget_source', 'sql')
        if budget_source == 'supabase':
            supabase_url = self.settings.get('supabase_url', '')
            supabase_key = self.settings.get('supabase_key', '')
            if supabase_url and supabase_key:
                data['bgt_majcat'] = self._load_supabase_budgets(
                    supabase_url, supabase_key, current_month, majcats
                )
                logger.info(f"  Budget from Supabase: {len(data['bgt_majcat'])} rows")
            else:
                logger.warning("  Supabase configured but URL/key missing — falling back to SQL")
                budget_source = 'sql'

        if budget_source == 'sql':
            for tbl in ['budget_majcat', 'budget_seg', 'budget_mvgr', 'budget_size']:
                key = f"bgt_{tbl.split('_')[1]}"
                try:
                    data[key] = pd.read_sql(f"SELECT * FROM {tbl}", self.data_db.bind)
                    logger.info(f"  {tbl}: {len(data[key])} rows")
                except:
                    data[key] = pd.DataFrame()

        # Ensure all budget keys exist
        for key in ['bgt_majcat', 'bgt_seg', 'bgt_mvgr', 'bgt_size']:
            if key not in data:
                data[key] = pd.DataFrame()

        # Load size contribution from Supabase if not from SQL
        if data['bgt_size'].empty and budget_source == 'supabase':
            s_url = self.settings.get('supabase_url', '')
            s_key = self.settings.get('supabase_key', '')
            if s_url and s_key:
                try:
                    import requests as req_lib
                    # Pull size contribution for relevant months
                    month_names = {1:'jan',2:'feb',3:'mar',4:'apr',5:'may',6:'jun',7:'jul',8:'aug',9:'sep',10:'oct',11:'nov',12:'dec'}
                    mon = month_names.get(current_month, 'apr')
                    yr = 2026 if current_month <= 8 else 2025
                    sale_col = f'sale_q_{mon}_{yr}'
                    # Try current month, fall back to nearest month with data
                    for try_col in [sale_col, 'sale_q_feb_2026', 'sale_q_jan_2026', 'sale_q_mar_2026']:
                        url = (f'{s_url}/rest/v1/co_budget_company_major_category_size'
                               f'?select=major_category,size,{try_col}'
                               f'&{try_col}=gt.0&limit=5000')
                        resp = req_lib.get(url, headers={'apikey': s_key, 'Authorization': f'Bearer {s_key}'}, timeout=30)
                        if resp.status_code == 200:
                            rows = resp.json()
                            if rows:
                                df = pd.DataFrame(rows)
                                totals = df.groupby('major_category')[try_col].sum().reset_index()
                                totals.rename(columns={try_col: 'total'}, inplace=True)
                                df = df.merge(totals, on='major_category')
                                df['bgt_cont_pct'] = df[try_col] / df['total'].clip(lower=1)
                                df.rename(columns={'major_category': 'majcat', 'size': 'sz'}, inplace=True)
                                data['bgt_size'] = df[['majcat', 'sz', 'bgt_cont_pct']]
                                logger.info(f"  Size contribution from Supabase ({try_col}): {len(df)} rows, {df['majcat'].nunique()} MAJCATs")
                                break
                except Exception as e:
                    logger.warning(f"  Size contribution from Supabase failed: {e}")

        # Derive stores from budget data if store list is small (DC codes only)
        if len(data['stores']) < 10 and not data['bgt_majcat'].empty:
            st_col = 'st_cd' if 'st_cd' in data['bgt_majcat'].columns else 'store_code'
            if st_col in data['bgt_majcat'].columns:
                budget_stores = data['bgt_majcat'][[st_col]].drop_duplicates().rename(columns={st_col: 'st_cd'})
                budget_stores['st_nm'] = budget_stores['st_cd']
                data['stores'] = budget_stores
                logger.info(f"  Stores (from budget data): {len(data['stores'])}")

        # Priority list
        try:
            data['priority_list'] = pd.read_sql(
                "SELECT * FROM dc_article_priority", self.data_db.bind
            )
            logger.info(f"  Priority list: {len(data['priority_list'])}")
        except:
            data['priority_list'] = pd.DataFrame()

        # ST-SPECIFIC
        try:
            data['st_specific'] = pd.read_sql(
                "SELECT st_cd, gen_clr as gen_art_color, is_specific FROM store_specific_listing WHERE is_specific = 1",
                self.data_db.bind
            )
            logger.info(f"  ST-SPECIFIC: {len(data['st_specific'])}")
        except:
            data['st_specific'] = pd.DataFrame()

        # Store stock — loaded per-MAJCAT during run (not at startup, too large)
        data['store_stock'] = pd.DataFrame()
        data['store_stock_gencolor'] = pd.DataFrame()
        # Snowflake connection will be used per-MAJCAT in _run_majcat

        return data

    def _load_supabase_budgets(self, supabase_url: str, supabase_key: str, current_month: int, majcats: List[str] = None) -> pd.DataFrame:
        """Load store-level budgets directly from Supabase co_budget_store_major_category."""
        import requests
        headers = {'apikey': supabase_key, 'Authorization': f'Bearer {supabase_key}'}
        month_names = {1:'jan',2:'feb',3:'mar',4:'apr',5:'may',6:'jun',7:'jul',8:'aug',9:'sep',10:'oct',11:'nov',12:'dec'}
        mon = month_names.get(current_month, 'apr')
        year = 2026 if current_month <= 8 else 2025

        sale_col = f'sale_q_{mon}_{year}'
        disp_col = f'disp_q_{mon}_{year}'
        table = self.settings.get('supabase_budget_table', 'co_budget_store_major_category')

        # Build MAJCAT filter for targeted loading
        mc_filter = ''
        if majcats and len(majcats) <= 20:
            mc_list = ','.join(majcats)
            mc_filter = f'&major_category=in.({mc_list})'
            logger.info(f"  Supabase budget filter: {len(majcats)} MAJCATs")

        # Pull rows with budget > 0 for this month (paginate in 5000-row chunks)
        all_rows = []
        offset = 0
        batch = 5000
        max_pages = 100  # Safety limit
        page = 0
        while page < max_pages:
            url = (f'{supabase_url}/rest/v1/{table}'
                   f'?select=store_code,major_category,{sale_col},{disp_col}'
                   f'&{sale_col}=gt.0'
                   f'{mc_filter}'
                   f'&order=store_code'
                   f'&limit={batch}&offset={offset}')
            try:
                resp = requests.get(url, headers=headers, timeout=120)
                if resp.status_code != 200:
                    logger.warning(f"Supabase returned {resp.status_code}: {resp.text[:200]}")
                    break
                chunk = resp.json()
                if not chunk:
                    break
                all_rows.extend(chunk)
                logger.info(f"  Supabase budget page {page+1}: +{len(chunk)} rows (total: {len(all_rows)})")
                offset += batch
                page += 1
                if len(chunk) < batch:
                    break
            except Exception as e:
                logger.error(f"Supabase fetch error at offset {offset}: {e}")
                break

        if not all_rows:
            logger.warning("No budget data from Supabase")
            return pd.DataFrame()

        df = pd.DataFrame(all_rows)
        df.rename(columns={
            'store_code': 'st_cd',
            'major_category': 'majcat',
            sale_col: 'bgt_sales',
            disp_col: 'bgt_display',
        }, inplace=True)

        logger.info(f"  Supabase budget: {len(df)} rows, {df['majcat'].nunique()} MAJCATs, "
                    f"total sale_q={df['bgt_sales'].sum():.0f}, disp_q={df['bgt_display'].sum():.0f}")
        return df

    def _get_available_majcats(self) -> List[str]:
        """Get list of MAJCATs with allocation data."""
        for query in [
            "SELECT DISTINCT majcat FROM msa_articles WHERE majcat IS NOT NULL ORDER BY majcat",
            "SELECT DISTINCT MAJ_CAT FROM ARS_MSA_GEN_ART WHERE MAJ_CAT IS NOT NULL ORDER BY MAJ_CAT",
            "SELECT DISTINCT MAJ_CAT FROM ALLOCATION_MRDC_RAW_DATA WHERE MAJ_CAT IS NOT NULL ORDER BY MAJ_CAT",
        ]:
            try:
                rows = self.data_db.execute(text(query)).fetchall()
                if rows:
                    return [r[0] for r in rows]
            except:
                continue
        return []

    def _get_msa_articles(self, majcat: str, rdc_code: str) -> pd.DataFrame:
        """Get MSA articles for a MAJCAT."""
        try:
            return pd.read_sql(text("""
                SELECT gen_art_color, gen_art, color, seg, macro_mvgr, mvgr1,
                       vendor_code, mrp, fabric, season, neck, majcat
                FROM msa_articles WHERE majcat = :mc
            """), self.data_db.bind, params={'mc': majcat})
        except:
            pass
        # Fallback: ALLOCATION_MRDC_RAW_DATA (the actual V2 data source)
        try:
            df = pd.read_sql(text("""
                SELECT DISTINCT
                    CAST(GEN_ART_NUMBER AS VARCHAR) + '_' + ISNULL(CLR, 'NA') as gen_art_color,
                    CAST(GEN_ART_NUMBER AS VARCHAR) as gen_art,
                    ISNULL(CLR, 'NA') as color,
                    ISNULL(SEG, '') as seg,
                    ISNULL(MACRO_MVGR, '') as macro_mvgr,
                    ISNULL(MVGR_SEG, '') as mvgr1,
                    CAST(ISNULL(MERGE_VENDOR_CODE, 0) AS VARCHAR) as vendor_code,
                    ISNULL(MRP, 0) as mrp,
                    ISNULL(FAB, '') as fabric,
                    ISNULL(SSN, '') as season,
                    '' as neck,
                    MAJ_CAT as majcat
                FROM ALLOCATION_MRDC_RAW_DATA
                WHERE MAJ_CAT = :mc
            """), self.data_db.bind, params={'mc': majcat})
            if not df.empty:
                logger.info(f"[{majcat}] Loaded {len(df)} articles from ALLOCATION_MRDC_RAW_DATA")
            return df
        except Exception as e:
            logger.error(f"Could not load articles for {majcat}: {e}")
            return pd.DataFrame()

    def _get_dc_stock(self, majcat: str, rdc_code: str) -> pd.DataFrame:
        """Get DC stock for a MAJCAT."""
        try:
            return pd.read_sql(text("""
                SELECT gen_art_color, SUM(stock_qty) as stock_qty
                FROM dc_stock WHERE majcat = :mc
                GROUP BY gen_art_color
            """), self.data_db.bind, params={'mc': majcat})
        except:
            pass
        # Fallback: derive from ALLOCATION_MRDC_RAW_DATA
        # V04 = DC stock qty (same across all store rows), V01 = store stock, V06 = intransit
        try:
            df = pd.read_sql(text("""
                SELECT
                    CAST(GEN_ART_NUMBER AS VARCHAR) + '_' + ISNULL(CLR, 'NA') as gen_art_color,
                    MAX(ISNULL(V04, 0)) as stock_qty
                FROM ALLOCATION_MRDC_RAW_DATA
                WHERE MAJ_CAT = :mc
                GROUP BY CAST(GEN_ART_NUMBER AS VARCHAR) + '_' + ISNULL(CLR, 'NA')
                HAVING MAX(ISNULL(V04, 0)) > 0
            """), self.data_db.bind, params={'mc': majcat})
            if not df.empty:
                logger.info(f"[{majcat}] DC stock: {len(df)} articles, total: {df['stock_qty'].sum():.0f}")
            else:
                # V04 is all zeros — try Supabase inventory_dc_major_category
                df = self._get_dc_stock_from_supabase(majcat)
            return df
        except Exception as e:
            logger.error(f"Could not load DC stock for {majcat}: {e}")
            # Last resort: try Supabase
            return self._get_dc_stock_from_supabase(majcat)

    def _get_dc_stock_from_supabase(self, majcat: str) -> pd.DataFrame:
        """Get DC stock from Supabase and distribute across articles."""
        import requests
        supabase_url = self.settings.get('supabase_url', '')
        supabase_key = self.settings.get('supabase_key', '')
        if not supabase_url or not supabase_key:
            return pd.DataFrame(columns=['gen_art_color', 'stock_qty'])

        headers = {'apikey': supabase_key, 'Authorization': f'Bearer {supabase_key}'}
        try:
            # Get total DC stock for this MAJCAT
            resp = requests.get(
                f'{supabase_url}/rest/v1/inventory_dc_major_category?select=td_stk_qty&major_category=eq.{majcat}',
                headers=headers, timeout=15)
            if resp.status_code != 200:
                return pd.DataFrame(columns=['gen_art_color', 'stock_qty'])
            dc_data = resp.json()
            total_dc = sum(r.get('td_stk_qty', 0) or 0 for r in dc_data)
            if total_dc <= 0:
                return pd.DataFrame(columns=['gen_art_color', 'stock_qty'])

            # Get article list from SQL
            try:
                arts = pd.read_sql(text("""
                    SELECT DISTINCT CAST(GEN_ART_NUMBER AS VARCHAR) + '_' + ISNULL(CLR, 'NA') as gen_art_color
                    FROM ALLOCATION_MRDC_RAW_DATA WHERE MAJ_CAT = :mc
                """), self.data_db.bind, params={'mc': majcat})
            except:
                arts = pd.DataFrame(columns=['gen_art_color'])

            if arts.empty:
                return pd.DataFrame(columns=['gen_art_color', 'stock_qty'])

            # Distribute DC stock evenly across articles
            per_art = max(1, int(total_dc / len(arts)))
            arts['stock_qty'] = per_art
            logger.info(f"[{majcat}] DC stock from Supabase: {total_dc} total, {len(arts)} articles, {per_art}/article")
            return arts
        except Exception as e:
            logger.error(f"Supabase DC stock failed for {majcat}: {e}")
            return pd.DataFrame(columns=['gen_art_color', 'stock_qty'])

    def _get_dc_variant_stock(self, majcat: str, rdc_code: str) -> pd.DataFrame:
        """Get DC variant-level stock for a MAJCAT."""
        try:
            return pd.read_sql(text("""
                SELECT gen_art_color, var_art, sz, stock_qty
                FROM dc_variant_stock WHERE gen_art_color IN (
                    SELECT gen_art_color FROM dc_stock WHERE majcat = :mc
                )
            """), self.data_db.bind, params={'mc': majcat})
        except:
            pass
        # Fallback: derive from ALLOCATION_MRDC_RAW_DATA
        try:
            df = pd.read_sql(text("""
                SELECT
                    CAST(GEN_ART_NUMBER AS VARCHAR) + '_' + ISNULL(CLR, 'NA') as gen_art_color,
                    CAST(ARTICLE_NUMBER AS VARCHAR) as var_art,
                    ISNULL(SIZE, '') as sz,
                    ISNULL(V04, 0) as stock_qty
                FROM ALLOCATION_MRDC_RAW_DATA
                WHERE MAJ_CAT = :mc AND STORE_CODE = :rdc AND ISNULL(V04, 0) > 0
            """), self.data_db.bind, params={'mc': majcat, 'rdc': rdc_code})
            return df
        except Exception as e:
            logger.error(f"Could not load variant stock for {majcat}: {e}")
            return pd.DataFrame(columns=['gen_art_color', 'var_art', 'sz', 'stock_qty'])

    def _save_results(self, run_id, majcat, budget, scores, assignments, variants):
        """Save all results to the database."""
        try:
            # Ensure new columns exist (schema migration)
            self._ensure_columns('alloc_variant_assignments', {
                'mrp': 'DECIMAL(12,2) DEFAULT 0',
                'majcat': 'NVARCHAR(100)',
            })
            self._ensure_columns('alloc_option_assignments', {
                'mrp': 'DECIMAL(12,2) DEFAULT 0',
            })

            # Save budget cascade
            if not budget.empty:
                budget['run_id'] = run_id
                budget.to_sql('alloc_budget_cascade', self.data_db.bind,
                              if_exists='append', index=False, method='multi', chunksize=500)

            # Save top scores (limit to keep DB manageable)
            if not scores.empty:
                top_scores = scores.head(50000).copy()
                top_scores['run_id'] = run_id
                cols_to_save = [c for c in top_scores.columns if c in [
                    'run_id', 'st_cd', 'majcat', 'gen_art_color', 'gen_art', 'color', 'seg',
                    'total_score', 'score_st_specific', 'score_hero', 'score_focus',
                    'score_seg', 'score_mvgr', 'score_vendor', 'score_mrp',
                    'score_fabric', 'score_color', 'score_season', 'score_neck',
                    'dc_stock_qty', 'mrp', 'vendor_code', 'fabric', 'season',
                    'is_st_specific', 'priority_type',
                ]]
                top_scores[cols_to_save].to_sql(
                    'alloc_article_scores', self.data_db.bind,
                    if_exists='append', index=False, method='multi', chunksize=1000
                )

            # Save option assignments
            if not assignments.empty:
                assignments['run_id'] = run_id
                assignments.to_sql('alloc_option_assignments', self.data_db.bind,
                                   if_exists='append', index=False, method='multi', chunksize=500)
                logger.info(f"[{majcat}] Saved {len(assignments)} option assignments")

            # Save variant assignments
            if not variants.empty:
                variants['run_id'] = run_id
                variants.to_sql('alloc_variant_assignments', self.data_db.bind,
                                if_exists='append', index=False, method='multi', chunksize=1000)
                logger.info(f"[{majcat}] Saved {len(variants)} variant assignments")

            self.data_db.commit()
            logger.info(f"[{majcat}] Results saved to database")
        except Exception as e:
            logger.error(f"[{majcat}] Failed to save results: {e}")
            self.data_db.rollback()

    def _ensure_columns(self, table_name, columns):
        """Add columns to table if they don't exist."""
        try:
            for col_name, col_type in columns.items():
                self.data_db.execute(text(f"""
                    IF NOT EXISTS (
                        SELECT 1 FROM INFORMATION_SCHEMA.COLUMNS 
                        WHERE TABLE_NAME = :tbl AND COLUMN_NAME = :col
                    )
                    ALTER TABLE [{table_name}] ADD [{col_name}] {col_type}
                """), {'tbl': table_name, 'col': col_name})
            self.data_db.commit()
        except Exception as e:
            logger.warning(f"Column migration for {table_name}: {e}")
            try:
                self.data_db.rollback()
            except:
                pass
