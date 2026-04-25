# `tools/` — 補助 CLI ツール

GUI アプリ ([../app](../app)) は Mod JAR 内の `assets/<modid>/lang/en_us.json` を翻訳しますが、それでカバーできないニーズが 2 つあります:

1. **Mod の `config/<modid>/lang/<locale>/*.json` 形式** で別管理されているクエストブック等の翻訳
2. **Mod 側に `SUPPORTED_LOCALES` のハードコード** があって `ja_jp` を認識しないケースへのワークアラウンド

これらを補完するスクリプトをここに置いています。両方とも **GUI アプリ本体と同じ `Provider` 抽象** を流用するので、Claude 定額プラン認証 / Anthropic API / Gemini API のいずれでも動きます。

---

## `translate_vault_quests.py`

Vault-style な mod config ディレクトリを再帰的に翻訳します。

### Vault-style 構造とは

```
config/<modid>/<rel>.json              ← 英語オリジナル
config/<modid>/lang/<locale>/<rel>.json ← 各言語の翻訳
```

このレイアウトの代表例が Vault Hunters (`config/the_vault/`)。Mod が起動時に `config/<modid>/lang/<Minecraft 言語>/<rel>.json` を直接読みに行くため、Mod JAR 内の en_us.json には対応するキーが存在しません(GUI アプリでは翻訳できない)。

### モード一覧

| モード | フラグ | 用途 |
|---|---|---|
| **プリセット** | `--target quest` | `config/the_vault/quest/quests.json` のみ翻訳 (デフォルト) |
| **任意ファイル** | `--src <path> --out <path>` | 1 ファイルだけ別ロケーションへ |
| **ディレクトリツリー** | `--src-dir <dir> --out-dir <dir>` | 任意のディレクトリを再帰翻訳、構造をミラー出力 |
| **Vault プリセット** | `--vault-all` | `--mod-config config/the_vault` のショートカット |
| **汎用 mod-config** | `--mod-config <dir>` | 任意の Vault-style mod を翻訳 (英語ソースを `<mod-config>/<rel>` から、出力を `<mod-config>/lang/<target>/<rel>` へ) |
| **自動検出** | `--auto-discover` | `config/<modid>/lang/<locale>/` パターンの mod を一覧表示 |

### 共通フラグ

| フラグ | 既定値 | 説明 |
|---|---|---|
| `--provider` | `claude_sdk` | `claude_sdk` / `claude` / `gemini` |
| `--model` | `claude-haiku-4-5` | Provider に応じたモデル名 |
| `--target-locale` | `ja_jp` | 出力先のロケール |
| `--reference-locale` | `fr_fr` | `--vault-all` / `--mod-config` で対象ファイル一覧として参照する既存ロケール |
| `--api-key` | `<subscription>` | claude_sdk 以外の Provider で必須 |
| `--sleep` | `0.4` | バッチ間の待機秒数 |
| `--dry-run` | — | 翻訳対象数だけ出して終了 |

### 例

```powershell
# 候補一覧を見る
python tools/translate_vault_quests.py "<instance>" --auto-discover

# Vault Hunters クエスト + UI ファイル一式
python tools/translate_vault_quests.py "<instance>" --vault-all

# 別 mod が同パターンを採用していれば
python tools/translate_vault_quests.py "<instance>" --mod-config config/another_mod

# 任意ファイル単発
python tools/translate_vault_quests.py --src en.json --out ja.json
```

### チェックポイント / レジューム

各出力ファイルの隣に `<file>.partial` を作って **バッチ完了ごとに保存**。同じコマンドを再実行すれば自動で続きから処理されます。完全成功時に `.partial` は自動削除。

レート制限 (`RateLimitExceeded`) を検知すると **その場で停止**、ディレクトリ走査も中断します。時間を空けて再実行してください。

### 翻訳対象フィールド

JSON ツリー内の以下のキーの値が翻訳対象になります:

- `text`
- `name`
- `title`
- `description`

それ以外のキー(色コード `$text`、ID 文字列等)は触りません。

---

## `patch_vault_locale.py`

Mod の JAR に **`ja_jp` を `SUPPORTED_LOCALES` のハードコードリストに認識させる** ための 5 バイトバイナリパッチ。

### 何をするか

Java の class ファイルの **定数プールに格納された UTF-8 文字列を 5 バイト→5 バイトでインプレース置換** します。Vault Hunters の `iskallia.vault.config.Config.class` の場合:

```
es_mx (5 byte) → ja_jp (5 byte)
```

`es_mx` は VH の `SUPPORTED_LOCALES` に登録されているのに対応する `lang/es_mx/` ディレクトリが存在しない **phantom locale** なので、置換しても VH の機能は失われません。

### なぜこの手法か

- 同じ長さ → 定数プールのオフセットも他フィールドも変わらない
- 命令列・メソッド本体・コードロジックを **一切触らない**
- 失敗時の影響が最小(class ファイルが破損しない)

### 使い方

```powershell
# instance ディレクトリ指定 (mods/the_vault-*.jar を自動検索)
python tools/patch_vault_locale.py "<instance dir>"

# JAR を直接指定
python tools/patch_vault_locale.py "<path>/the_vault-*.jar"
```

### 動作

1. `<jar>.bak` バックアップ自動生成 (既存なら上書きしない)
2. `iskallia/vault/config/Config.class` を取り出して `es_mx` → `ja_jp` 置換
3. JAR を新しい内容で書き直す
4. 整合性チェック後、元の JAR と置き換え

冪等(2 回実行しても何もしない)、Minecraft が起動中だと JAR ロックでエラー。

### 制約

- **Minecraft / CurseForge を完全終了** してから実行。JAR がロックされていると失敗します
- VH のアップデートで JAR が新しくなったら **再パッチ必要**
- 他 mod に流用するには:
  - `OLD` / `NEW` / `CLASS_PATH` 定数を書き換える
  - 5 バイト同長の置換可能な文字列を見つける必要

### ロールバック

```powershell
Move-Item -Force "<mods>/the_vault-*.jar.bak" "<mods>/the_vault-*.jar"
```

---

## トラブルシューティング

| 症状 | 対処 |
|---|---|
| `claude-agent-sdk が見つかりません` | `pip install claude-agent-sdk` を実行、その後 `claude login` |
| `RateLimitExceeded` で中断 | 時間を空けて (5 時間〜) 同じコマンドを再実行 (チェックポイントから続き) |
| `アクセスが拒否されました` (patch_vault_locale.py) | Minecraft / CurseForge を完全終了。タスクマネージャで `javaw.exe` / `Minecraft.exe` が残っていないか確認 |
| パッチ後に Minecraft 起動エラー | `<jar>.bak` を `<jar>` にリネームして元に戻す |
| `translate_vault_quests.py` で `[SKIP] no English source` | 英語オリジナルが `<mod-config>/<rel>` に存在しない。手動で英語ファイルを用意するか、別ロケールをソースに使う |
| `--vault-all` で 0 file processed | 参照ロケール (デフォルト `fr_fr`) が存在しない。`--reference-locale` で別ロケールを指定 |
