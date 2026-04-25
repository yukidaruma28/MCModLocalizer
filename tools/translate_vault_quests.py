"""Translate Vault Hunters style locale JSON files to Japanese.

Vault Hunters Third Edition (and similar mods) store quest / tooltip / skill
text in `<instance>/config/the_vault/quest/quests.json` (default English) and
provide per-locale overrides under `config/the_vault/lang/<locale>/...`. This
script generates `ja_jp` overrides by walking either a single file or an
entire directory tree and translating every `text` / `name` / `title` /
`description` field via the existing Claude Agent SDK (Pro/Max subscription).

Modes:
1. Single-file (target preset)
       python tools\\translate_vault_quests.py "<instance dir>"
       (defaults to --target quest = config/the_vault/quest/quests.json)

2. Single-file (custom paths)
       python tools\\translate_vault_quests.py --src <abs/rel> --out <abs/rel>

3. Directory tree (mirrored output)
       python tools\\translate_vault_quests.py --src-dir <dir> --out-dir <dir>
       (e.g. --src-dir config/the_vault/lang/fr_fr --out-dir config/the_vault/lang/ja_jp)

Features:
- Token protection (`%s`, `§a`, `{name}`, `\\n` etc.).
- Per-file checkpoint (`<output>.partial.json`) → resumable across runs / rate
  limits / Ctrl-C without redoing finished work.
- RateLimitExceeded → bails fast, leaves checkpoint intact for next run.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Tuple

# Make the parent app/ package importable when invoking the script directly.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from app.core.chunking import chunk_pairs  # noqa: E402
from app.core.constants import SYSTEM_INSTRUCTIONS_BASE  # noqa: E402
from app.core.llm_providers import RateLimitExceeded  # noqa: E402
from app.core.token_protection import protect_tokens, restore_tokens  # noqa: E402
from app.core.translation_batch import translate_batch  # noqa: E402

# Field names whose string values should be translated.
TRANSLATABLE_KEYS = frozenset({"text", "name", "title", "description"})

# Predefined relative source/output path pairs for one-shot use.
TARGETS: Dict[str, Tuple[str, str]] = {
    "quest": (
        "config/the_vault/quest/quests.json",
        "config/the_vault/lang/ja_jp/quest/quests.json",
    ),
}

# Vault-style 構造規約:
#   <mod-config>/<rel>                ← 英語オリジナル
#   <mod-config>/lang/<locale>/<rel>  ← 各言語の翻訳
# Vault Hunters / The Vault がこのレイアウトを採用。他 mod でもこのレイアウト
# のものは --mod-config で同じ仕組みで翻訳可能。
VAULT_BASE_DIR = "config/the_vault"  # --vault-all のプリセット用
VAULT_REFERENCE_LOCALE = "fr_fr"


# ---------------------------------------------------------------------------
# JSON tree walk
# ---------------------------------------------------------------------------

def _walk_collect(node: Any, path: tuple) -> Iterable[Tuple[tuple, str]]:
    """Yield (path, value) tuples for every translatable string field."""
    if isinstance(node, dict):
        for k, v in node.items():
            sub_path = path + (k,)
            if k in TRANSLATABLE_KEYS and isinstance(v, str) and v.strip():
                yield (sub_path, v)
            elif isinstance(v, (dict, list)):
                yield from _walk_collect(v, sub_path)
    elif isinstance(node, list):
        for i, v in enumerate(node):
            yield from _walk_collect(v, path + (i,))


def _walk_apply(node: Any, translations: Dict[tuple, str], path: tuple) -> None:
    """Mutate `node` in-place: replace translatable fields with translated text."""
    if isinstance(node, dict):
        for k, v in node.items():
            sub_path = path + (k,)
            if k in TRANSLATABLE_KEYS and isinstance(v, str) and v.strip():
                if sub_path in translations:
                    node[k] = translations[sub_path]
            elif isinstance(v, (dict, list)):
                _walk_apply(v, translations, sub_path)
    elif isinstance(node, list):
        for i, v in enumerate(node):
            _walk_apply(v, translations, path + (i,))


def _path_to_str(path: tuple) -> str:
    return "/".join(str(p) for p in path)


# ---------------------------------------------------------------------------
# Checkpoint helpers
# ---------------------------------------------------------------------------

def _ckpt_path_for(out_path: Path) -> Path:
    return out_path.with_suffix(out_path.suffix + ".partial")


def _load_checkpoint(ckpt_path: Path) -> Dict[str, str]:
    if not ckpt_path.exists():
        return {}
    try:
        return json.loads(ckpt_path.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"  [WARN] checkpoint unreadable ({e}); starting fresh.")
        return {}


def _save_checkpoint(ckpt_path: Path, translations: Dict[str, str]) -> None:
    ckpt_path.parent.mkdir(parents=True, exist_ok=True)
    ckpt_path.write_text(
        json.dumps(translations, ensure_ascii=False, indent=2), encoding="utf-8"
    )


# ---------------------------------------------------------------------------
# Single-file translation
# ---------------------------------------------------------------------------

def translate_file(
    src_path: Path,
    out_path: Path,
    *,
    api_key: str,
    model: str,
    provider: str,
    sleep_between_batches: float,
) -> str:
    """Translate one JSON file. Returns 'ok' / 'rate_limited' / 'partial' / 'skipped'."""
    if not src_path.exists():
        print(f"  [SKIP] source missing: {src_path}")
        return "skipped"
    # Skip if output is already a finalized translation (partial absent).
    ckpt_path = _ckpt_path_for(out_path)
    if out_path.exists() and not ckpt_path.exists():
        print(f"  [SKIP] already translated (no checkpoint, file present): {out_path}")
        return "skipped"

    try:
        data = json.loads(src_path.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"  [SKIP] not JSON or unreadable ({e}): {src_path}")
        return "skipped"

    items_with_path: List[Tuple[tuple, str]] = list(_walk_collect(data, ()))
    if not items_with_path:
        print(f"  [SKIP] no translatable fields: {src_path}")
        return "skipped"

    print(f"  fields: {len(items_with_path)}")

    saved = _load_checkpoint(ckpt_path)
    if saved:
        print(f"  resuming: {len(saved)} already translated")

    prepared: List[Dict[str, Any]] = []
    for idx, (path, value) in enumerate(items_with_path):
        pv, base_map = protect_tokens(value)
        prepared.append({"idx": idx, "path": path, "protected": pv, "base_map": base_map})

    todo: List[Tuple[str, str]] = []
    for entry in prepared:
        if str(entry["idx"]) in saved:
            continue
        todo.append((str(entry["idx"]), entry["protected"]))

    if todo:
        batches = list(chunk_pairs(todo))
        print(f"  remaining: {len(todo)} items in {len(batches)} batches")
        for bidx, batch in enumerate(batches, start=1):
            print(f"  [BATCH {bidx}/{len(batches)}] {len(batch)} items...", flush=True)
            payload = [{"key": k, "value": v} for k, v in batch]
            try:
                out_map, _usage = translate_batch(
                    api_key=api_key,
                    items=payload,
                    model=model,
                    system_instructions=SYSTEM_INSTRUCTIONS_BASE,
                    log_fn=lambda m, _b=bidx: print(f"    [b{_b}] {m}"),
                    provider_name=provider,
                )
            except RateLimitExceeded as e:
                print(f"  [STOP] rate limit detected. Checkpoint saved at {ckpt_path}")
                print(f"         Resume later (5h+) with the same command. cause: {e}")
                return "rate_limited"
            except KeyboardInterrupt:
                print("  [STOP] interrupted; checkpoint saved.")
                return "partial"

            for k, ja in out_map.items():
                saved[k] = ja
            _save_checkpoint(ckpt_path, saved)
            if sleep_between_batches > 0:
                time.sleep(sleep_between_batches)

    # Apply translations and write final output.
    translations: Dict[tuple, str] = {}
    for entry in prepared:
        key = str(entry["idx"])
        if key not in saved:
            continue
        ja = saved[key]
        if entry["base_map"]:
            ja = restore_tokens(ja, entry["base_map"])
        translations[entry["path"]] = ja

    _walk_apply(data, translations, ())
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"  [OK] wrote: {out_path}  ({len(translations)}/{len(items_with_path)} translated)")

    if len(translations) == len(items_with_path):
        try:
            ckpt_path.unlink()
        except Exception:
            pass
        return "ok"
    return "partial"


# ---------------------------------------------------------------------------
# Directory-tree translation
# ---------------------------------------------------------------------------

def translate_directory(
    src_dir: Path,
    out_dir: Path,
    *,
    api_key: str,
    model: str,
    provider: str,
    sleep_between_batches: float,
) -> int:
    if not src_dir.is_dir():
        print(f"[ERROR] source dir not found: {src_dir}")
        return 2

    json_files = sorted(p for p in src_dir.rglob("*.json") if p.is_file())
    # Exclude any leftover *.partial files from prior interrupted runs.
    json_files = [p for p in json_files if not p.name.endswith(".partial")]
    if not json_files:
        print(f"[INFO] no .json files under {src_dir}")
        return 0

    print(f"[INFO] {len(json_files)} JSON files to process under {src_dir}")
    counts: Dict[str, int] = {"ok": 0, "skipped": 0, "partial": 0, "rate_limited": 0}
    for i, src_file in enumerate(json_files, start=1):
        rel = src_file.relative_to(src_dir)
        out_file = out_dir / rel
        print(f"\n[{i}/{len(json_files)}] {rel}")
        result = translate_file(
            src_file,
            out_file,
            api_key=api_key,
            model=model,
            provider=provider,
            sleep_between_batches=sleep_between_batches,
        )
        counts[result] = counts.get(result, 0) + 1
        if result == "rate_limited":
            print("\n[STOP] rate limit hit — stopping directory walk. Re-run later to continue.")
            break

    print("\n=== Directory summary ===")
    for k, v in counts.items():
        print(f"  {k}: {v}")
    return 0 if counts.get("rate_limited", 0) == 0 else 3


# ---------------------------------------------------------------------------
# Vault-style mod config translation (generic)
# ---------------------------------------------------------------------------

def translate_modconfig_locale(
    mod_config_dir: Path,
    target_locale: str,
    reference_locale: str,
    *,
    api_key: str,
    model: str,
    provider: str,
    sleep_between_batches: float,
    dry_run: bool = False,
) -> int:
    """Translate `<mod_config_dir>/lang/<reference_locale>/...` files into
    `<mod_config_dir>/lang/<target_locale>/...` using `<mod_config_dir>/<rel>`
    as the English source.

    This is the generic version of `--vault-all`. Works for any mod whose
    config directory follows the `lang/<locale>/<file>` convention with an
    English original at the same relative path directly under the config dir.
    """
    if not mod_config_dir.is_dir():
        print(f"[ERROR] mod-config directory not found: {mod_config_dir}")
        return 2
    lang_dir = mod_config_dir / "lang"
    ref_dir = lang_dir / reference_locale
    if not ref_dir.is_dir():
        print(f"[ERROR] reference locale not found: {ref_dir}")
        print(f"        Try a different --reference-locale. Available: ", end="")
        if lang_dir.is_dir():
            print(", ".join(sorted(p.name for p in lang_dir.iterdir() if p.is_dir())))
        else:
            print("(no lang/ subdir)")
        return 2

    rel_paths = sorted(
        p.relative_to(ref_dir)
        for p in ref_dir.rglob("*.json")
        if p.is_file() and not p.name.endswith(".partial")
    )
    if not rel_paths:
        print(f"[INFO] no .json files under {ref_dir}")
        return 0

    print(f"[INFO] mod-config : {mod_config_dir}")
    print(f"[INFO] reference  : {reference_locale}  ({len(rel_paths)} files)")
    print(f"[INFO] target     : {target_locale}")

    if dry_run:
        missing = 0
        for rel in rel_paths:
            src_path = mod_config_dir / rel
            out_path = lang_dir / target_locale / rel
            ok = "✓" if src_path.exists() else "✗"
            if not src_path.exists():
                missing += 1
            print(f"  {ok}  {rel}  →  {target_locale}/{rel}")
        if missing:
            print(f"\n[WARN] {missing} files have no English source at {mod_config_dir}/<rel>")
        return 0

    print(f"[INFO] Provider={provider}  Model={model}")
    counts: Dict[str, int] = {"ok": 0, "skipped": 0, "partial": 0, "rate_limited": 0}
    for i, rel in enumerate(rel_paths, start=1):
        src_path = mod_config_dir / rel
        out_path = lang_dir / target_locale / rel
        print(f"\n[{i}/{len(rel_paths)}] {rel}")
        if not src_path.exists():
            print(f"  [SKIP] no English source: {src_path}")
            counts["skipped"] += 1
            continue
        result = translate_file(
            src_path, out_path,
            api_key=api_key, model=model, provider=provider,
            sleep_between_batches=sleep_between_batches,
        )
        counts[result] = counts.get(result, 0) + 1
        if result == "rate_limited":
            print("\n[STOP] レート制限のため中断。再実行で続きから処理されます。")
            break

    print("\n=== mod-config locale summary ===")
    for k, v in counts.items():
        print(f"  {k}: {v}")
    return 0 if counts.get("rate_limited", 0) == 0 else 3


def auto_discover_mod_configs(instance_dir: Path) -> List[Path]:
    """Find all `config/<modid>/` directories that look like Vault-style.

    Heuristic: a directory under `config/` whose `lang/<locale>/` subfolder
    contains at least one .json file. Returns the mod-config directories
    (NOT the lang dirs).
    """
    config_dir = instance_dir / "config"
    if not config_dir.is_dir():
        return []
    found: List[Path] = []
    for mod_dir in sorted(p for p in config_dir.iterdir() if p.is_dir()):
        lang = mod_dir / "lang"
        if not lang.is_dir():
            continue
        # any locale subfolder with .json files?
        for locale in lang.iterdir():
            if locale.is_dir() and any(locale.rglob("*.json")):
                found.append(mod_dir)
                break
    return found


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "instance_dir",
        type=Path,
        nargs="?",
        help="Vault Hunters instance directory (used for --target presets and as base for relative --src/--out)",
    )

    mode = parser.add_argument_group("mode")
    mode.add_argument("--target", choices=tuple(TARGETS.keys()), help="Use a preset source/output pair (default: quest)")
    mode.add_argument("--src", type=Path, help="Single source JSON file")
    mode.add_argument("--out", type=Path, help="Single output JSON file")
    mode.add_argument("--src-dir", type=Path, help="Source directory (recursive)")
    mode.add_argument("--out-dir", type=Path, help="Output directory (mirror of --src-dir)")
    mode.add_argument(
        "--vault-all",
        action="store_true",
        help=f"プリセット: --mod-config {VAULT_BASE_DIR} と等価 (Vault Hunters)",
    )
    mode.add_argument(
        "--mod-config",
        type=Path,
        help=(
            "Vault-style 構造の mod config ディレクトリ。"
            "<mod-config>/<rel> を英語、<mod-config>/lang/<locale>/<rel> を翻訳とみなす。"
            "例: --mod-config config/the_vault"
        ),
    )
    mode.add_argument(
        "--auto-discover",
        action="store_true",
        help="config/ 配下から Vault-style な mod config を自動検出して一覧表示 (--dry-run と組み合わせ推奨)",
    )
    mode.add_argument(
        "--target-locale",
        default="ja_jp",
        help="翻訳の出力ロケール (default: ja_jp)",
    )
    mode.add_argument(
        "--reference-locale",
        default=VAULT_REFERENCE_LOCALE,
        help=f"対象ファイル一覧として使う既存ロケール (default: {VAULT_REFERENCE_LOCALE})",
    )

    parser.add_argument("--model", default="claude-haiku-4-5")
    parser.add_argument("--provider", default="claude_sdk", choices=("claude_sdk", "claude", "gemini"))
    parser.add_argument("--api-key", default="<subscription>")
    parser.add_argument("--sleep", type=float, default=0.4, help="Seconds between batches")
    parser.add_argument("--dry-run", action="store_true", help="Print translatable count and exit")

    args = parser.parse_args()

    # --auto-discover: 候補一覧を表示して終了 (常に dry-run 動作)
    if args.auto_discover:
        if not args.instance_dir:
            parser.error("--auto-discover は instance_dir を必要とします")
        candidates = auto_discover_mod_configs(args.instance_dir)
        if not candidates:
            print(f"[INFO] Vault-style な mod config が見つかりませんでした: {args.instance_dir / 'config'}")
            return 0
        print(f"[INFO] {len(candidates)} 件の Vault-style mod config を検出:")
        for c in candidates:
            rel = c.relative_to(args.instance_dir)
            locales = sorted(p.name for p in (c / "lang").iterdir() if p.is_dir())
            print(f"  {rel}   locales: {', '.join(locales)}")
        print("\n各 mod config を翻訳するには:")
        print(f'  python {sys.argv[0]} "{args.instance_dir}" --mod-config <PATH ABOVE> [--reference-locale fr_fr]')
        return 0

    # --vault-all / --mod-config: Vault-style の locale 翻訳
    mod_config_dir: Path | None = None
    if args.vault_all:
        if not args.instance_dir:
            parser.error("--vault-all は instance_dir を必要とします")
        mod_config_dir = args.instance_dir / VAULT_BASE_DIR
    elif args.mod_config is not None:
        mc = args.mod_config
        if not mc.is_absolute():
            if not args.instance_dir:
                parser.error("相対パスの --mod-config を使うときは instance_dir を指定してください")
            mc = args.instance_dir / mc
        mod_config_dir = mc

    if mod_config_dir is not None:
        return translate_modconfig_locale(
            mod_config_dir,
            target_locale=args.target_locale,
            reference_locale=args.reference_locale,
            api_key=args.api_key, model=args.model, provider=args.provider,
            sleep_between_batches=args.sleep,
            dry_run=args.dry_run,
        )

    if args.src_dir or args.out_dir:
        if not (args.src_dir and args.out_dir):
            parser.error("--src-dir and --out-dir must be used together")
        src_dir = args.src_dir if args.src_dir.is_absolute() else (args.instance_dir or Path.cwd()) / args.src_dir
        out_dir = args.out_dir if args.out_dir.is_absolute() else (args.instance_dir or Path.cwd()) / args.out_dir
        if args.dry_run:
            json_files = sorted(p for p in src_dir.rglob("*.json") if p.is_file() and not p.name.endswith(".partial"))
            print(f"[DRY-RUN] would process {len(json_files)} files under {src_dir}")
            for f in json_files[:10]:
                print(f"  {f.relative_to(src_dir)}")
            if len(json_files) > 10:
                print(f"  ... and {len(json_files) - 10} more")
            return 0
        print(f"[INFO] Provider={args.provider}  Model={args.model}")
        return translate_directory(
            src_dir, out_dir,
            api_key=args.api_key, model=args.model, provider=args.provider,
            sleep_between_batches=args.sleep,
        )

    if args.src or args.out:
        if not (args.src and args.out):
            parser.error("--src and --out must be used together")
        src_path = args.src if args.src.is_absolute() else (args.instance_dir or Path.cwd()) / args.src
        out_path = args.out if args.out.is_absolute() else (args.instance_dir or Path.cwd()) / args.out
    else:
        if not args.instance_dir:
            parser.error("instance_dir is required when using --target preset")
        target = args.target or "quest"
        rel_src, rel_out = TARGETS[target]
        src_path = args.instance_dir / rel_src
        out_path = args.instance_dir / rel_out

    print(f"[INFO] Source : {src_path}")
    print(f"[INFO] Output : {out_path}")
    print(f"[INFO] Provider: {args.provider}, Model: {args.model}")

    if args.dry_run:
        if not src_path.exists():
            print(f"[ERROR] source missing: {src_path}")
            return 2
        data = json.loads(src_path.read_text(encoding="utf-8"))
        items = list(_walk_collect(data, ()))
        print(f"[DRY-RUN] {len(items)} translatable fields")
        for path, value in items[:8]:
            preview = value.replace("\n", "\\n")[:80]
            print(f"  {_path_to_str(path)}: {preview}")
        return 0

    result = translate_file(
        src_path, out_path,
        api_key=args.api_key, model=args.model, provider=args.provider,
        sleep_between_batches=args.sleep,
    )
    return 0 if result in ("ok", "skipped") else 3


if __name__ == "__main__":
    sys.exit(main())
