"""S3 utilities for taxonomy storage and retrieval."""

import json
import logging
import os
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

from insights.config import settings

logger = logging.getLogger(__name__)


def get_s3_client():
    """Create and return an S3 client configured for Optum S3."""
    if not settings.s3_access_key or not settings.s3_secret_key:
        logger.warning("S3 credentials not configured")
        return None
    
    config = Config(
        signature_version="s3",
        s3={"addressing_style": "virtual"},
    )
    
    return boto3.client(
        "s3",
        aws_access_key_id=settings.s3_access_key,
        aws_secret_access_key=settings.s3_secret_key,
        endpoint_url=settings.s3_endpoint_url,
        region_name=settings.s3_region_name,
        config=config,
    )


def get_taxonomy_s3_key(subcategory: str) -> str:
    """Generate S3 key for a taxonomy file."""
    safe_name = subcategory.lower().replace(" ", "_").replace("/", "_")
    return f"{settings.s3_taxonomy_prefix}/l4_{safe_name}_taxonomy.json"


def upload_taxonomy_to_s3(subcategory: str, taxonomy_dict: dict[str, Any]) -> bool:
    """
    Upload a taxonomy to S3.
    
    Args:
        subcategory: The subcategory name
        taxonomy_dict: The taxonomy dictionary to upload
        
    Returns:
        True if upload succeeded, False otherwise
    """
    client = get_s3_client()
    if client is None:
        logger.warning("S3 client not available, skipping upload")
        return False
    
    s3_key = get_taxonomy_s3_key(subcategory)
    
    # Add metadata
    taxonomy_dict["s3_uploaded_at"] = datetime.now().isoformat()
    
    try:
        client.put_object(
            Bucket=settings.s3_bucket_name,
            Key=s3_key,
            Body=json.dumps(taxonomy_dict, indent=2, default=str),
            ContentType="application/json",
            CacheControl="max-age=86400",
        )
        logger.info(f"Uploaded taxonomy to s3://{settings.s3_bucket_name}/{s3_key}")
        return True
    except ClientError as e:
        logger.error(f"Failed to upload taxonomy to S3: {e}")
        return False


def download_taxonomy_from_s3(subcategory: str) -> dict[str, Any] | None:
    """
    Download a taxonomy from S3.
    
    Args:
        subcategory: The subcategory name
        
    Returns:
        The taxonomy dictionary if found and valid, None otherwise
    """
    client = get_s3_client()
    if client is None:
        return None
    
    s3_key = get_taxonomy_s3_key(subcategory)
    
    try:
        response = client.get_object(
            Bucket=settings.s3_bucket_name,
            Key=s3_key,
        )
        content = response["Body"].read().decode("utf-8")
        taxonomy_dict = json.loads(content)
        
        # Check if taxonomy is still valid (less than 24 hours old)
        if "s3_uploaded_at" in taxonomy_dict:
            uploaded_at = datetime.fromisoformat(taxonomy_dict["s3_uploaded_at"])
            age = datetime.now() - uploaded_at
            if age > timedelta(hours=24):
                logger.info(f"Taxonomy for {subcategory} is {age.total_seconds()/3600:.1f}h old, needs refresh")
                return None
            logger.info(f"Downloaded valid taxonomy from S3 (age: {age.total_seconds()/3600:.1f}h)")
        
        return taxonomy_dict
    except ClientError as e:
        if e.response["Error"]["Code"] == "NoSuchKey":
            logger.info(f"No taxonomy found in S3 for {subcategory}")
        else:
            logger.error(f"Failed to download taxonomy from S3: {e}")
        return None


def check_taxonomy_exists_in_s3(subcategory: str) -> bool:
    """Check if a taxonomy exists in S3 and is less than 24 hours old."""
    client = get_s3_client()
    if client is None:
        return False
    
    s3_key = get_taxonomy_s3_key(subcategory)
    
    try:
        response = client.head_object(
            Bucket=settings.s3_bucket_name,
            Key=s3_key,
        )
        # Check last modified time
        last_modified = response.get("LastModified")
        if last_modified:
            age = datetime.now(last_modified.tzinfo) - last_modified
            return age < timedelta(hours=24)
        return True
    except ClientError:
        return False


def list_taxonomies_in_s3() -> list[str]:
    """List all taxonomy files in S3."""
    client = get_s3_client()
    if client is None:
        return []
    
    try:
        response = client.list_objects_v2(
            Bucket=settings.s3_bucket_name,
            Prefix=settings.s3_taxonomy_prefix,
        )
        
        taxonomies = []
        for obj in response.get("Contents", []):
            key = obj["Key"]
            if key.endswith("_taxonomy.json"):
                # Extract subcategory name from key
                filename = key.split("/")[-1]
                subcategory = filename.replace("l4_", "").replace("_taxonomy.json", "")
                taxonomies.append(subcategory)
        
        return taxonomies
    except ClientError as e:
        logger.error(f"Failed to list taxonomies in S3: {e}")
        return []


def sync_taxonomies_to_s3() -> int:
    """
    Sync all local taxonomies to S3.
    
    Returns:
        Number of taxonomies uploaded
    """
    uploaded = 0
    taxonomy_dir = settings.taxonomy_dir
    
    if not taxonomy_dir.exists():
        return 0
    
    for taxonomy_file in taxonomy_dir.glob("l4_*_taxonomy.json"):
        try:
            with open(taxonomy_file, "r") as f:
                taxonomy_dict = json.load(f)
            
            # Extract subcategory from filename
            subcategory = taxonomy_file.stem.replace("l4_", "").replace("_taxonomy", "")
            
            if upload_taxonomy_to_s3(subcategory, taxonomy_dict):
                uploaded += 1
        except Exception as e:
            logger.error(f"Failed to sync {taxonomy_file}: {e}")
    
    return uploaded


def sync_taxonomies_from_s3() -> int:
    """
    Download all taxonomies from S3 to local storage.
    
    Returns:
        Number of taxonomies downloaded
    """
    client = get_s3_client()
    if client is None:
        return 0
    
    downloaded = 0
    taxonomy_dir = settings.taxonomy_dir
    taxonomy_dir.mkdir(parents=True, exist_ok=True)
    
    try:
        response = client.list_objects_v2(
            Bucket=settings.s3_bucket_name,
            Prefix=settings.s3_taxonomy_prefix,
        )
        
        for obj in response.get("Contents", []):
            key = obj["Key"]
            if not key.endswith("_taxonomy.json"):
                continue
            
            filename = key.split("/")[-1]
            local_path = taxonomy_dir / filename
            
            try:
                obj_response = client.get_object(
                    Bucket=settings.s3_bucket_name,
                    Key=key,
                )
                content = obj_response["Body"].read().decode("utf-8")
                
                with open(local_path, "w") as f:
                    f.write(content)
                
                downloaded += 1
                logger.info(f"Downloaded {filename} from S3")
            except ClientError as e:
                logger.error(f"Failed to download {key}: {e}")
        
        logger.info(f"Synced {downloaded} taxonomies from S3")
        return downloaded
    except ClientError as e:
        logger.error(f"Failed to sync taxonomies from S3: {e}")
        return 0


# =============================================================================
# Artifact Storage Functions
# =============================================================================

def get_run_id() -> str:
    """Return a canonical run ID for this WALLE run.

    Honors the ``WALLE_RUN_ID`` environment variable when set so that all
    partition workers, the L123 merge job, and the finalize job in a single
    GitHub Actions workflow share the same canonical id. This is what ties
    together rows in ``WALLE_L123_TAXONOMY_AUDIT``, ``WALLE_L4_NULL_REASONS``,
    and ``WALLE_CLASSIFIED_INCIDENTS`` for the same run.

    Falls back to ``datetime.now().strftime("%Y%m%d_%H%M%S")`` when the env
    var is unset, preserving the original behavior for local runs and any
    callers that don't set it.
    """
    env_id = os.environ.get("WALLE_RUN_ID")
    if env_id:
        return env_id
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def upload_artifact_to_s3(
    local_path: Path | str,
    run_id: str,
    artifact_type: str = "output",
) -> bool:
    """
    Upload an artifact file to S3.
    
    Args:
        local_path: Path to the local file
        run_id: Run identifier (timestamp-based)
        artifact_type: Type of artifact (e.g., 'incidents', 'classified', 'logs')
        
    Returns:
        True if upload succeeded, False otherwise
    """
    client = get_s3_client()
    if client is None:
        logger.warning("S3 client not available, skipping artifact upload")
        return False
    
    local_path = Path(local_path)
    if not local_path.exists():
        logger.warning(f"Artifact file not found: {local_path}")
        return False
    
    # S3 key: walle/artifacts/{run_id}/{artifact_type}/{filename}
    s3_key = f"{settings.s3_artifacts_prefix}/{run_id}/{artifact_type}/{local_path.name}"
    
    # Determine content type
    if local_path.suffix == ".csv":
        content_type = "text/csv"
    elif local_path.suffix == ".json":
        content_type = "application/json"
    elif local_path.suffix == ".log":
        content_type = "text/plain"
    else:
        content_type = "application/octet-stream"
    
    try:
        with open(local_path, "rb") as f:
            client.put_object(
                Bucket=settings.s3_bucket_name,
                Key=s3_key,
                Body=f.read(),
                ContentType=content_type,
            )
        logger.info(f"Uploaded artifact to s3://{settings.s3_bucket_name}/{s3_key}")
        return True
    except ClientError as e:
        logger.error(f"Failed to upload artifact to S3: {e}")
        return False


def upload_run_artifacts(
    run_id: str,
    incidents_file: Path | str | None = None,
    classified_file: Path | str | None = None,
    log_file: Path | str | None = None,
    additional_files: list[tuple[Path | str, str]] | None = None,
) -> dict[str, bool]:
    """
    Upload all artifacts from a pipeline run to S3.
    
    Args:
        run_id: Run identifier
        incidents_file: Path to fetched incidents CSV
        classified_file: Path to classified output CSV
        log_file: Path to log file
        additional_files: List of (file_path, artifact_type) tuples
        
    Returns:
        Dict mapping artifact names to upload success status
    """
    results = {}
    
    if incidents_file:
        results["incidents"] = upload_artifact_to_s3(incidents_file, run_id, "incidents")
    
    if classified_file:
        results["classified"] = upload_artifact_to_s3(classified_file, run_id, "classified")
    
    if log_file:
        results["logs"] = upload_artifact_to_s3(log_file, run_id, "logs")
    
    if additional_files:
        for file_path, artifact_type in additional_files:
            results[f"{artifact_type}_{Path(file_path).name}"] = upload_artifact_to_s3(
                file_path, run_id, artifact_type
            )
    
    # Print summary
    uploaded = sum(1 for v in results.values() if v)
    if uploaded > 0:
        logger.info(f"Uploaded {uploaded}/{len(results)} artifacts to S3 (run: {run_id})")
    
    return results


def list_artifact_runs() -> list[tuple[str, datetime]]:
    """
    List all artifact runs in S3, sorted by date (newest first).
    
    Returns:
        List of (run_id, timestamp) tuples
    """
    client = get_s3_client()
    if client is None:
        return []
    
    try:
        # List objects with the artifacts prefix
        paginator = client.get_paginator("list_objects_v2")
        runs = set()
        
        for page in paginator.paginate(
            Bucket=settings.s3_bucket_name,
            Prefix=settings.s3_artifacts_prefix + "/",
            Delimiter="/",
        ):
            for prefix in page.get("CommonPrefixes", []):
                # Extract run_id from prefix like "walle/artifacts/20260115-123456/"
                run_id = prefix["Prefix"].rstrip("/").split("/")[-1]
                # Parse timestamp from run_id (handle both hyphen and underscore formats)
                ts = None
                for fmt in ("%Y%m%d-%H%M%S", "%Y%m%d_%H%M%S"):
                    try:
                        ts = datetime.strptime(run_id, fmt)
                        break
                    except ValueError:
                        continue
                if ts:
                    runs.add((run_id, ts))
                else:
                    logger.warning(f"Skipping malformed run ID: {run_id}")
        
        # Sort by timestamp descending (newest first)
        return sorted(runs, key=lambda x: x[1], reverse=True)
    except ClientError as e:
        logger.error(f"Failed to list artifact runs: {e}")
        return []


def cleanup_old_artifact_runs(keep_count: int | None = None) -> int:
    """
    Remove old artifact runs, keeping only the most recent N runs.
    
    Args:
        keep_count: Number of runs to keep (default: settings.s3_artifacts_retention)
        
    Returns:
        Number of runs deleted
    """
    if keep_count is None:
        keep_count = settings.s3_artifacts_retention
    
    client = get_s3_client()
    if client is None:
        return 0
    
    runs = list_artifact_runs()
    
    if len(runs) <= keep_count:
        logger.info(f"Only {len(runs)} runs exist, no cleanup needed (retention: {keep_count})")
        return 0
    
    # Runs to delete (everything after the first keep_count)
    runs_to_delete = runs[keep_count:]
    deleted_count = 0
    
    for run_id, ts in runs_to_delete:
        prefix = f"{settings.s3_artifacts_prefix}/{run_id}/"
        
        try:
            # List all objects under this run
            paginator = client.get_paginator("list_objects_v2")
            objects_to_delete = []
            
            for page in paginator.paginate(
                Bucket=settings.s3_bucket_name,
                Prefix=prefix,
            ):
                for obj in page.get("Contents", []):
                    objects_to_delete.append(obj["Key"])
            
            if objects_to_delete:
                # Delete objects one at a time (batch delete requires Content-MD5 on some S3 providers)
                for key in objects_to_delete:
                    try:
                        client.delete_object(
                            Bucket=settings.s3_bucket_name,
                            Key=key,
                        )
                    except ClientError as del_err:
                        logger.warning(f"Failed to delete {key}: {del_err}")
                
                logger.info(f"Deleted run {run_id} ({len(objects_to_delete)} objects)")
                deleted_count += 1
        except ClientError as e:
            logger.error(f"Failed to delete run {run_id}: {e}")
    
    if deleted_count > 0:
        logger.info(f"Cleaned up {deleted_count} old runs (keeping last {keep_count})")
    
    return deleted_count