"""
Azure AD authentication for OpenAI API
"""

import logging
import os
import time
from typing import Optional
import httpx
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()

logger = logging.getLogger(__name__)

# Token cache with expiry tracking
_token_cache = {
    'token': None,
    'expires_at': 0,
    'fetched_at': 0
}

# Retry configuration
MAX_RETRIES = 3
RETRY_DELAY = 2  # seconds
REQUEST_TIMEOUT = 30  # seconds


def _fetch_new_token(
    client_id: str,
    client_secret: str,
    token_url: str,
    scope: str
) -> tuple[str, int]:
    """Fetch a new token from Azure AD with retry logic.
    
    Returns:
        Tuple of (token, expires_in_seconds)
    """
    data = {
        'grant_type': 'client_credentials',
        'client_id': client_id,
        'client_secret': client_secret,
        'scope': scope
    }
    
    last_error = None
    for attempt in range(MAX_RETRIES):
        try:
            response = httpx.post(
                token_url, 
                data=data,
                timeout=httpx.Timeout(REQUEST_TIMEOUT, connect=REQUEST_TIMEOUT)
            )
            response.raise_for_status()
            
            json_response = response.json()
            token = json_response['access_token']
            expires_in = json_response.get('expires_in', 3600)
            
            return token, expires_in
            
        except (httpx.ConnectTimeout, httpx.ReadTimeout, httpx.ConnectError) as e:
            last_error = e
            if attempt < MAX_RETRIES - 1:
                wait_time = RETRY_DELAY * (attempt + 1)
                logger.warning(
                    f"Token fetch attempt {attempt + 1}/{MAX_RETRIES} failed: {e}. "
                    f"Retrying in {wait_time}s..."
                )
                time.sleep(wait_time)
            else:
                logger.error(f"Token fetch failed after {MAX_RETRIES} attempts: {e}")
                raise
        except httpx.HTTPStatusError as e:
            logger.error(f"Token fetch failed with HTTP error: {e.response.status_code}")
            raise
    
    # Should not reach here, but just in case
    raise last_error or RuntimeError("Token fetch failed")


def get_azure_ad_token(
    client_id: Optional[str] = None,
    client_secret: Optional[str] = None,
    tenant_id: Optional[str] = None,
    force_refresh: bool = False
) -> str:
    """
    Get Azure AD access token using client credentials flow.
    Token is cached and automatically refreshed when expired.
    """
    client_id = client_id or os.getenv('CLIENT_ID') or ""
    client_secret = client_secret or os.getenv('CLIENT_SECRET') or ""
    token_url = os.getenv('TOKEN_URL') or ""
    scope = os.getenv('SCOPE') or ""
    
    if not all([client_id, client_secret, token_url, scope]):
        raise ValueError(
            "Azure AD credentials required. Set CLIENT_ID, "
            "CLIENT_SECRET, TOKEN_URL, and SCOPE environment variables."
        )
    
    current_time = time.time()
    buffer_time = 600  # Refresh 10 minutes before expiry
    
    if (not force_refresh and 
        _token_cache['token'] and 
        current_time < (_token_cache['expires_at'] - buffer_time)):
        return _token_cache['token']
    
    token, expires_in = _fetch_new_token(client_id, client_secret, token_url, scope)
    
    _token_cache['token'] = token
    _token_cache['expires_at'] = current_time + expires_in
    _token_cache['fetched_at'] = current_time
    
    return token


def refresh_token() -> str:
    """Force refresh the cached token."""
    return get_azure_ad_token(force_refresh=True)


def is_token_expired() -> bool:
    """Check if the current cached token is expired or will expire soon."""
    if not _token_cache['token']:
        return True
    
    current_time = time.time()
    buffer_time = 600
    return current_time >= (_token_cache['expires_at'] - buffer_time)


def get_token_expiry_time() -> float:
    """Get the expiry timestamp of the cached token."""
    return _token_cache['expires_at']
