import os

from agi_logger.path_input import complete_path, is_path_key


def test_complete_path_dirs_files_and_home(tmp_path, monkeypatch):
    (tmp_path / "flights").mkdir()
    (tmp_path / "flight_notes.txt").write_text("x")
    (tmp_path / "bags").mkdir()
    got = complete_path(str(tmp_path / "fl"))
    assert got == [str(tmp_path / "flight_notes.txt"), str(tmp_path / "flights") + "/"]
    assert complete_path(str(tmp_path / "zz")) == []
    monkeypatch.setenv("HOME", str(tmp_path))
    assert complete_path("~/ba") == ["~/bags/"]
    monkeypatch.chdir(tmp_path)
    assert complete_path("bag") == ["bags/"]


def test_is_path_key():
    for k in ("agi_logger.logger.bag_path", "flight_session.session_path", "client.destination_path",
              "server.file_path", "agi_logger.logger.qos_settings"):
        assert is_path_key(k), k
    for k in ("logger.duration", "logger.mcap", "client.port", "flight_session.namespace"):
        assert not is_path_key(k), k
