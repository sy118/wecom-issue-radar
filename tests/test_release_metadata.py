import json
import subprocess
import tempfile
import textwrap
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

    def test_updater_manifest_reads_the_draft_release_by_id(self) -> None:
        workflow = (ROOT / ".github" / "workflows" / "build-windows.yml").read_text(
            encoding="utf-8"
        )

        self.assertIn("id: draft_release", workflow)
        self.assertIn(
            "RELEASE_ID: ${{ steps.draft_release.outputs.id }}",
            workflow,
        )
        self.assertIn("const releaseId = Number(process.env.RELEASE_ID);", workflow)
        self.assertIn("github.rest.repos.getRelease({", workflow)
        self.assertIn("release_id: releaseId", workflow)
        self.assertNotIn("getReleaseByTag", workflow)

    def test_updater_manifest_uses_the_post_publish_asset_urls(self) -> None:
        workflow = (ROOT / ".github" / "workflows" / "build-windows.yml").read_text(
            encoding="utf-8"
        )

        self.assertIn(
            "releases/download/${encodeURIComponent(tag)}/${encodeURIComponent(asset.name)}",
            workflow,
        )
        self.assertNotIn("url: asset.browser_download_url", workflow)

    def test_updater_manifest_handles_github_sanitized_asset_names(self) -> None:
        workflow = (ROOT / ".github" / "workflows" / "build-windows.yml").read_text(
            encoding="utf-8"
        )
        generate_step = workflow.split(
            "      - name: Generate updater manifest", 1
        )[1].split("      - name: Upload updater manifest", 1)[0]
        script = textwrap.dedent(generate_step.split("          script: |\n", 1)[1])

        release = {
            "tag_name": "v3.2.1",
            "body": "Release notes",
            "published_at": None,
            "created_at": "2026-07-24T00:00:00Z",
            "assets": [
                {"name": "_3.2.1_x64-setup.exe"},
                {"name": "_3.2.1_x64_en-US.msi"},
            ],
        }
        harness = f"""
            const release = {json.dumps(release)};
            global.context = {{ repo: {{ owner: "sy118", repo: "wecom-issue-radar" }} }};
            global.github = {{
              rest: {{ repos: {{ getRelease: async () => ({{ data: release }}) }} }},
            }};
            process.env.RELEASE_ID = "1";
            process.env.RELEASE_TAG = "v3.2.1";
            (async () => {{
            {textwrap.indent(script, "  ")}
            }})().catch((error) => {{
              console.error(error && error.stack ? error.stack : error);
              process.exitCode = 1;
            }});
        """

        with tempfile.TemporaryDirectory() as temp_dir:
            release_assets = Path(temp_dir) / "release-assets"
            release_assets.mkdir()
            (release_assets / "企微问题雷达_3.2.1_x64-setup.exe.sig").write_text(
                "nsis-signature\n", encoding="utf-8"
            )
            (release_assets / "企微问题雷达_3.2.1_x64_en-US.msi.sig").write_text(
                "msi-signature\n", encoding="utf-8"
            )

            result = subprocess.run(
                ["node", "-e", harness],
                cwd=temp_dir,
                capture_output=True,
                text=True,
                encoding="utf-8",
                check=False,
            )

            self.assertEqual(0, result.returncode, result.stderr)
            manifest = json.loads(
                (release_assets / "latest.json").read_text(encoding="utf-8")
            )
            self.assertEqual(
                "nsis-signature", manifest["platforms"]["windows-x86_64"]["signature"]
            )
            self.assertTrue(
                manifest["platforms"]["windows-x86_64"]["url"].endswith(
                    "/_3.2.1_x64-setup.exe"
                )
            )


if __name__ == "__main__":
    unittest.main()
