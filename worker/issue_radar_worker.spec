import sys
from pathlib import Path


worker_root = Path(SPECPATH)
repository_root = worker_root.parent
python_dll_dir = Path(sys.base_prefix) / "DLLs"
openssl_dll_names = ("libcrypto-3-x64.dll", "libssl-3-x64.dll")

openssl_binaries = []
for dll_name in openssl_dll_names:
    dll_path = python_dll_dir / dll_name
    if dll_path.is_file():
        openssl_binaries.append((str(dll_path), "."))

a = Analysis(
    [str(worker_root / "main.py")],
    pathex=[str(repository_root)],
    binaries=openssl_binaries,
    datas=[(str(repository_root / "config.example.json"), ".")],
    hiddenimports=[
        "worker.ocr",
        "worker.pipeline.config_store",
        "worker.pipeline.detector",
        "worker.pipeline.exporter",
        "worker.pipeline.llm_analyzer",
        "worker.pipeline.smart_sheet",
        "worker.pipeline.tasks",
        "worker.wecom.cache_messages",
        "worker.wecom.content_decoder",
        "worker.wecom.crypto",
        "worker.wecom.extract_keys",
        "worker.wecom.local_db",
        "worker.wecom.paths",
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
