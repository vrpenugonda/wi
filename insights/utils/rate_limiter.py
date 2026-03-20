"""Rate limiting utilities for API request management."""

import asyncio
import time
from collections import deque
from typing import Optional


class GlobalRateLimiter:
    """
    High-throughput global rate limiter for Azure OpenAI.
    
    Tracks COMPLETED requests per minute, not started requests.
    This is important because Azure OpenAI rate limits are based on
    completed requests, and with high latency (60-100s), we need to
    track completions not starts.
    """
    
    _instance: Optional["GlobalRateLimiter"] = None
    
    def __new__(cls, max_rpm: int = 550):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance
    
    def __init__(self, max_rpm: int = 550):
        if self._initialized:
            return
        
        self.max_rpm = max_rpm
        self.window_seconds = 60
        # Track COMPLETED request times, not started
        self.completed_times: deque[float] = deque()
        self._lock = asyncio.Lock()
        self._initialized = True
    
    @classmethod
    def get_instance(cls, max_rpm: int = 550) -> "GlobalRateLimiter":
        """Get or create the singleton instance."""
        if cls._instance is None:
            cls._instance = cls(max_rpm)
        return cls._instance
    
    @classmethod
    def reset(cls):
        """Reset the singleton (useful for testing)."""
        cls._instance = None
    
    def _cleanup_old_requests(self, current_time: float):
        """Remove requests older than the window."""
        cutoff = current_time - self.window_seconds
        while self.completed_times and self.completed_times[0] < cutoff:
            self.completed_times.popleft()
    
    def get_current_rpm(self) -> float:
        """Get the current completed requests per minute (lock-free read)."""
        current_time = time.time()
        cutoff = current_time - self.window_seconds
        count = sum(1 for t in self.completed_times if t >= cutoff)
        return float(count)
    
    async def wait_if_needed(self, request_count: int = 1) -> float:
        """
        For high-latency APIs, don't throttle on start - let requests fly.
        We only track completions for stats purposes.
        
        Returns:
            Always 0 - no waiting on request start
        """
        # Don't wait on request START - just let it fly
        # Rate limiting for Azure OpenAI is handled by the service itself
        return 0.0
    
    async def record_completion(self, count: int = 1):
        """Record that a request completed (for stats tracking)."""
        async with self._lock:
            current_time = time.time()
            for _ in range(count):
                self.completed_times.append(current_time)
    
    def get_stats(self) -> dict:
        """Get current rate limiter statistics."""
        current_time = time.time()
        self._cleanup_old_requests(current_time)
        current_rpm = len(self.completed_times)
        
        return {
            "current_rpm": current_rpm,
            "max_rpm": self.max_rpm,
            "requests_in_window": current_rpm,
            "utilization": current_rpm / self.max_rpm if self.max_rpm > 0 else 0,
        }
        """Get current rate limiter statistics."""
        current_time = time.time()
        self._cleanup_old_requests(current_time)
        current_rpm = len(self.request_times)
        
        return {
            "current_rpm": current_rpm,
            "max_rpm": self.max_rpm,
            "requests_in_window": current_rpm,
            "utilization": current_rpm / self.max_rpm if self.max_rpm > 0 else 0,
        }


# Global instance for easy access
_rate_limiter: GlobalRateLimiter | None = None


def get_rate_limiter(max_rpm: int = 550) -> GlobalRateLimiter:
    """Get the global rate limiter instance."""
    global _rate_limiter
    if _rate_limiter is None:
        _rate_limiter = GlobalRateLimiter(max_rpm)
    return _rate_limiter


def reset_rate_limiter():
    """Reset the global rate limiter."""
    global _rate_limiter
    _rate_limiter = None
    GlobalRateLimiter.reset()
