# Tick Data Logging and Simulation System

## Overview

This system captures every market tick in real-time and saves it to files for later analysis and simulation. It's designed for minimal performance impact on your live trading system while providing comprehensive historical data for strategy development and backtesting.

## Architecture

### 1. TickDataManager (`tick_data_manager.py`)

- **High-performance tick logging**: Non-blocking, queue-based system
- **Compressed storage**: Optional gzip compression to save disk space
- **Buffered I/O**: Batches writes to minimize disk operations
- **Thread-safe**: Background processing doesn't interfere with live trading

### 2. Enhanced CentralSocketData (`central_socket_data.py`)

- **Integrated tick logging**: Seamlessly captures quotes and depth data
- **Zero-impact design**: Tick logging errors don't affect main functionality
- **Configurable**: Can enable/disable tick logging as needed

### 3. Analysis Tools (`tick_data_utils.py`)

- **Data analysis**: Comprehensive statistics and insights
- **Market simulation**: Replay historical ticks at any speed
- **CLI interface**: Easy command-line tools for data exploration

## Features

### ✅ **Real-time Tick Capture**

- Captures every `ReducedQuotesFeedCallback` tick with timestamp
- Captures every `DepthStreamerCallback` tick with timestamp
- Preserves all original data structure and content
- Adds precise timestamps using `time.time()`

### ✅ **High Performance**

- **Non-blocking**: Uses queues and background threads
- **Buffered writes**: Batches data to minimize I/O operations
- **Compressed storage**: Optional gzip compression (70-90% space savings)
- **Error isolation**: Tick logging failures don't affect live trading

### ✅ **Organized Storage**

```
tick_data/
├── 20241205/          # Date-based folders (YYYYMMDD)
│   ├── quotes/        # Reduced quotes data
│   │   ├── quotes_NIFTY_20241205_09.jsonl.gz
│   │   └── quotes_SENSEX_20241205_09.jsonl.gz
│   ├── depth/         # Market depth data
│   │   ├── depth_NIFTY_25000_CE-28NOV24_20241205_09.jsonl.gz
│   │   └── depth_NIFTY_25000_PE-28NOV24_20241205_09.jsonl.gz
│   └── stats/         # Statistics and metadata
```

### ✅ **Analysis & Simulation Tools**

- **Data analysis**: Traffic patterns, active hours, symbol statistics
- **Market replay**: Simulate historical market conditions at any speed
- **Comparison tools**: Compare different trading days
- **CLI utilities**: Easy command-line interface

## Installation & Setup

### 1. No Additional Dependencies

The system uses only standard Python libraries and your existing dependencies:

- `orjson` (already in use)
- `redis` (already in use)
- Standard library: `threading`, `queue`, `gzip`, `datetime`, `os`

### 2. Enable Tick Logging

Your `central_socket_data.py` is already modified. To use:

```python
# Enable tick logging (default)
socket = CentralSocketData(
    enable_tick_logging=True,
    tick_data_base_dir="tick_data"
)

# Disable tick logging if needed
socket = CentralSocketData(enable_tick_logging=False)
```

## Usage Examples

### 1. **Running with Tick Logging**

```bash
cd c:\Users\shree\OneDrive\Desktop\TrueData\nuvama\websocket
python central_socket_data.py
```

Expected output:

```
✅ Tick data logging enabled - Directory: tick_data
Starting Central Socket Data with Tick Logging...
📁 Created tick data file: quotes_NIFTY_20241205_09.jsonl.gz
📁 Created tick data file: depth_NIFTY_25000_CE-28NOV24_20241205_09.jsonl.gz

📊 TICK DATA MANAGER STATISTICS
----------------------------------------
📥 Total ticks received: 15,234
💾 Total ticks written: 15,234
📁 Files created: 12
🔄 Queue size: 0
📋 Active buffers: 4
⏳ Buffered ticks: 0
❌ Errors: 0
🕐 Last tick: 14:23:45
----------------------------------------
```

### 2. **Analyzing Saved Data**

```bash
# List available dates
python tick_data_utils.py list

# Analyze a specific date
python tick_data_utils.py analyze --date 20241205

# Find active trading hours
python tick_data_utils.py hours --date 20241205 --symbol NIFTY

# Compare two dates
python tick_data_utils.py compare --date 20241205 --date2 20241206
```

### 3. **Market Simulation**

```bash
# Simulate market session at normal speed
python tick_data_utils.py simulate --date 20241205

# Simulate NIFTY only at 10x speed
python tick_data_utils.py simulate --date 20241205 --symbol NIFTY --speed 10.0

# Simulate specific hours
python tick_data_utils.py simulate --date 20241205 --start-hour 9 --end-hour 15
```

### 4. **Programmatic Usage**

```python
from tick_data_manager import TickDataReader

# Read historical data
reader = TickDataReader("tick_data")
for tick in reader.read_tick_file("tick_data/20241205/quotes/quotes_NIFTY_20241205_09.jsonl.gz"):
    print(f"Time: {tick['datetime']}, Symbol: {tick['symbol']}")
    # Process tick data for backtesting
```

## Data Format

### Quotes Tick Format

```json
{
  "timestamp": 1733123456.789,
  "datetime": "2024-12-05T14:23:45.789000",
  "type": "quotes",
  "symbol": "NIFTY",
  "data": {
    "response": {
      "data": {
        "sym": "-29",
        "symbol": "NIFTY",
        "ltp": 25000.5,
        "volume": 1234567
        // ... all original tick data
      }
    }
  }
}
```

### Depth Tick Format

```json
{
  "timestamp": 1733123456.789,
  "datetime": "2024-12-05T14:23:45.789000",
  "type": "depth",
  "redis_key": "depth:NIFTY_25000_CE-28NOV24",
  "data": {
    "response": {
      "data": {
        "symbol": "12345",
        "symbolname": "NIFTY",
        "strikeprice": 25000,
        "optiontype": "CE",
        "expiry": "28NOV24",
        "bid": 150.25,
        "ask": 150.75
        // ... all original depth data
      }
    }
  }
}
```

## Performance Characteristics

### **Memory Usage**

- Queue buffer: ~10MB (10,000 ticks × ~1KB each)
- Write buffers: ~500KB (500 ticks × ~1KB each)
- Total overhead: ~15-20MB

### **Disk Usage**

- Uncompressed: ~1KB per tick
- Compressed: ~200-300 bytes per tick (70-90% savings)
- Daily storage (100k ticks): ~20-100MB depending on compression

### **CPU Impact**

- Main thread: <1% overhead (just queue operations)
- Background thread: Handles all I/O operations
- Compression: Uses ~10-20% CPU when enabled

### **I/O Performance**

- Buffered writes every 500 ticks or 3 seconds
- Minimal disk operations during trading hours
- Background processing prevents blocking

## Configuration Options

### TickDataManager Parameters

```python
TickDataManager(
    base_directory="tick_data",    # Base directory for data files
    enable_compression=True,       # Enable gzip compression
    buffer_size=500,              # Ticks to buffer before writing
    flush_interval=3.0            # Seconds between forced flushes
)
```

### Optimizations for Different Scenarios

#### **High-Volume Trading** (>50k ticks/day)

```python
TickDataManager(
    enable_compression=True,       # Save disk space
    buffer_size=1000,             # Larger buffers
    flush_interval=5.0            # Less frequent writes
)
```

#### **Low-Latency Requirements**

```python
TickDataManager(
    enable_compression=False,      # Faster writes
    buffer_size=100,              # Smaller buffers
    flush_interval=1.0            # More frequent writes
)
```

#### **Storage-Constrained**

```python
TickDataManager(
    enable_compression=True,       # Maximum compression
    buffer_size=2000,             # Maximize compression efficiency
    flush_interval=10.0           # Minimize I/O
)
```

## Troubleshooting

### Common Issues

#### **"Tick queue full" warnings**

- Increase queue size in `TickDataManager.__init__()`:

```python
self.tick_queue = queue.Queue(maxsize=20000)  # Increase from 10000
```

#### **High disk usage**

- Enable compression: `enable_compression=True`
- Reduce retention: Delete old data files periodically
- Increase buffer size to improve compression efficiency

#### **Performance impact**

- Disable compression for faster processing
- Increase flush interval to reduce I/O frequency
- Monitor with `socket.print_tick_data_stats()`

### Monitoring

```python
# Print statistics every minute (already implemented)
socket.print_tick_data_stats()

# Get programmatic access to stats
stats = socket.get_tick_data_stats()
print(f"Queue size: {stats['queue_size']}")
print(f"Total ticks: {stats['total_ticks_received']}")
```

## Integration with Your Strategy

### Using Historical Data in Strategies

```python
from tick_data_manager import TickDataReader

class HistoricalDataStrategy:
    def __init__(self):
        self.reader = TickDataReader("tick_data")

    def backtest_strategy(self, date_str):
        for tick in self.reader.simulate_tick_replay(date_str):
            # Your strategy logic here
            if tick['type'] == 'quotes':
                symbol = tick['symbol']
                price = tick['data']['response']['data'].get('ltp')
                # Process price update

            elif tick['type'] == 'depth':
                redis_key = tick['redis_key']
                bid = tick['data']['response']['data'].get('bid')
                ask = tick['data']['response']['data'].get('ask')
                # Process order book update
```

### Real-time Strategy with Historical Context

```python
class LiveStrategyWithHistory:
    def __init__(self):
        self.historical_reader = TickDataReader("tick_data")

    def initialize_with_yesterday_data(self):
        yesterday = "20241204"  # Calculate dynamically
        for tick in self.historical_reader.read_tick_file(f"tick_data/{yesterday}/quotes/quotes_NIFTY_{yesterday}_15.jsonl.gz"):
            # Initialize strategy state with yesterday's closing data
            pass
```

## Maintenance

### Daily Cleanup (Optional)

```bash
# Delete tick data older than 30 days
find tick_data -type d -name "????????" -mtime +30 -exec rm -rf {} \;

# Compress uncompressed files
find tick_data -name "*.jsonl" -exec gzip {} \;
```

### Backup Strategy

```bash
# Daily backup to external storage
rsync -av tick_data/ /external_storage/tick_data_backup/

# Cloud backup (example)
aws s3 sync tick_data/ s3://your-bucket/tick-data/
```

This system provides a robust foundation for tick data capture and analysis while maintaining the performance of your live trading system. The historical data can be invaluable for strategy development, backtesting, and market analysis.
