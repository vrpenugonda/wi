"""
Request metrics tracking
"""

import time


class RequestMetrics:
    """Track request metrics for rate limiting and monitoring"""
    
    def __init__(self, window_size: int = 60):
        self.start_time = time.time()
        self.request_count = 0
        self.recent_requests = []
        self.window_size = window_size
        self.successful = 0
        self.failed = 0
    
    def record_request(self, success: bool = True):
        """Record a request"""
        current_time = time.time()
        self.request_count += 1
        self.recent_requests.append(current_time)
        
        if success:
            self.successful += 1
        else:
            self.failed += 1
        
        # Cleanup old requests
        cutoff = current_time - self.window_size
        self.recent_requests = [t for t in self.recent_requests if t > cutoff]
    
    def get_rpm(self) -> float:
        """Get requests per minute over the last window"""
        current_time = time.time()
        cutoff = current_time - self.window_size
        self.recent_requests = [t for t in self.recent_requests if t > cutoff]
        window_elapsed = min(current_time - self.start_time, self.window_size)
        if window_elapsed > 0:
            return (len(self.recent_requests) / window_elapsed) * 60
        return 0
    
    def get_total_requests(self) -> int:
        """Get total request count"""
        return self.request_count
    
    def get_success_rate(self) -> float:
        """Get success rate"""
        if self.request_count == 0:
            return 0.0
        return self.successful / self.request_count
    
    def get_elapsed_time(self) -> float:
        """Get elapsed time since start"""
        return time.time() - self.start_time
    
    def reset(self):
        """Reset all metrics"""
        self.start_time = time.time()
        self.request_count = 0
        self.recent_requests = []
        self.successful = 0
        self.failed = 0
    
    def get_summary(self) -> dict:
        """Get summary of all metrics"""
        elapsed = self.get_elapsed_time()
        return {
            "total_requests": self.request_count,
            "successful": self.successful,
            "failed": self.failed,
            "success_rate": self.get_success_rate(),
            "rpm": self.get_rpm(),
            "elapsed_seconds": elapsed,
            "avg_requests_per_second": self.request_count / elapsed if elapsed > 0 else 0
        }
