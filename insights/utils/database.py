"""Database utilities for PostgreSQL DataMart connectivity."""

import logging
from contextlib import contextmanager
from datetime import datetime, date
from typing import Generator

import pandas as pd
import psycopg2
from psycopg2.extensions import connection
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

from insights.config import settings

logger = logging.getLogger(__name__)


def build_incidents_query(
    days_back: int | None = None,
    hours_back: int | None = None,
    minutes_back: int | None = None,
    start_date: str | date | datetime | None = None,
    end_date: str | date | datetime | None = None,
) -> str:
    """Build the incidents query matching the mtv_tsc_servicenow_incident_data view.

    The CTE ``sn_inc`` reproduces the materialized view column-for-column.
    The outer WHERE applies the customer volume / assignment / category /
    KI-exclusion / status filters.  Only the OPEN_TIME date range is
    parameterized.

    Args:
        days_back: Number of days to look back (default 9).
        hours_back: Overrides days_back.
        minutes_back: Overrides hours_back and days_back.
        start_date: Explicit start date (YYYY-MM-DD or datetime).
        end_date: Explicit end date (YYYY-MM-DD or datetime).

    Returns:
        SQL query string.
    """
    # ── date filter on OPEN_TIME ──────────────────────────────────────
    if start_date is not None:
        if isinstance(start_date, (datetime, date)):
            start_str = start_date.strftime('%Y-%m-%d') if isinstance(start_date, datetime) else str(start_date)
        else:
            start_str = str(start_date)[:10]

        if end_date is not None:
            if isinstance(end_date, (datetime, date)):
                end_str = end_date.strftime('%Y-%m-%d') if isinstance(end_date, datetime) else str(end_date)
            else:
                end_str = str(end_date)[:10]
        else:
            end_str = "CURRENT_DATE"

        if end_str == "CURRENT_DATE":
            open_time_filter = f"CAST(tickets.open_time AS date) >= '{start_str}'"
        else:
            open_time_filter = f"CAST(tickets.open_time AS date) BETWEEN '{start_str}' AND '{end_str}'"
    else:
        if minutes_back is not None:
            interval = f"{minutes_back} minutes"
        elif hours_back is not None:
            interval = f"{hours_back} hours"
        elif days_back is not None:
            interval = f"{days_back} days"
        else:
            interval = "9 days"

        open_time_filter = f"CAST(tickets.open_time AS date) >= CURRENT_DATE - INTERVAL '{interval}'"

    return f"""
WITH wrk_nts AS (
    SELECT DISTINCT sm_incidents_work.in_id
    FROM sm_dm.sm_incidents_work
    WHERE upper(sm_incidents_work.wrk_notes::text) ~~ '%RCO%'::text
      AND sm_incidents_work.in_id::text ~~ 'INC%'::text
),
sn_inc AS (
    SELECT
        tickets.in_id,
        tickets.sn_sys_id,
        CASE
            WHEN tickets.uh_start_time < (CURRENT_DATE - '7500 days'::interval)
            THEN tickets.uh_start_time + (date_part('year', CURRENT_DATE) - date_part('year', tickets.uh_start_time)) * '1 year'::interval
            ELSE tickets.uh_start_time
        END AS uh_start_time,
        tickets.open_time,
        tsc_data.close_time,
        tsc_data.uh_pending_closed,
        tsc_data.update_time,
        date_trunc('day', tsc_data.update_time) AS update_date,
        tickets.uh_incident_type,
        tsc_data.problem_status,
        tickets.priority_code,
        tickets.in_category,
        tickets.subcategory,
        tickets.uh_assignment_counter,
        col.uh_undefined_service,
        substring(tickets.uh_logical_name::text, 1, 60) AS uh_logical_name,
        tickets.uh_undefined_ci1,
        tickets.uh_callback_contact,
        caller.uh_msdomainid   AS caller_msdomainid,
        caller.full_name       AS caller_full_name,
        caller.uh_gl_code      AS caller_gl_code,
        caller.location_cd     AS caller_location_cd,
        caller.uh_org_business AS caller_org_business,
        caller.uh_org_division AS caller_org_division,
        caller.uh_org_segment  AS caller_org_segment,
        tickets.uh_opened_by_id,
        opened_by.full_name    AS opened_by_full_name,
        opened_by.manager      AS opened_by_manager,
        opened_by.location_cd  AS opened_by_location,
        opened_by.uh_msdomainid AS opened_by_msdomainid,
        tsc_data.first_assigned_id,
        first_assigned.uh_msdomainid AS first_assigned_msdomainid,
        first_assigned.full_name     AS first_assigned_full_name,
        first_assigned.manager       AS first_assigned_manager,
        first_assigned.location_cd   AS first_assigned_location,
        tsc_data.last_assigned_id,
        substr(tickets.assignment::text, 1, 50) AS assignment,
        tickets.uh_reopened_counter,
        tickets.resolution_code,
        CASE
            WHEN tickets.uh_restored_duration < 0 THEN NULL
            ELSE tickets.uh_restored_duration
        END AS uh_restored_duration,
        tickets.uh_restoration_goal,
        tickets.uh_missed_sla,
        tickets.brief_description,
        col.resolution,
        tickets.company AS uh_business,
        CASE
            WHEN tickets.open_time > to_date('4/6/2021', 'MM/DD/YYYY') AND tickets.sn_kb_integration IS NOT NULL
            THEN tickets.sn_kb_integration::text
            ELSE NULL
        END AS uks_tsc_issue_ki_id,
        CASE
            WHEN tickets.open_time > '2021-04-06'::timestamp AND tickets.uh_usc_knowledge::text ~~ 'KB%'::text
            THEN substr(tickets.uh_usc_knowledge::text, 1, 40)
            ELSE NULL
        END AS uks_tsc_res_ki_id,
        tickets.uh_connection_type,
        tickets.uh_platform_type,
        tickets.sn_parent_id,
        tickets.uh_sla_group,
        tickets.company,
        upper(col.uh_ess_app_notworking::text) AS uh_ess_app_notworking,
        CASE
            WHEN tsc_data.sn_contact_type IS NOT NULL THEN tsc_data.sn_contact_type::text
            ELSE lower(tickets.sn_contact_type::text)
        END AS sn_contact_type,
        tsc_data.sn_contact_type_direct,
        tsc_data.resolved_by_tsc,
        tsc_data.tsc_missed_fcr,
        tsc_data.first_contact_resolution,
        tsc_data.contacts_total,
        tsc_data.contacts_tsc,
        tsc_data.ess_response_time_minute,
        tsc_data.volume_phone,
        tsc_data.volume_chat,
        tsc_data.volume_web_portal,
        tsc_data.volume_integration,
        tsc_data.volume_email,
        tsc_data.volume_technician,
        tsc_data.volume_other,
        CASE
            WHEN tsc_data.volume_web_portal = 1 AND (tsc_data.contacts_tsc = 0 OR tsc_data.contacts_tsc IS NULL) THEN 1
            WHEN tsc_data.volume_web_portal = 1 THEN 0
            ELSE NULL
        END AS web_portal_nonworked,
        tickets.assignee_name,
        tickets.uh_assignee_full_name,
        0 AS overdue_escalation_count,
        0 AS mytickets_escalation_count,
        tickets.uh_svc_cmplnt_cnt::integer AS escalation_count,
        col.update_action_ess AS u_customer_states,
        col.uh_ess_errormsg,
        tickets.uh_machine_name,
        col.action AS description,
        caller.uh_msdomainid,
        caller.email,
        CASE
            WHEN tickets.restored_duration_xwt < 0 THEN NULL
            ELSE tickets.restored_duration_xwt
        END AS restored_duration_xwt,
        tickets.sn_issue_resolved_ticket_only,
        tickets.uh_classification,
        tickets.sn_classification_2,
        tickets.sn_classification_3,
        CASE
            WHEN wn.in_id = tickets.in_id THEN 1
            ELSE 0
        END AS rco_work_notes,
        tickets.uh_alternate_full_name,
        tickets.uh_alternate_phone,
        tickets.sn_alt_email_add AS alt_email,
        tickets.sn_subcategory2,
        tickets.uh_css_knowledge,
        tsc_val.first_assigned_workgroup,
        tickets.sn_user_bussiness_impact
    FROM sm_dm.sm_incidents_v2 tickets
    JOIN sm_dm.sm_incidents_text_cols col ON tickets.in_id = col.in_id
    JOIN sm_dm.sn_incidents_tsc tsc_data ON tickets.in_id = tsc_data.in_id
    LEFT JOIN wrk_nts wn ON wn.in_id = tickets.in_id
    LEFT JOIN sm_dm.sm_contacts caller ON tickets.uh_callback_contact = caller.contact_name
    LEFT JOIN sm_dm.sm_contacts opened_by ON tickets.uh_opened_by_id = opened_by.contact_name
    LEFT JOIN sm_dm.sm_contacts first_assigned ON tsc_data.first_assigned_id = first_assigned.contact_name
    LEFT JOIN sm_dm.sm_contacts last_assigned ON tsc_data.last_assigned_id = last_assigned.contact_name
    LEFT JOIN sm_dm.sn_incidents_tsc_values tsc_val ON tsc_val.in_id = tickets.in_id
    WHERE tickets.event_id IS NULL
      AND tickets.uh_incident_type::text <> 'Primary Incident'
      AND tickets.in_id::text ~~ 'INC%'::text
      AND (
          tickets.update_time >= '2023-06-01 00:00:00'::timestamp
          OR (tickets.sn_parent_id IS NOT NULL AND tickets.update_time < '2023-06-01 00:00:00'::timestamp)
      )
      AND {open_time_filter}
)
SELECT
    sn_inc.*,
    -- Aliases for pipeline/Snowflake backward compatibility
    open_time                                AS opened_at,
    COALESCE(uh_pending_closed, close_time)  AS closed_at,
    description                              AS action,
    first_assigned_workgroup                 AS first_assignment_group
FROM sn_inc
WHERE
    (
        volume_phone = 1
        OR volume_chat = 1
        OR volume_web_portal = 1
        OR (
            (volume_integration = 1 AND UPPER(first_assigned_workgroup) = 'RCO_SMART SPOT')
            OR (volume_technician = 1 AND UPPER(first_assigned_workgroup) = 'RCO_SMART SPOT')
        )
        OR (
            volume_integration = 1
            AND (
                opened_by_full_name = 'MAX VIRTUAL ASSISTANT - POWER PLATFORM Integration'
                OR opened_by_full_name = 'CAIP ITSS MAX Integration'
            )
        )
    )
    AND UPPER(first_assigned_workgroup) IN (
        'CLINICAL SUPPORT CENTER - DSK',
        'COMMERCIAL SUPPORT',
        'MAC SUPPORT - SPT',
        'TSC COMMERCIAL SUPPORT - WEB PORTAL',
        'TSC EXECUTIVE SUPPORT - SPT',
        'TSC HELP DESK - SPT',
        'TSC SELF SERVICE',
        'RCO_SMART SPOT',
        'OC (NWP) - SERVICE DESK',
        'OC (NWP) - DESKTOP FIELD SERVICES',
        'OC (LDMGCO) - SERVICE DESK',
        'OC (LDMGCO) - DESKTOP FIELD SERVICES',
        'OC (LDMGNM) - SERVICE DESK',
        'OC (LDMGNM) - DESKTOP FIELD SERVICES',
        'CARE HELP DESK',
        'CARE FIELD SERVICES - NWP',
        'OGS_IT_SERVICEDESK SELF-SERVICE',
        'CLINICAL SUPPORT SELF SERVICE',
        'CARE HELP DESK SELF SERVICE',
        'OGS_IT_SERVICEDESK'
    )
    AND (uh_opened_by_id NOT IN (
            '700000001','700000003','700000005','700000008','700000009','700000030','700000120',
            '700000205','700000227','700001051','700001087','700001758'
        ) OR uh_opened_by_id IS NULL)
    AND (priority_code NOT IN ('1','2') OR priority_code IS NULL)
    AND (uh_incident_type = 'Standard Incident' OR (uh_incident_type = 'Secondary Incident' AND first_assigned_id IS NOT NULL))
    AND in_category IN ('Break/Fix','Request for Information','Password Reset','Service Request')
    AND (
        TRIM(uks_tsc_res_ki_id) NOT IN (
            '72606','72678','78739','96087','96086','96084','96085','115405','121221','136262','159889','191022',
            'KBB0023510','KBB0014545','KBB0014567','KBB0015165','KBB0016365','KBB0016372','KBB0016373','KBB0016374',
            'KBB0017923','KBB0018832','KBB0021056','KBB0030369','KBB0091411',
            'KBB0091415','KBB0091416','KBB0091417','KBB0091418','KBB0092078','KBB0092997','KBB0091851'
        )
        OR uks_tsc_res_ki_id IS NULL
    )
    AND problem_status = 'Closed';
"""


# Default query for backwards compatibility (9 days matches materialized view)
INCIDENTS_QUERY = build_incidents_query(9)


def get_engine() -> Engine:
    """Create a SQLAlchemy engine for database connections.
    
    Returns:
        SQLAlchemy Engine object
    """
    print(f"[DB] get_engine: reading settings...", flush=True)
    host = settings.datamart_host
    port = settings.datamart_port
    db = settings.datamart_db
    user = settings.datamart_user
    print(f"[DB] get_engine: host={host}, port={port}, db={db}, user={user}", flush=True)
    
    connection_string = (
        f"postgresql+psycopg2://{user}:{settings.datamart_pass}"
        f"@{host}:{port}/{db}"
    )
    print(f"[DB] get_engine: creating engine...", flush=True)
    engine = create_engine(
        connection_string,
        connect_args={"connect_timeout": 30, "options": "-c statement_timeout=600000"}  # 10 min query timeout
    )
    print(f"[DB] get_engine: engine created", flush=True)
    return engine


@contextmanager
def get_connection() -> Generator[connection, None, None]:
    """Create and yield a database connection, ensuring proper cleanup.
    
    Yields:
        psycopg2 connection object
        
    Raises:
        psycopg2.Error: If connection fails
    """
    conn = None
    try:
        conn = psycopg2.connect(
            dbname=settings.datamart_db,
            host=settings.datamart_host,
            user=settings.datamart_user,
            password=settings.datamart_pass,
            port=settings.datamart_port,
        )
        logger.debug("Database connection established")
        yield conn
    except psycopg2.Error as e:
        logger.error("Database connection failed: %s", e)
        raise
    finally:
        if conn is not None:
            conn.close()
            logger.debug("Database connection closed")


def load_incidents_from_database(
    query: str | None = None,
    days_back: int | None = None,
    hours_back: int | None = None,
    minutes_back: int | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
) -> pd.DataFrame:
    """Load incident data from the PostgreSQL DataMart.
    
    Args:
        query: Custom SQL query. If None, builds the default query using time parameters.
        days_back: Number of days to look back for incidents.
        hours_back: Number of hours to look back (overrides days_back).
        minutes_back: Number of minutes to look back (overrides hours_back and days_back).
        start_date: Start date for date range (format: YYYY-MM-DD). Takes precedence over lookback.
        end_date: End date for date range (format: YYYY-MM-DD). Defaults to now if start_date is set.
    
    Returns:
        DataFrame containing incident records with columns:
        - in_id: Incident ID
        - assignment: Current assignment group
        - first_assignment_group: First allowed assignment group
        - brief_description: Brief summary
        - action: Action taken
        - resolution: Resolution notes
        - update_action_ess: ESS update action
        - uh_ess_errormsg: ESS error message
        - update_action: Update action
        - comments: Device comments
        - uh_monitoring_notes: Monitoring notes
        
    Raises:
        psycopg2.Error: If database query fails
    """
    sql = query if query is not None else build_incidents_query(
        days_back=days_back, hours_back=hours_back, minutes_back=minutes_back,
        start_date=start_date, end_date=end_date
    )
    
    # Determine lookback description for logging
    if start_date is not None:
        if end_date is not None:
            lookback_desc = f"date range {start_date} to {end_date}"
        else:
            lookback_desc = f"from {start_date} to now"
    elif minutes_back is not None:
        lookback_desc = f"{minutes_back} minutes"
    elif hours_back is not None:
        lookback_desc = f"{hours_back} hours"
    elif days_back is not None:
        lookback_desc = f"{days_back} days"
    else:
        lookback_desc = "9 days (default)"
    
    logger.info("Fetching incidents from database (last %s)", lookback_desc)
    
    # Use psycopg2 directly with a server-side (named) cursor for chunked
    # fetching — this matches DBeaver's behaviour and avoids the overhead of
    # SQLAlchemy type-introspection + full-result-set buffering that makes
    # pd.read_sql painfully slow on wide result sets.
    print(f"[DB] Connecting via psycopg2...", flush=True)
    with get_connection() as conn:
        # Bump work_mem for this session so sorts/hashes stay in RAM
        with conn.cursor() as setup_cur:
            setup_cur.execute("SET work_mem = '256MB'")

        # Named cursor → server-side portal; itersize controls fetch batch
        with conn.cursor(name="walle_incidents") as cur:
            cur.itersize = 5000
            print(f"[DB] Executing query...", flush=True)
            cur.execute(sql)
            # Trigger the first fetch so .description gets populated
            first_batch = cur.fetchmany(cur.itersize)
            cols = [desc[0] for desc in cur.description]
            print(f"[DB] Fetching rows (batch size {cur.itersize})...", flush=True)
            rows = list(first_batch)
            while True:
                batch = cur.fetchmany(cur.itersize)
                if not batch:
                    break
                rows.extend(batch)
        df = pd.DataFrame(rows, columns=cols)
        print(f"[DB] Query complete, got {len(df)} rows", flush=True)
    
    record_count = len(df)
    logger.info("Retrieved %d incident records from database", record_count)
    
    if record_count == 0:
        logger.warning("No incidents found in database for the specified criteria")
    
    return df


def test_connection() -> bool:
    """Test database connectivity.
    
    Returns:
        True if connection succeeds, False otherwise
    """
    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
                result = cur.fetchone()
                return result is not None and result[0] == 1
    except psycopg2.Error as e:
        logger.error("Connection test failed: %s", e)
        return False
