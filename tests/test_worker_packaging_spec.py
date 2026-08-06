from __future__ import annotations

import ast
import unittest
from pathlib import Path


class WorkerPackagingSpecTests(unittest.TestCase):
    def test_mcp_collection_excludes_optional_cli_modules(self):
        spec_path = Path(__file__).resolve().parents[1] / "worker" / "issue_radar_worker.spec"
        tree = ast.parse(spec_path.read_text(encoding="utf-8"), filename=str(spec_path))
        function = next(
            node
            for node in tree.body
            if isinstance(node, ast.FunctionDef)
            and node.name == "include_mcp_runtime_submodule"
        )
        namespace: dict[str, object] = {}
        ast.fix_missing_locations(function)
        exec(compile(ast.Module(body=[function], type_ignores=[]), str(spec_path), "exec"), namespace)
        include = namespace["include_mcp_runtime_submodule"]

        self.assertTrue(include("mcp.client.sse"))
        self.assertTrue(include("mcp.client.streamable_http"))
        self.assertFalse(include("mcp.cli"))
        self.assertFalse(include("mcp.cli.cli"))

        calls = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "collect_submodules"
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and node.args[0].value == "mcp"
        ]
        self.assertEqual(len(calls), 1)
        filter_keyword = next(
            keyword for keyword in calls[0].keywords if keyword.arg == "filter"
        )
        self.assertIsInstance(filter_keyword.value, ast.Name)
        self.assertEqual(filter_keyword.value.id, "include_mcp_runtime_submodule")

    def test_agent_runtime_modules_and_package_data_are_collected(self):
        spec_path = Path(__file__).resolve().parents[1] / "worker" / "issue_radar_worker.spec"
        source = spec_path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(spec_path))

        self.assertIn('"worker.reply_runtime.agent"', source)
        collected_packages = {
            node.args[0].value
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id in {"collect_data_files", "collect_submodules"}
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, str)
        }
        declared_packages = {
            element.value
            for node in tree.body
            if isinstance(node, ast.Assign)
            and any(
                isinstance(target, ast.Name)
                and target.id == "agent_runtime_packages"
                for target in node.targets
            )
            and isinstance(node.value, ast.Tuple)
            for element in node.value.elts
            if isinstance(element, ast.Constant)
            and isinstance(element.value, str)
        }
        self.assertEqual(
            declared_packages,
            {"langgraph", "langchain_core", "langchain_openai", "langchain_anthropic"},
        )
        self.assertIn("mcp", collected_packages)
        per_package_collectors = {
            node.func.id
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id in {"collect_data_files", "collect_submodules"}
            and node.args
            and isinstance(node.args[0], ast.Name)
            and node.args[0].id == "package_name"
        }
        self.assertEqual(
            per_package_collectors,
            {"collect_data_files", "collect_submodules"},
        )
        self.assertIn("include_agent_runtime_submodule", source)
        self.assertIn("copy_metadata", source)


if __name__ == "__main__":
    unittest.main()
