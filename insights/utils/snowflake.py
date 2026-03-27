"""Snowflake utilities for outputting classified incidents."""

import logging
from datetime import datetime

from typing import Any

import pandas as pd

from insights.config import settings

logger = logging.getLogger(__name__)

# Snowflake connector is optional - check if available
try:
    import snowflake.connector
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.backends import default_backend
    SNOWFLAKE_AVAILABLE = True
except ImportError:
    SNOWFLAKE_AVAILABLE = False
    logger.warning("Snowflake connector not installed. Run: uv add snowflake-connector-python cryptography")


def get_private_key_bytes() -> bytes | None:
    """Parse the private key from settings."""
    if not settings.snowflake_certificate:
        return None
    
    try:
        # Handle escaped newlines in env var
        pem_data = settings.snowflake_certificate.replace("\\n", "\n")
        
        # Load the private key
        private_key = serialization.load_pem_private_key(
            pem_data.encode(),
            password=None,
            backend=default_backend()
        )
        
        # Serialize to DER format for Snowflake
        private_key_bytes = private_key.private_bytes(
            encoding=serialization.Encoding.DER,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption()
        )
        
        return private_key_bytes
    except Exception as e:
        logger.error(f"Failed to parse private key: {e}")
        return None


def get_snowflake_connection():
    """Create and return a Snowflake connection using key-pair auth."""
    if not SNOWFLAKE_AVAILABLE:
        raise ImportError("Snowflake connector not installed")
    
    if not settings.snowflake_account or not settings.snowflake_user:
        raise ValueError("Snowflake credentials not configured")
    
    private_key_bytes = get_private_key_bytes()
    if private_key_bytes is None:
        raise ValueError("Snowflake private key not configured or invalid")
    
    conn = snowflake.connector.connect(
        account=settings.snowflake_account,
        user=settings.snowflake_user,
        private_key=private_key_bytes,
        database=settings.snowflake_database,
        schema=settings.snowflake_schema,
        warehouse=settings.snowflake_warehouse,
    )
    
    logger.info(f"Connected to Snowflake: {settings.snowflake_database}.{settings.snowflake_schema}")
    return conn


def ensure_table_exists(conn) -> bool:
    """
    Ensure the target table exists in Snowflake.
    Creates the table if it doesn't exist.
    
    Returns:
        True if table exists or was created, False otherwise
    """
    table_name = settings.snowflake_table
    
    create_table_sql = f"""
    CREATE TABLE IF NOT EXISTS {table_name} (
        -- Original incident fields
        in_id VARCHAR(50) PRIMARY KEY,
        assignment VARCHAR(255),
        first_assignment_group VARCHAR(255),
        brief_description VARCHAR(4000),
        opened_at TIMESTAMP_NTZ,
        closed_at TIMESTAMP_NTZ,
        action VARCHAR(4000),
        resolution VARCHAR(4000),
        update_action_ess VARCHAR(4000),
        uh_ess_errormsg VARCHAR(4000),
        update_action VARCHAR(4000),
        comments VARCHAR(4000),
        uh_monitoring_notes VARCHAR(4000),
        
        -- AI Classification Categories (L1-L4 grouped together)
        ai_l1 VARCHAR(255),
        ai_l2 VARCHAR(255),
        ai_l3 VARCHAR(255),
        ai_l4 VARCHAR(255),
        
        -- L123 Classification Details
        vendor VARCHAR(255),
        ai_confidence FLOAT,
        ai_self_resolved BOOLEAN,
        ai_rationale VARCHAR(4000),
        ai_keywords VARCHAR(4000),
        ai_root_cause_indicator VARCHAR(255),
        ai_root_cause VARCHAR(4000),
        
        -- L4 Classification Details
        ai_l4_confidence FLOAT,
        ai_l4_resolution_action VARCHAR(4000),
        ai_l4_actionable BOOLEAN,
        ai_l4_actionability_reason VARCHAR(4000),
        ai_l4_rationale VARCHAR(4000),
        
        -- Metadata
        walle_run_id VARCHAR(50),
        walle_processed_at TIMESTAMP_NTZ DEFAULT CURRENT_TIMESTAMP(),
        walle_version VARCHAR(20) DEFAULT '1.0.0'
    )
    """
    
    try:
        cursor = conn.cursor()
        cursor.execute(create_table_sql)
        cursor.close()
        logger.info(f"Table {table_name} is ready")
        return True
    except Exception as e:
        logger.error(f"Failed to create table: {e}")
        return False


def get_table_columns(conn, table_name: str) -> list[str]:
    """
    Get the list of column names in a Snowflake table.
    
    Args:
        conn: Snowflake connection
        table_name: Name of the table
        
    Returns:
        List of column names (uppercase)
    """
    try:
        cursor = conn.cursor()
        cursor.execute(f"DESCRIBE TABLE {table_name}")
        columns = [row[0].upper() for row in cursor.fetchall()]
        cursor.close()
        return columns
    except Exception as e:
        logger.warning(f"Could not get table columns: {e}")
        return []


def ensure_l4_null_reasons_table_exists(conn, table_name: str = "WALLE_L4_NULL_REASONS") -> bool:
    """
    Ensure the L4 NULL reasons audit table exists in Snowflake.

    Table design:
    - One row per (in_id, walle_run_id) so the same incident can be audited across runs.
    - recorded_at stored as TIMESTAMP_NTZ for easy time slicing.
    """
    create_sql = f"""
    CREATE TABLE IF NOT EXISTS {table_name} (
        in_id VARCHAR(50),
        walle_run_id VARCHAR(50),
        reason VARCHAR(100),
        cause VARCHAR(4000),
        subcategory VARCHAR(255),
        recorded_at TIMESTAMP_NTZ,
        original_value VARCHAR(4000),
        PRIMARY KEY (in_id, walle_run_id)
    )
    """
    try:
        cursor = conn.cursor()
        cursor.execute(create_sql)
        cursor.close()
        logger.info(f"Table {table_name} is ready")
        return True
    except Exception as e:
        logger.error(f"Failed to create L4 NULL reasons table {table_name}: {e}")
        return False


def upsert_l4_null_reasons_final(
    conn,
    rows: list[dict[str, Any]],
    *,
    table_name: str = "WALLE_L4_NULL_REASONS",
) -> int:
    """
    Upsert final L4 NULL reasons into Snowflake.

    Args:
        conn: Snowflake connection
        rows: List of dict rows with keys:
            in_id, walle_run_id, reason, cause, subcategory, recorded_at, original_value
        table_name: Target table name.

    Returns:
        Number of rows written to staging (best-effort indicator).
    """
    if not SNOWFLAKE_AVAILABLE:
        raise ImportError("Snowflake connector not installed")
    if not rows:
        return 0

    # Normalize into a DataFrame.
    df = pd.DataFrame(rows)
    # Required keys for merge.
    if "in_id" not in df.columns or "walle_run_id" not in df.columns:
        raise ValueError("rows must include in_id and walle_run_id")

    # Uppercase for Snowflake.
    df.columns = [c.upper() for c in df.columns]

    # Ensure expected columns exist (MERGE will only use those present).
    expected = ["IN_ID", "WALLE_RUN_ID", "REASON", "CAUSE", "SUBCATEGORY", "RECORDED_AT", "ORIGINAL_VALUE"]
    for col in expected:
        if col not in df.columns:
            df[col] = None
    df = df[expected]

    # Convert recorded_at to a Snowflake-friendly string format if present.
    if "RECORDED_AT" in df.columns:
        try:
            df["RECORDED_AT"] = pd.to_datetime(df["RECORDED_AT"], errors="coerce").apply(
                lambda x: x.strftime("%Y-%m-%d %H:%M:%S") if pd.notna(x) else None
            )
        except Exception:
            # Keep as-is; Snowflake TRY_TO_TIMESTAMP_NTZ in merge will handle best-effort.
            pass

    from snowflake.connector.pandas_tools import write_pandas

    staging_table = f"{table_name}_STAGING_{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}"
    cursor = conn.cursor()
    try:
        success, _nchunks, nrows, _ = write_pandas(
            conn=conn,
            df=df,
            table_name=staging_table,
            auto_create_table=True,
            overwrite=True,
        )
        if not success:
            raise RuntimeError("write_pandas failed for L4 NULL reasons staging table")

        # MERGE by (IN_ID, WALLE_RUN_ID). Update all other fields.
        merge_sql = f"""
        MERGE INTO {table_name} AS target
        USING {staging_table} AS source
          ON target.IN_ID = source.IN_ID
         AND target.WALLE_RUN_ID = source.WALLE_RUN_ID
        WHEN MATCHED THEN UPDATE SET
          target.REASON = source.REASON,
          target.CAUSE = source.CAUSE,
          target.SUBCATEGORY = source.SUBCATEGORY,
          target.RECORDED_AT = TRY_TO_TIMESTAMP_NTZ(source.RECORDED_AT::VARCHAR),
          target.ORIGINAL_VALUE = source.ORIGINAL_VALUE
        WHEN NOT MATCHED THEN INSERT (
          IN_ID, WALLE_RUN_ID, REASON, CAUSE, SUBCATEGORY, RECORDED_AT, ORIGINAL_VALUE
        ) VALUES (
          source.IN_ID,
          source.WALLE_RUN_ID,
          source.REASON,
          source.CAUSE,
          source.SUBCATEGORY,
          TRY_TO_TIMESTAMP_NTZ(source.RECORDED_AT::VARCHAR),
          source.ORIGINAL_VALUE
        )
        """
        cursor.execute(merge_sql)
        cursor.execute(f"DROP TABLE IF EXISTS {staging_table}")
        logger.info(f"Upserted {nrows} L4 NULL reason rows into {table_name}")
        return int(nrows)
    finally:
        try:
            cursor.close()
        except Exception:
            pass


def upload_to_snowflake(
    df: pd.DataFrame,
    run_id: str,
    replace: bool = False,
) -> dict[str, Any]:
    """
    Upload classified incidents DataFrame to Snowflake.
    
    Args:
        df: DataFrame with classified incidents
        run_id: Pipeline run identifier
        replace: If True, replace existing records; if False, merge/upsert
        
    Returns:
        Dict with upload statistics
    """
    if not SNOWFLAKE_AVAILABLE:
        logger.warning("Snowflake connector not available, skipping upload")
        return {"success": False, "error": "Snowflake connector not installed"}
    
    if df.empty:
        logger.warning("Empty DataFrame, nothing to upload")
        return {"success": True, "rows_uploaded": 0}
    
    try:
        conn = get_snowflake_connection()
        ensure_table_exists(conn)
        
        table_name = settings.snowflake_table
        
        # Add metadata columns
        df = df.copy()
        df["walle_run_id"] = run_id
        # Use ISO format string for timestamp - Snowflake handles this correctly
        df["walle_processed_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        
        # Comprehensive column mapping from checkpoint/intermediate formats to final schema
        # This handles all the variations from L123 checkpoints, L4 checkpoints, and merged files
        column_mapping = {
            # ID column variations
            'incident_id': 'in_id',
            'incidentid': 'in_id',
            'inc_id': 'in_id',
            'ticket_id': 'in_id',
            # L123 checkpoint columns -> final schema
            'category': 'ai_l1',
            'subcategory': 'ai_l2',
            'product': 'ai_l3',
            'confidence_score': 'ai_confidence',
            'self_resolved': 'ai_self_resolved',
            'rationale': 'ai_rationale',
            'keywords_identified': 'ai_keywords',
            'root_cause_indicator': 'ai_root_cause_indicator',
            # L4 checkpoint columns -> final schema
            'l4_category': 'ai_l4',
            'l4_subcategory': 'ai_l4_subcategory',
            'resolution_action': 'ai_l4_resolution_action',
            'l4_confidence': 'ai_l4_confidence',
            'keywords': 'ai_l4_keywords',
            'is_actionable': 'ai_l4_actionable',
            'actionability_reason': 'ai_l4_actionability_reason',
            'l4_rationale': 'ai_l4_rationale',
        }
        
        # Apply column mapping (case-insensitive)
        new_columns = []
        for c in df.columns:
            c_lower = c.lower()
            if c_lower in column_mapping:
                new_columns.append(column_mapping[c_lower])
            else:
                new_columns.append(c_lower)
        df.columns = new_columns
        
        # Remove duplicate columns (keep first occurrence)
        df = df.loc[:, ~df.columns.duplicated()]
        
        # Log column transformations
        logger.info(f"Columns after normalization: {list(df.columns)[:15]}...")
        
        # Clean up column names for Snowflake (uppercase)
        df.columns = [c.upper() for c in df.columns]
        
        # Convert timestamp columns to proper string format for Snowflake
        # The staging table is auto-created, so we need string format that Snowflake can CAST to TIMESTAMP_NTZ
        timestamp_cols = ['OPENED_AT', 'CLOSED_AT', 'WALLE_PROCESSED_AT']
        for col in timestamp_cols:
            if col in df.columns:
                # Check the current dtype
                col_dtype = df[col].dtype
                logger.info(f"  Converting {col}: dtype={col_dtype}, sample={df[col].head(2).tolist()}")
                
                # Handle different input formats
                try:
                    if pd.api.types.is_numeric_dtype(df[col]):
                        # Numeric values - could be Unix timestamps (seconds or milliseconds)
                        sample = df[col].dropna().head(1)
                        if len(sample) > 0 and sample.iloc[0] > 1e12:
                            # Milliseconds
                            df[col] = pd.to_datetime(df[col], unit='ms', errors='coerce')
                        elif len(sample) > 0 and sample.iloc[0] > 1e9:
                            # Seconds
                            df[col] = pd.to_datetime(df[col], unit='s', errors='coerce')
                        else:
                            df[col] = pd.to_datetime(df[col], errors='coerce')
                    else:
                        # String or other format
                        df[col] = pd.to_datetime(df[col], errors='coerce')
                    
                    # Convert to string, handling NaT properly
                    # Use apply to handle NaT values as None
                    df[col] = df[col].apply(
                        lambda x: x.strftime('%Y-%m-%d %H:%M:%S') if pd.notna(x) else None
                    )
                    logger.info(f"  {col} converted successfully: sample={df[col].head(2).tolist()}")
                except Exception as e:
                    logger.warning(f"  Failed to convert {col}: {e}, dropping column")
                    df = df.drop(columns=[col])
        
        # Log column fill rates before filtering
        key_cols = ['IN_ID', 'AI_L1', 'AI_L2', 'AI_L3', 'AI_L4', 'AI_CONFIDENCE', 'AI_L4_CONFIDENCE', 'AI_L4_RESOLUTION_ACTION']
        logger.info("Column fill rates (key columns):")
        for col in key_cols:
            if col in df.columns:
                # Handle potential duplicate columns by converting to scalar
                col_data = df[col]
                if isinstance(col_data, pd.DataFrame):
                    col_data = col_data.iloc[:, 0]  # Take first column if duplicated
                non_null = int(col_data.notna().sum())
                pct = float(100 * non_null / len(df)) if len(df) > 0 else 0.0
                logger.info(f"  {col}: {non_null}/{len(df)} ({pct:.1f}%)")
            else:
                logger.warning(f"  {col}: NOT PRESENT in DataFrame")
        
        # Verify we have the primary key column
        if 'IN_ID' not in df.columns:
            # Try to find any column that looks like an ID
            id_candidates = [c for c in df.columns if 'ID' in c.upper() and c.upper() not in ('WALLE_RUN_ID',)]
            if id_candidates:
                logger.warning(f"IN_ID not found, using {id_candidates[0]} as primary key")
                df = df.rename(columns={id_candidates[0]: 'IN_ID'})
            else:
                logger.error(f"No primary key column found. Available columns: {list(df.columns)}")
                return {"success": False, "error": "No primary key column (IN_ID) found in data"}
        
        # Get target table columns to filter DataFrame
        target_columns = get_table_columns(conn, table_name)
        if not target_columns:
            logger.warning("Could not retrieve target table columns, will use all DataFrame columns")
            target_columns = list(df.columns)
        
        # Filter DataFrame to only include columns that exist in the target table
        df_columns = set(df.columns)
        common_columns = [c for c in df.columns if c in target_columns]
        extra_columns = df_columns - set(target_columns)
        missing_in_df = set(target_columns) - df_columns
        
        if extra_columns:
            logger.info(f"Ignoring {len(extra_columns)} columns not in target table: {sorted(extra_columns)}")
        if missing_in_df:
            logger.info(f"Target table columns not in DataFrame: {sorted(missing_in_df)}")
        
        # Ensure IN_ID is included
        if 'IN_ID' not in common_columns:
            common_columns.insert(0, 'IN_ID')
        
        # Filter DataFrame to only common columns
        df = df[[c for c in common_columns if c in df.columns]]
        logger.info(f"Using {len(df.columns)} columns for upload: {list(df.columns)}")
        
        # Use write_pandas for efficient bulk upload
        from snowflake.connector.pandas_tools import write_pandas
        
        # Create a staging table name
        staging_table = f"{table_name}_STAGING_{run_id.replace('-', '_')}"
        
        cursor = conn.cursor()
        
        try:
            # Write to staging table
            success, nchunks, nrows, _ = write_pandas(
                conn=conn,
                df=df,
                table_name=staging_table,
                auto_create_table=True,
                overwrite=True,
            )
            
            if success:
                # Build dynamic MERGE based on columns that exist in target table
                available_cols = [c.upper() for c in df.columns]
                
                # Timestamp columns need TRY_TO_TIMESTAMP_NTZ conversion
                # Since we convert to strings in pandas, staging table has VARCHAR for these
                timestamp_cols_upper = {'OPENED_AT', 'CLOSED_AT', 'WALLE_PROCESSED_AT'}
                
                def get_source_expr(col):
                    """Get the source expression for a column, with conversion for timestamps."""
                    if col in timestamp_cols_upper:
                        # TRY_TO_TIMESTAMP_NTZ handles VARCHAR input and returns NULL on failure
                        return f"TRY_TO_TIMESTAMP_NTZ(source.{col}::VARCHAR)"
                    return f"source.{col}"
                
                # Columns that should be updated (exclude primary key IN_ID)
                update_cols = [c for c in available_cols if c != "IN_ID"]
                
                # Build SET clause for UPDATE with CAST for timestamp columns
                set_clause = ",\n                    ".join([
                    f"target.{col} = {get_source_expr(col)}" for col in update_cols
                ])
                
                # Build INSERT columns and VALUES with CAST for timestamp columns
                insert_cols = ", ".join(available_cols)
                insert_vals = ", ".join([get_source_expr(col) for col in available_cols])
                
                merge_sql = f"""
                MERGE INTO {table_name} AS target
                USING {staging_table} AS source
                ON target.IN_ID = source.IN_ID
                WHEN MATCHED THEN UPDATE SET
                    {set_clause}
                WHEN NOT MATCHED THEN INSERT (
                    {insert_cols}
                ) VALUES (
                    {insert_vals}
                )
                """
                
                logger.debug(f"MERGE SQL:\n{merge_sql}")
                cursor.execute(merge_sql)
                
                # Drop staging table
                cursor.execute(f"DROP TABLE IF EXISTS {staging_table}")
                
                logger.info(f"Uploaded {nrows} rows to Snowflake table {table_name}")
                
                return {
                    "success": True,
                    "rows_uploaded": nrows,
                    "table": table_name,
                }
            else:
                return {"success": False, "error": "write_pandas failed"}
                
        finally:
            # Cleanup staging table if it exists
            try:
                cursor.execute(f"DROP TABLE IF EXISTS {staging_table}")
            except:
                pass
            cursor.close()
            conn.close()
            
    except Exception as e:
        error_msg = f"{type(e).__name__}: {e}"
        if hasattr(e, 'errno'):
            error_msg += f" (errno: {e.errno})"
        if hasattr(e, 'msg'):
            error_msg += f" (msg: {e.msg})"
        logger.error(f"Failed to upload to Snowflake: {error_msg}")
        return {"success": False, "error": error_msg}


def test_snowflake_connection() -> bool:
    """Test Snowflake connectivity."""
    if not SNOWFLAKE_AVAILABLE:
        logger.warning("Snowflake connector not installed")
        return False
    
    try:
        conn = get_snowflake_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT CURRENT_VERSION()")
        version = cursor.fetchone()[0]
        cursor.close()
        conn.close()
        logger.info(f"Connected to Snowflake (version: {version})")
        return True
    except Exception as e:
        logger.error(f"Snowflake connection failed: {e}")
        return False


def delete_incidents_from_snowflake(incident_ids: set[str]) -> int:
    """
    Delete incidents from Snowflake by their IDs.
    
    Args:
        incident_ids: Set of incident IDs to delete
        
    Returns:
        Number of rows deleted
    """
    if not SNOWFLAKE_AVAILABLE:
        raise ImportError("Snowflake connector not installed")
    
    if not incident_ids:
        return 0
    
    table_name = settings.snowflake_table
    
    # Convert to list and create IN clause
    ids_list = list(incident_ids)
    
    # Delete in batches to avoid query size limits
    batch_size = 1000
    total_deleted = 0
    
    try:
        conn = get_snowflake_connection()
        cursor = conn.cursor()
        
        for i in range(0, len(ids_list), batch_size):
            batch = ids_list[i:i + batch_size]
            placeholders = ",".join([f"'{id}'" for id in batch])
            delete_query = f"DELETE FROM {table_name} WHERE in_id IN ({placeholders})"
            cursor.execute(delete_query)
            deleted = cursor.rowcount
            total_deleted += deleted
            logger.info(f"Deleted batch {i // batch_size + 1}: {deleted} rows")
        
        cursor.close()
        conn.close()
        
        logger.info(f"Total deleted from Snowflake: {total_deleted}")
        return total_deleted
        
    except Exception as e:
        logger.error(f"Failed to delete from Snowflake: {e}")
        raise


def get_incident_ids_from_snowflake(
    start_date: str | None = None,
    end_date: str | None = None,
    timeout_seconds: int = 60,
) -> set[str]:
    """
    Query all incident IDs currently in Snowflake.
    
    Args:
        start_date: Optional start date filter (YYYY-MM-DD or YYYY-MM-DD HH:MM:SS)
        end_date: Optional end date filter (YYYY-MM-DD)
        timeout_seconds: Query timeout in seconds (default: 60)
        
    Returns:
        Set of incident IDs (in_id) in Snowflake
    """
    if not SNOWFLAKE_AVAILABLE:
        raise ImportError("Snowflake connector not installed")
    
    table_name = settings.snowflake_table
    
    query = f"SELECT DISTINCT in_id FROM {table_name}"
    conditions = []
    
    if start_date:
        conditions.append(f"closed_at >= '{start_date}'::timestamp")
    if end_date:
        conditions.append(f"closed_at <= '{end_date} 23:59:59'::timestamp")
    
    if conditions:
        query += " WHERE " + " AND ".join(conditions)
    
    logger.info(f"Querying incident IDs from Snowflake table: {table_name}")
    logger.info(f"Query: {query}")
    
    try:
        conn = get_snowflake_connection()
        cursor = conn.cursor()
        # Set query timeout
        cursor.execute(f"ALTER SESSION SET STATEMENT_TIMEOUT_IN_SECONDS = {timeout_seconds}")
        cursor.execute(query)
        rows = cursor.fetchall()
        cursor.close()
        conn.close()
        
        incident_ids = {row[0] for row in rows}
        logger.info(f"Found {len(incident_ids)} incident IDs in Snowflake")
        return incident_ids
        
    except Exception as e:
        logger.error(f"Failed to query Snowflake: {e}")
        raise


def get_incidents_missing_l4(
    start_date: str | None = None,
    end_date: str | None = None,
    timeout_seconds: int = 120,
) -> set[str]:
    """
    Query incident IDs that exist in Snowflake but are missing L4 classification.
    
    Args:
        start_date: Start date filter (YYYY-MM-DD or YYYY-MM-DD HH:MM:SS)
        end_date: End date filter (YYYY-MM-DD)
        timeout_seconds: Query timeout in seconds (default: 120)
        
    Returns:
        Set of incident IDs (in_id) missing L4 classification
    """
    if not SNOWFLAKE_AVAILABLE:
        raise ImportError("Snowflake connector not installed")
    
    table_name = settings.snowflake_table
    
    # Query for incidents where AI_L4 is NULL or empty string
    query = f"""
        SELECT DISTINCT in_id 
        FROM {table_name}
        WHERE (ai_l4 IS NULL OR TRIM(ai_l4) = '' OR ai_l4 = 'None')
    """
    
    conditions = []
    if start_date:
        conditions.append(f"closed_at >= '{start_date}'::timestamp")
    if end_date:
        conditions.append(f"closed_at <= '{end_date} 23:59:59'::timestamp")
    
    if conditions:
        query += " AND " + " AND ".join(conditions)
    
    logger.info(f"Querying incidents missing L4 from Snowflake table: {table_name}")
    logger.info(f"Query: {query}")
    
    try:
        conn = get_snowflake_connection()
        cursor = conn.cursor()
        cursor.execute(f"ALTER SESSION SET STATEMENT_TIMEOUT_IN_SECONDS = {timeout_seconds}")
        cursor.execute(query)
        rows = cursor.fetchall()
        cursor.close()
        conn.close()
        
        incident_ids = {row[0] for row in rows}
        logger.info(f"Found {len(incident_ids)} incidents missing L4 classification")
        return incident_ids
        
    except Exception as e:
        logger.error(f"Failed to query Snowflake for missing L4: {e}")
        raise


def get_incidents_with_l123_missing_l4(
    start_date: str | None = None,
    end_date: str | None = None,
    timeout_seconds: int = 180,
) -> "pd.DataFrame":
    """
    Fetch full incident records (with L123 classifications) for incidents missing L4.
    
    Args:
        start_date: Start date filter (YYYY-MM-DD)
        end_date: End date filter (YYYY-MM-DD)
        timeout_seconds: Query timeout in seconds (default: 180)
        
    Returns:
        DataFrame with incident data including L123 classifications
    """
    import pandas as pd
    
    if not SNOWFLAKE_AVAILABLE:
        raise ImportError("Snowflake connector not installed")
    
    table_name = settings.snowflake_table
    
    # Fetch incidents missing L4 with their L123 classifications
    # Only select columns that exist in the Snowflake table schema
    query = f"""
        SELECT 
            in_id,
            ai_l1 as category,
            ai_l2 as subcategory,
            ai_l3 as product,
            ai_confidence as confidence_score,
            ai_rationale as rationale,
            ai_keywords as keywords_identified,
            ai_self_resolved as self_resolved,
            ai_root_cause_indicator as root_cause_indicator,
            brief_description,
            resolution,
            action,
            closed_at,
            opened_at
        FROM {table_name}
        WHERE (ai_l4 IS NULL OR TRIM(ai_l4) = '' OR ai_l4 = 'None')
    """
    
    conditions = []
    if start_date:
        conditions.append(f"closed_at >= '{start_date}'::timestamp")
    if end_date:
        conditions.append(f"closed_at <= '{end_date} 23:59:59'::timestamp")
    
    if conditions:
        query += " AND " + " AND ".join(conditions)
    
    logger.info(f"Fetching incidents with L123 missing L4 from Snowflake")
    logger.info(f"Query: {query}")
    
    try:
        conn = get_snowflake_connection()
        cursor = conn.cursor()
        cursor.execute(f"ALTER SESSION SET STATEMENT_TIMEOUT_IN_SECONDS = {timeout_seconds}")
        cursor.execute(query)
        
        columns = [desc[0].lower() for desc in cursor.description]
        rows = cursor.fetchall()
        cursor.close()
        conn.close()
        
        df = pd.DataFrame(rows, columns=columns)
        logger.info(f"Fetched {len(df)} incidents with L123, missing L4")
        return df
        
    except Exception as e:
        logger.error(f"Failed to fetch incidents from Snowflake: {e}")
        raise
