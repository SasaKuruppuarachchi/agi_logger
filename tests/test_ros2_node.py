import shutil
from pathlib import Path
import pytest

from agi_logger.ros2_node import (
    CRITICAL_RAM_GB,
    CRITICAL_STORAGE_GB,
    WARN_RAM_GB,
    WARN_STORAGE_GB,
    get_system_resources,
)


def test_get_system_resources(tmp_path):
    free_ram, total_ram, ram_pct, free_store, total_store, store_pct = get_system_resources(str(tmp_path))

    assert total_ram >= 0.0
    assert free_ram >= 0.0
    assert 0.0 <= ram_pct <= 100.0 or total_ram == 0.0
    assert total_store > 0.0
    assert free_store > 0.0
    assert 0.0 <= store_pct <= 100.0


def test_resource_threshold_constants():
    assert CRITICAL_STORAGE_GB < WARN_STORAGE_GB
    assert CRITICAL_RAM_GB < WARN_RAM_GB
    assert CRITICAL_STORAGE_GB == 2.0
    assert WARN_STORAGE_GB == 10.0
