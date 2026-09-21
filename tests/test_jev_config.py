"""Jev configuration fixtures.

Locks in the first requirement of `jev-semantic-client`: an old config with no
`jev` block keeps loading unchanged with the capability disabled, and no
hand-edited value can escape the safe bounds.

Implementation/CI fixtures; not executed during this change's preparation.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from worker.jev_config import (
    DEFAULT_JEV_CONFIG,
    JEV_FEATURES,
    JevConfig,
    normalize_jev_config,
)
from worker.pipeline.config_store import ensure_jev_config, load_config, save_config


class NormalizeJevConfigTests(unittest.TestCase):
    def test_omitted_block_yields_disabled_defaults(self):
        config = normalize_jev_config(None)
        self.assertFalse(config["enabled"])
        self.assertEqual(config["mode"], "shadow")
        self.assertEqual(
            config["features"], {name: False for name in JEV_FEATURES}
        )
        self.assertEqual(sorted(config), sorted(DEFAULT_JEV_CONFIG))

    def test_unrecognized_mode_degrades_to_off(self):
        # Fail safe: never call out on ambiguous intent.
        self.assertEqual(normalize_jev_config({"mode": "authoritative"})["mode"], "off")
        self.assertEqual(normalize_jev_config({"mode": "ASSIST"})["mode"], "assist")

    def test_review_floor_cannot_exceed_the_decision_threshold(self):
        config = normalize_jev_config({"decision_threshold": 0.5, "review_floor": 0.9})
        self.assertEqual(config["review_floor"], 0.5)
        self.assertLessEqual(config["review_floor"], config["decision_threshold"])

    def test_out_of_range_values_are_clamped(self):
        config = normalize_jev_config({
            "timeout_seconds": 99_999,
            "max_state_chars": 1,
            "max_candidates": 9_999,
            "max_retries": 500,
            "decision_threshold": 5.0,
            "review_floor": -3.0,
        })
        self.assertLessEqual(config["timeout_seconds"], 300)
        self.assertGreaterEqual(config["max_state_chars"], 200)
        self.assertLessEqual(config["max_candidates"], 50)
        self.assertLessEqual(config["max_retries"], 5)
        self.assertLessEqual(config["decision_threshold"], 1.0)
        self.assertGreaterEqual(config["review_floor"], 0.0)

    def test_backoff_cap_is_never_below_the_base(self):
        config = normalize_jev_config(
            {"retry_backoff_seconds": 5.0, "max_retry_backoff_seconds": 1.0}
        )
        self.assertGreaterEqual(
            config["max_retry_backoff_seconds"], config["retry_backoff_seconds"]
        )

    def test_non_numeric_junk_falls_back_to_defaults(self):
        config = normalize_jev_config({"timeout_seconds": "soon", "enabled": "yes"})
        self.assertEqual(config["timeout_seconds"], DEFAULT_JEV_CONFIG["timeout_seconds"])
        self.assertTrue(config["enabled"])  # bool() of a non-empty string

    def test_per_feature_thresholds_clamp_against_their_own_yes(self):
        config = normalize_jev_config({
            "decision_threshold": 0.8,
            "review_floor": 0.2,
            "thresholds": {"runtime_classification": {"yes": 0.6, "no": 0.95}},
        })
        entry = config["thresholds"]["runtime_classification"]
        self.assertEqual(entry["yes"], 0.6)
        self.assertLessEqual(entry["no"], entry["yes"])


class EnsureJevConfigTests(unittest.TestCase):
    def test_absent_block_is_left_absent(self):
        config = {"llm": {"model": "x"}}
        self.assertNotIn("jev", ensure_jev_config(config))

    def test_present_block_is_normalized_in_place(self):
        config = {"jev": {"mode": "nonsense"}}
        ensure_jev_config(config)
        self.assertEqual(config["jev"]["mode"], "off")

    def test_save_rejects_enabled_config_without_an_endpoint(self):
        with self.assertRaises(ValueError):
            ensure_jev_config({"jev": {"enabled": True, "mode": "assist", "api_key": "k" * 12}}, strict=True)
        with self.assertRaises(ValueError):
            ensure_jev_config({"jev": {"enabled": True, "mode": "assist", "base_url": "https://a.example"}}, strict=True)

    def test_save_rejects_plain_http_off_localhost(self):
        with self.assertRaises(ValueError):
            ensure_jev_config(
                {"jev": {"enabled": True, "mode": "assist", "base_url": "http://gw.internal", "api_key": "k" * 12}},
                strict=True,
            )

    def test_save_accepts_localhost_http_and_https(self):
        for base in ("https://api.typesafe.ai", "http://localhost:8080"):
            ensure_jev_config(
                {"jev": {"enabled": True, "mode": "assist", "base_url": base, "api_key": "k" * 12}},
                strict=True,
            )

    def test_save_ignores_an_unusable_block_while_disabled(self):
        # Disabled or off means the fields are simply unused, not invalid.
        ensure_jev_config({"jev": {"enabled": False, "base_url": ""}}, strict=True)
        ensure_jev_config({"jev": {"enabled": True, "mode": "off", "base_url": ""}}, strict=True)


class ConfigRoundTripTests(unittest.TestCase):
    def test_legacy_config_without_jev_loads_disabled(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.local.json"
            path.write_text(
                json.dumps({
                    "config_version": 3,
                    "llm": {"provider": "openai_compatible", "base_url": "https://llm.example", "api_key": "llm-key", "model": "m1"},
                    "ocr": {"base_url": "https://ocr.example", "api_key": "ocr-key", "model": "v1"},
                }),
                encoding="utf-8",
            )
            config, _ = load_config(path)
            self.assertFalse(config["jev"]["enabled"])
            self.assertEqual(config["jev"]["mode"], "shadow")
            # Existing LLM/OCR configuration is untouched by the Jev defaults.
            self.assertEqual(config["llm"]["model"], "m1")
            self.assertEqual(config["llm"]["api_key"], "llm-key")
            self.assertEqual(config["ocr"]["api_key"], "ocr-key")

    def test_load_is_permissive_about_an_unusable_enabled_block(self):
        # An unusable block must not stop the app from starting: it simply
        # resolves to "not configured" and every capability fails open.
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.local.json"
            path.write_text(
                json.dumps({"config_version": 3, "jev": {"enabled": True, "mode": "assist", "base_url": ""}}),
                encoding="utf-8",
            )
            config, _ = load_config(path)
            self.assertTrue(config["jev"]["enabled"])
            self.assertFalse(JevConfig.from_app_config(config).configured)

    def test_enabling_jev_alone_never_enables_a_capability(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.local.json"
            path.write_text(
                json.dumps({"config_version": 3, "jev": {"enabled": True, "mode": "assist"}}),
                encoding="utf-8",
            )
            config, _ = load_config(path)
            jev = JevConfig.from_app_config(config)
            for feature in JEV_FEATURES:
                self.assertFalse(jev.feature_enabled(feature), feature)
                self.assertFalse(jev.active(feature), feature)

    def test_save_round_trips_a_valid_block(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.local.json"
            save_config(
                {
                    "config_version": 3,
                    "jev": {
                        "enabled": True,
                        "mode": "assist",
                        "base_url": "https://api.typesafe.ai",
                        "api_key": "secret-key-value",
                        "features": {name: True for name in JEV_FEATURES},
                    },
                },
                path,
            )
            reloaded, _ = load_config(path)
            jev = JevConfig.from_app_config(reloaded)
            self.assertTrue(jev.configured)
            for feature in JEV_FEATURES:
                self.assertTrue(jev.active(feature), feature)


if __name__ == "__main__":
    unittest.main()
