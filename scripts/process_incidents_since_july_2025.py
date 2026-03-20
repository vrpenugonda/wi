#!/usr/bin/env python3
"""
Find missing incidents in Snowflake since July 2025.

This script:
1. Queries all incident IDs from the PostgreSQL database since July 1, 2025
2. Queries all incident IDs currently in Snowflake  
3. Outputs list of missing IDs (in DB but not in Snowflake) to CSV

Usage:
    python scripts/process_incidents_since_july_2025.py [--output-dir DIR]
    
Arguments:
    --output-dir    Output directory for results (default: data/output/july_2025_backfill)
"""

import argparse
import logging
import sys
from datetime import datetime
from pathlib import Path

# Add project root to path for imports
PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import pandas as pd
from sqlalchemy import text

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

# Import shared database utilities
from insights.utils.database import build_incidents_query, get_engine
from insights.utils.snowflake import delete_incidents_from_snowflake, get_incident_ids_from_snowflake


def get_incidents_from_database(start_date: str = "2025-07-01") -> pd.DataFrame:
    """
    Query all incidents from the database since the start date.
    Uses the shared build_incidents_query function for consistent filtering.
    
    Returns:
        DataFrame with incident data.
    """
    query = build_incidents_query(start_date=start_date)
    
    logger.info(f"Querying incidents from database since {start_date}...")
    
    engine = get_engine()
    with engine.connect() as conn:
        df = pd.read_sql(text(query), conn)
    
    engine.dispose()
    logger.info(f"Found {len(df)} incidents in database")
    
    return df


def main():
    parser = argparse.ArgumentParser(
        description="Find missing incidents in Snowflake since July 2025",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("data/output/july_2025_backfill"),
        help="Output directory for results",
    )
    parser.add_argument(
        "--start-date",
        type=str,
        default="2025-07-01",
        help="Start date for incident query (default: 2025-07-01)",
    )
    
    args = parser.parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    
    # =========================================================================
    # STEP 1: Get incidents from PostgreSQL database
    # =========================================================================
    print("\n" + "=" * 60)
    print("STEP 1: QUERYING INCIDENTS FROM DATABASE")
    print("=" * 60)
    
    try:
        db_df = get_incidents_from_database(start_date=args.start_date)
    except Exception as e:
        logger.error(f"Failed to query database: {e}")
        return 1
    
    if db_df.empty:
        logger.warning("No incidents found in database")
        return 0
    
    # Save database incidents to CSV
    db_csv_path = args.output_dir / f"db_incidents_{timestamp}.csv"
    db_df.to_csv(db_csv_path, index=False)
    logger.info(f"Saved database incidents to: {db_csv_path}")
    
    db_incident_ids = set(db_df['in_id'].unique())
    
    print(f"\nTotal incidents in database: {len(db_incident_ids)}")
    if 'closed_at' in db_df.columns:
        closed_df = db_df[db_df['closed_at'].notna()]
        if not closed_df.empty:
            print(f"Close time range: {closed_df['closed_at'].min()} to {closed_df['closed_at'].max()}")
    
    # =========================================================================
    # STEP 2: Get incidents from Snowflake
    # =========================================================================
    print("\n" + "=" * 60)
    print("STEP 2: QUERYING INCIDENTS FROM SNOWFLAKE")
    print("=" * 60)
    
    try:
        snowflake_ids = get_incident_ids_from_snowflake(start_date=args.start_date)
    except Exception as e:
        logger.error(f"Failed to query Snowflake: {e}")
        return 1
    
    print(f"\nTotal incidents in Snowflake: {len(snowflake_ids)}")
    
    # Save Snowflake IDs to CSV
    sf_csv_path = args.output_dir / f"snowflake_ids_{timestamp}.csv"
    pd.DataFrame({'in_id': list(snowflake_ids)}).to_csv(sf_csv_path, index=False)
    logger.info(f"Saved Snowflake IDs to: {sf_csv_path}")
    
    # =========================================================================
    # STEP 3: Find missing incidents and output to CSV
    # =========================================================================
    print("\n" + "=" * 60)
    print("STEP 3: FINDING MISSING INCIDENTS")
    print("=" * 60)
    
    # IDs in database but NOT in Snowflake
    missing_ids = db_incident_ids - snowflake_ids
    
    # IDs in Snowflake but NOT in database (stale data to remove)
    extra_ids = snowflake_ids - db_incident_ids
    
    print(f"\nSummary:")
    print(f"  Incidents in database:   {len(db_incident_ids):,}")
    print(f"  Incidents in Snowflake:  {len(snowflake_ids):,}")
    print(f"  Missing from Snowflake:  {len(missing_ids):,}")
    print(f"  Extra in Snowflake:      {len(extra_ids):,}")
    
    # Save missing IDs to CSV with full incident details
    if missing_ids:
        missing_df = db_df[db_df['in_id'].isin(missing_ids)].copy()
        missing_csv_path = args.output_dir / f"missing_incidents_{timestamp}.csv"
        missing_df.to_csv(missing_csv_path, index=False)
        logger.info(f"Saved missing incidents to: {missing_csv_path}")
        
        # Also save just the IDs
        missing_ids_path = args.output_dir / f"missing_ids_{timestamp}.txt"
        with open(missing_ids_path, 'w') as f:
            for in_id in sorted(missing_ids):
                f.write(f"{in_id}\n")
        logger.info(f"Saved missing IDs to: {missing_ids_path}")
        
        print(f"\n  Missing incidents saved to: {missing_csv_path}")
        print(f"  Missing IDs saved to: {missing_ids_path}")
    else:
        print("\n  No missing incidents - Snowflake is up to date!")
    
    # =========================================================================
    # STEP 4: Delete extra incidents from Snowflake
    # =========================================================================
    if extra_ids:
        print("\n" + "=" * 60)
        print("STEP 4: DELETING EXTRA INCIDENTS FROM SNOWFLAKE")
        print("=" * 60)
        
        # Save extra IDs before deletion for audit
        extra_ids_path = args.output_dir / f"deleted_snowflake_ids_{timestamp}.txt"
        with open(extra_ids_path, 'w') as f:
            for in_id in sorted(extra_ids):
                f.write(f"{in_id}\n")
        logger.info(f"Saved IDs to delete to: {extra_ids_path}")
        
        try:
            deleted_count = delete_incidents_from_snowflake(extra_ids)
            print(f"\n  Deleted {deleted_count:,} extra incidents from Snowflake")
            print(f"  Deleted IDs saved to: {extra_ids_path}")
        except Exception as e:
            logger.error(f"Failed to delete from Snowflake: {e}")
            return 1
    
    print("\n" + "=" * 60)
    print("COMPLETE")
    print("=" * 60)
    
    return 0


if __name__ == "__main__":
    sys.exit(main())
