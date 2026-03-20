"""Global progress tracking for pipeline runs."""

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any


@dataclass
class GlobalProgress:
    """
    Centralized progress tracker for the classification pipeline.
    
    Displays a single unified view of progress across all workers and subcategories.
    """
    
    # Overall stats
    total_records: int = 0
    processed_records: int = 0
    successful: int = 0
    failed: int = 0
    
    # L123 stats
    l123_total: int = 0
    l123_processed: int = 0
    l123_success: int = 0
    
    # L4 stats  
    l4_total: int = 0
    l4_processed: int = 0
    l4_success: int = 0
    l4_subcategories_total: int = 0
    l4_subcategories_done: int = 0
    
    # Rate limiting
    current_rpm: float = 0.0
    max_rpm: int = 550
    
    # Timing
    start_time: float = field(default_factory=time.time)
    
    # Active subcategories
    active_subcats: dict[str, dict[str, Any]] = field(default_factory=dict)
    
    # Lock for thread safety
    _lock: asyncio.Lock | None = None
    
    def __post_init__(self):
        self._lock = asyncio.Lock()
    
    @property
    def elapsed_seconds(self) -> float:
        return time.time() - self.start_time
    
    @property
    def records_per_second(self) -> float:
        elapsed = self.elapsed_seconds
        if elapsed > 0:
            return self.processed_records / elapsed
        return 0.0
    
    @property
    def eta_seconds(self) -> float:
        """Estimated time remaining."""
        rps = self.records_per_second
        remaining = self.total_records - self.processed_records
        if rps > 0:
            return remaining / rps
        return 0.0
    
    async def update(self, **kwargs):
        """Thread-safe update of progress values."""
        if self._lock is None:
            self._lock = asyncio.Lock()
        async with self._lock:
            for key, value in kwargs.items():
                if hasattr(self, key):
                    setattr(self, key, value)
    
    async def increment(self, **kwargs):
        """Thread-safe increment of progress counters."""
        if self._lock is None:
            self._lock = asyncio.Lock()
        async with self._lock:
            for key, value in kwargs.items():
                if hasattr(self, key):
                    current = getattr(self, key)
                    setattr(self, key, current + value)
    
    def format_time(self, seconds: float) -> str:
        """Format seconds as HH:MM:SS or MM:SS."""
        if seconds < 0:
            return "--:--"
        hours = int(seconds // 3600)
        minutes = int((seconds % 3600) // 60)
        secs = int(seconds % 60)
        if hours > 0:
            return f"{hours}:{minutes:02d}:{secs:02d}"
        return f"{minutes}:{secs:02d}"
    
    def get_status_line(self) -> str:
        """Generate a single-line status string."""
        pct = (self.processed_records / self.total_records * 100) if self.total_records > 0 else 0
        
        parts = [
            f"[{pct:5.1f}%]",
            f"{self.processed_records:,}/{self.total_records:,}",
            f"| OK:{self.successful:,} ERR:{self.failed:,}",
            f"| {self.current_rpm:.0f}/{self.max_rpm} RPM",
            f"| {self.records_per_second:.1f} rec/s",
            f"| ETA: {self.format_time(self.eta_seconds)}",
        ]
        
        if self.l4_subcategories_total > 0:
            parts.append(f"| L4: {self.l4_subcategories_done}/{self.l4_subcategories_total} subcats")
        
        return " ".join(parts)
    
    def print_status(self):
        """Print current status to console (overwrites line)."""
        status = self.get_status_line()
        # Use carriage return to overwrite the line
        print(f"\r{status}", end="", flush=True)
    
    def print_header(self, stage: str):
        """Print a stage header."""
        print(f"\n{'='*60}")
        print(f"  {stage}")
        print(f"{'='*60}")
    
    def print_summary(self):
        """Print final summary."""
        elapsed = self.elapsed_seconds
        print(f"\n{'='*60}")
        print(f"  PIPELINE SUMMARY")
        print(f"{'='*60}")
        print(f"  Duration:     {self.format_time(elapsed)}")
        print(f"  Total:        {self.total_records:,} records")
        print(f"  Successful:   {self.successful:,}")
        print(f"  Failed:       {self.failed:,}")
        print(f"  Throughput:   {self.records_per_second:.1f} records/sec")
        print(f"  Avg RPM:      {self.current_rpm:.0f}")
        
        if self.l123_total > 0:
            print(f"\n  L1/L2/L3:")
            print(f"    Processed:  {self.l123_processed:,}/{self.l123_total:,}")
            print(f"    Success:    {self.l123_success:,}")
        
        if self.l4_total > 0:
            print(f"\n  L4:")
            print(f"    Processed:  {self.l4_processed:,}/{self.l4_total:,}")
            print(f"    Success:    {self.l4_success:,}")
            print(f"    Subcats:    {self.l4_subcategories_done}/{self.l4_subcategories_total}")
        
        print(f"{'='*60}")


# Singleton instance
_global_progress: GlobalProgress | None = None


def get_progress() -> GlobalProgress:
    """Get the global progress tracker instance."""
    global _global_progress
    if _global_progress is None:
        _global_progress = GlobalProgress()
    return _global_progress


def reset_progress():
    """Reset the global progress tracker."""
    global _global_progress
    _global_progress = GlobalProgress()
    return _global_progress
