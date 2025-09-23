#!/usr/bin/env python3

import os
import subprocess
import sys
import time
import re
import matplotlib.pyplot as plt
import numpy as np

def main():
    # Root directory containing benchmarks
    benchmark_root = os.path.expanduser("~/egglog-pointer-analysis-benchmark/benchmark-input/postgresql-9.5.2")
    
    # Path to egglog binaries
    egglog_binary = "egglog/target/release/egglog"
    egglog_baseline_binary = "egglog-baseline/target/release/egglog"
    
    # Path to pointer-analysis.egg file
    pointer_analysis_file = "pointer-analysis.egg"
    
    # Check if the benchmark directory exists
    if not os.path.exists(benchmark_root):
        print(f"Error: Benchmark directory not found at {benchmark_root}")
        sys.exit(1)
    
    # Check if egglog binaries exist
    if not os.path.exists(egglog_binary):
        print(f"Error: egglog binary not found at {egglog_binary}")
        sys.exit(1)
    
    if not os.path.exists(egglog_baseline_binary):
        print(f"Error: egglog-baseline binary not found at {egglog_baseline_binary}")
        sys.exit(1)
    
    # Check if pointer-analysis.egg file exists
    if not os.path.exists(pointer_analysis_file):
        print(f"Error: pointer-analysis.egg file not found at {pointer_analysis_file}")
        sys.exit(1)
    
    # Get all benchmark directories
    try:
        benchmark_dirs = [d for d in os.listdir(benchmark_root) 
                         if os.path.isdir(os.path.join(benchmark_root, d))]
        benchmark_dirs.sort()  # Sort for consistent ordering
    except OSError as e:
        print(f"Error reading benchmark directory: {e}")
        sys.exit(1)
    
    if not benchmark_dirs:
        print(f"No benchmark directories found in {benchmark_root}")
        sys.exit(1)
    
    print(f"Found {len(benchmark_dirs)} benchmark directories:")
    for bench_dir in benchmark_dirs:
        print(f"  - {bench_dir}")
    print()
    
    # Results storage
    results = {'benchmarks': [], 'egglog_times': [], 'baseline_times': []}
    
    # Run both egglog binaries for each benchmark
    for benchmark_name in benchmark_dirs:
        print(f"Running benchmark: {benchmark_name}")
        
        egglog_time = None
        baseline_time = None
        
        # Run egglog binary
        egglog_time = run_benchmark(egglog_binary, pointer_analysis_file, benchmark_root, benchmark_name, "egglog")
        
        # Run egglog-baseline binary (with stderr redirected)
        baseline_time = run_benchmark(egglog_baseline_binary, pointer_analysis_file, benchmark_root, benchmark_name, "egglog-baseline", redirect_stderr=True)
        
        # Store results if both succeeded
        if egglog_time is not None and baseline_time is not None:
            results['benchmarks'].append(benchmark_name)
            results['egglog_times'].append(egglog_time)
            results['baseline_times'].append(baseline_time)
            
            print(f"Results for {benchmark_name}:")
            print(f"  egglog: {egglog_time:.3f}s")
            print(f"  egglog-baseline: {baseline_time:.3f}s")
            speedup = baseline_time / egglog_time if egglog_time > 0 else float('inf')
            print(f"  speedup: {speedup:.2f}x")
        
        print("-" * 80)
        print()
    
    # Print summary and create visualization
    if results['benchmarks']:
        print_summary(results)
        create_bar_chart(results)
    else:
        print("No successful benchmark runs to compare.")


def run_benchmark(binary_path, pointer_analysis_file, benchmark_root, benchmark_name, binary_name, redirect_stderr=False):
    """Run a single benchmark and return the execution time in seconds."""
    print(f"  Running {binary_name}...")
    
    # Construct the command
    cmd = [
        binary_path,
        pointer_analysis_file,
        "-F",
        f"{benchmark_root}/{benchmark_name}"
    ]
    
    try:
        start_time = time.time()
        
        # Redirect stderr for baseline if requested
        # stderr_target = subprocess.DEVNULL if redirect_stderr else subprocess.PIPE
        stderr_target = subprocess.DEVNULL
        
        result = subprocess.run(cmd, text=True, 
                              stderr=stderr_target, timeout=3600)
        
        end_time = time.time()
        execution_time = end_time - start_time
        
        if result.returncode == 0:
            print(f"    {binary_name} completed in {execution_time:.3f}s")
            return execution_time
        else:
            print(f"    {binary_name} failed with exit code {result.returncode}")
            if not redirect_stderr and result.stderr:
                print(f"    Error: {result.stderr[:200]}...")  # Show first 200 chars
            return None
            
    except subprocess.TimeoutExpired:
        print(f"    {binary_name} timed out (>1 hour)")
        return None
    except Exception as e:
        print(f"    {binary_name} error: {e}")
        return None


def print_summary(results):
    """Print a summary of benchmark results."""
    print("\n" + "=" * 80)
    print("BENCHMARK SUMMARY")
    print("=" * 80)
    
    total_egglog = sum(results['egglog_times'])
    total_baseline = sum(results['baseline_times'])
    overall_speedup = total_baseline / total_egglog if total_egglog > 0 else float('inf')
    
    print(f"Total benchmarks completed: {len(results['benchmarks'])}")
    print(f"Total egglog time: {total_egglog:.3f}s")
    print(f"Total egglog-baseline time: {total_baseline:.3f}s")
    print(f"Overall speedup: {overall_speedup:.2f}x")
    print()
    
    print("Individual results:")
    for i, benchmark in enumerate(results['benchmarks']):
        egglog_time = results['egglog_times'][i]
        baseline_time = results['baseline_times'][i]
        speedup = baseline_time / egglog_time if egglog_time > 0 else float('inf')
        print(f"  {benchmark:<30} | egglog: {egglog_time:8.3f}s | baseline: {baseline_time:8.3f}s | speedup: {speedup:6.2f}x")


def create_bar_chart(results):
    """Create a bar chart comparing the performance of both binaries."""
    if not results['benchmarks']:
        return
    
    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(15, 6))
    
    # Chart 1: Execution times
    x = np.arange(len(results['benchmarks']))
    width = 0.35
    
    bars1 = ax1.bar(x - width/2, results['egglog_times'], width, label='egglog', alpha=0.8)
    bars2 = ax1.bar(x + width/2, results['baseline_times'], width, label='egglog-baseline', alpha=0.8)
    
    ax1.set_xlabel('Benchmarks')
    ax1.set_ylabel('Execution Time (seconds)')
    ax1.set_title('Execution Time Comparison')
    ax1.set_xticks(x)
    ax1.set_xticklabels(results['benchmarks'], rotation=45, ha='right')
    ax1.legend()
    ax1.grid(True, alpha=0.3)
    
    # Chart 2: Speedup ratios
    speedups = [results['baseline_times'][i] / results['egglog_times'][i] 
                if results['egglog_times'][i] > 0 else 0 
                for i in range(len(results['benchmarks']))]
    
    bars3 = ax2.bar(x, speedups, alpha=0.8, color='green')
    ax2.axhline(y=1, color='red', linestyle='--', alpha=0.7, label='No speedup')
    ax2.set_xlabel('Benchmarks')
    ax2.set_ylabel('Speedup (baseline/egglog)')
    ax2.set_title('Speedup Comparison (higher is better)')
    ax2.set_xticks(x)
    ax2.set_xticklabels(results['benchmarks'], rotation=45, ha='right')
    ax2.legend()
    ax2.grid(True, alpha=0.3)
    
    plt.tight_layout()
    plt.savefig('benchmark_comparison.png', dpi=300, bbox_inches='tight')
    plt.show()
    print(f"\nChart saved as 'benchmark_comparison.png'")


if __name__ == "__main__":
    main()