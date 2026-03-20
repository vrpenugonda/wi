"""
Base classifier with common functionality
"""

import os
import time
import asyncio
from abc import ABC, abstractmethod
from typing import List, Dict, Any
from pydantic_ai.models.openai import OpenAIChatModel, OpenAIChatModelSettings

from ..config import get_settings
from ..utils.auth import get_azure_ad_token
from ..utils.rate_limiter import get_rate_limiter


class RequestMetrics:
    """Track request metrics for rate limiting and monitoring"""
    
    def __init__(self):
        self.start_time = time.time()
        self.request_count = 0
        self.recent_requests: list[float] = []
        self.window_size = 60
        self.successful = 0
        self.failed = 0
        self.total_input_tokens = 0
        self.total_output_tokens = 0
    
    def record_request(self, success: bool = True):
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
    
    def add_request(self, input_tokens: int = 0, output_tokens: int = 0, success: bool = True):
        """Record a request with token counts"""
        self.record_request(success)
        self.total_input_tokens += input_tokens
        self.total_output_tokens += output_tokens
    
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
        return self.request_count
    
    def get_success_rate(self) -> float:
        if self.request_count == 0:
            return 0.0
        return self.successful / self.request_count
    
    def get_summary_dict(self) -> dict:
        """Get metrics as a dictionary for logging."""
        elapsed = time.time() - self.start_time
        return {
            "total_requests": self.request_count,
            "successful": self.successful,
            "failed": self.failed,
            "success_rate": f"{self.get_success_rate()*100:.1f}%",
            "elapsed_seconds": f"{elapsed:.1f}",
            "avg_rpm": f"{self.request_count / (elapsed / 60):.1f}" if elapsed > 0 else "N/A",
            "total_tokens": self.total_input_tokens + self.total_output_tokens,
        }


class BaseClassifier(ABC):
    """Base class for all classifiers"""
    
    def __init__(
        self,
        batch_size: int = 10,
        workers: int = 1,
        debug: bool = False,
        max_rpm: int = 550,
    ):
        self.settings = get_settings()
        self.batch_size = batch_size
        self.workers = workers
        self.debug = debug
        self.max_rpm = max_rpm
        self.metrics = RequestMetrics()
        self._model: OpenAIChatModel | None = None
        self._rate_limiter = get_rate_limiter(max_rpm)
    
    def get_model(self) -> OpenAIChatModel:
        """Get or create the Azure OpenAI model"""
        if self._model is None:
            # Refresh token
            azure_ad_token = get_azure_ad_token()
            
            # Set environment variables
            os.environ["AZURE_OPENAI_API_KEY"] = azure_ad_token
            os.environ["AZURE_OPENAI_ENDPOINT"] = self.settings.azure_endpoint
            os.environ["AZURE_OPENAI_API_VERSION"] = self.settings.api_version
            os.environ["OPENAI_API_VERSION"] = self.settings.api_version
            os.environ["AZURE_OPENAI_DEPLOYMENT_NAME"] = self.settings.azure_deployment
            
            self._model = OpenAIChatModel(
                self.settings.azure_deployment,
                provider='azure',
            )
        
        return self._model
    
    def get_model_settings(self) -> OpenAIChatModelSettings:
        """Get model settings with extra headers"""
        return OpenAIChatModelSettings(
            extra_headers={
                "X-Upstream-Env": self.settings.x_upstream_env,
                "projectId": self.settings.project_id,
                "X-Model-Usage-Type": self.settings.x_upstream_env,
                "modelUsageType": self.settings.x_upstream_env,
            }
        )
    
    def refresh_model(self):
        """Force refresh the model (e.g., on token expiry)"""
        self._model = None
        return self.get_model()
    
    @abstractmethod
    async def classify_batch(
        self,
        batch: List[Dict[str, Any]],
        **kwargs
    ) -> List[Dict[str, Any]]:
        """Classify a batch of incidents - to be implemented by subclasses"""
        pass
    
    async def classify_all(
        self,
        records: List[Dict[str, Any]],
        progress_callback=None,
        **kwargs
    ) -> List[Dict[str, Any]]:
        """
        Classify all records with multi-worker support
        
        Args:
            records: List of incident records to classify
            progress_callback: Optional callback for progress updates
            **kwargs: Additional arguments passed to classify_batch
        
        Returns:
            List of classified records
        """
        total = len(records)
        
        if self.workers == 1:
            # Single worker mode
            return await self._classify_single_worker(records, progress_callback, **kwargs)
        else:
            # Multi-worker mode
            return await self._classify_multi_worker(records, progress_callback, **kwargs)
    
    async def _classify_single_worker(
        self,
        records: List[Dict[str, Any]],
        progress_callback=None,
        **kwargs
    ) -> List[Dict[str, Any]]:
        """Single worker classification with parallel batch processing."""
        from tqdm import tqdm
        
        total = len(records)
        all_batches = [
            (i, records[i:i + self.batch_size])
            for i in range(0, len(records), self.batch_size)
        ]
        
        results_by_index: dict[int, list] = {}
        results_lock = asyncio.Lock()
        processed_count = 0
        stats_lock = asyncio.Lock()
        
        # Use workers as the semaphore limit for single-worker mode
        semaphore = asyncio.Semaphore(self.workers)
        
        async def process_batch(batch_idx: int, batch: List[Dict]) -> None:
            nonlocal processed_count
            
            async with semaphore:
                results = await self.classify_batch(batch, **kwargs)
                
                # Record completion for stats
                await self._rate_limiter.record_completion(1)
                
                async with results_lock:
                    results_by_index[batch_idx] = results
                
                async with stats_lock:
                    processed_count += len(batch)
        
        async def display_progress():
            pbar = tqdm(total=total, desc="TOTAL", unit="records")
            try:
                while True:
                    await asyncio.sleep(0.5)
                    async with stats_lock:
                        pbar.n = processed_count
                    rpm = self._rate_limiter.get_current_rpm()
                    pbar.set_postfix_str(f"{rpm:.0f}/{self.max_rpm} RPM")
                    pbar.refresh()
                    if processed_count >= total:
                        break
            finally:
                pbar.close()
        
        batch_tasks = [
            asyncio.create_task(process_batch(batch_idx, batch))
            for batch_idx, batch in all_batches
        ]
        progress_task = asyncio.create_task(display_progress())
        
        try:
            await asyncio.gather(*batch_tasks)
            await asyncio.sleep(0.5)
            progress_task.cancel()
        except:
            progress_task.cancel()
            raise
        
        # Reconstruct in order
        all_results = []
        for batch_idx, _ in all_batches:
            if batch_idx in results_by_index:
                all_results.extend(results_by_index[batch_idx])
        
        return all_results
    
    async def _classify_multi_worker(
        self,
        records: List[Dict[str, Any]],
        progress_callback=None,
        **kwargs
    ) -> List[Dict[str, Any]]:
        """Multi-worker classification with adaptive concurrency tuning."""
        from tqdm import tqdm
        
        total = len(records)
        
        # Create ALL batches upfront
        all_batches = [
            (i, records[i:i + self.batch_size])
            for i in range(0, len(records), self.batch_size)
        ]
        
        # Shared state
        results_by_index: dict[int, list] = {}
        results_lock = asyncio.Lock()
        processed_count = 0
        success_count = 0
        error_count = 0
        stats_lock = asyncio.Lock()
        
        # Adaptive concurrency for large datasets
        # Start at workers setting, tune based on throughput efficiency
        enable_tuning = total >= 100_000
        tuning_duration = 300  # 5 minutes
        tuning_start = time.time()
        
        # Start with configured workers, cap reasonably
        initial_concurrent = min(self.workers, 500, len(all_batches))
        current_limit = [initial_concurrent]
        active_count = [0]
        active_lock = asyncio.Lock()
        
        # Batch size tuning (2-20 range)
        current_batch_size = [self.batch_size]
        batch_sizes_to_try = [2, 5, 10, 15, 20]
        
        # Track metrics for tuning
        latencies: list[float] = []
        latency_lock = asyncio.Lock()
        last_error: str = ""
        
        # Throughput tracking for tuning: (timestamp, records/s, concurrency, batch_size)
        throughput_samples: list[tuple[float, float, int, int]] = []
        best_config = {'concurrency': initial_concurrent, 'batch_size': self.batch_size, 'throughput': 0.0}
        
        async def process_batch(batch_idx: int, batch: List[Dict]) -> None:
            """Process a single batch with adaptive concurrency control."""
            nonlocal processed_count, success_count, error_count, last_error
            
            # Wait if we're at the current limit
            while True:
                async with active_lock:
                    if active_count[0] < current_limit[0]:
                        active_count[0] += 1
                        break
                await asyncio.sleep(0.02)
            
            try:
                start_time = time.time()
                try:
                    batch_results = await self.classify_batch(batch, **kwargs)
                    
                    elapsed = time.time() - start_time
                    async with latency_lock:
                        latencies.append(elapsed)
                    
                    await self._rate_limiter.record_completion(1)
                    
                    async with results_lock:
                        results_by_index[batch_idx] = batch_results
                    
                    async with stats_lock:
                        processed_count += len(batch)
                        success_count += len(batch_results)
                        
                except Exception as e:
                    async with stats_lock:
                        processed_count += len(batch)
                        error_count += len(batch)
                        last_error = str(e)[:100]
            finally:
                async with active_lock:
                    active_count[0] -= 1
        
        # Adaptive tuning - optimizes for throughput by testing concurrency and batch size
        async def tune_parameters():
            """Tune concurrency and batch size based on throughput."""
            nonlocal best_config
            last_processed = 0
            last_time = time.time()
            tune_count = 0
            tuning_phase = 'batch_size'  # Start by finding optimal batch size
            batch_test_idx = 0
            
            while enable_tuning and (time.time() - tuning_start) < tuning_duration:
                await asyncio.sleep(20)  # Sample every 20 seconds
                
                current_time = time.time()
                async with stats_lock:
                    current_processed = processed_count
                    curr_errors = error_count
                
                # Calculate throughput
                elapsed = current_time - last_time
                records_delta = current_processed - last_processed
                throughput = records_delta / elapsed if elapsed > 0 else 0
                
                async with latency_lock:
                    avg_latency = sum(latencies[-50:]) / len(latencies[-50:]) if latencies else 0
                
                async with active_lock:
                    active = active_count[0]
                
                # Record sample with current config
                throughput_samples.append((current_time, throughput, current_limit[0], current_batch_size[0]))
                
                # Update best config if this is better
                if throughput > best_config['throughput']:
                    best_config = {
                        'concurrency': current_limit[0],
                        'batch_size': current_batch_size[0],
                        'throughput': throughput
                    }
                
                # Don't tune if errors
                if curr_errors > 0:
                    last_processed = current_processed
                    last_time = current_time
                    continue
                
                tune_count += 1
                
                # Phase 1: Test different batch sizes (first 2 minutes)
                if tuning_phase == 'batch_size' and (time.time() - tuning_start) < 120:
                    if tune_count >= 2:
                        # Try next batch size
                        batch_test_idx = (batch_test_idx + 1) % len(batch_sizes_to_try)
                        new_batch = batch_sizes_to_try[batch_test_idx]
                        if new_batch != current_batch_size[0]:
                            old = current_batch_size[0]
                            current_batch_size[0] = new_batch
                            self.batch_size = new_batch  # Update for new batches
                            print(f"\n[TUNE] Testing batch_size: {old} -> {new_batch} "
                                  f"(throughput: {throughput:.1f} rec/s, latency: {avg_latency:.1f}s)")
                
                # Phase 2: Optimize concurrency with best batch size (after 2 minutes)
                elif (time.time() - tuning_start) >= 120:
                    if tuning_phase == 'batch_size':
                        # Switch to concurrency tuning, use best batch size found
                        tuning_phase = 'concurrency'
                        if best_config['batch_size'] != current_batch_size[0]:
                            current_batch_size[0] = best_config['batch_size']
                            self.batch_size = best_config['batch_size']
                            print(f"\n[TUNE] Locked batch_size: {best_config['batch_size']} "
                                  f"(best throughput: {best_config['throughput']:.1f} rec/s)")
                    
                    # Tune concurrency
                    if tune_count >= 2 and len(throughput_samples) >= 2:
                        prev_throughput = throughput_samples[-2][1]
                        throughput_gain = (throughput - prev_throughput) / prev_throughput if prev_throughput > 0 else 0
                        
                        # If throughput dropped or latency too high, reduce concurrency
                        if throughput_gain < -0.05 or (throughput_gain < 0.05 and avg_latency > 60):
                            new_limit = max(200, int(best_config['concurrency'] * 0.9))
                            if new_limit < current_limit[0]:
                                old = current_limit[0]
                                current_limit[0] = new_limit
                                print(f"\n[TUNE] Reduced concurrency: {old} -> {new_limit} "
                                      f"(throughput: {throughput:.1f} rec/s, latency: {avg_latency:.1f}s)")
                        elif throughput_gain > 0.05:
                            # Throughput improving, try more
                            new_limit = min(current_limit[0] + 50, 600)
                            if new_limit > current_limit[0]:
                                old = current_limit[0]
                                current_limit[0] = new_limit
                                print(f"\n[TUNE] Increased concurrency: {old} -> {new_limit} "
                                      f"(throughput: {throughput:.1f} rec/s, latency: {avg_latency:.1f}s)")
                
                last_processed = current_processed
                last_time = current_time
        
        # Progress display
        async def display_progress():
            pbar = tqdm(total=total, desc="TOTAL", unit="records")
            last_processed = 0
            last_time = time.time()
            
            try:
                while True:
                    await asyncio.sleep(0.5)
                    
                    current_time = time.time()
                    async with stats_lock:
                        current_processed = processed_count
                        current_success = success_count
                        current_error = error_count
                        current_last_error = last_error
                    
                    async with latency_lock:
                        avg_latency = sum(latencies[-100:]) / len(latencies[-100:]) if latencies else 0
                    
                    async with active_lock:
                        active = active_count[0]
                    
                    pbar.n = current_processed
                    rpm = self._rate_limiter.get_current_rpm()
                    
                    if current_error > 0 and current_success == 0 and current_last_error:
                        pbar.set_postfix_str(f"Err: {current_error} | {current_last_error[:60]}")
                    else:
                        pbar.set_postfix_str(
                            f"OK: {current_success} | Err: {current_error} | {rpm:.0f}/{self.max_rpm} RPM | "
                            f"Lat: {avg_latency:.1f}s | Active: {active}/{current_limit[0]} | Batch: {current_batch_size[0]}"
                        )
                    pbar.refresh()
                    
                    last_processed = current_processed
                    last_time = current_time
                    
                    if current_processed >= total:
                        break
            finally:
                pbar.close()
        
        # Start all batch tasks
        batch_tasks = [
            asyncio.create_task(process_batch(batch_idx, batch))
            for batch_idx, batch in all_batches
        ]
        progress_task = asyncio.create_task(display_progress())
        tuning_task = asyncio.create_task(tune_parameters()) if enable_tuning else None
        
        try:
            await asyncio.gather(*batch_tasks)
            await asyncio.sleep(0.5)
            progress_task.cancel()
            if tuning_task:
                tuning_task.cancel()
        except:
            progress_task.cancel()
            if tuning_task:
                tuning_task.cancel()
            raise
        
        # Reconstruct results in order
        all_results = []
        for batch_idx, _ in all_batches:
            if batch_idx in results_by_index:
                all_results.extend(results_by_index[batch_idx])
        
        return all_results
