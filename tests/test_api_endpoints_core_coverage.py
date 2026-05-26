from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, mock_open, patch

import cherrypy
import pytest

from repeater.web.api_endpoints import APIEndpoints


def _make_api(config=None):
    api = APIEndpoints.__new__(APIEndpoints)
    api.config = config or {}
    api.daemon_instance = None
    api._config_path = "/tmp/test-config.yaml"
    api.config_manager = MagicMock()
    return api


def _attach_storage(api, storage):
    api.daemon_instance = SimpleNamespace(
        repeater_handler=SimpleNamespace(storage=storage)
    )


@pytest.fixture
def cherrypy_ctx(monkeypatch):
    request = SimpleNamespace(method="GET", params={}, json={})
    response = SimpleNamespace(headers={}, status=200)
    monkeypatch.setattr(cherrypy, "request", request, raising=False)
    monkeypatch.setattr(cherrypy, "response", response, raising=False)
    return request, response


def test_set_cors_headers_enabled(cherrypy_ctx):
    _, response = cherrypy_ctx
    api = _make_api({"web": {"cors_enabled": True}})

    api._set_cors_headers()

    assert response.headers["Access-Control-Allow-Origin"] == "*"
    assert "POST" in response.headers["Access-Control-Allow-Methods"]
    assert "Authorization" in response.headers["Access-Control-Allow-Headers"]


def test_set_cors_headers_disabled(cherrypy_ctx):
    _, response = cherrypy_ctx
    api = _make_api({"web": {"cors_enabled": False}})

    api._set_cors_headers()

    assert response.headers == {}


def test_default_returns_empty_for_options(cherrypy_ctx):
    request, _ = cherrypy_ctx
    request.method = "OPTIONS"
    api = _make_api()

    assert api.default() == ""


def test_default_raises_404_for_non_options(cherrypy_ctx):
    request, _ = cherrypy_ctx
    request.method = "GET"
    api = _make_api()

    with pytest.raises(cherrypy.HTTPError) as exc:
        api.default()
    assert exc.value.status == 404


def test_get_storage_success_and_failure_paths():
    api = _make_api()
    with pytest.raises(Exception, match="Daemon not available"):
        api._get_storage()

    api.daemon_instance = SimpleNamespace()
    with pytest.raises(Exception, match="Repeater handler not initialized"):
        api._get_storage()

    api.daemon_instance.repeater_handler = SimpleNamespace(storage=None)
    with pytest.raises(Exception, match="Storage not initialized"):
        api._get_storage()

    storage = object()
    api.daemon_instance.repeater_handler.storage = storage
    assert api._get_storage() is storage


def test_get_params_casts_int_float_and_none(cherrypy_ctx):
    request, _ = cherrypy_ctx
    request.params = {"count": "7", "ratio": "2.5", "name": "node", "maybe": None}
    api = _make_api()

    parsed = api._get_params({"count": 0, "ratio": 0.0, "name": "", "maybe": 1})

    assert parsed == {"count": 7, "ratio": 2.5, "name": "node", "maybe": None}


def test_require_post_enforces_method(cherrypy_ctx):
    request, response = cherrypy_ctx
    api = _make_api()

    request.method = "GET"
    with pytest.raises(cherrypy.HTTPError) as exc:
        api._require_post()
    assert exc.value.status == 405
    assert response.status == 405
    assert response.headers["Allow"] == "POST"

    request.method = "POST"
    api._require_post()


def test_fmt_hash_respects_path_hash_mode():
    pubkey = bytes.fromhex("19272233AA")

    api = _make_api({"mesh": {"path_hash_mode": 0}})
    assert api._fmt_hash(pubkey) == "0x19"

    api.config["mesh"]["path_hash_mode"] = 1
    assert api._fmt_hash(pubkey) == "0x1927"

    api.config["mesh"]["path_hash_mode"] = 2
    assert api._fmt_hash(pubkey) == "0x192722"


def test_process_counter_and_gauge_data():
    api = _make_api()

    counter = api._process_counter_data([None, 10, 13, 9], [1000, 2000, 3000, 4000])
    gauge = api._process_gauge_data([1, None, 3], [1000, 2000, 3000])

    assert counter == [[1000, 0], [2000, 0], [3000, 3], [4000, 0]]
    assert gauge == [[1000, 1], [2000, 0], [3000, 3]]


def test_success_and_error_helpers():
    api = _make_api()

    ok = api._success([1, 2], source="unit")
    err = api._error("boom")

    assert ok == {"success": True, "data": [1, 2], "source": "unit"}
    assert err == {"success": False, "error": "boom"}


def test_get_time_range_uses_current_time(monkeypatch):
    api = _make_api()
    monkeypatch.setattr("repeater.web.api_endpoints.time.time", lambda: 10_000)

    start, end = api._get_time_range(2)

    assert end == 10_000
    assert start == 2_800


def test_setup_status_from_config_variants():
    api = _make_api()

    needs_setup, reasons = api._setup_status_from_config(
        {
            "repeater": {
                "node_name": "mesh-repeater-01",
                "security": {"admin_password": "admin123"},
            },
            "radio_type": "none",
        }
    )
    assert needs_setup is True
    assert reasons == {
        "default_name": True,
        "default_password": True,
        "radio_not_configured": True,
    }

    needs_setup2, reasons2 = api._setup_status_from_config(
        {
            "repeater": {
                "node_name": "mesh-node-77",
                "security": {"admin_password": "verysecret"},
            },
            "radio_type": "sx1262",
        }
    )
    assert needs_setup2 is False
    assert reasons2["radio_not_configured"] is False


def test_site_info_success_and_error_fallback():
    api = _make_api({"web": {"site_name": "Field Node"}})
    assert api.site_info() == {"success": True, "site_name": "Field Node"}

    class _BadConfig(dict):
        def get(self, *args, **kwargs):
            raise RuntimeError("bad")

    api_bad = _make_api(_BadConfig())
    assert api_bad.site_info() == {"success": True, "site_name": ""}


def test_hardware_options_loads_installed_file(tmp_path):
    config = {"repeater": {"storage_dir": str(tmp_path)}}
    api = _make_api(config)
    api._config_path = str(tmp_path / "config.yaml")

    hardware_file = tmp_path / "radio-settings.json"
    hardware_file.write_text(
        '{"hardware":{"pymc_usb":{"name":"USB","description":"desc","radio_type":"pymc_usb"}}}',
        encoding="utf-8",
    )

    with patch("repeater.web.api_endpoints.resolve_storage_dir", return_value=Path(tmp_path)):
        result = api.hardware_options()

    assert len(result["hardware"]) == 1
    assert result["hardware"][0]["key"] == "pymc_usb"
    assert result["hardware"][0]["name"] == "USB"


def test_radio_presets_returns_error_when_file_missing(tmp_path):
    config = {"repeater": {"storage_dir": str(tmp_path)}}
    api = _make_api(config)
    api._config_path = str(tmp_path / "config.yaml")

    with patch("repeater.web.api_endpoints.resolve_storage_dir", return_value=Path(tmp_path)):
        with patch("os.path.exists", return_value=False):
            result = api.radio_presets()

    assert result["error"] == "Radio presets file not found"


def test_radio_presets_loads_entries_from_installed_file(tmp_path):
    config = {"repeater": {"storage_dir": str(tmp_path)}}
    api = _make_api(config)
    api._config_path = str(tmp_path / "config.yaml")

    presets_file = tmp_path / "radio-presets.json"
    presets_file.write_text(
        '{"config":{"suggested_radio_settings":{"entries":[{"label":"Fast","frequency":869.5}]}}}',
        encoding="utf-8",
    )

    with patch("repeater.web.api_endpoints.resolve_storage_dir", return_value=Path(tmp_path)):
        result = api.radio_presets()

    assert result["source"] == "local"
    assert len(result["presets"]) == 1
    assert result["presets"][0]["label"] == "Fast"


def test_needs_setup_reads_config_file_when_available(tmp_path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        """
repeater:
  node_name: mesh-node-11
  security:
    admin_password: longsecret
radio_type: sx1262
""".strip(),
        encoding="utf-8",
    )

    api = _make_api(
        {
            "repeater": {
                "node_name": "mesh-repeater-01",
                "security": {"admin_password": "admin123"},
            },
            "radio_type": "none",
        }
    )
    api._config_path = str(config_path)

    result = api.needs_setup()

    assert result["needs_setup"] is False
    assert result["reasons"]["radio_not_configured"] is False


def test_serial_ports_uses_pyserial_metadata(cherrypy_ctx):
    del cherrypy_ctx
    api = _make_api()

    p1 = SimpleNamespace(device="/dev/ttyACM0", description="USB CDC", hwid="VID:PID")
    p2 = SimpleNamespace(device="/dev/ttyUSB0", description="CH340", hwid="n/a")

    with patch("serial.tools.list_ports.comports", return_value=[p1, p2]):
        result = api.serial_ports()

    assert result["success"] is True
    devices = result["data"]
    assert devices[0]["device"] == "/dev/ttyACM0"
    assert "VID:PID" in devices[0]["description"]
    assert devices[1]["device"] == "/dev/ttyUSB0"


def test_serial_ports_dedupes_duplicate_devices(cherrypy_ctx):
    del cherrypy_ctx
    api = _make_api()

    p1 = SimpleNamespace(device="/dev/ttyACM0", description="first", hwid="A")
    p2 = SimpleNamespace(device="/dev/ttyACM0", description="second", hwid="B")

    with patch("serial.tools.list_ports.comports", return_value=[p1, p2]):
        result = api.serial_ports()

    assert result["success"] is True
    assert len(result["data"]) == 1
    assert "first" in result["data"][0]["description"]


def test_config_export_redacts_secrets_and_identity_keys(cherrypy_ctx):
    request, _ = cherrypy_ctx
    request.method = "GET"

    api = _make_api(
        {
            "repeater": {
                "security": {
                    "admin_password": "pw1",
                    "guest_password": "pw2",
                    "jwt_secret": "jwt",
                },
                "identity_key": bytes.fromhex("AABB"),
            },
            "identities": {
                "companions": [{"name": "c1", "identity_key": bytes.fromhex("0102")}],
                "room_servers": [{"name": "r1", "identity_key": bytes.fromhex("0304")}],
            },
            "misc": {"blob": b"\x0A\x0B"},
        }
    )

    result = api.config_export()

    assert result["success"] is True
    exported = result["data"]["config"]
    sec = exported["repeater"]["security"]
    assert sec["admin_password"] == "*** REDACTED ***"
    assert sec["guest_password"] == "*** REDACTED ***"
    assert sec["jwt_secret"] == "*** REDACTED ***"
    assert "identity_key" not in exported["repeater"]
    assert exported["identities"]["companions"][0]["identity_key"] == "*** REDACTED ***"
    assert exported["misc"]["blob"] == "0a0b"
    assert result["data"]["meta"]["includes_secrets"] is False


def test_config_export_full_backup_includes_hex_keys(cherrypy_ctx):
    request, _ = cherrypy_ctx
    request.method = "GET"

    api = _make_api(
        {
            "repeater": {"identity_key": bytes.fromhex("AABB")},
            "identities": {
                "companions": [{"name": "c1", "identity_key": bytes.fromhex("0102")}],
                "room_servers": [{"name": "r1", "identity_key": bytes.fromhex("0304")}],
            },
        }
    )

    result = api.config_export(include_secrets="true")

    assert result["success"] is True
    exported = result["data"]["config"]
    assert exported["repeater"]["identity_key"] == "aabb"
    assert exported["identities"]["companions"][0]["identity_key"] == "0102"
    assert exported["identities"]["room_servers"][0]["identity_key"] == "0304"
    assert result["data"]["meta"]["includes_secrets"] is True


def test_config_import_rejects_missing_config_object(cherrypy_ctx):
    request, _ = cherrypy_ctx
    request.method = "POST"
    request.json = {}
    api = _make_api()

    result = api.config_import()

    assert result["success"] is False
    assert "Missing or invalid 'config' object" in result["error"]


def test_config_import_updates_sections_and_preserves_redacted(cherrypy_ctx):
    request, _ = cherrypy_ctx
    request.method = "POST"

    api = _make_api(
        {
            "repeater": {
                "security": {
                    "admin_password": "keep-admin",
                    "guest_password": "keep-guest",
                    "jwt_secret": "keep-jwt",
                }
            },
            "identities": {
                "companions": [
                    {"name": "c1", "identity_key": bytes.fromhex("C0FFEE")},
                ]
            },
        }
    )

    request.json = {
        "config": {
            "repeater": {
                "security": {
                    "admin_password": "*** REDACTED ***",
                    "guest_password": "new-guest",
                    "jwt_secret": "*** REDACTED ***",
                },
                "identity_key": "AABBCC",
                "identity_file": "/tmp/remove-me",
            },
            "identities": {
                "companions": [
                    {"name": "c1", "identity_key": "*** REDACTED ***"},
                ]
            },
            "radio": {"frequency": 915000000},
            "radio_type": "pymc_usb",
            "unknown": {"x": 1},
        }
    }

    api.config_manager.update_and_save.return_value = {"ok": True}
    api.config_manager.save_to_file.return_value = True

    result = api.config_import()

    assert result["success"] is True
    assert result["restart_required"] is True
    assert set(result["sections_updated"]) == {"repeater", "identities", "radio", "radio_type"}

    sec = api.config["repeater"]["security"]
    assert sec["admin_password"] == "keep-admin"
    assert sec["guest_password"] == "new-guest"
    assert sec["jwt_secret"] == "keep-jwt"
    assert api.config["repeater"]["identity_key"] == bytes.fromhex("AABBCC")
    assert "identity_file" not in api.config["repeater"]
    assert api.config["identities"]["companions"][0]["identity_key"] == bytes.fromhex("C0FFEE")


def test_openapi_success_sets_content_type(cherrypy_ctx):
    _, response = cherrypy_ctx
    api = _make_api()

    with patch("builtins.open", mock_open(read_data="openapi: 3.0.0")):
        content = api.openapi()

    assert response.headers["Content-Type"] == "application/x-yaml"
    assert content == b"openapi: 3.0.0"


def test_openapi_not_found_returns_404(cherrypy_ctx):
    _, response = cherrypy_ctx
    api = _make_api()

    with patch("builtins.open", side_effect=FileNotFoundError):
        content = api.openapi()

    assert response.status == 404
    assert content == b"OpenAPI spec not found"


def test_docs_returns_html_bytes_and_content_type(cherrypy_ctx):
    _, response = cherrypy_ctx
    api = _make_api()

    content = api.docs()

    assert response.headers["Content-Type"] == "text/html"
    assert isinstance(content, bytes)
    assert b"SwaggerUIBundle" in content


def test_packet_and_route_stats_endpoints(cherrypy_ctx):
    del cherrypy_ctx
    api = _make_api()
    storage = SimpleNamespace(
        get_packet_stats=MagicMock(return_value={"total": 10}),
        get_packet_type_stats=MagicMock(return_value={"types": {1: 3}}),
        get_route_stats=MagicMock(return_value={"routes": {2: 5}}),
    )
    _attach_storage(api, storage)

    assert api.packet_stats("24") == {"success": True, "data": {"total": 10}}
    assert api.packet_type_stats("12") == {"success": True, "data": {"types": {1: 3}}}
    assert api.route_stats("6") == {"success": True, "data": {"routes": {2: 5}}}

    storage.get_packet_stats.assert_called_once_with(hours=24)
    storage.get_packet_type_stats.assert_called_once_with(hours=12)
    storage.get_route_stats.assert_called_once_with(hours=6)


def test_recent_packets_and_bulk_packets(cherrypy_ctx):
    del cherrypy_ctx
    api = _make_api()
    packets = [{"h": "aa"}, {"h": "bb"}]
    storage = SimpleNamespace(
        get_recent_packets=MagicMock(return_value=packets),
        get_filtered_packets=MagicMock(return_value=packets),
    )
    _attach_storage(api, storage)

    recent = api.recent_packets("2")
    bulk = api.bulk_packets(limit="20000", offset="-4", start_timestamp="1.5", end_timestamp="3.5")

    assert recent == {"success": True, "data": packets, "count": 2}
    assert bulk["success"] is True
    assert bulk["count"] == 2
    assert bulk["offset"] == 0
    assert bulk["limit"] == 10000
    assert bulk["compressed"] is True

    storage.get_recent_packets.assert_called_once_with(limit=2)
    storage.get_filtered_packets.assert_called_once_with(
        packet_type=None,
        route=None,
        start_timestamp=1.5,
        end_timestamp=3.5,
        limit=10000,
        offset=0,
    )


def test_filtered_packets_options_and_success(cherrypy_ctx):
    request, _ = cherrypy_ctx
    api = _make_api({"web": {"cors_enabled": True}})
    packets = [{"h": "a1"}]
    storage = SimpleNamespace(get_filtered_packets=MagicMock(return_value=packets))
    _attach_storage(api, storage)

    request.method = "OPTIONS"
    assert api.filtered_packets() == ""

    request.method = "GET"
    result = api.filtered_packets(
        start_timestamp="10",
        end_timestamp="20",
        limit="5",
        type="3",
        route="2",
    )

    assert result["success"] is True
    assert result["count"] == 1
    assert result["filters"] == {
        "type": 3,
        "route": 2,
        "start_timestamp": 10.0,
        "end_timestamp": 20.0,
        "limit": 5,
    }
    storage.get_filtered_packets.assert_called_once_with(
        packet_type=3,
        route=2,
        start_timestamp=10.0,
        end_timestamp=20.0,
        limit=5,
    )


def test_filtered_packets_invalid_parameter_format(cherrypy_ctx):
    request, _ = cherrypy_ctx
    request.method = "GET"
    api = _make_api()
    _attach_storage(api, SimpleNamespace(get_filtered_packets=MagicMock()))

    result = api.filtered_packets(type="not-an-int")

    assert result["success"] is False
    assert "Invalid parameter format" in result["error"]


def test_airtime_data_limit_and_error(cherrypy_ctx):
    del cherrypy_ctx
    api = _make_api()
    storage = SimpleNamespace(get_airtime_data=MagicMock(return_value=[{"a": 1}]))
    _attach_storage(api, storage)

    ok = api.airtime_data(start_timestamp="1", end_timestamp="2", limit="999999")
    assert ok["success"] is True
    assert ok["count"] == 1
    storage.get_airtime_data.assert_called_once_with(
        start_timestamp=1.0,
        end_timestamp=2.0,
        limit=50000,
    )

    storage.get_airtime_data.side_effect = RuntimeError("db down")
    err = api.airtime_data()
    assert err["success"] is False
    assert "db down" in err["error"]


def test_db_stats_options_success_and_error(cherrypy_ctx, tmp_path):
    request, _ = cherrypy_ctx
    api = _make_api({"web": {"cors_enabled": True}})

    request.method = "OPTIONS"
    assert api.db_stats() == ""

    request.method = "GET"
    rrd = tmp_path / "metrics.rrd"
    rrd.write_bytes(b"123456")
    sqlite_handler = SimpleNamespace(
        get_table_stats=MagicMock(return_value={"packets": {"rows": 10}}),
        storage_dir=tmp_path,
    )
    _attach_storage(api, SimpleNamespace(sqlite_handler=sqlite_handler))

    result = api.db_stats()
    assert result["success"] is True
    assert result["data"]["packets"]["rows"] == 10
    assert result["data"]["rrd_size_bytes"] == 6

    sqlite_handler.get_table_stats.side_effect = RuntimeError("stats failed")
    err = api.db_stats()
    assert err["success"] is False
    assert "stats failed" in err["error"]


def test_db_purge_validation_and_results(cherrypy_ctx):
    request, _ = cherrypy_ctx
    request.method = "POST"
    api = _make_api({"web": {"cors_enabled": True}})

    sqlite_handler = SimpleNamespace(
        purge_table=MagicMock(side_effect=[5, ValueError("bad table")])
    )
    _attach_storage(api, SimpleNamespace(sqlite_handler=sqlite_handler))

    request.json = {}
    missing = api.db_purge()
    assert missing["success"] is False
    assert "Missing 'tables'" in missing["error"]

    request.json = {"tables": "nope"}
    bad_type = api.db_purge()
    assert bad_type["success"] is False
    assert "must be a list" in bad_type["error"]

    request.json = {"tables": ["packets", "invalid"]}
    result = api.db_purge()
    assert result["success"] is True
    assert result["data"]["packets"]["deleted"] == 5
    assert "bad table" in result["data"]["invalid"]["error"]


def test_db_purge_all_and_options(cherrypy_ctx):
    request, _ = cherrypy_ctx
    api = _make_api({"web": {"cors_enabled": True}})

    request.method = "OPTIONS"
    assert api.db_purge() == ""

    request.method = "POST"
    request.json = {"tables": "all"}
    sqlite_handler = SimpleNamespace(purge_table=MagicMock(return_value=1))
    _attach_storage(api, SimpleNamespace(sqlite_handler=sqlite_handler))

    result = api.db_purge()
    assert result["success"] is True
    assert sqlite_handler.purge_table.call_count == 10


def test_db_vacuum_options_success_and_error(cherrypy_ctx):
    request, _ = cherrypy_ctx
    api = _make_api({"web": {"cors_enabled": True}})

    request.method = "OPTIONS"
    assert api.db_vacuum() == ""

    request.method = "POST"
    stat_values = [SimpleNamespace(st_size=1000), SimpleNamespace(st_size=700)]
    sqlite_path = SimpleNamespace(stat=MagicMock(side_effect=stat_values))
    sqlite_handler = SimpleNamespace(sqlite_path=sqlite_path, vacuum=MagicMock())
    _attach_storage(api, SimpleNamespace(sqlite_handler=sqlite_handler))

    result = api.db_vacuum()
    assert result["success"] is True
    assert result["data"] == {"size_before": 1000, "size_after": 700, "freed_bytes": 300}

    sqlite_path.stat = MagicMock(side_effect=[SimpleNamespace(st_size=700), SimpleNamespace(st_size=700)])
    sqlite_handler.vacuum.side_effect = RuntimeError("vacuum failed")
    err = api.db_vacuum()
    assert err["success"] is False
    assert "vacuum failed" in err["error"]


def test_config_export_options_preflight(cherrypy_ctx):
    request, _ = cherrypy_ctx
    request.method = "OPTIONS"
    api = _make_api({"web": {"cors_enabled": True}})

    assert api.config_export() == ""


def test_config_import_options_and_no_valid_sections(cherrypy_ctx):
    request, _ = cherrypy_ctx
    api = _make_api({"web": {"cors_enabled": True}})

    request.method = "OPTIONS"
    assert api.config_import() == ""

    request.method = "POST"
    request.json = {"config": {"unknown_section": {"x": 1}}}
    result = api.config_import()
    assert result["success"] is False
    assert "No valid configuration sections" in result["error"]


def test_config_import_invalid_identity_key_hex_is_skipped(cherrypy_ctx):
    request, _ = cherrypy_ctx
    request.method = "POST"
    api = _make_api({"repeater": {"security": {}}})
    api.config_manager.update_and_save.return_value = {"ok": True}
    api.config_manager.save_to_file.return_value = True
    request.json = {
        "config": {
            "repeater": {
                "security": {},
                "identity_key": "NOTHEX",
            }
        }
    }

    result = api.config_import()

    assert result["success"] is True
    assert "identity_key" not in api.config["repeater"]


def test_validate_config_options_and_method_guard(cherrypy_ctx):
    request, response = cherrypy_ctx
    api = _make_api({"web": {"cors_enabled": True}})

    request.method = "OPTIONS"
    assert api.validate_config() == ""

    request.method = "POST"
    with pytest.raises(cherrypy.HTTPError) as exc:
        api.validate_config()
    assert exc.value.status == 405
    assert response.status == 405
    assert response.headers["Allow"] == "GET"


def test_validate_config_reports_missing_file(cherrypy_ctx, tmp_path):
    request, _ = cherrypy_ctx
    request.method = "GET"
    api = _make_api({"web": {"cors_enabled": True}})
    api._config_path = str(tmp_path / "missing.yaml")

    result = api.validate_config()

    assert result["success"] is True
    assert result["data"]["valid"] is False
    assert result["data"]["summary"]["error_count"] >= 1
    assert result["data"]["errors"][0]["path"] == "config"


def test_validate_config_reports_yaml_parse_error(cherrypy_ctx, tmp_path):
    request, _ = cherrypy_ctx
    request.method = "GET"
    api = _make_api()
    api._config_path = str(tmp_path / "bad.yaml")
    (tmp_path / "bad.yaml").write_text("repeater: [unterminated", encoding="utf-8")

    result = api.validate_config()

    assert result["success"] is True
    assert result["data"]["valid"] is False
    assert any("YAML syntax error" in e["message"] for e in result["data"]["errors"])


def test_validate_config_valid_kiss_configuration(cherrypy_ctx, tmp_path):
    request, _ = cherrypy_ctx
    request.method = "GET"
    api = _make_api()
    api._config_path = str(tmp_path / "config.yaml")
    (tmp_path / "config.yaml").write_text(
        """
repeater:
  node_name: mesh-node-01
  security:
    admin_password: supersecret
radio_type: kiss
radio:
  frequency: 869618000
  bandwidth: 62500
  spreading_factor: 8
  coding_rate: 5
  tx_power: 22
  preamble_length: 16
kiss:
  port: /dev/ttyUSB0
  baud_rate: 115200
""".strip(),
        encoding="utf-8",
    )

    result = api.validate_config()

    assert result["success"] is True
    assert result["data"]["valid"] is True
    assert result["data"]["summary"]["error_count"] == 0


def test_validate_config_disabled_radio_warns_but_valid(cherrypy_ctx, tmp_path):
    request, _ = cherrypy_ctx
    request.method = "GET"
    api = _make_api()
    api._config_path = str(tmp_path / "config.yaml")
    (tmp_path / "config.yaml").write_text(
        """
repeater:
  node_name: mesh-node-02
  security:
    admin_password: supersecret
radio_type: none
""".strip(),
        encoding="utf-8",
    )

    result = api.validate_config()

    assert result["success"] is True
    assert result["data"]["valid"] is True
    assert result["data"]["summary"]["warning_count"] >= 1
    assert any(w["path"] == "radio_type" for w in result["data"]["warnings"])


def test_update_web_config_options_no_updates_success_failure(cherrypy_ctx):
        request, _ = cherrypy_ctx
        api = _make_api({"web": {"cors_enabled": True}})

        request.method = "OPTIONS"
        assert api.update_web_config() == ""

        request.method = "POST"
        request.json = {}
        no_updates = api.update_web_config()
        assert no_updates["success"] is False
        assert "No configuration updates" in no_updates["error"]

        request.json = {"web": {"cors_enabled": True}}
        api.config_manager.update_and_save.return_value = {"success": True, "saved": True}
        ok = api.update_web_config()
        assert ok["success"] is True
        assert ok["data"]["persisted"] is True
        api.config_manager.update_and_save.assert_called_with(
                updates={"web": {"cors_enabled": True}},
                live_update=False,
        )

        api.config_manager.update_and_save.return_value = {"success": False, "error": "bad"}
        fail = api.update_web_config()
        assert fail["success"] is False
        assert fail["error"] == "bad"


def test_update_web_config_requires_post_and_handles_exception(cherrypy_ctx):
        request, _ = cherrypy_ctx
        api = _make_api({"web": {"cors_enabled": True}})

        request.method = "GET"
        with pytest.raises(cherrypy.HTTPError) as exc:
                api.update_web_config()
        assert exc.value.status == 405

        request.method = "POST"
        request.json = {"web": {"site_name": "mesh"}}
        api.config_manager.update_and_save.side_effect = RuntimeError("write failed")
        err = api.update_web_config()
        assert err["success"] is False
        assert "write failed" in err["error"]


def test_validate_config_top_level_must_be_mapping(cherrypy_ctx, tmp_path):
        request, _ = cherrypy_ctx
        request.method = "GET"
        api = _make_api()
        api._config_path = str(tmp_path / "config.yaml")
        (tmp_path / "config.yaml").write_text("- list\n- not\n- mapping\n", encoding="utf-8")

        result = api.validate_config()

        assert result["success"] is True
        assert result["data"]["valid"] is False
        assert any(e["message"].startswith("Top-level YAML value must be a mapping") for e in result["data"]["errors"])


def test_validate_config_invalid_radio_type_and_missing_sections(cherrypy_ctx, tmp_path):
        request, _ = cherrypy_ctx
        request.method = "GET"
        api = _make_api()
        api._config_path = str(tmp_path / "config.yaml")
        (tmp_path / "config.yaml").write_text(
                """
repeater:
    node_name: ""
radio_type: weird_radio
""".strip(),
                encoding="utf-8",
        )

        result = api.validate_config()

        assert result["success"] is True
        assert result["data"]["valid"] is False
        paths = {e["path"] for e in result["data"]["errors"]}
        assert "repeater.node_name" in paths
        assert "repeater.security" in paths
        assert "radio_type" in paths


def test_validate_config_pymc_tcp_placeholder_and_bad_port(cherrypy_ctx, tmp_path):
        request, _ = cherrypy_ctx
        request.method = "GET"
        api = _make_api()
        api._config_path = str(tmp_path / "config.yaml")
        (tmp_path / "config.yaml").write_text(
                """
repeater:
    node_name: mesh-node-03
    security:
        admin_password: supersecret
radio_type: pymc_tcp
radio:
    frequency: 869618000
    bandwidth: 62500
    spreading_factor: 8
    coding_rate: 5
    tx_power: 22
    preamble_length: 16
pymc_tcp:
    host: REPLACE_WITH_MODEM_HOST
    port: 70000
""".strip(),
                encoding="utf-8",
        )

        result = api.validate_config()

        assert result["success"] is True
        assert result["data"]["valid"] is False
        paths = {e["path"] for e in result["data"]["errors"]}
        assert "pymc_tcp.host" in paths
        assert "pymc_tcp.port" in paths


def test_validate_config_sx1262_ch341_missing_sections(cherrypy_ctx, tmp_path):
        request, _ = cherrypy_ctx
        request.method = "GET"
        api = _make_api()
        api._config_path = str(tmp_path / "config.yaml")
        (tmp_path / "config.yaml").write_text(
                """
repeater:
    node_name: mesh-node-04
    security:
        admin_password: supersecret
radio_type: sx1262_ch341
radio:
    frequency: 869618000
    bandwidth: 62500
    spreading_factor: 8
    coding_rate: 5
    tx_power: 22
    preamble_length: 16
""".strip(),
                encoding="utf-8",
        )

        result = api.validate_config()

        assert result["success"] is True
        assert result["data"]["valid"] is False
        paths = {e["path"] for e in result["data"]["errors"]}
        assert "sx1262" in paths
        assert "ch341" in paths


def test_validate_config_rejects_bool_numeric_fields(cherrypy_ctx, tmp_path):
        """Booleans silently cast to int in Python, so this guards explicit type checks."""
        request, _ = cherrypy_ctx
        request.method = "GET"
        api = _make_api()
        api._config_path = str(tmp_path / "config.yaml")
        (tmp_path / "config.yaml").write_text(
                """
repeater:
    node_name: mesh-node-bool
    security:
        admin_password: supersecret
radio_type: kiss
radio:
    frequency: 869618000
    bandwidth: true
    spreading_factor: 8
    coding_rate: 5
    tx_power: 22
    preamble_length: 16
kiss:
    port: /dev/ttyUSB0
    baud_rate: true
""".strip(),
                encoding="utf-8",
        )

        result = api.validate_config()

        assert result["success"] is True
        assert result["data"]["valid"] is False
        errors = {e["path"]: e["message"] for e in result["data"]["errors"]}
        assert "radio.bandwidth" in errors
        assert "kiss.baud_rate" in errors


def test_validate_config_radio_numeric_ranges_and_modes(cherrypy_ctx, tmp_path):
        request, _ = cherrypy_ctx
        request.method = "GET"
        api = _make_api()
        api._config_path = str(tmp_path / "config.yaml")
        (tmp_path / "config.yaml").write_text(
                """
repeater:
    node_name: mesh-node-ranges
    security:
        admin_password: supersecret
radio_type: sx1262
radio:
    frequency: 99
    bandwidth: 12345
    spreading_factor: 4
    coding_rate: 9
    tx_power: 31
    preamble_length: 0
sx1262:
    bus_id: 0
    cs_id: 0
    cs_pin: 8
    reset_pin: 25
    busy_pin: 24
    irq_pin: 16
    txen_pin: 18
    rxen_pin: 17
""".strip(),
                encoding="utf-8",
        )

        result = api.validate_config()

        assert result["success"] is True
        assert result["data"]["valid"] is False
        paths = {e["path"] for e in result["data"]["errors"]}
        assert "radio.frequency" in paths
        assert "radio.bandwidth" in paths
        assert "radio.spreading_factor" in paths
        assert "radio.coding_rate" in paths
        assert "radio.tx_power" in paths
        assert "radio.preamble_length" in paths


def test_validate_config_en_pins_type_and_entry_validation(cherrypy_ctx, tmp_path):
        request, _ = cherrypy_ctx
        request.method = "GET"
        api = _make_api()
        api._config_path = str(tmp_path / "config.yaml")
        (tmp_path / "config.yaml").write_text(
                """
repeater:
    node_name: mesh-node-enpins
    security:
        admin_password: supersecret
radio_type: sx1262
radio:
    frequency: 869618000
    bandwidth: 62500
    spreading_factor: 8
    coding_rate: 5
    tx_power: 22
    preamble_length: 16
sx1262:
    bus_id: 0
    cs_id: 0
    cs_pin: 8
    reset_pin: 25
    busy_pin: 24
    irq_pin: 16
    txen_pin: 18
    rxen_pin: 17
    en_pins: [21, bad]
""".strip(),
                encoding="utf-8",
        )

        result = api.validate_config()

        assert result["success"] is True
        assert result["data"]["valid"] is False
        paths = {e["path"] for e in result["data"]["errors"]}
        assert "sx1262.en_pins[1]" in paths


def test_config_import_web_only_no_restart_required(cherrypy_ctx):
        request, _ = cherrypy_ctx
        request.method = "POST"
        api = _make_api({"web": {"site_name": "old"}})
        api.config_manager.update_and_save.return_value = {"ok": True}
        api.config_manager.save_to_file.return_value = True
        request.json = {"config": {"web": {"site_name": "new", "cors_enabled": True}}}

        result = api.config_import()

        assert result["success"] is True
        assert result["restart_required"] is False
        assert result["sections_updated"] == ["web"]
        assert api.config["web"]["site_name"] == "new"
        assert api.config["web"]["cors_enabled"] is True


def test_config_import_identity_redaction_preserves_by_name_for_room_servers(cherrypy_ctx):
        request, _ = cherrypy_ctx
        request.method = "POST"
        api = _make_api(
                {
                        "identities": {
                                "room_servers": [
                                        {"name": "main-room", "identity_key": bytes.fromhex("ABCD")},
                                ]
                        }
                }
        )
        api.config_manager.update_and_save.return_value = {"ok": True}
        api.config_manager.save_to_file.return_value = True
        request.json = {
                "config": {
                        "identities": {
                                "room_servers": [
                                        {"name": "main-room", "identity_key": "*** REDACTED ***"},
                                        {"name": "new-room", "identity_key": "*** REDACTED ***"},
                                ]
                        }
                }
        }

        result = api.config_import()

        assert result["success"] is True
        rooms = api.config["identities"]["room_servers"]
        by_name = {r["name"]: r["identity_key"] for r in rooms}
        assert by_name["main-room"] == bytes.fromhex("ABCD")
        # Unknown existing room keeps empty value when imported as redacted.
        assert by_name["new-room"] == ""
