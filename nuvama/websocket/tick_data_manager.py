"""
High-Performance Tick Data Manager
Efficiently saves tick data to files for later simulation and analysis
"""

import os
import time
import threading
import queue
import orjson
from datetime import datetime, date
from typing import Dict, Any
import gzip
import asyncio
import redis
import re
from concurrent.futures import ThreadPoolExecutor


class TickDataManager:
    """
    High-performance tick data manager that saves market data to files
    with minimal impact on real-time performance
    """
    
    def __init__(self, base_directory="tick_data", enable_compression=True, buffer_size=1000, flush_interval=5.0):
        """
        Initialize tick data manager
        
        Args:
            base_directory: Base directory for tick data files
            enable_compression: Whether to compress files (saves space but uses more CPU)
            buffer_size: Number of ticks to buffer before writing to disk
            flush_interval: Time interval (seconds) to force flush buffers
        """
        self.base_directory = base_directory
        self.enable_compression = enable_compression
        self.buffer_size = buffer_size
        self.flush_interval = flush_interval
        
        # Create base directory structure
        self._setup_directories()
       
        
        # Threading components for async processing
        self.tick_queue = queue.Queue(maxsize=100000)  # Large queue to handle bursts
        self.worker_thread = None
        self.is_running = False
        
        # File handles and buffers
        self.file_handles = {}
        self.tick_buffers = {}
        self.last_flush_time = time.time()
        
        # Thread pool for I/O operations
        self.executor = ThreadPoolExecutor(max_workers=2)
        
        # Statistics
        self.stats = {
            "total_ticks_received": 0,
            "total_ticks_written": 0,
            "files_created": 0,
            "errors": 0,
            "last_tick_time": None
        }
        
        # Start background processing
        self.start()
    
    def _setup_directories(self):
        """Create directory structure for tick data"""
        today = date.today().strftime("%Y%m%d")
        
        self.directories = {
            "quotes": os.path.join(self.base_directory, today, "quotes"),
            "depth": os.path.join(self.base_directory, today, "depth"),
            "stats": os.path.join(self.base_directory, today, "stats")
        }
        
        for directory in self.directories.values():
            os.makedirs(directory, exist_ok=True)
    
    def start(self):
        """Start the background tick processing thread"""
        if not self.is_running:
            self.is_running = True
            self.worker_thread = threading.Thread(target=self._process_ticks, daemon=True)
            self.worker_thread.start()
            print(f"[SUCCESS] TickDataManager started - Base directory: {self.base_directory}")
    
    def stop(self):
        """Stop the background processing and flush all buffers"""
        if self.is_running:
            self.is_running = False
            
            # Signal stop by putting None in queue
            self.tick_queue.put(None)
            
            # Wait for worker thread to finish
            if self.worker_thread and self.worker_thread.is_alive():
                self.worker_thread.join(timeout=10)
            
            # Final flush
            self._flush_all_buffers()
            self._close_all_files()
            
            # Shutdown executor
            self.executor.shutdown(wait=True)
            
            print(f"[SUCCESS] TickDataManager stopped - Total ticks processed: {self.stats['total_ticks_written']}")
    
    def save_quotes_tick(self, symbol: str, tick_data: Dict[Any, Any]):
        """
        Save a quotes tick to the processing queue
        
        Args:
            symbol: Symbol name (e.g., 'NIFTY', 'SENSEX')
            tick_data: The tick data dictionary
        """
        try:
            timestamp = time.time()
            tick_entry = {
                "timestamp": timestamp,
                "datetime": datetime.fromtimestamp(timestamp).isoformat(),
                "type": "quotes",
                "symbol": symbol,
                "data": tick_data
            }
            
            # Non-blocking put with timeout to prevent blocking the callback
            self.tick_queue.put(tick_entry, timeout=0.01)
            self.stats["total_ticks_received"] += 1
            self.stats["last_tick_time"] = timestamp
            
        except queue.Full:
            self.stats["errors"] += 1
            print(f"[WARNING] Tick queue full - dropping quotes tick for {symbol}")
        except Exception as e:
            self.stats["errors"] += 1
            print(f"[ERROR] Error queuing quotes tick for {symbol}: {e}")
    
    def save_depth_tick(self, redis_key: str, tick_data: Dict[Any, Any]):
        """
        Save a depth tick to the processing queue
        
        Args:
            redis_key: Redis key (e.g., 'depth:NIFTY_25000_CE-28NOV24')
            tick_data: The tick data dictionary
        """
        try:
            timestamp = time.time()
            tick_entry = {
                "timestamp": timestamp,
                "datetime": datetime.fromtimestamp(timestamp).isoformat(),
                "type": "depth",
                "redis_key": redis_key,
                "data": tick_data
            }
            
            # Non-blocking put with timeout
            self.tick_queue.put(tick_entry, timeout=0.01)
            self.stats["total_ticks_received"] += 1
            self.stats["last_tick_time"] = timestamp
            
        except queue.Full:
            self.stats["errors"] += 1
            print(f"[WARNING] Tick queue full - dropping depth tick for {redis_key}")
        except Exception as e:
            self.stats["errors"] += 1
            print(f"[ERROR] Error queuing depth tick for {redis_key}: {e}")
    
    def _process_ticks(self):
        """Background thread function to process ticks from queue"""
        print("[INFO] Tick processing thread started")
        
        while self.is_running:
            try:
                # Get tick from queue with timeout
                tick_entry = self.tick_queue.get(timeout=1.0)
                
                # Check for stop signal
                if tick_entry is None:
                    break
                
                # Process the tick
                self._process_single_tick(tick_entry)
                
                # Check if we need to flush buffers
                current_time = time.time()
                if current_time - self.last_flush_time >= self.flush_interval:
                    self._flush_all_buffers()
                    self.last_flush_time = current_time
                
            except queue.Empty:
                # Timeout occurred, check for periodic flush
                current_time = time.time()
                if current_time - self.last_flush_time >= self.flush_interval:
                    self._flush_all_buffers()
                    self.last_flush_time = current_time
                continue
            except Exception as e:
                self.stats["errors"] += 1
                print(f"[ERROR] Error processing tick: {e}")
        
        print("[INFO] Tick processing thread stopped")
    
    def _process_single_tick(self, tick_entry: Dict[Any, Any]):
        """Process a single tick entry"""
        try:
            tick_type = tick_entry["type"]
            
            if tick_type == "quotes":
                self._process_quotes_tick(tick_entry)
            elif tick_type == "depth":
                self._process_depth_tick(tick_entry)
            else:
                print(f"[WARNING] Unknown tick type: {tick_type}")
                
        except Exception as e:
            self.stats["errors"] += 1
            print(f"[ERROR] Error processing single tick: {e}")
    
    def _process_quotes_tick(self, tick_entry: Dict[Any, Any]):
        """Process a quotes tick and add to buffer"""
        symbol = tick_entry["symbol"]
        file_key = f"quotes_{symbol}"
        
        # Add to buffer
        if file_key not in self.tick_buffers:
            self.tick_buffers[file_key] = []
        
        self.tick_buffers[file_key].append(tick_entry)
        
        # Check if buffer needs flushing
        if len(self.tick_buffers[file_key]) >= self.buffer_size:
            self._flush_buffer(file_key)
    
    def _process_depth_tick(self, tick_entry: Dict[Any, Any]):
        """Process a depth tick and add to buffer"""
        redis_key = tick_entry["redis_key"]
        
        # Extract symbol info from redis key for file naming
        # Format: depth:NIFTY_25000_CE-28NOV24
        if redis_key.startswith("depth:"):
            file_suffix = redis_key[6:]  # Remove 'depth:' prefix
        else:
            file_suffix = redis_key
        
        # Clean filename (replace invalid characters)
        file_suffix = file_suffix.replace(":", "_").replace("/", "_").replace("\\", "_")
        file_key = f"depth_{file_suffix}"
        
        # Add to buffer
        if file_key not in self.tick_buffers:
            self.tick_buffers[file_key] = []
        
        self.tick_buffers[file_key].append(tick_entry)
        
        # Check if buffer needs flushing
        if len(self.tick_buffers[file_key]) >= self.buffer_size:
            self._flush_buffer(file_key)
    
    def _flush_buffer(self, file_key: str):
        """Flush a specific buffer to file"""
        if file_key not in self.tick_buffers or not self.tick_buffers[file_key]:
            return
        
        try:
            # Get the appropriate directory
            if file_key.startswith("quotes_"):
                directory = self.directories["quotes"]
            elif file_key.startswith("depth_"):
                directory = self.directories["depth"]
            else:
                directory = self.directories["stats"]
            
            # Create filename with timestamp
            today = date.today().strftime("%Y%m%d")
            hour = datetime.now().strftime("%H")
            
            if self.enable_compression:
                filename = f"{file_key}_{today}_{hour}.jsonl.gz"
            else:
                filename = f"{file_key}_{today}_{hour}.jsonl"
            
            filepath = os.path.join(directory, filename)
            
            # Write buffer to file
            ticks_to_write = self.tick_buffers[file_key].copy()
            self.tick_buffers[file_key].clear()
            
            # Submit to thread pool for I/O
            self.executor.submit(self._write_ticks_to_file, filepath, ticks_to_write)
            
        except Exception as e:
            self.stats["errors"] += 1
            print(f"[ERROR] Error flushing buffer for {file_key}: {e}")
    
    def _write_ticks_to_file(self, filepath: str, ticks: list):
        """Write ticks to file (runs in thread pool)"""
        try:
            mode = 'ab' if self.enable_compression else 'a'
            
            if self.enable_compression:
                with gzip.open(filepath, mode) as f:
                    for tick in ticks:
                        line = orjson.dumps(tick) + b'\n'
                        f.write(line)
            else:
                with open(filepath, mode, encoding='utf-8') as f:
                    for tick in ticks:
                        line = orjson.dumps(tick).decode() + '\n'
                        f.write(line)
            
            self.stats["total_ticks_written"] += len(ticks)
            
            # Track file creation
            if filepath not in self.file_handles:
                self.stats["files_created"] += 1
                print(f"[INFO] Created tick data file: {os.path.basename(filepath)}")
            
        except Exception as e:
            self.stats["errors"] += 1
            print(f"[ERROR] Error writing to file {filepath}: {e}")
    
    def _flush_all_buffers(self):
        """Flush all buffers to disk"""
        for file_key in list(self.tick_buffers.keys()):
            if self.tick_buffers[file_key]:  # Only flush non-empty buffers
                self._flush_buffer(file_key)
    
    def _close_all_files(self):
        """Close all open file handles"""
        for handle in self.file_handles.values():
            try:
                handle.close()
            except Exception as e:
                print(f"[ERROR] Error closing file handle: {e}")
        self.file_handles.clear()
    
    def get_stats(self) -> Dict[str, Any]:
        """Get current statistics"""
        current_stats = self.stats.copy()
        current_stats["queue_size"] = self.tick_queue.qsize()
        current_stats["buffer_count"] = len(self.tick_buffers)
        current_stats["total_buffered_ticks"] = sum(len(buffer) for buffer in self.tick_buffers.values())
        return current_stats
    
    def print_stats(self):
        """Print current statistics"""
        stats = self.get_stats()
        print("\n[INFO] TICK DATA MANAGER STATISTICS")
        print("-" * 40)
        print(f"[INFO] Total ticks received: {stats['total_ticks_received']:,}")
        print(f"[INFO] Total ticks written: {stats['total_ticks_written']:,}")
        print(f"[INFO] Files created: {stats['files_created']}")
        print(f"[INFO] Queue size: {stats['queue_size']}")
        print(f"[INFO] Active buffers: {stats['buffer_count']}")
        print(f"[INFO] Buffered ticks: {stats['total_buffered_ticks']}")
        print(f"[ERROR] Errors: {stats['errors']}")
        if stats['last_tick_time']:
            last_tick = datetime.fromtimestamp(stats['last_tick_time']).strftime("%H:%M:%S")
            print(f"[INFO] Last tick: {last_tick}")
        print("-" * 40)


class TickDataReader:
    """
    Utility class to read and simulate tick data from saved files
    """
    
    def __init__(self, base_directory="tick_data"):
        self.base_directory = base_directory
    
    def list_available_dates(self) -> list:
        """List all available dates with tick data"""
        dates = []
        if os.path.exists(self.base_directory):
            for item in os.listdir(self.base_directory):
                path = os.path.join(self.base_directory, item)
                if os.path.isdir(path) and item.isdigit() and len(item) == 8:
                    dates.append(item)
        return sorted(dates)
    
    def list_available_files(self, date_str: str) -> Dict[str, list]:
        """List all available files for a specific date"""
        date_path = os.path.join(self.base_directory, date_str)
        files = {"quotes": [], "depth": []}
        
        if os.path.exists(date_path):
            for data_type in ["quotes", "depth"]:
                type_path = os.path.join(date_path, data_type)
                if os.path.exists(type_path):
                    files[data_type] = [f for f in os.listdir(type_path) if f.endswith(('.jsonl', '.jsonl.gz'))]
        
        return files
    
    def read_tick_file(self, filepath: str, start_time=None, end_time=None):
        """
        Read tick data from a file with optional time filtering
        
        Args:
            filepath: Path to the tick data file
            start_time: Start timestamp (optional)
            end_time: End timestamp (optional)
            
        Yields:
            Individual tick data entries
        """
        try:
            if filepath.endswith('.gz'):
                file_obj = gzip.open(filepath, 'rt', encoding='utf-8')
            else:
                file_obj = open(filepath, 'r', encoding='utf-8')
            
            with file_obj as f:
                for line in f:
                    if line.strip():
                        tick = orjson.loads(line.strip())
                        
                        # Apply time filtering if specified
                        if start_time and tick['timestamp'] < start_time:
                            continue
                        if end_time and tick['timestamp'] > end_time:
                            break
                        
                        yield tick
                        
        except Exception as e:
            print(f"[ERROR] Error reading tick file {filepath}: {e}")
    
    def simulate_tick_replay(self, date_str: str, symbol=None, speed_multiplier=1.0,start_hour=10,end_hour=15):
        """
        Simulate tick data replay for a specific date
        
        Args:
            date_str: Date in YYYYMMDD format
            symbol: Specific symbol to replay (optional)
            speed_multiplier: Speed multiplier for replay (1.0 = real-time)
        """
        files = self.list_available_files(date_str)
        date_path = os.path.join(self.base_directory, date_str)
        
        print(f"[INFO] Starting tick replay for {date_str}")
        if symbol:
            print(f"[INFO] Filtering for symbol: {symbol}")
        
        # Collect all ticks with timestamps
        all_ticks = []
        
        # Read quotes files
        for filename in files["quotes"]:
            match = re.search(r"_(\d+)\.jsonl\.gz$", filename)
            if match:
                num = int(match.group(1))
                if start_hour <= num < end_hour:
                    if symbol and symbol not in filename:
                        continue
                    filepath = os.path.join(date_path, "quotes", filename)
                    for tick in self.read_tick_file(filepath):
                        # print("Tick : ",tick)
                        all_ticks.append(tick)
        
        # Read depth files
        for filename in files["depth"]:
            match = re.search(r"_(\d+)\.jsonl\.gz$", filename)
            if match:
                num = int(match.group(1))
                if start_hour <= num < end_hour:
                    if symbol and symbol not in filename:
                        continue
                    filepath = os.path.join(date_path, "depth", filename)
                    for tick in self.read_tick_file(filepath):
                        all_ticks.append(tick)
                    print("READING DEPTH FILES COMPLETED")
        
        # Sort by timestamp
        all_ticks.sort(key=lambda x: x['timestamp'])
    
        if not all_ticks:
            print(f"[ERROR] No tick data found for {date_str}")
            return
        
        print(f"[INFO] Found {len(all_ticks)} ticks to replay")
        
        # Replay ticks
        start_timestamp = all_ticks[0]['timestamp']
        start_replay_time = time.time()
        
        for i, tick in enumerate(all_ticks):
            # Calculate delay
            elapsed_market_time = tick['timestamp'] - start_timestamp
            elapsed_replay_time = time.time() - start_replay_time
            target_replay_time = elapsed_market_time / speed_multiplier
            
            if target_replay_time > elapsed_replay_time:
                time.sleep(target_replay_time - elapsed_replay_time)
            
            # Yield the tick for processing
            yield tick
            
            if (i + 1) % 1000 == 0:
                print(f"[INFO] Replayed {i + 1:,}/{len(all_ticks):,} ticks")
        
        print(f"[SUCCESS] Tick replay completed for {date_str}")


if __name__ == "__main__":
    # Example usage
    manager = TickDataManager()
    
    # Simulate some tick data
    import random
    
    for i in range(100):
        quotes_data = {
            "ltp": 25000 + random.randint(-100, 100),
            "volume": random.randint(1000, 5000)
        }
        manager.save_quotes_tick("NIFTY", quotes_data)
        
        depth_data = {
            "bid": 25000 + random.randint(-50, 0),
            "ask": 25000 + random.randint(0, 50)
        }
        manager.save_depth_tick("depth:NIFTY_25000_CE-28NOV24", depth_data)
        
        time.sleep(0.01)
    
    # Print stats and stop
    time.sleep(2)
    manager.print_stats()
    manager.stop()
