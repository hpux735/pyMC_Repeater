"""Tests for per-companion bridge settings parsing and startup guard."""

from __future__ import annotations

import logging
from unittest.mock import MagicMock

import pytest

from repeater.companion.utils import (
    CompanionContactCapacityError,
    check_companion_contact_capacity,
    effective_max_contacts,
    merge_companion_settings_update,
    parse_companion_bridge_kwargs,
    parse_positive_int,
    validate_companion_config_capacity,
)
# pymc_core defaults (CompanionBridge / ContactStore)
_DEFAULT_MAX_CONTACTS = 1000


class TestParsePositiveInt:
    def test_valid(self):
        assert parse_positive_int("100", "max_contacts") == 100

    def test_invalid_type(self):
        with pytest.raises(ValueError, match="max_contacts"):
            parse_positive_int("abc", "max_contacts")

    def test_below_minimum(self):
        with pytest.raises(ValueError, match="max_contacts"):
            parse_positive_int(0, "max_contacts")


class TestParseCompanionBridgeKwargs:
    def test_empty_settings(self):
        assert parse_companion_bridge_kwargs({}) == {}

    def test_max_contacts_and_offline_queue(self):
        assert parse_companion_bridge_kwargs(
            {"max_contacts": 2000, "offline_queue_size": 1024}
        ) == {"max_contacts": 2000, "offline_queue_size": 1024}

    def test_ignored_keys_warn(self, caplog):
        caplog.set_level(logging.WARNING)
        result = parse_companion_bridge_kwargs(
            {"max_contacts": 500, "max_channels": 64, "adv_type": 2}
        )
        assert result == {"max_contacts": 500}
        assert any("max_channels" in r.message for r in caplog.records)
        assert any("adv_type" in r.message for r in caplog.records)

    def test_invalid_max_contacts(self):
        with pytest.raises(ValueError):
            parse_companion_bridge_kwargs({"max_contacts": -1})


class TestEffectiveMaxContacts:
    def test_default(self):
        assert effective_max_contacts({}) == _DEFAULT_MAX_CONTACTS

    def test_override(self):
        assert effective_max_contacts({"max_contacts": 500}) == 500


class TestMergeCompanionSettingsUpdate:
    def test_merges_bridge_settings(self):
        merged = merge_companion_settings_update(
            {"node_name": "a"},
            {"max_contacts": 500},
        )
        assert merged == {"node_name": "a", "max_contacts": 500}

    def test_unknown_key_raises(self):
        with pytest.raises(ValueError, match="Unknown companion setting"):
            merge_companion_settings_update({}, {"max_channels": 64})


class TestValidateCompanionConfigCapacity:
    def test_uses_merged_settings_not_stale_identity(self):
        identity = {
            "identity_key": "aa" * 32,
            "settings": {"max_contacts": 1000},
        }
        sqlite = MagicMock()
        sqlite.companion_count_contacts.return_value = 600
        with pytest.raises(CompanionContactCapacityError):
            validate_companion_config_capacity(
                identity,
                sqlite,
                settings={"max_contacts": 500},
            )
        sqlite.companion_count_contacts.assert_called_once()


class TestCheckCompanionContactCapacity:
    def test_skips_without_sqlite(self):
        check_companion_contact_capacity("0x01", 100, None)

    def test_passes_when_under_limit(self):
        sqlite = MagicMock()
        sqlite.companion_count_contacts.return_value = 100
        check_companion_contact_capacity("0x01", 500, sqlite)

    def test_raises_when_over_limit(self):
        sqlite = MagicMock()
        sqlite.companion_count_contacts.return_value = 812
        with pytest.raises(CompanionContactCapacityError) as exc:
            check_companion_contact_capacity(
                "0xab", 500, sqlite, companion_name="BotCompanion"
            )
        assert exc.value.stored_count == 812
        assert exc.value.max_contacts == 500
        assert "BotCompanion" in str(exc.value)
