from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Tuple

RESET = "\033[0m"
BOLD = "\033[1m"
RED = "\033[31m"
GREEN = "\033[32m"
YELLOW = "\033[33m"
CYAN = "\033[36m"
LIGHT_GRAY = "\033[90m"

# Resource alert thresholds
WARN_STORAGE_GB = 10.0
CRITICAL_STORAGE_GB = 2.0
WARN_RAM_GB = 1.0
CRITICAL_RAM_GB = 0.3


def get_system_resources(bag_path: str) -> Tuple[float, float, float, float, float, float]:
    """Returns (free_ram_gb, total_ram_gb, ram_pct, free_storage_gb, total_storage_gb, storage_pct)."""
    # RAM calculation
    free_ram_gb = 0.0
    total_ram_gb = 0.0
    ram_pct = 0.0
    try:
        with open("/proc/meminfo", "r", encoding="utf-8") as f:
            mem_dict = {}
            for line in f:
                parts = line.split(":")
                if len(parts) == 2:
                    key = parts[0].strip()
                    val_kb = int(parts[1].split()[0].strip())
                    mem_dict[key] = val_kb
            total_ram_gb = mem_dict.get("MemTotal", 0) / (1024 * 1024)
            avail_ram_gb = mem_dict.get("MemAvailable", mem_dict.get("MemFree", 0)) / (1024 * 1024)
            free_ram_gb = avail_ram_gb
            ram_pct = (free_ram_gb / total_ram_gb) * 100 if total_ram_gb > 0 else 0.0
    except Exception:
        pass

    # Storage calculation
    try:
        path_obj = Path(bag_path).expanduser().resolve()
        check_dir = path_obj if path_obj.exists() else path_obj.parent
        check_dir.mkdir(parents=True, exist_ok=True)
        disk = shutil.disk_usage(check_dir)
        free_storage_gb = disk.free / (1024**3)
        total_storage_gb = disk.total / (1024**3)
        storage_pct = (disk.free / disk.total) * 100 if disk.total > 0 else 0.0
    except Exception:
        free_storage_gb = 0.0
        total_storage_gb = 0.0
        storage_pct = 0.0

    return free_ram_gb, total_ram_gb, ram_pct, free_storage_gb, total_storage_gb, storage_pct


def check_system_resources(
    bag_path: str, print_output: bool = True
) -> Tuple[str, Tuple[float, float, float, float, float, float]]:
    """Checks RAM and storage resources, prints formatted status if requested, and returns (status, metrics).

    Status values: 'critical', 'warning', 'ok'.
    """
    metrics = get_system_resources(bag_path)
    free_ram, total_ram, ram_pct, free_store, total_store, store_pct = metrics

    if free_store < CRITICAL_STORAGE_GB or free_ram < CRITICAL_RAM_GB:
        status = "critical"
        if print_output:
            print(
                f"\n{BOLD}{RED}[CRITICAL RESOURCE ALERT] Low Storage ({free_store:.2f} GB < {CRITICAL_STORAGE_GB} GB) "
                f"or RAM ({free_ram:.2f} GB < {CRITICAL_RAM_GB} GB)! Stopping recording safely to protect data...{RESET}"
            )
        return status, metrics

    if free_store < WARN_STORAGE_GB or free_ram < WARN_RAM_GB:
        status = "warning"
        if print_output:
            print(
                f"{BOLD}{RED}[LOW RESOURCE WARNING] RAM Free: {free_ram:.2f}/{total_ram:.2f} GB ({ram_pct:.1f}%) | "
                f"Storage Free: {free_store:.2f}/{total_store:.2f} GB ({store_pct:.1f}%){RESET}"
            )
        return status, metrics

    status = "ok"
    if print_output:
        print(
            f"{CYAN}[SYSTEM MONITOR]{RESET} RAM Free: {GREEN}{free_ram:.2f}{RESET}/{total_ram:.2f} GB ({ram_pct:.1f}%) | "
            f"Storage Free: {GREEN}{free_store:.2f}{RESET}/{total_store:.2f} GB ({store_pct:.1f}%)"
        )
    return status, metrics
