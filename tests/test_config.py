import tempfile
from pathlib import Path
import pytest
import yaml

from agi_logger.config import (
    ConfigError,
    expand_path,
    get_config_section,
    iter_nested_keys,
    load_raw_config,
    resolve_logger_paths,
    resolve_tcp_paths,
    save_raw_config,
    update_nested_value,
)


@pytest.fixture
def sample_config_path(tmp_path):
    cfg_data = {
        "agi_logger": {
            "verbosity": "INFO",
            "logger": {
                "bag_path": "bags",
                "topics": ["/tf", "/clock"],
                "duration": 5,
                "qos_settings": "qos.yaml",
            },
            "tcp_file_communication": {
                "mode": "server",
                "server": {
                    "port": 6000,
                    "file_path": "bags/sample_bag",
                },
                "client": {
                    "host": "127.0.0.1",
                    "port": 6000,
                    "destination_path": "received_bags",
                },
            },
        }
    }
    cfg_file = tmp_path / "configs.yaml"
    with cfg_file.open("w") as f:
        yaml.dump(cfg_data, f)
    return cfg_file


def test_load_and_save_raw_config(sample_config_path, tmp_path):
    data = load_raw_config(sample_config_path)
    assert data["agi_logger"]["logger"]["duration"] == 5

    data["agi_logger"]["logger"]["duration"] = 15
    out_file = tmp_path / "saved.yaml"
    save_raw_config(data, out_file)

    reloaded = load_raw_config(out_file)
    assert reloaded["agi_logger"]["logger"]["duration"] == 15


def test_update_nested_value(sample_config_path):
    data = load_raw_config(sample_config_path)
    update_nested_value(data, "agi_logger.logger.name", "my_flight_bag")
    assert data["agi_logger"]["logger"]["name"] == "my_flight_bag"

    update_nested_value(data, "agi_logger.logger.topics", ["/odom", "/scan"])
    assert data["agi_logger"]["logger"]["topics"] == ["/odom", "/scan"]


def test_resolve_paths(sample_config_path):
    data = load_raw_config(sample_config_path)
    resolved_logger = resolve_logger_paths(data, sample_config_path)
    assert Path(resolved_logger["bag_path"]).is_absolute()
    assert Path(resolved_logger["qos_settings"]).is_absolute()

    resolved_tcp = resolve_tcp_paths(data, sample_config_path)
    assert Path(resolved_tcp["server"]["file_path"]).is_absolute()
    assert Path(resolved_tcp["client"]["destination_path"]).is_absolute()
