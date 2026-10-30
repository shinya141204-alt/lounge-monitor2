#!/usr/bin/env python3
"""DB Query Benchmark: Legacy Full-Scan vs Window Function Optimization.

Measures and compares:
- Execution latency (Mean, Median, p95, Min, Max)
- Memory usage (Peak allocated bytes via tracemalloc)
- Data transfer volume (Fetched row count & payload byte size)
- Result correctness (Strict equality check between methods)
"""

import os
import sys
import time
import datetime
import tempfile
import sqlite3
import tracemalloc
import statistics

# Ensure project root is in path
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

# 53 standard stores in Aiseki Navi master
SAMPLE_STORES = [
    "オリエンタルラウンジ 渋谷本店", "オリエンタルラウンジ 新宿", "オリエンタルラウンジ 上野",
    "オリエンタルラウンジ 町田", "オリエンタルラウンジ 横浜", "オリエンタルラウンジ 大宮",
    "オリエンタルラウンジ 宇都宮", "オリエンタルラウンジ 仙台", "オリエンタルラウンジ 盛岡",
    "オリエンタルラウンジ 札幌", "オリエンタルラウンジ 静岡", "オリエンタルラウンジ 浜松",
    "オリエンタルラウンジ 名古屋栄", "オリエンタルラウンジ 名古屋名駅", "オリエンタルラウンジ 京都",
    "オリエンタルラウンジ 梅田", "オリエンタルラウンジ 心斎橋", "オリエンタルラウンジ 難波",
    "オリエンタルラウンジ 神戸", "オリエンタルラウンジ 広島", "オリエンタルラウンジ 松山",
    "オリエンタルラウンジ 福岡天神", "オリエンタルラウンジ 小倉", "オリエンタルラウンジ 熊本",
    "オリエンタルラウンジ 鹿児島", "オリエンタルラウンジ 沖縄那覇",
    "ag 渋谷", "ag 新宿", "ag 上野", "ag 六本木", "ag 恵比寿", "ag 銀座", "ag 横浜",
    "ag 名古屋", "ag 京都", "ag 心斎橋", "ag 難波", "ag 神戸三宮", "ag 広島", "ag 福岡天神",
    "THE SINGLE 恵比寿", "THE SINGLE 新宿東口", "THE SINGLE 新宿靖国通り", "THE SINGLE 池袋東口",
    "THE SINGLE 上野", "THE SINGLE 渋谷", "THE SINGLE 横浜", "THE SINGLE 大宮",
    "THE SINGLE 名古屋栄", "THE SINGLE 梅田", "THE SINGLE 神戸三宮", "THE SINGLE 福岡天神",
    "FIX LOUNGE 銀座"
]


def setup_benchmark_db(db_path: str, target_rows: int = 50000):
    """Sets up a SQLite database with 50,000 rows across 53 stores."""
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    cur.execute("PRAGMA journal_mode=WAL;")
    cur.execute("PRAGMA synchronous=NORMAL;")
    cur.execute("""
        CREATE TABLE monitor_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            store_name TEXT NOT NULL,
            men INTEGER NOT NULL,
            women INTEGER NOT NULL,
            source TEXT
        );
    """)
    cur.execute("CREATE INDEX idx_store_ts ON monitor_logs(store_name, timestamp);")
    cur.execute("CREATE INDEX idx_ts_store_wm ON monitor_logs(timestamp, store_name, women, men);")

    # Generate timestamp series
    # Target window is around 10-25 mins ago, but table has historical logs
    base_time = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=9))).replace(tzinfo=None)
    
    # We create ~950 timestamps per store (53 * 944 = ~50,032 rows)
    timestamps_per_store = (target_rows // len(SAMPLE_STORES)) + 1
    
    rows_to_insert = []
    for step in range(timestamps_per_store):
        # 1 minute intervals going back ~16 hours
        ts = (base_time - datetime.timedelta(minutes=step)).strftime("%Y-%m-%d %H:%M:%S")
        for idx, store in enumerate(SAMPLE_STORES):
            men = (idx * 3 + step) % 40
            women = (idx * 2 + step) % 35
            rows_to_insert.append((ts, store, men, women, "crawler"))
            
    cur.executemany("INSERT INTO monitor_logs (timestamp, store_name, men, women, source) VALUES (?, ?, ?, ?, ?)", rows_to_insert)
    conn.commit()
    actual_count = cur.execute("SELECT count(*) FROM monitor_logs").fetchone()[0]
    conn.close()
    return actual_count, base_time


def benchmark_legacy_query(conn, start_ts, end_ts):
    """Legacy method: fetch all records in time range + python deduplication."""
    cur = conn.cursor()
    cur.execute("""
        SELECT store_name, men, women, timestamp
        FROM monitor_logs
        WHERE timestamp >= ? AND timestamp <= ?
        ORDER BY timestamp DESC
    """, (start_ts, end_ts))
    raw_rows = cur.fetchall()
    result = {}
    for r in raw_rows:
        sname = r[0]
        if sname not in result:
            result[sname] = {'men': r[1], 'women': r[2], 'ts': r[3]}
    return result, len(raw_rows), sys.getsizeof(raw_rows)


def benchmark_window_query(conn, start_ts, end_ts):
    """New M4 method: SQL Window function ROW_NUMBER() OVER."""
    cur = conn.cursor()
    cur.execute("""
        WITH ranked AS (
            SELECT store_name, men, women, timestamp,
                   ROW_NUMBER() OVER (PARTITION BY store_name ORDER BY timestamp DESC) as rn
            FROM monitor_logs
            WHERE timestamp >= ? AND timestamp <= ?
        )
        SELECT store_name, men, women, timestamp
        FROM ranked
        WHERE rn = 1
    """, (start_ts, end_ts))
    raw_rows = cur.fetchall()
    result = {r[0]: {'men': r[1], 'women': r[2], 'ts': r[3]} for r in raw_rows}
    return result, len(raw_rows), sys.getsizeof(raw_rows)


def run_benchmark():
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tf:
        temp_db_path = tf.name

    try:
        print(f"Setting up benchmark database at {temp_db_path}...")
        total_rows, base_time = setup_benchmark_db(temp_db_path, 50000)
        print(f"Database populated with {total_rows:,} rows across {len(SAMPLE_STORES)} stores.")

        # Test query window: past 15-min to 12-hour window or wide range
        # In real production, query covers minutes_ago_min to max (e.g. 10 to 25 min or wider search)
        # To simulate high-volume scan where range matches thousands of rows:
        start_ts = (base_time - datetime.timedelta(hours=10)).strftime("%Y-%m-%d %H:%M:%S")
        end_ts = (base_time - datetime.timedelta(minutes=10)).strftime("%Y-%m-%d %H:%M:%S")

        conn = sqlite3.connect(temp_db_path)
        conn.row_factory = sqlite3.Row

        # 1. Warm-up and correctness verification
        res_legacy, rows_legacy, size_legacy = benchmark_legacy_query(conn, start_ts, end_ts)
        res_window, rows_window, size_window = benchmark_window_query(conn, start_ts, end_ts)

        assert len(res_legacy) == len(res_window), f"Size mismatch: {len(res_legacy)} vs {len(res_window)}"
        for k in res_legacy:
            assert res_legacy[k] == res_window[k], f"Value mismatch for {k}: {res_legacy[k]} vs {res_window[k]}"
        print("✅ Correctness check: Legacy and Window queries return EXACT identical store snapshots!")

        # 2. Benchmark iterations
        ITERATIONS = 50
        print(f"\nRunning {ITERATIONS} benchmark iterations for each method...")

        # Benchmark Legacy
        legacy_times = []
        tracemalloc.start()
        snapshot_start = tracemalloc.take_snapshot()
        for _ in range(ITERATIONS):
            t0 = time.perf_counter()
            _, _, _ = benchmark_legacy_query(conn, start_ts, end_ts)
            t1 = time.perf_counter()
            legacy_times.append((t1 - t0) * 1000.0)
        legacy_peak_mem = tracemalloc.get_traced_memory()[1]
        tracemalloc.stop()

        # Benchmark Window Function
        window_times = []
        tracemalloc.start()
        snapshot_start = tracemalloc.take_snapshot()
        for _ in range(ITERATIONS):
            t0 = time.perf_counter()
            _, _, _ = benchmark_window_query(conn, start_ts, end_ts)
            t1 = time.perf_counter()
            window_times.append((t1 - t0) * 1000.0)
        window_peak_mem = tracemalloc.get_traced_memory()[1]
        tracemalloc.stop()

        # Compute stats
        mean_leg = statistics.mean(legacy_times)
        median_leg = statistics.median(legacy_times)
        min_leg = min(legacy_times)
        max_leg = max(legacy_times)
        p95_leg = sorted(legacy_times)[int(len(legacy_times) * 0.95)]

        mean_win = statistics.mean(window_times)
        median_win = statistics.median(window_times)
        min_win = min(window_times)
        max_win = max(window_times)
        p95_win = sorted(window_times)[int(len(window_times) * 0.95)]

        speedup_mean = (mean_leg - mean_win) / mean_leg * 100.0
        speedup_factor = mean_leg / mean_win if mean_win > 0 else 0
        mem_reduction = (legacy_peak_mem - window_peak_mem) / legacy_peak_mem * 100.0 if legacy_peak_mem > 0 else 0
        row_reduction = (rows_legacy - rows_window) / rows_legacy * 100.0

        print("\n" + "=" * 60)
        print("📊 DB BENCHMARK RESULTS (50,000 Rows)")
        print("=" * 60)
        print(f"| Metric               | Legacy (Full-Scan) | Window Function    | Improvement |")
        print(f"|----------------------|--------------------|--------------------|-------------|")
        print(f"| Fetched Rows         | {rows_legacy:>18,d} | {rows_window:>18,d} | {row_reduction:>9.1f}% reduction |")
        print(f"| Transfer Bytes       | {size_legacy:>15,d} B | {size_window:>15,d} B | {(size_legacy - size_window)/size_legacy*100:>9.1f}% reduction |")
        print(f"| Peak Memory          | {legacy_peak_mem / 1024:>15.1f} KB | {window_peak_mem / 1024:>15.1f} KB | {mem_reduction:>9.1f}% reduction |")
        print(f"| Mean Latency         | {mean_leg:>16.2f} ms | {mean_win:>16.2f} ms | {speedup_mean:>9.1f}% faster ({speedup_factor:.1f}x) |")
        print(f"| Median (p50) Latency | {median_leg:>16.2f} ms | {median_win:>16.2f} ms | {(median_leg-median_win)/median_leg*100:>9.1f}% faster |")
        print(f"| 95th Percentile (p95)| {p95_leg:>16.2f} ms | {p95_win:>16.2f} ms | {(p95_leg-p95_win)/p95_leg*100:>9.1f}% faster |")
        print(f"| Min Latency          | {min_leg:>16.2f} ms | {min_win:>16.2f} ms | {(min_leg-min_win)/min_leg*100:>9.1f}% faster |")
        print(f"| Max Latency          | {max_leg:>16.2f} ms | {max_win:>16.2f} ms | {(max_leg-max_win)/max_leg*100:>9.1f}% faster |")
        print("=" * 60)

        # Output JSON result for report generator
        import json
        result_data = {
            "total_rows": total_rows,
            "legacy": {
                "fetched_rows": rows_legacy,
                "transfer_bytes": size_legacy,
                "peak_memory_kb": legacy_peak_mem / 1024,
                "mean_ms": mean_leg,
                "median_ms": median_leg,
                "p95_ms": p95_leg,
                "min_ms": min_leg,
                "max_ms": max_leg
            },
            "window": {
                "fetched_rows": rows_window,
                "transfer_bytes": size_window,
                "peak_memory_kb": window_peak_mem / 1024,
                "mean_ms": mean_win,
                "median_ms": median_win,
                "p95_ms": p95_win,
                "min_ms": min_win,
                "max_ms": max_win
            },
            "speedup_mean_percent": speedup_mean,
            "speedup_factor": speedup_factor,
            "memory_reduction_percent": mem_reduction,
            "row_reduction_percent": row_reduction
        }
        with open(os.path.join(ROOT, "benchmark_db_result.json"), "w") as f:
            json.dump(result_data, f, indent=2)

        conn.close()

    finally:
        if os.path.exists(temp_db_path):
            try:
                os.remove(temp_db_path)
            except Exception:
                pass


if __name__ == "__main__":
    run_benchmark()
