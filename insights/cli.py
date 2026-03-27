"""
WALLE CLI - Command Line Interface for Incident Classification

Usage:
    # Run full pipeline (L123 + L4)
    python -m walle.cli run
    
    # Run L123 classification only
    python -m walle.cli l123
    
    # Run L4 classification only
    python -m walle.cli l4 --input classified.csv --subcategory VPN_RemoteAccess
    
    # List available categories
    python -m walle.cli list --taxonomy
    
    # Derive L4 taxonomy only
    python -m walle.cli taxonomy --input classified.csv --subcategory VPN_RemoteAccess
"""

import argparse
import asyncio
import sys
from typing import Any

import pandas as pd


def create_parser() -> argparse.ArgumentParser:
    """Create the CLI argument parser."""
    parser = argparse.ArgumentParser(
        prog="walle",
        description="WALLE Incident Classification System",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    
    parser.add_argument(
        "--debug", "-d",
        action="store_true",
        help="Enable debug output",
    )
    
    subparsers = parser.add_subparsers(dest="command", help="Available commands")
    
    # =========================================================================
    # run command - Full pipeline
    # =========================================================================
    run_parser = subparsers.add_parser("run", help="Run full classification pipeline")
    run_parser.add_argument(
        "--input", "-i",
        help="Input CSV file (skip database fetch if provided)",
    )
    run_parser.add_argument(
        "--days-back",
        type=int,
        default=None,
        help="Days to look back for incidents (default: 180)",
    )
    run_parser.add_argument(
        "--hours-back",
        type=int,
        default=None,
        help="Hours to look back for incidents (overrides --days-back)",
    )
    run_parser.add_argument(
        "--minutes-back",
        type=int,
        default=None,
        help="Minutes to look back for incidents (overrides --hours-back and --days-back)",
    )
    run_parser.add_argument(
        "--start-date",
        type=str,
        default=None,
        help="Start date for date range (YYYY-MM-DD). Takes precedence over lookback options.",
    )
    run_parser.add_argument(
        "--end-date",
        type=str,
        default=None,
        help="End date for date range (YYYY-MM-DD). Defaults to now if --start-date is set.",
    )
    run_parser.add_argument(
        "--output", "-o",
        help="Output directory (default: data/output)",
    )
    run_parser.add_argument(
        "--batch-size", "-b",
        type=int,
        default=10,
        help="Incidents per API call (default: 10)",
    )
    run_parser.add_argument(
        "--workers", "-w",
        type=int,
        default=100,
        help="Parallel workers (default: 100)",
    )
    run_parser.add_argument(
        "--max-rpm",
        type=int,
        default=550,
        help="Maximum requests per minute limit (default: 550)",
    )
    run_parser.add_argument(
        "--skip-l123",
        action="store_true",
        help="Skip L123 classification (use existing)",
    )
    run_parser.add_argument(
        "--skip-l4",
        action="store_true",
        help="Skip L4 classification",
    )
    run_parser.add_argument(
        "--l4-only",
        action="store_true",
        help="Run L4 only on incidents missing L4 classification in Snowflake (requires --start-date)",
    )
    run_parser.add_argument(
        "--subcategories",
        nargs="+",
        help="Specific subcategories for L4 (default: all)",
    )
    run_parser.add_argument(
        "--generate-taxonomy",
        action="store_true",
        help="Generate fresh L4 taxonomy (otherwise use cached from S3)",
    )
    run_parser.add_argument(
        "--taxonomy-days",
        type=int,
        default=7,
        help="Days of data to use for taxonomy generation (default: 7)",
    )
    
    # =========================================================================
    # l123 command - L1/L2/L3 classification only
    # =========================================================================
    l123_parser = subparsers.add_parser("l123", help="Run L1/L2/L3 classification")
    l123_parser.add_argument(
        "--input", "-i",
        help="Input CSV file (skip database fetch if provided)",
    )
    l123_parser.add_argument(
        "--days-back",
        type=int,
        default=None,
        help="Days to look back for incidents (default: 180)",
    )
    l123_parser.add_argument(
        "--hours-back",
        type=int,
        default=None,
        help="Hours to look back for incidents (overrides --days-back)",
    )
    l123_parser.add_argument(
        "--minutes-back",
        type=int,
        default=None,
        help="Minutes to look back for incidents (overrides --hours-back and --days-back)",
    )
    l123_parser.add_argument(
        "--start-date",
        type=str,
        default=None,
        help="Start date for date range (YYYY-MM-DD). Takes precedence over lookback options.",
    )
    l123_parser.add_argument(
        "--end-date",
        type=str,
        default=None,
        help="End date for date range (YYYY-MM-DD). Defaults to now if --start-date is set.",
    )
    l123_parser.add_argument(
        "--output", "-o",
        help="Output file path",
    )
    l123_parser.add_argument(
        "--batch-size", "-b",
        type=int,
        default=10,
        help="Incidents per API call",
    )
    l123_parser.add_argument(
        "--workers", "-w",
        type=int,
        default=100,
        help="Parallel workers",
    )
    
    # =========================================================================
    # l4 command - L4 classification
    # =========================================================================
    l4_parser = subparsers.add_parser("l4", help="Run L4 classification")
    l4_parser.add_argument(
        "--input", "-i",
        required=True,
        help="Input CSV file (with L123 classifications)",
    )
    l4_parser.add_argument(
        "--category", "-c",
        help="Filter to specific category",
    )
    l4_parser.add_argument(
        "--subcategory", "-s",
        help="Filter to specific subcategory",
    )
    l4_parser.add_argument(
        "--taxonomy", "-t",
        help="Load existing taxonomy file",
    )
    l4_parser.add_argument(
        "--output", "-o",
        help="Output file path",
    )
    l4_parser.add_argument(
        "--batch-size", "-b",
        type=int,
        default=5,
        help="Incidents per API call",
    )
    l4_parser.add_argument(
        "--workers", "-w",
        type=int,
        default=100,
        help="Parallel workers",
    )
    l4_parser.add_argument(
        "--sample-size",
        type=int,
        help="Sample size for taxonomy derivation",
    )
    
    # =========================================================================
    # list command - List categories/subcategories
    # =========================================================================
    list_parser = subparsers.add_parser("list", help="List categories and subcategories")
    list_parser.add_argument(
        "--input", "-i",
        help="Input CSV to analyze (shows distribution)",
    )
    list_parser.add_argument(
        "--taxonomy",
        action="store_true",
        help="Show taxonomy structure",
    )
    
    # =========================================================================
    # taxonomy command - Derive L4 taxonomy
    # =========================================================================
    taxonomy_parser = subparsers.add_parser("taxonomy", help="Derive L4 taxonomy")
    taxonomy_parser.add_argument(
        "--input", "-i",
        required=True,
        help="Input CSV file",
    )
    taxonomy_parser.add_argument(
        "--category", "-c",
        help="Filter to category",
    )
    taxonomy_parser.add_argument(
        "--subcategory", "-s",
        help="Filter to subcategory",
    )
    taxonomy_parser.add_argument(
        "--sample-size",
        type=int,
        help="Sample size (default: auto-calculated)",
    )
    taxonomy_parser.add_argument(
        "--output", "-o",
        help="Output file for taxonomy JSON",
    )
    
    # =========================================================================
    # fetch-datamart command - Fetch incidents from PostgreSQL DataMart
    # =========================================================================
    fetch_dm_parser = subparsers.add_parser(
        "fetch-datamart", 
        help="Fetch incidents from PostgreSQL DataMart"
    )
    fetch_dm_parser.add_argument("--days-back", type=int)
    fetch_dm_parser.add_argument("--hours-back", type=int)
    fetch_dm_parser.add_argument("--minutes-back", type=int, default=30)
    fetch_dm_parser.add_argument("--start-date", type=str)
    fetch_dm_parser.add_argument("--end-date", type=str)
    fetch_dm_parser.add_argument("--output", "-o", required=True, help="Output CSV file path")
    
    # =========================================================================
    # fetch-snowflake-ids command - Get existing incident IDs from Snowflake
    # =========================================================================
    fetch_sf_parser = subparsers.add_parser(
        "fetch-snowflake-ids", 
        help="Get existing incident IDs from Snowflake for deduplication"
    )
    fetch_sf_parser.add_argument("--hours-back", type=int, default=12, help="Hours to look back (default: 12)")
    fetch_sf_parser.add_argument("--timeout", type=int, default=60, help="Query timeout in seconds (default: 60)")
    fetch_sf_parser.add_argument("--output", "-o", required=True, help="Output file for incident IDs (one per line)")
    
    # =========================================================================
    # dedup command - Filter incidents against existing IDs
    # =========================================================================
    dedup_parser = subparsers.add_parser(
        "dedup", 
        help="Filter incidents CSV against list of existing IDs"
    )
    dedup_parser.add_argument("--input", "-i", required=True, help="Input CSV file")
    dedup_parser.add_argument("--ids-file", required=True, help="File containing IDs to exclude (one per line)")
    dedup_parser.add_argument("--output", "-o", required=True, help="Output CSV file")
    dedup_parser.add_argument("--id-column", default="in_id", help="Column name for incident ID (default: in_id)")
    
    # =========================================================================
    # prepare command - Fetch & deduplicate data (convenience wrapper)
    # =========================================================================
    prepare_parser = subparsers.add_parser(
        "prepare", 
        help="Fetch incidents from database and deduplicate against Snowflake (combines fetch-datamart + fetch-snowflake-ids + dedup)"
    )
    prepare_parser.add_argument("--days-back", type=int)
    prepare_parser.add_argument("--hours-back", type=int)
    prepare_parser.add_argument("--minutes-back", type=int, default=30)
    prepare_parser.add_argument("--start-date", type=str)
    prepare_parser.add_argument("--end-date", type=str)
    prepare_parser.add_argument("--output", "-o", required=True, help="Output CSV file path")
    prepare_parser.add_argument("--skip-dedup", action="store_true", help="Skip Snowflake deduplication")
    prepare_parser.add_argument("--dedup-hours", type=int, default=12, help="Hours to check for duplicates")
    prepare_parser.add_argument("--dedup-timeout", type=int, default=60, help="Snowflake query timeout")
    
    # =========================================================================
    # finalize command - Merge results + upload to S3/Snowflake
    # =========================================================================
    finalize_parser = subparsers.add_parser(
        "finalize",
        help="Merge L123/L4 classification results and upload to S3/Snowflake",
    )
    finalize_parser.add_argument(
        "--artifacts-dir", "-a",
        default="artifacts",
        help="Directory containing artifact CSVs to merge (default: artifacts/)",
    )
    finalize_parser.add_argument(
        "--run-id", "-r",
        default=None,
        help="Run identifier / timestamp (default: auto-generated)",
    )
    finalize_parser.add_argument(
        "--output", "-o",
        default=None,
        help="Output file path (default: data/output/walle_classified_incidents_<run_id>.csv)",
    )
    finalize_parser.add_argument(
        "--skip-s3",
        action="store_true",
        help="Skip S3 upload",
    )
    finalize_parser.add_argument(
        "--skip-snowflake",
        action="store_true",
        help="Skip Snowflake upload",
    )
    finalize_parser.add_argument(
        "--env",
        choices=["dev", "stage", "prod"],
        default="prod",
        help="Target environment (default: prod)",
    )

    return parser


async def cmd_run(args):
    """Execute full pipeline."""
    import tempfile
    from pathlib import Path
    import logging
    from .pipeline import run_full_pipeline
    from .utils import load_incidents_from_database
    from .config import get_settings
    
    # Ensure logs show up in GitHub Actions / terminals.
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    settings = get_settings()
    
    # Handle --l4-only mode: find incidents missing L4 in Snowflake
    if getattr(args, 'l4_only', False):
        from .utils.snowflake import get_incidents_with_l123_missing_l4
        
        if not args.start_date:
            print("Error: --l4-only requires --start-date")
            return 1
        
        print(f"Finding incidents missing L4 classification...")
        print(f"  Date range: {args.start_date} to {args.end_date or 'now'}")
        
        try:
            # Fetch incidents with their L123 classifications from Snowflake
            l123_df = get_incidents_with_l123_missing_l4(
                start_date=args.start_date,
                end_date=args.end_date,
            )
        except Exception as e:
            print(f"Error querying Snowflake: {e}")
            return 1
        
        if l123_df.empty:
            print("No incidents found missing L4 classification in date range")
            return 0
        
        print(f"Found {len(l123_df)} incidents missing L4 classification")
        
        # Create a temporary input file and L123 checkpoint
        timestamp = args.start_date.replace('-', '')
        base_name = f"l4only_{timestamp}"
        
        # Save incident data as input file (with L123 columns)
        input_file = Path(tempfile.gettempdir()) / f"{base_name}.csv"
        l123_df.to_csv(input_file, index=False)
        input_source = str(input_file)
        
        # Save as L123 checkpoint (required by pipeline when skip_l123=True)
        checkpoint_file = settings.checkpoint_dir / f"{base_name}_l123_checkpoint.csv"
        l123_df.to_csv(checkpoint_file, index=False)
        print(f"Created L123 checkpoint: {checkpoint_file}")
        
        # Force skip_l123 since L123 already done
        args.skip_l123 = True
    # Check if input file is provided
    elif getattr(args, 'input', None):
        print(f"Loading incidents from file: {args.input}")
        input_source = args.input
    else:
        # Determine lookback period or date range
        if args.start_date is not None:
            if args.end_date is not None:
                lookback_desc = f"date range {args.start_date} to {args.end_date}"
            else:
                lookback_desc = f"from {args.start_date} to now"
        elif args.minutes_back is not None:
            lookback_desc = f"{args.minutes_back} minutes"
        elif args.hours_back is not None:
            lookback_desc = f"{args.hours_back} hours"
        elif args.days_back is not None:
            lookback_desc = f"{args.days_back} days"
        else:
            lookback_desc = "180 days (default)"
            args.days_back = 180
        
        print(f"Loading incidents from database ({lookback_desc})...")
        df = load_incidents_from_database(
            days_back=args.days_back,
            hours_back=args.hours_back,
            minutes_back=getattr(args, 'minutes_back', None),
            start_date=getattr(args, 'start_date', None),
            end_date=getattr(args, 'end_date', None),
        )
        print(f"Loaded {len(df)} incidents from database")
        
        with tempfile.NamedTemporaryFile(mode='w', suffix='.csv', delete=False) as f:
            df.to_csv(f.name, index=False)
            input_source = f.name
    
    result = await run_full_pipeline(
        input_file=input_source,
        output_dir=args.output,
        batch_size=args.batch_size,
        workers=args.workers,
        l4_subcategories=args.subcategories,
        skip_l123=args.skip_l123,
        skip_l4=args.skip_l4,
        debug=args.debug,
        generate_taxonomy=args.generate_taxonomy,
        taxonomy_days=args.taxonomy_days,
        max_rpm=args.max_rpm,
    )
    
    print(f"\nPipeline completed with status: {result.status.value}")
    return 0


async def cmd_l123(args):
    """Execute L123 classification."""
    import tempfile
    from .classifiers import run_l123_classification
    from .utils import load_incidents_from_database
    
    # Check if input file is provided
    if args.input:
        print(f"Loading incidents from file: {args.input}")
        input_source = args.input
    else:
        # Determine lookback period or date range
        if getattr(args, 'start_date', None) is not None:
            if getattr(args, 'end_date', None) is not None:
                lookback_desc = f"date range {args.start_date} to {args.end_date}"
            else:
                lookback_desc = f"from {args.start_date} to now"
        elif args.minutes_back is not None:
            lookback_desc = f"{args.minutes_back} minutes"
        elif args.hours_back is not None:
            lookback_desc = f"{args.hours_back} hours"
        elif args.days_back is not None:
            lookback_desc = f"{args.days_back} days"
        else:
            lookback_desc = "180 days (default)"
            args.days_back = 180
        
        print(f"Loading incidents from database ({lookback_desc})...")
        df = load_incidents_from_database(
            days_back=args.days_back,
            hours_back=args.hours_back,
            minutes_back=getattr(args, 'minutes_back', None),
            start_date=getattr(args, 'start_date', None),
            end_date=getattr(args, 'end_date', None),
        )
        print(f"Loaded {len(df)} incidents from database")
        
        with tempfile.NamedTemporaryFile(mode='w', suffix='.csv', delete=False) as f:
            df.to_csv(f.name, index=False)
            input_source = f.name
    
    output = await run_l123_classification(
        input_file=input_source,
        output_file=args.output,
        batch_size=args.batch_size,
        workers=args.workers,
        debug=args.debug,
    )
    
    print(f"\nL123 classification complete. Output: {output}")
    return 0


async def cmd_l4(args):
    """Execute L4 classification."""
    from .classifiers import run_l4_classification
    
    output, taxonomy = await run_l4_classification(
        input_file=args.input,
        category=args.category,
        subcategory=args.subcategory,
        taxonomy_file=args.taxonomy,
        output_file=args.output,
        batch_size=args.batch_size,
        workers=args.workers,
        sample_size=args.sample_size,
        debug=args.debug,
    )
    
    print(f"\nL4 classification complete.")
    print(f"Output: {output}")
    print(f"Taxonomy: {len(taxonomy.categories)} categories")
    return 0


def cmd_list(args):
    """List categories and subcategories."""
    from .models import INCIDENT_TAXONOMY, get_all_categories, get_subcategories
    
    if args.taxonomy:
        print("\n" + "=" * 60)
        print("INCIDENT CLASSIFICATION TAXONOMY")
        print("=" * 60)
        
        for category in get_all_categories():
            subcats = get_subcategories(category)
            print(f"\n{category} ({len(subcats)} subcategories)")
            for subcat in subcats:
                products = INCIDENT_TAXONOMY[category][subcat]
                print(f"    {subcat} ({len(products)} products)")
        return 0
    
    if args.input:
        df = pd.read_csv(args.input)
        
        print("\n" + "=" * 60)
        print("CATEGORY DISTRIBUTION")
        print("=" * 60)
        
        if "ai_l1" in df.columns:
            cat_counts = df["ai_l1"].value_counts()
            for cat, count in cat_counts.items():
                pct = count / len(df) * 100
                print(f"\n{cat}: {count:,} ({pct:.1f}%)")
                
                if "ai_l2" in df.columns:
                    subcat_counts = df[df["ai_l1"] == cat]["ai_l2"].value_counts().head(5)
                    for subcat, sub_count in subcat_counts.items():
                        sub_pct = sub_count / count * 100
                        print(f"    {subcat}: {sub_count:,} ({sub_pct:.1f}%)")
        else:
            print("No ai_l1 column found in input file.")
            print("Available columns:", list(df.columns))
    else:
        print("Specify --input FILE or --taxonomy to list categories.")
    
    return 0


async def cmd_taxonomy(args):
    """Derive L4 taxonomy."""
    from .classifiers import L4Classifier
    from .utils import DataLoader
    
    loader = DataLoader()
    df = loader.load_csv(args.input)
    
    if df is None or df.empty:
        print(f"No data in {args.input}")
        return 1
    
    # Filter
    if args.category:
        df = df[df["ai_l1"] == args.category]
    if args.subcategory:
        df = df[df["ai_l2"] == args.subcategory]
    
    if df.empty:
        print("No data after filtering.")
        return 1
    
    incidents: list[dict[str, Any]] = df.to_dict(orient="records")  # type: ignore[assignment]
    
    classifier = L4Classifier(debug=args.debug)
    
    taxonomy = await classifier.derive_taxonomy(
        incidents,
        category=args.category or "Unknown",
        subcategory=args.subcategory,
        sample_size=args.sample_size,
    )
    
    # Save taxonomy
    if args.output:
        output_path = args.output
    else:
        output_path = classifier.save_taxonomy()
    
    print(f"\n" + "=" * 60)
    print("L4 TAXONOMY DERIVED")
    print("=" * 60)
    print(f"Categories: {len(taxonomy.categories)}")
    print(f"Sample size: {taxonomy.sample_size_analyzed}")
    print(f"Coverage: {taxonomy.estimated_coverage:.1f}%")
    print(f"Saved to: {output_path}")
    
    # Show categories
    actionable = [c for c in taxonomy.categories if c.is_actionable]
    non_actionable = [c for c in taxonomy.categories if not c.is_actionable]
    
    print(f"\nActionable ({len(actionable)}):")
    for cat in actionable[:10]:
        print(f"   - {cat.name}: {cat.description}")
    if len(actionable) > 10:
        print(f"   ... and {len(actionable) - 10} more")
    
    print(f"\nNon-actionable ({len(non_actionable)}):")
    for cat in non_actionable:
        print(f"   - {cat.name}: {cat.actionability_reason}")
    
    return 0


def cmd_fetch_datamart(args):
    """Fetch incidents from PostgreSQL DataMart."""
    import time
    from datetime import datetime
    from pathlib import Path
    
    def log(msg):
        print(f"[{datetime.now().isoformat()}] {msg}", flush=True)
    
    log("fetch-datamart starting...")
    log(f"Arguments: {vars(args)}")
    
    log("Importing database module...")
    from .utils.database import load_incidents_from_database
    log("Import complete")
    
    # Determine time range
    start_date = args.start_date or None
    end_date = args.end_date or None
    days_back = args.days_back if not start_date and args.days_back else None
    hours_back = args.hours_back if not start_date and not days_back and args.hours_back else None
    minutes_back = args.minutes_back if not start_date and not days_back and not hours_back else None
    
    if start_date:
        log(f"Fetching: start_date={start_date}, end_date={end_date or 'now'}")
    else:
        log(f"Fetching: days_back={days_back}, hours_back={hours_back}, minutes_back={minutes_back}")
    
    log("Querying database...")
    db_start = time.time()
    df = load_incidents_from_database(
        days_back=days_back,
        hours_back=hours_back,
        minutes_back=minutes_back,
        start_date=start_date,
        end_date=end_date,
    )
    log(f"Database query completed in {time.time() - db_start:.1f}s")
    log(f"Fetched {len(df)} incidents")
    
    # Save output
    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(output_path, index=False)
    log(f"Saved {len(df)} records to {output_path}")
    
    return 0


def cmd_fetch_snowflake_ids(args):
    """Get existing incident IDs from Snowflake."""
    import time
    from datetime import datetime, timedelta
    from pathlib import Path
    
    def log(msg):
        print(f"[{datetime.now().isoformat()}] {msg}", flush=True)
    
    log("fetch-snowflake-ids starting...")
    log(f"Arguments: {vars(args)}")
    
    log("Importing snowflake module...")
    from .utils.snowflake import get_incident_ids_from_snowflake
    log("Import complete")
    
    start_time = (datetime.now() - timedelta(hours=args.hours_back)).strftime('%Y-%m-%d %H:%M:%S')
    log(f"Fetching IDs since: {start_time}")
    
    log("Querying Snowflake...")
    sf_start = time.time()
    try:
        existing_ids = get_incident_ids_from_snowflake(
            start_date=start_time,
            timeout_seconds=args.timeout
        )
        log(f"Snowflake query completed in {time.time() - sf_start:.1f}s")
        log(f"Found {len(existing_ids)} existing IDs")
        
        # Save IDs to file
        output_path = Path(args.output)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        with open(output_path, 'w') as f:
            for id_ in existing_ids:
                f.write(f"{id_}\n")
        log(f"Saved {len(existing_ids)} IDs to {output_path}")
        return 0
        
    except Exception as e:
        log(f"Snowflake query failed after {time.time() - sf_start:.1f}s: {e}")
        # Write empty file so dedup can proceed
        output_path = Path(args.output)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text("")
        log(f"Wrote empty IDs file to {output_path} (dedup will include all incidents)")
        return 0  # Don't fail the job, just skip dedup


def cmd_dedup(args):
    """Filter incidents against list of existing IDs."""
    import time
    from datetime import datetime
    from pathlib import Path
    import pandas as pd
    
    def log(msg):
        print(f"[{datetime.now().isoformat()}] {msg}", flush=True)
    
    log("dedup starting...")
    
    # Load incidents
    log(f"Loading incidents from {args.input}...")
    df = pd.read_csv(args.input)
    original_count = len(df)
    log(f"Loaded {original_count} incidents")
    
    # Load IDs to exclude
    ids_path = Path(args.ids_file)
    if ids_path.exists() and ids_path.stat().st_size > 0:
        log(f"Loading IDs from {args.ids_file}...")
        with open(ids_path) as f:
            existing_ids = set(line.strip() for line in f if line.strip())
        log(f"Loaded {len(existing_ids)} IDs to exclude")
        
        # Filter
        df = df[~df[args.id_column].isin(existing_ids)]
        filtered_count = original_count - len(df)
        log(f"Filtered out {filtered_count} duplicates")
    else:
        log("No IDs file or empty - keeping all incidents")
    
    # Save output
    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(output_path, index=False)
    log(f"Saved {len(df)} records to {output_path}")
    
    return 0


def cmd_prepare(args):
    """Fetch incidents and deduplicate against Snowflake."""
    import sys
    import time
    from datetime import datetime, timedelta
    from pathlib import Path
    
    # Ensure output is flushed immediately for CI visibility
    def log(msg):
        print(f"[{datetime.now().isoformat()}] {msg}", flush=True)
    
    log("prepare command starting...")
    log(f"Arguments: {vars(args)}")
    
    log("Importing database module...")
    from .utils.database import load_incidents_from_database
    log("Importing snowflake module...")
    from .utils.snowflake import get_incident_ids_from_snowflake
    log("Imports complete")
    
    # Determine time range (precedence: start_date > days_back > hours_back > minutes_back)
    start_date = args.start_date or None
    end_date = args.end_date or None
    days_back = args.days_back if not start_date and args.days_back else None
    hours_back = args.hours_back if not start_date and not days_back and args.hours_back else None
    minutes_back = args.minutes_back if not start_date and not days_back and not hours_back else None
    
    if start_date:
        log(f"Fetching incidents: start_date={start_date}, end_date={end_date or 'now'}")
    else:
        log(f"Fetching incidents: days_back={days_back}, hours_back={hours_back}, minutes_back={minutes_back}")
    
    # Step 1: Query database
    log("[1/3] Querying database...")
    db_start = time.time()
    df = load_incidents_from_database(
        days_back=days_back,
        hours_back=hours_back,
        minutes_back=minutes_back,
        start_date=start_date,
        end_date=end_date,
    )
    log(f"[1/3] Database query completed in {time.time() - db_start:.1f}s")
    log(f"Fetched {len(df)} incidents from database")
    
    # Step 2: Deduplicate against Snowflake
    if not args.skip_dedup and len(df) > 0:
        # Auto-calculate dedup lookback to cover the entire fetch window + 6h buffer
        dedup_hours = args.dedup_hours
        if dedup_hours == 12:  # default was not overridden
            if start_date:
                try:
                    fmt = '%Y-%m-%d %H:%M:%S' if ' ' in start_date else '%Y-%m-%d'
                    fetch_start = datetime.strptime(start_date, fmt)
                    fetch_end = datetime.strptime(end_date, fmt) if end_date else datetime.now()
                    span_hours = (fetch_end - fetch_start).total_seconds() / 3600
                    dedup_hours = max(12, int(span_hours + 6))
                except ValueError:
                    pass  # keep default 12h if dates can't be parsed
            elif days_back:
                dedup_hours = max(12, days_back * 24 + 6)
            elif hours_back:
                dedup_hours = max(12, hours_back + 6)
            # else: minutes_back — 12h default is fine
        log(f"[2/3] Checking Snowflake for duplicates (last {dedup_hours}h)...")
        sf_start = time.time()
        try:
            dedup_start = (datetime.now() - timedelta(hours=dedup_hours)).strftime('%Y-%m-%d %H:%M:%S')
            existing_ids = get_incident_ids_from_snowflake(
                start_date=dedup_start, 
                timeout_seconds=args.dedup_timeout
            )
            log(f"[2/3] Snowflake query completed in {time.time() - sf_start:.1f}s")
            original_count = len(df)
            df = df[~df['in_id'].isin(existing_ids)]
            filtered_count = original_count - len(df)
            log(f"Filtered out {filtered_count} incidents already in Snowflake")
            log(f"Remaining incidents to process: {len(df)}")
        except Exception as e:
            log(f"[2/3] Snowflake query failed after {time.time() - sf_start:.1f}s: {e}")
            log("Proceeding with all incidents from database (no dedup)")
    else:
        log("[2/3] Skipping Snowflake deduplication")
    
    # Step 3: Save output
    log("[3/3] Saving data...")
    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(output_path, index=False)
    log(f"Saved {len(df)} records to {output_path}")
    
    return 0


def cmd_finalize(args):
    """Merge L123/L4 results and upload to S3/Snowflake."""
    from datetime import datetime

    from .pipeline.finalize import run_finalize

    run_id = args.run_id or datetime.now().strftime("%Y%m%d-%H%M%S")

    result = run_finalize(
        artifacts_dir=args.artifacts_dir,
        run_id=run_id,
        output_file=args.output,
        skip_s3=args.skip_s3,
        skip_snowflake=args.skip_snowflake,
        environment=args.env,
    )

    # Check for Snowflake failure
    sf = result.get("snowflake_result")
    if isinstance(sf, dict) and not sf.get("success") and not args.skip_snowflake:
        return 1

    return 0


def main():
    """Main CLI entry point."""
    parser = create_parser()
    args = parser.parse_args()
    
    if args.command is None:
        parser.print_help()
        return 0
    
    # Route to command handlers
    if args.command == "run":
        return asyncio.run(cmd_run(args))
    elif args.command == "l123":
        return asyncio.run(cmd_l123(args))
    elif args.command == "l4":
        return asyncio.run(cmd_l4(args))
    elif args.command == "list":
        return cmd_list(args)
    elif args.command == "taxonomy":
        return asyncio.run(cmd_taxonomy(args))
    elif args.command == "fetch-datamart":
        return cmd_fetch_datamart(args)
    elif args.command == "fetch-snowflake-ids":
        return cmd_fetch_snowflake_ids(args)
    elif args.command == "dedup":
        return cmd_dedup(args)
    elif args.command == "prepare":
        return cmd_prepare(args)
    elif args.command == "finalize":
        return cmd_finalize(args)
    else:
        parser.print_help()
        return 1


if __name__ == "__main__":
    sys.exit(main())
