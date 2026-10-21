"""Release metadata guard rails for the Electron desktop shell.

These tests only read checked-in metadata: ``package.json``,
``electron-builder.yml``, the GitHub Actions workflows and the legacy updater
manifest script. They never build the application and never touch the network,
so they run quickly on every Python test job.

The Tauri/Cargo metadata assertions from v3.x were removed on purpose: after M0
the single version source is ``package.json`` and the desktop installers are
produced by electron-builder. The workflow assertions below keep a regression
back to ``tauri build`` from silently reappearing.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PACKAGE_JSON = ROOT / "package.json"
ELECTRON_BUILDER_CONFIG = ROOT / "electron-builder.yml"
BUILD_WORKFLOW = ROOT / ".github" / "workflows" / "build-windows.yml"
CI_WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"
LEGACY_MANIFEST_SCRIPT = ROOT / "scripts" / "make-legacy-updater-manifest.mjs"

SEMVER = re.compile(r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$")


class ReleaseMetadataTests(unittest.TestCase):
    def read_text(self, path: Path) -> str:
        return path.read_text(encoding="utf-8")

    def test_package_json_is_the_desktop_version_source(self) -> None:
        package = json.loads(self.read_text(PACKAGE_JSON))

        self.assertRegex(package["version"], SEMVER)
        self.assertEqual("dist-electron/main.cjs", package["main"])

        scripts = package["scripts"]
        for name in (
            "dev",
            "build",
            "build:renderer",
            "build:electron",
            "package",
            "typecheck",
            "test",
        ):
            with self.subTest(script=name):
                self.assertIn(name, scripts)
        self.assertIn("vite build", scripts["build"])
        self.assertIn("scripts/build-electron.mjs", scripts["build"])

    def test_electron_builder_config_describes_nsis_packaging(self) -> None:
        config = self.read_text(ELECTRON_BUILDER_CONFIG)

        required_fragments = (
            "appId: com.sy118.wecom-issue-radar",
            "productName: 企微问题雷达",
            "copyright:",
            "output: release",
            "buildResources: build",
            "dist-electron/**/*",
            "frontend-dist/**/*",
            "package.json",
            "from: dist/wecom-issue-radar-worker.exe",
            "to: bin/wecom-issue-radar-worker.exe",
            "asar: true",
            "target: nsis",
            "- x64",
            "icon: build/icon.ico",
            'artifactName: "wecom-issue-radar-${version}-setup.${ext}"',
            "oneClick: false",
            "perMachine: false",
            "allowToChangeInstallationDirectory: true",
            "deleteAppDataOnUninstall: false",
            "createDesktopShortcut: true",
            "shortcutName: 企微问题雷达",
            "provider: github",
            "owner: sy118",
            "repo: wecom-issue-radar",
            "releaseType: release",
        )
        for fragment in required_fragments:
            with self.subTest(fragment=fragment):
                self.assertIn(fragment, config)

    def test_build_workflow_builds_electron_not_tauri(self) -> None:
        workflow = self.read_text(BUILD_WORKFLOW)

        required_fragments = (
            "workflow_dispatch:",
            'tags: ["v*"]',
            "fetch-depth: 0",
            "pnpm install --frozen-lockfile",
            "pnpm test",
            "pnpm typecheck",
            "pyinstaller --clean --noconfirm worker\\issue_radar_worker.spec",
            "dist\\wecom-issue-radar-worker.exe",
            "--self-check",
            "python -m unittest discover -s tests -v",
            "pnpm build",
            "pnpm exec electron-builder --win nsis --publish never",
            "release/latest.yml",
            "release/*.blockmap",
            "actions/upload-artifact@v4",
            "wecom-issue-radar-windows-x64",
            "softprops/action-gh-release@v2",
            "scripts/make-legacy-updater-manifest.mjs",
            "releases/latest/download/latest.yml",
            "releases/latest/download/latest.json",
        )
        for fragment in required_fragments:
            with self.subTest(fragment=fragment):
                self.assertIn(fragment, workflow)

        forbidden_fragments = (
            "tauri build",
            "cargo tauri",
            "src-tauri",
            "TAURI_SIGNING_PRIVATE_KEY",
        )
        for fragment in forbidden_fragments:
            with self.subTest(forbidden=fragment):
                self.assertNotIn(fragment, workflow)

    def test_build_workflow_uses_package_json_as_version_source(self) -> None:
        workflow = self.read_text(BUILD_WORKFLOW)

        self.assertIn(
            "if ($tag -notmatch '^v([0-9]+)\\.([0-9]+)\\.([0-9]+)$')",
            workflow,
        )
        self.assertIn("$version = $Matches[0].Substring(1)", workflow)
        self.assertIn(
            "$packageVersion = (Get-Content -Raw -Encoding utf8 package.json | ConvertFrom-Json).version",
            workflow,
        )
        self.assertIn("if ($version -ne $packageVersion)", workflow)
        self.assertIn(
            "git merge-base --is-ancestor $env:GITHUB_SHA refs/remotes/origin/main",
            workflow,
        )
        self.assertIn('gh api "repos/$env:GITHUB_REPOSITORY" --jq .visibility', workflow)
        self.assertIn('if ($visibility.Trim() -ne "public")', workflow)

    def test_build_workflow_validates_electron_updater_metadata(self) -> None:
        workflow = self.read_text(BUILD_WORKFLOW)

        for fragment in (
            "latest.yml",
            "release/latest.yml is missing",
            "sha512",
            "manifestVersion",
            "installerPath",
            ".blockmap",
        ):
            with self.subTest(fragment=fragment):
                self.assertIn(fragment, workflow)

    def test_build_workflow_publishes_legacy_tauri_manifest(self) -> None:
        workflow = self.read_text(BUILD_WORKFLOW)

        self.assertIn("LEGACY_TAURI_TAG: v3.5.3", workflow)
        self.assertIn("release/latest.json", workflow)
        self.assertIn(
            'gh release upload $env:RELEASE_TAG "release/latest.json" --clobber',
            workflow,
        )
        self.assertIn("gh release edit $env:RELEASE_TAG --draft=false", workflow)
        self.assertIn("--range 0-0", workflow)

    def test_ci_workflow_runs_electron_and_python_checks(self) -> None:
        workflow = self.read_text(CI_WORKFLOW)

        required_fragments = (
            "pnpm install --frozen-lockfile",
            "pnpm test",
            "pnpm typecheck",
            "pnpm build:renderer",
            "pnpm build:electron",
            "dist-electron\\main.cjs",
            "dist-electron\\preload.cjs",
            "python -m unittest discover -s tests -v",
            "pyinstaller --clean --noconfirm worker\\issue_radar_worker.spec",
            "--self-check",
        )
        for fragment in required_fragments:
            with self.subTest(fragment=fragment):
                self.assertIn(fragment, workflow)

        for fragment in (
            "tauri build",
            "cargo tauri",
            "src-tauri",
        ):
            with self.subTest(forbidden=fragment):
                self.assertNotIn(fragment, workflow)

    def test_native_helper_build_steps_exist_in_both_workflows(self) -> None:
        rust_fragments = (
            "dtolnay/rust-toolchain@1.97.1",
            "components: rustfmt, clippy",
            "targets: x86_64-pc-windows-msvc",
            "cargo fmt --manifest-path native/wechat-input-helper/Cargo.toml",
            "cargo clippy --manifest-path native/wechat-input-helper/Cargo.toml "
            "--target x86_64-pc-windows-msvc --all-targets",
            "cargo test --manifest-path native/wechat-input-helper/Cargo.toml "
            "--target x86_64-pc-windows-msvc",
            "cargo build --release --manifest-path "
            "native/wechat-input-helper/Cargo.toml --target x86_64-pc-windows-msvc",
            "native\\wechat-input-helper\\target\\x86_64-pc-windows-msvc\\release\\"
            "wechat-input-helper.exe",
            "dist\\wechat-input-helper.exe",
        )

        for workflow_path in (BUILD_WORKFLOW, CI_WORKFLOW):
            workflow = self.read_text(workflow_path)
            for fragment in rust_fragments:
                with self.subTest(workflow=workflow_path.name, fragment=fragment):
                    self.assertIn(fragment, workflow)

    def test_electron_builder_packages_native_helper_as_extra_resource(self) -> None:
        config = self.read_text(ELECTRON_BUILDER_CONFIG)

        self.assertIn("from: dist/wechat-input-helper.exe", config)
        self.assertIn("to: bin/wechat-input-helper.exe", config)

        workflow = self.read_text(BUILD_WORKFLOW)
        self.assertIn("release\\win-unpacked\\resources\\bin", workflow)
        self.assertIn(
            "Packaged native binary is missing: resources/bin/$name",
            workflow,
        )

    def test_legacy_updater_manifest_script_contract(self) -> None:
        source = self.read_text(LEGACY_MANIFEST_SCRIPT)

        required_fragments = (
            'DEFAULT_LEGACY_TAG = "v3.5.3"',
            "LEGACY_TAURI_TAG",
            "GH_TOKEN",
            "GITHUB_REPOSITORY",
            "GITHUB_API_URL",
            "application/octet-stream",
            "windows-x86_64",
            "windows-x86_64-nsis",
            ".exe",
            ".sig",
            "process.exitCode = 1",
            "JSON.stringify(manifest, null, 2)",
        )
        for fragment in required_fragments:
            with self.subTest(fragment=fragment):
                self.assertIn(fragment, source)

    @unittest.skipUnless(
        shutil.which("node"),
        "node is required to syntax-check the legacy manifest script",
    )
    def test_legacy_updater_manifest_script_parses(self) -> None:
        result = subprocess.run(
            ["node", "--check", str(LEGACY_MANIFEST_SCRIPT)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
        )

        self.assertEqual(0, result.returncode, result.stderr)


if __name__ == "__main__":
    unittest.main()
