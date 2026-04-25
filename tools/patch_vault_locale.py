"""Patch the_vault*.jar so it recognizes ja_jp as a supported locale.

`iskallia.vault.config.Config` has a hardcoded SUPPORTED_LOCALES list that
omits `ja_jp`. We swap the unused `es_mx` entry for `ja_jp` with an
in-place 5-byte replacement in the class file's UTF-8 constant pool.
This preserves all class file offsets and avoids any code rewriting.

Why `es_mx`? It is registered in SUPPORTED_LOCALES but VH does NOT ship
a `config/the_vault/lang/es_mx/` directory — it's a phantom locale slot
that's safe to repurpose with no functional loss.

Usage:
    python tools/patch_vault_locale.py "<instance dir>"
    python tools/patch_vault_locale.py "<path to the_vault-*.jar>"
"""
from __future__ import annotations

import argparse
import shutil
import sys
import zipfile
from pathlib import Path

CLASS_PATH = "iskallia/vault/config/Config.class"
OLD = b"es_mx"
NEW = b"ja_jp"


def find_jar(arg: Path) -> Path:
    if arg.is_file() and arg.suffix == ".jar":
        return arg
    if arg.is_dir():
        candidates = list((arg / "mods").glob("the_vault-*.jar"))
        if not candidates:
            raise SystemExit(f"the_vault-*.jar not found under {arg / 'mods'}")
        if len(candidates) > 1:
            joined = "\n".join(f"  {c}" for c in candidates)
            raise SystemExit(f"multiple the_vault jars; specify one explicitly:\n{joined}")
        return candidates[0]
    raise SystemExit(f"not a jar or directory: {arg}")


def patch_jar(jar_path: Path) -> None:
    backup = jar_path.with_suffix(jar_path.suffix + ".bak")
    if not backup.exists():
        shutil.copy2(jar_path, backup)
        print(f"[INFO] backup: {backup}")
    else:
        print(f"[INFO] backup already exists, leaving alone: {backup}")

    with zipfile.ZipFile(jar_path, "r") as zin:
        if CLASS_PATH not in zin.namelist():
            raise SystemExit(f"{CLASS_PATH} not in JAR")
        original = zin.read(CLASS_PATH)

    if NEW in original and OLD not in original:
        print("[INFO] already patched (ja_jp present, es_mx absent). Nothing to do.")
        return

    occurrences = original.count(OLD)
    if occurrences == 0:
        raise SystemExit(f"{OLD!r} not found in {CLASS_PATH}; aborting")

    if occurrences > 1:
        # Defensive: if "es_mx" appears more than once, restrict to the
        # canonical UTF-8 constant-pool entry: tag (0x01) + length (0x00 0x05) + bytes.
        marker = b"\x01\x00\x05" + OLD
        if marker not in original:
            raise SystemExit(
                f"unsafe: {OLD!r} appears {occurrences}x and no canonical UTF-8 entry found"
            )
        patched = original.replace(marker, b"\x01\x00\x05" + NEW, 1)
    else:
        patched = original.replace(OLD, NEW, 1)

    if len(patched) != len(original):
        raise SystemExit("internal: length mismatch after patch (should never happen)")

    tmp = jar_path.with_suffix(jar_path.suffix + ".tmp")
    with zipfile.ZipFile(jar_path, "r") as zin, zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zout:
        for item in zin.infolist():
            data = patched if item.filename == CLASS_PATH else zin.read(item.filename)
            zout.writestr(item, data)
    tmp.replace(jar_path)
    print(f"[OK] patched {jar_path}  ({CLASS_PATH}: es_mx → ja_jp)")


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("target", type=Path, help="instance dir or JAR path")
    args = p.parse_args()
    jar = find_jar(args.target)
    print(f"[INFO] target JAR: {jar}")
    patch_jar(jar)
    return 0


if __name__ == "__main__":
    sys.exit(main())
