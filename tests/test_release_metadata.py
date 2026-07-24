import json
import tomllib
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class ReleaseMetadataTests(unittest.TestCase):
    def test_desktop_versions_match(self) -> None:
        package_version = json.loads((ROOT / "package.json").read_text(encoding="utf-8"))["version"]
        tauri_config_version = json.loads(
            (ROOT / "src-tauri" / "tauri.conf.json").read_text(encoding="utf-8")
        )["version"]
        cargo_manifest_version = tomllib.loads(
            (ROOT / "src-tauri" / "Cargo.toml").read_text(encoding="utf-8")
        )["package"]["version"]
        cargo_lock = tomllib.loads(
            (ROOT / "src-tauri" / "Cargo.lock").read_text(encoding="utf-8")
        )
        cargo_lock_version = next(
            package["version"]
            for package in cargo_lock["package"]
            if package["name"] == "wecom-issue-radar"
        )

        versions = {
            "package.json": package_version,
            "src-tauri/tauri.conf.json": tauri_config_version,
            "src-tauri/Cargo.toml": cargo_manifest_version,
            "src-tauri/Cargo.lock": cargo_lock_version,
        }
        self.assertEqual(1, len(set(versions.values())), versions)

    def test_updater_is_signed_and_points_to_latest_manifest(self) -> None:
        tauri_config = json.loads(
            (ROOT / "src-tauri" / "tauri.conf.json").read_text(encoding="utf-8")
        )
        self.assertIs(True, tauri_config["bundle"]["createUpdaterArtifacts"])

        updater = tauri_config["plugins"]["updater"]
        self.assertGreater(len(updater["pubkey"]), 100)
        self.assertNotIn("updater.key", updater["pubkey"])
        self.assertEqual(
            [
                "https://github.com/sy118/wecom-issue-radar/"
                "releases/latest/download/latest.json"
            ],
            updater["endpoints"],
        )
        self.assertEqual("passive", updater["windows"]["installMode"])

    def test_desktop_capability_allows_updater_and_restart(self) -> None:
        capability = json.loads(
            (ROOT / "src-tauri" / "capabilities" / "default.json").read_text(
                encoding="utf-8"
            )
        )
        permissions = set(capability["permissions"])
        self.assertIn("updater:default", permissions)
        self.assertIn("process:allow-restart", permissions)

    def test_release_workflow_publishes_signed_updater_metadata(self) -> None:
        workflow = (ROOT / ".github" / "workflows" / "build-windows.yml").read_text(
            encoding="utf-8"
        )
        required_fragments = (
            "TAURI_SIGNING_PRIVATE_KEY",
            "*.exe.sig",
            "*.msi.sig",
            "release-assets/latest.json",
            '"windows-x86_64": nsisEntry',
            "Require a public updater source",
        )
        for fragment in required_fragments:
            with self.subTest(fragment=fragment):
                self.assertIn(fragment, workflow)


if __name__ == "__main__":
    unittest.main()
