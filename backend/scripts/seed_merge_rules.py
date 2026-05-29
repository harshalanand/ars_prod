"""
Seed initial ARS_MERGE_RULES rows for RNG_SEG and refresh the derived master.

Run once after deploying the derived-masters change:
    python -m backend.scripts.seed_merge_rules

Idempotent: re-running just refreshes Master_CONT_MERGE_RNG_SEG from current rules.
"""
from sqlalchemy import text
from loguru import logger

from app.database.session import get_data_engine
from app.services import derived_masters as dm


SEED_RULES = [
    # (source_col, source_value, target_value, agg)
    ("RNG_SEG", "E",  "EV",  "SUM"),
    ("RNG_SEG", "V",  "EV",  "SUM"),
    ("RNG_SEG", "P",  "PSP", "SUM"),
    ("RNG_SEG", "SP", "PSP", "SUM"),
]


def main():
    engine = get_data_engine()
    with engine.connect() as conn:
        dm.ensure_rules_table(conn)

        inserted, skipped = 0, 0
        for source_col, source_value, target_value, agg in SEED_RULES:
            existing = conn.execute(text(f"""
                SELECT rule_id FROM {dm.RULES_TABLE}
                WHERE source_col = :c AND source_value = :v
            """), {"c": source_col, "v": source_value}).scalar()
            if existing:
                skipped += 1
                continue
            conn.execute(text(f"""
                INSERT INTO {dm.RULES_TABLE}
                    (source_col, source_value, target_value, agg, active, modified_by)
                VALUES (:c, :v, :tv, :a, 1, 'seed_script')
            """), {"c": source_col, "v": source_value, "tv": target_value, "a": agg})
            inserted += 1
        conn.commit()
        logger.info(f"Seed: {inserted} inserted, {skipped} already present")

        # Refresh derived master (if parent Master_CONT_RNG_SEG exists)
        result = dm.refresh_derived_for_source_col(conn, "RNG_SEG")
        logger.info(f"Derived refresh: {result}")


if __name__ == "__main__":
    main()
