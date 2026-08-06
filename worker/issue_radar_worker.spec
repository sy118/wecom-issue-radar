import sys
from pathlib import Path
from PyInstaller.utils.hooks import collect_data_files, collect_submodules, copy_metadata


def include_mcp_runtime_submodule(name):
    """Exclude the optional MCP CLI, which requires the unused typer extra."""

    return name != "mcp.cli" and not name.startswith("mcp.cli.")


def include_agent_runtime_submodule(name):
    """Skip optional middleware integrations that require the full langchain package."""

    return not name.endswith(".middleware") and ".middleware." not in name


worker_root = Path(SPECPATH)
repository_root = worker_root.parent
python_dll_dir = Path(sys.base_prefix) / "DLLs"
openssl_dll_names = ("libcrypto-3-x64.dll", "libssl-3-x64.dll")
agent_runtime_packages = (
    "langgraph",
    "langchain_core",
    "langchain_openai",
    "langchain_anthropic",
)
agent_runtime_distributions = (
    "langgraph",
    "langgraph-checkpoint",
    "langgraph-prebuilt",
    "langchain-core",
    "langchain-openai",
    "langchain-anthropic",
    "openai",
    "anthropic",
)

agent_runtime_datas = []
agent_runtime_hiddenimports = []
for package_name in agent_runtime_packages:
    agent_runtime_datas.extend(collect_data_files(package_name))
    agent_runtime_hiddenimports.extend(
        collect_submodules(package_name, filter=include_agent_runtime_submodule)
    )
for distribution_name in agent_runtime_distributions:
    agent_runtime_datas.extend(copy_metadata(distribution_name))

openssl_binaries = []
for dll_name in openssl_dll_names:
    dll_path = python_dll_dir / dll_name
    if dll_path.is_file():
        openssl_binaries.append((str(dll_path), "."))

a = Analysis(
    [str(worker_root / "main.py")],
    pathex=[str(repository_root)],
    binaries=openssl_binaries,
    datas=[
        (str(repository_root / "config.example.json"), "."),
        *agent_runtime_datas,
    ],
    hiddenimports=[
        "worker.ocr",
        "worker.pipeline.config_store",
        "worker.pipeline.detector",
        "worker.pipeline.exporter",
        "worker.pipeline.issue_schema",
        "worker.pipeline.llm_analyzer",
        "worker.pipeline.smart_sheet",
        "worker.pipeline.tasks",
        "worker.wecom.cache_messages",
        "worker.wecom.content_decoder",
        "worker.wecom.crypto",
        "worker.wecom.extract_keys",
        "worker.wecom.local_db",
        "worker.wecom.paths",
        "worker.reply_runtime.agent",
        "worker.reply_runtime.answer_engine",
        "worker.reply_runtime.adapters",
        "worker.reply_runtime.dify",
        "worker.reply_runtime.errors",
        "worker.reply_runtime.factory",
        "worker.reply_runtime.message_source",
        "worker.reply_runtime.runtime",
        "worker.reply_runtime.stdio",
        "worker.reply_runtime.store",
        *agent_runtime_hiddenimports,
        *collect_submodules("mcp", filter=include_mcp_runtime_submodule),
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)

# PyInstaller resolves transitive DLLs using PATH and can accidentally select
# an incompatible Anaconda OpenSSL build. When that runtime ships standalone
# OpenSSL DLLs, replace auto-detected entries with its own copies. Official
# GitHub Actions Python builds do not expose these files and need no override.
if openssl_binaries:
    openssl_names_lower = {name.lower() for name in openssl_dll_names}
    a.binaries = [
        entry
        for entry in a.binaries
        if Path(entry[0]).name.lower() not in openssl_names_lower
    ]
    for dll_name in openssl_dll_names:
        dll_path = python_dll_dir / dll_name
        if dll_path.is_file():
            a.binaries.append((dll_name, str(dll_path), "BINARY"))

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="wecom-issue-radar-worker",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=True,
)
