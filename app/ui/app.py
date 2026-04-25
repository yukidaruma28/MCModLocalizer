from __future__ import annotations

import base64
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import traceback
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from xml.sax import saxutils

import flet as ft


from ..core.app_logging import (
    LOG_FILENAME,
    configure_file_logging,
    log_gui_line,
    log_startup_context,
    redirect_print,
)
from ..core.llm_providers import RateLimitExceeded
from ..core.usage import UsageStats
from ..services import ExtractionResult, extract_localizations, translate_localizations

APP_NAME = "MCModLocalizer"
BASE_DIR = Path(__file__).resolve().parent.parent
RESOURCE_TEMPLATE_DIR = BASE_DIR / "a"

try:
    import keyring  # type: ignore
except Exception:
    keyring = None


@dataclass
class TranslationSummary:
    translated_mods: int
    total_mods: int
    translated_entries: int
    total_entries: int
    aborted: bool
    had_error: bool
    pack_dir: Path | None
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    model: str = ""
    usage_records: list[tuple[int, int, int]] = field(default_factory=list)
    rate_limited: bool = False


class LocalizeApp:
    def __init__(self, page: ft.Page):
        self.page = page
        self.stop_event = threading.Event()
        self._log_lines: list[str] = []
        self._max_log_lines = 500
        # 保存キー
        self.K_MODEL = "openai_model"
        self.K_DIR_MODS = "dir_mods_root"
        self.K_DIR_OUTPUT = "dir_output_pack"
        self.K_LAST_MODS_PATH = "last_mods_dir_path"
        self.K_LAST_OUTPUT_PATH = "last_output_dir_path"
        self.K_USAGE_HISTORY = "token_usage_history"
        self.K_USAGE_TOTAL_COST = "token_usage_total_cost"
        self.K_USAGE_HISTORY = "token_usage_history"
        self.K_USAGE_TOTAL_COST = "token_usage_total_cost"
        self.K_USAGE_TOTAL_STATS = "token_usage_total_stats"
        # API Keys
        self.K_KEY_GEMINI = "GEMINI_API_KEY"
        self.K_KEY_CLAUDE = "ANTHROPIC_API_KEY"
        self.K_PROVIDER = "llm_provider"
        # claude_sdk = Claude Agent SDK 経由で Claude Code (Pro/Max) のログイン認証を流用するモード。
        # API キー不要だが Claude Code CLI のインストール+ログインが前提。
        self.providers_without_api_key = {"claude_sdk"}
        # 既定値
        self.default_provider = "gemini"
        self.provider_choices = (
            ("gemini", "Gemini"),
            ("claude", "Claude (API)"),
            ("claude_sdk", "Claude (定額/Code SDK)"),
        )
        self.default_model_by_provider = {
            "gemini": "gemini-2.5-flash-lite",
            "claude": "claude-haiku-4-5",
            "claude_sdk": "claude-haiku-4-5",
        }
        self.model_pricing = {}
        self.models_by_provider: dict[str, list[str]] = {
            "gemini": [],
            "claude": [],
            "claude_sdk": [],
        }
        self.available_models = []
        self.pricing_version = "-"
        self._load_model_pricing()
        saved_provider = self._load_value(self.K_PROVIDER)
        if saved_provider not in {p for p, _ in self.provider_choices}:
            saved_provider = self.default_provider
        self.current_provider = saved_provider
        self.available_models = self.models_by_provider.get(self.current_provider, [])
        # Default model (provider-aware)
        self.default_model = self.default_model_by_provider.get(
            self.current_provider, "gemini-2.5-flash-lite"
        )
        # -------------- UI 構築 --------------
        page.title = f"{APP_NAME} (Flet)"
        page.padding = 16
        page.window_width = 1000
        page.window_height = 820
        page.theme_mode = "light"
        # ログ & 進捗
        self.log_view = ft.ListView(
            expand=True,
            spacing=2,
            auto_scroll=True,
        )
        self.log_container = ft.Container(
            content=self.log_view,
            expand=True,
            border=ft.border.all(1, ft.Colors.OUTLINE),
            border_radius=4,
            padding=5,
        )
        self.chk_auto_scroll = ft.Checkbox(
            label="自動スクロール",
            value=True,
            on_change=self._on_change_auto_scroll,
        )
        self.btn_copy_log = ft.IconButton(
            icon=ft.Icons.COPY,
            tooltip="ログをクリップボードにコピー",
            on_click=self._on_click_copy_log
        )
        self.progress = ft.ProgressBar(width=420, value=0)
        self.counter = ft.Text("待機中")
        self.detail_progress = ft.ProgressBar(width=420, value=0)
        self.detail_counter = ft.Text("")
        # -------- 抽出タブ UI --------
        self.mods_dir_path = ft.TextField(
            label="Mods フォルダ（必須）",
            dense=True,
            expand=True,
            read_only=True,
            value=self._load_value(self.K_LAST_MODS_PATH) or "",
        )
        self.output_dir = ft.TextField(
            label="出力フォルダ（リソースパック保存先）",
            dense=True,
            expand=True,
            read_only=True,
            value=self._load_value(self.K_LAST_OUTPUT_PATH) or "",
        )
        self.fp_mods = ft.FilePicker(on_result=self._on_pick_mods_dir)
        self.fp_dir = ft.FilePicker(on_result=self._on_pick_dir)
        self.page.overlay.extend([self.fp_mods, self.fp_dir])
        pick_mods_btn = ft.ElevatedButton(
            "modsフォルダを選択",
            icon=ft.Icons.FOLDER_OPEN,
            tooltip="Mods フォルダを選択",
            on_click=self._open_mods_picker,
        )
        pick_dir_btn = ft.ElevatedButton(
            "リソースパックフォルダを選択",
            icon=ft.Icons.FOLDER_OPEN,
            tooltip="出力フォルダを選択",
            on_click=self._open_output_dir_picker,
        )
        self.btn_extract = ft.ElevatedButton("抽出 / リソースパック生成", icon=ft.Icons.DOWNLOAD, on_click=self.on_extract)
        self.btn_stop = ft.OutlinedButton("停止", icon=ft.Icons.STOP, on_click=self.on_stop, disabled=True)
        self.progress_panel = ft.Column(
            visible=False,
            controls=[
                ft.Row(
                    [self.progress, self.counter],
                    spacing=12,
                    alignment=ft.MainAxisAlignment.START,
                    vertical_alignment=ft.CrossAxisAlignment.CENTER,
                ),
                ft.Row(
                    [self.detail_progress, self.detail_counter],
                    spacing=12,
                    alignment=ft.MainAxisAlignment.START,
                    vertical_alignment=ft.CrossAxisAlignment.CENTER,
                ),
            ],
            spacing=8,
        )
        extract_tab = ft.Column(
            controls=[
                ft.Text("ステップ: JAR から en_us.json を抽出し、ja_jp.json まで自動生成します。", weight=ft.FontWeight.BOLD),
                ft.Row([self.mods_dir_path, pick_mods_btn], alignment=ft.MainAxisAlignment.START),
                ft.Row([self.output_dir, pick_dir_btn], alignment=ft.MainAxisAlignment.START),
                ft.Row(
                    [self.btn_extract, self.btn_stop],
                    spacing=16,
                    alignment=ft.MainAxisAlignment.START,
                    vertical_alignment=ft.CrossAxisAlignment.CENTER,
                ),
                self.progress_panel,
                self.progress_panel,
                ft.Row(
                    [ft.Text("ログ"), ft.Container(expand=True), self.chk_auto_scroll, self.btn_copy_log],
                    alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
                    vertical_alignment=ft.CrossAxisAlignment.CENTER,
                ),
                self.log_container,
            ],
            expand=True,
            spacing=12,
        )
        # -------- 設定タブ UI --------
        saved_model = self._load_value(self.K_MODEL) or self.default_model
        # Coerce saved model to current provider's model list
        if saved_model not in self.available_models:
            saved_model = self.default_model

        self.btn_config_api_key = ft.ElevatedButton("APIキー再設定", icon=ft.Icons.KEY, on_click=self._open_api_key_dialog)

        self.model_pricing_table = ft.DataTable(
            columns=[
                ft.DataColumn(ft.Text("Model")),
                ft.DataColumn(ft.Text("Input ($/1M tokens)")),
                ft.DataColumn(ft.Text("Cached input ($/1M tokens)")),
                ft.DataColumn(ft.Text("Output ($/1M tokens)")),
            ],
            rows=[],
        )
        self._refresh_pricing_table_ui()

        self.provider_field = ft.Dropdown(
            label="Provider",
            value=self.current_provider,
            options=[ft.dropdown.Option(key=k, text=label) for k, label in self.provider_choices],
            dense=True, expand=False, width=140,
            on_change=self._on_provider_change,
        )

        self.model_field = ft.Dropdown(
            label="モデル",
            value=saved_model,
            options=[ft.dropdown.Option(m) for m in self.available_models],
            dense=True, expand=False, width=220,
            on_change=self._on_model_change,
        )

        settings_tab = ft.Column(
            controls=[
                ft.Text("API 設定", weight=ft.FontWeight.BOLD),
                ft.Row([self.provider_field, self.model_field, self.btn_config_api_key], spacing=12),
                ft.Text("※APIキーは keyring を使用してシステムに安全に保存されます。"),
                ft.Row([
                    ft.Text("料金テーブル (USD, 1M トークンあたり)", weight=ft.FontWeight.BOLD),
                    ft.Text(f"最終更新: {self.pricing_version}")
                ], alignment=ft.MainAxisAlignment.SPACE_BETWEEN, vertical_alignment=ft.CrossAxisAlignment.END),
                self.model_pricing_table,
                ft.Divider(),
                ft.Text("デバッグ・メンテナンス", weight=ft.FontWeight.BOLD),
                ft.ElevatedButton("アプリを初期化する（設定リセット）", color=ft.Colors.ERROR, on_click=self._on_click_debug_reset),
            ],
            expand=True,
            spacing=12,
        )

        self.usage_history: list[dict[str, object]] = self._load_usage_history()
        self.total_usage: UsageStats = self._load_total_stats()
        self.token_usage_summary = ft.Text("まだ翻訳の実行履歴がありません。")
        self.token_usage_prompt_text = ft.Text(f"入力トークン: {self.total_usage.prompt_tokens}")
        self.token_usage_completion_text = ft.Text(f"出力トークン: {self.total_usage.completion_tokens}")
        self.token_usage_total_text = ft.Text(f"合計トークン: {self.total_usage.total_tokens}")
        self.total_cost = self._load_total_cost()
        self.token_usage_cost_text = ft.Text(f"概算コスト累計: ${self.total_cost:.3f}")
        self.token_usage_updated_text = ft.Text("更新時刻: -")
        history_rows = self._generate_history_rows()
        self.token_usage_history_table = ft.DataTable(
            columns=[
                ft.DataColumn(ft.Text("日時")),
                ft.DataColumn(ft.Text("モデル")),
                ft.DataColumn(ft.Text("入力トークン")),
                ft.DataColumn(ft.Text("出力トークン")),
                ft.DataColumn(ft.Text("概算コスト")),
            ],
            rows=history_rows,
            width=None,
        )
        token_tab = ft.Column(
            controls=[
                ft.Text("API のトークン使用量を確認します。", weight=ft.FontWeight.BOLD),
                self.token_usage_summary,
                self.token_usage_prompt_text,
                self.token_usage_completion_text,
                self.token_usage_total_text,
                self.token_usage_cost_text,
                self.token_usage_updated_text,
                ft.Text("API 利用履歴", weight=ft.FontWeight.BOLD),
                ft.Container(
                    content=ft.Column(
                        controls=[self.token_usage_history_table],
                        tight=True,
                        scroll=ft.ScrollMode.AUTO,
                    ),
                    expand=True,
                    width=float("inf"),
                    height=240,
                    border=ft.border.all(1, ft.Colors.TRANSPARENT),
                    clip_behavior=ft.ClipBehavior.HARD_EDGE,
                ),
            ],
            expand=True,
            spacing=12,
        )
        # -------- Tabs --------
        self.tabs = ft.Tabs(
            selected_index=0,
            tabs=[
                ft.Tab(text="抽出", icon=ft.Icons.DOWNLOAD, content=extract_tab),
                ft.Tab(text="トークン", icon=ft.Icons.ASSESSMENT, content=token_tab),
                ft.Tab(text="設定", icon=ft.Icons.SETTINGS, content=settings_tab),
            ],
            expand=True,
        )
        page.add(self.tabs)

        # ファイルログを初期化(出力フォルダが指定されていればそこに、なければ ~/.mcmodlocalizer/ に)
        try:
            initial_out = (self.output_dir.value or "").strip()
            initial_log_dir = Path(initial_out) if initial_out else None
            log_path = configure_file_logging(initial_log_dir)
            redirect_print()
            log_startup_context(
                provider=self.current_provider,
                model=self.default_model,
            )
            self._append_log(f"準備完了。ログファイル: {log_path}")
        except Exception as ex:
            print(f"[WARN] ファイルログ初期化失敗: {ex}")
            self._append_log("準備完了。Mods フォルダと出力フォルダを指定して抽出を実行するとリソースパックを自動生成します。")

        if not self._load_api_key(self.current_provider):
            self._open_api_key_dialog()

    def _load_model_pricing(self):
        defaults = {
            "gemini-2.5-flash": {"input": 0.30, "cached_input": 0.03, "output": 2.50},
            "gemini-2.5-flash-lite": {"input": 0.10, "cached_input": 0.01, "output": 0.40},
            "claude-haiku-4-5": {"input": 1.00, "cached_input": 0.10, "output": 5.00},
            "claude-sonnet-4-6": {"input": 3.00, "cached_input": 0.30, "output": 15.00},
            "claude-opus-4-7": {"input": 15.00, "cached_input": 1.50, "output": 75.00},
        }
        try:
            path = self._get_bundled_asset_path("pricing.json")
            if path.exists():
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    if "models" in data:
                        self.model_pricing = data["models"]
                        self.pricing_version = data.get("version", "-")
                    else:
                         self.model_pricing = defaults
            else:
                self.model_pricing = defaults
        except Exception as e:
            print(f"Failed to load pricing.json: {e}")
            self.model_pricing = defaults

        gemini_models: list[str] = []
        claude_models: list[str] = []
        for name in self.model_pricing.keys():
            if name.startswith("claude-"):
                claude_models.append(name)
            else:
                gemini_models.append(name)
        self.models_by_provider = {
            "gemini": gemini_models,
            "claude": claude_models,
            "claude_sdk": list(claude_models),
        }
        # available_models is filled to the active provider after current_provider is set
        self.available_models = list(self.model_pricing.keys())

    def _provider_for_model(self, model_name: str) -> str:
        # 名前だけでは claude / claude_sdk の区別が付かないため、現在のプロバイダ設定を尊重。
        if (model_name or "").startswith("claude-"):
            current = getattr(self, "current_provider", None)
            if current in ("claude", "claude_sdk"):
                return current
            return "claude"
        return "gemini"

    def _refresh_pricing_table_ui(self):
        self.model_pricing_table.rows = [
            ft.DataRow(
                cells=[
                    ft.DataCell(ft.Text(model)),
                    ft.DataCell(ft.Text(f"${rates['input']:.3f}")),
                    ft.DataCell(ft.Text(f"${rates['cached_input']:.3f}")),
                    ft.DataCell(ft.Text(f"${rates['output']:.3f}")),
                ]
            )
            for model, rates in self.model_pricing.items()
        ]
        if self.model_pricing_table.page:
            self.model_pricing_table.update()

    # ------------------------------
    # FilePicker launchers
    # ------------------------------
    def _open_mods_picker(self, e: ft.ControlEvent):
        init_dir = self._get_initial_directory(self.K_DIR_MODS)
        self.fp_mods.get_directory_path(initial_directory=init_dir)

    def _open_output_dir_picker(self, e: ft.ControlEvent):
        init_dir = self._get_initial_directory(self.K_DIR_OUTPUT)
        self.fp_dir.get_directory_path(initial_directory=init_dir)

    # ------------------------------
    # FilePicker handlers
    # ------------------------------
    def _on_pick_mods_dir(self, e: ft.FilePickerResultEvent):
        if e.path:
            selected = Path(e.path)
            self.mods_dir_path.value = str(selected)
            self.mods_dir_path.update()
            self._save_value(self.K_LAST_MODS_PATH, str(selected))
            self._remember_dir(self.K_DIR_MODS, selected)
            self._auto_set_output_dir(selected)

    def _on_pick_dir(self, e: ft.FilePickerResultEvent):
        if e.path:
            selected = Path(e.path)
            self.output_dir.value = str(selected)
            self.output_dir.update()
            self._save_value(self.K_LAST_OUTPUT_PATH, str(selected))
            self._remember_dir(self.K_DIR_OUTPUT, selected)
            self._refresh_log_file_destination()

    # ------------------------------
    # Settings
    # ------------------------------
    def _load_value(self, key: str) -> str | None:
        return self.page.client_storage.get(key)

    def _save_value(self, key: str, value: str):
        self.page.client_storage.set(key, value)

    def _remember_dir(self, key: str, path: Path | None):
        if not path:
            return
        dir_path = path if path.is_dir() else path.parent
        if not str(dir_path):
            return
        try:
            dir_path = dir_path.resolve()
        except Exception:
            dir_path = dir_path.absolute()
        self._save_value(key, str(dir_path))

    def _auto_set_output_dir(self, source_path: Path):
        try:
            source_path = source_path.resolve()
        except Exception:
            source_path = source_path.absolute()
        target_root: Path | None = None
        if source_path.is_dir() and source_path.name.lower() == "mods":
            target_root = source_path.parent
        else:
            for parent in source_path.parents:
                if parent.name.lower() == "mods":
                    target_root = parent.parent
                    break
        if not target_root:
            return
        candidate_dir: Path | None = None
        candidate_names = [
            "resourcepacks",
            "resource_packs",
            "resourcepack",
            "resource",
        ]
        for name in candidate_names:
            candidate = target_root / name
            if candidate.exists() and candidate.is_dir():
                candidate_dir = candidate
                break
        if candidate_dir is None:
            candidate_dir = target_root / "resourcepacks"
        self.output_dir.value = str(candidate_dir)
        self.output_dir.update()
        self._save_value(self.K_LAST_OUTPUT_PATH, str(candidate_dir))
        self._remember_dir(self.K_DIR_OUTPUT, candidate_dir)
        self._append_log(
            f"[INFO] リソースパックフォルダを自動設定しました: {candidate_dir}"
        )
        self._refresh_log_file_destination()

    def _get_initial_directory(self, key: str) -> str | None:
        stored = self._load_value(key)
        if not stored:
            return None
        p = Path(stored)
        while True:
            if p.exists():
                return str(p)
            if p.parent == p:
                break
            p = p.parent
        return None

    def _keyring_name_for(self, provider: str | None) -> str:
        prov = provider or getattr(self, "current_provider", self.default_provider)
        # claude_sdk は API キーを使わないが、ダイアログ等での参照防止に Anthropic 側へ寄せる。
        return self.K_KEY_CLAUDE if prov in ("claude", "claude_sdk") else self.K_KEY_GEMINI

    def _provider_requires_api_key(self, provider: str | None = None) -> bool:
        prov = provider or getattr(self, "current_provider", self.default_provider)
        return prov not in self.providers_without_api_key

    def _load_api_key(self, provider: str | None = None) -> str | None:
        if not self._provider_requires_api_key(provider):
            # SDK 経由は Claude Code のログインを使うため、ダミーの非空文字列を返して
            # 既存の truthy チェックを通過させる。実際のプロバイダ実装はこの値を無視する。
            return "<claude-code-subscription>"
        key_name = self._keyring_name_for(provider)
        if keyring:
            try:
                v = keyring.get_password(APP_NAME, key_name)
                if v:
                    return v
            except Exception:
                pass
        return None

    def _save_api_key(self, value: str, provider: str | None = None):
        key_name = self._keyring_name_for(provider)
        if keyring:
            try:
                keyring.set_password(APP_NAME, key_name, value)
                return
            except Exception as e:
                self._append_log(f"[ERROR] keyring への保存に失敗しました: {e}")
        else:
             self._append_log("[ERROR] keyring モジュールが利用できないため、APIキーを保存できません。")

    def _on_model_change(self, e: ft.ControlEvent):
        value = (self.model_field.value or "").strip()
        if value not in self.available_models:
            return
        self._save_value(self.K_MODEL, value)
        self._append_log(f"[INFO] モデル選択を更新しました: {value}")

    def _on_provider_change(self, e: ft.ControlEvent):
        value = (self.provider_field.value or "").strip()
        valid = {k for k, _ in self.provider_choices}
        if value not in valid:
            return
        self.current_provider = value
        self._save_value(self.K_PROVIDER, value)
        self.available_models = self.models_by_provider.get(value, [])
        new_default = self.default_model_by_provider.get(value)
        if new_default and new_default in self.available_models:
            self.default_model = new_default
        elif self.available_models:
            self.default_model = self.available_models[0]
        # Rebuild model dropdown options
        self.model_field.options = [ft.dropdown.Option(m) for m in self.available_models]
        if self.model_field.value not in self.available_models:
            self.model_field.value = self.default_model
            self._save_value(self.K_MODEL, self.default_model)
        if self.model_field.page:
            self.model_field.update()
        self._append_log(f"[INFO] Provider を切替えました: {value}")
        if not self._provider_requires_api_key(value):
            self._append_log(
                "[INFO] 定額プランモードでは Claude Code CLI のログイン認証を使用します。API キーは不要です。"
            )
        elif not self._load_api_key(value):
            self._append_log(f"[WARN] {value} の API キーが未設定です。設定タブの「APIキー再設定」から登録してください。")

    def _load_usage_history(self) -> list[dict[str, object]]:
        raw = self._load_value(self.K_USAGE_HISTORY)
        if not raw:
            return []
        try:
            data = json.loads(raw)
        except Exception:
            self._append_log("[WARN] トークン使用履歴の読み込みに失敗しました。データを破棄します。")
            return []
        if not isinstance(data, list):
            return []
        history: list[dict[str, object]] = []
        for item in data:
            if not isinstance(item, dict):
                continue
            ts = str(item.get("timestamp", ""))
            model = str(item.get("model", ""))
            prompt = item.get("prompt", 0)
            completion = item.get("completion", 0)
            total = item.get("total", 0)
            cost = item.get("cost", 0.0)
            try:
                prompt_i = int(prompt)
            except Exception:
                prompt_i = 0
            try:
                completion_i = int(completion)
            except Exception:
                completion_i = 0
            try:
                total_i = int(total) if total else prompt_i + completion_i
            except Exception:
                total_i = prompt_i + completion_i
            try:
                cost_f = float(cost)
            except Exception:
                cost_f = 0.0
            history.append(
                {
                    "timestamp": ts,
                    "model": model,
                    "prompt": prompt_i,
                    "completion": completion_i,
                    "total": total_i,
                    "cost": cost_f,
                }
            )
        return history

    def _load_total_cost(self) -> float:
        raw = self._load_value(self.K_USAGE_TOTAL_COST)
        if not raw:
            return 0.0
        try:
            return float(raw)
        except Exception:
            return 0.0

    def _load_total_stats(self) -> UsageStats:
        raw = self._load_value(self.K_USAGE_TOTAL_STATS)
        if raw:
            try:
                data = json.loads(raw)
                return UsageStats(
                    prompt_tokens=int(data.get("prompt_tokens", 0)),
                    completion_tokens=int(data.get("completion_tokens", 0)),
                    total_tokens=int(data.get("total_tokens", 0)),
                )
            except Exception:
                pass
        
        # Fallback: calculate from history if no saved stats found
        stats = UsageStats()
        for record in self.usage_history:
            stats.prompt_tokens += int(record.get("prompt", 0))
            stats.completion_tokens += int(record.get("completion", 0))
            stats.total_tokens += int(record.get("total", 0))
        return stats

    def _persist_usage_history(self) -> None:
        try:
            payload = json.dumps(self.usage_history, ensure_ascii=False)
            self._save_value(self.K_USAGE_HISTORY, payload)
        except Exception as ex:
            self._append_log(f"[WARN] トークン使用履歴の保存に失敗しました: {repr(ex)}")

    def _persist_total_cost(self) -> None:
        try:
            self._save_value(self.K_USAGE_TOTAL_COST, f"{self.total_cost:.6f}")
        except Exception as ex:
            self._append_log(f"[WARN] トークン累計コストの保存に失敗しました: {repr(ex)}")

    def _persist_total_stats(self) -> None:
        try:
            data = {
                "prompt_tokens": self.total_usage.prompt_tokens,
                "completion_tokens": self.total_usage.completion_tokens,
                "total_tokens": self.total_usage.total_tokens,
            }
            self._save_value(self.K_USAGE_TOTAL_STATS, json.dumps(data))
        except Exception as ex:
            self._append_log(f"[WARN] トークン累計使用量の保存に失敗しました: {repr(ex)}")

    def _generate_history_rows(self) -> list[ft.DataRow]:
        grouped: dict[tuple[str, str], dict] = {}
        for record in self.usage_history:
            ts = str(record.get("timestamp", "-"))
            model = str(record.get("model", "-"))
            key = (ts, model)
            
            p = int(record.get("prompt", 0))
            c = int(record.get("completion", 0))
            cost = float(record.get("cost", 0.0))
            
            if key not in grouped:
                grouped[key] = {
                    "timestamp": ts,
                    "model": model,
                    "prompt": 0,
                    "completion": 0,
                    "cost": 0.0,
                }
            
            grouped[key]["prompt"] += p
            grouped[key]["completion"] += c
            grouped[key]["cost"] += cost
            
        return [
            ft.DataRow(
                cells=[
                    ft.DataCell(ft.Text(data["timestamp"])),
                    ft.DataCell(ft.Text(data["model"])),
                    ft.DataCell(ft.Text(str(data["prompt"]))),
                    ft.DataCell(ft.Text(str(data["completion"]))),
                    ft.DataCell(ft.Text(f"${data['cost']:.3f}")),
                ]
            )
            for data in grouped.values()
        ]

    def _refresh_usage_history_table(self) -> None:
        self.token_usage_history_table.rows = self._generate_history_rows()
        self.token_usage_history_table.update()

    def _open_api_key_dialog(self, e=None):
        try:

            def close_dlg(e):
                dlg.open = False
                self.page.update()

            def save_dlg(e):
                val_gemini = key_field_gemini.value.strip()
                val_claude = key_field_claude.value.strip()
                saved_any = False
                if val_gemini:
                    self._save_api_key(val_gemini, provider="gemini")
                    saved_any = True
                if val_claude:
                    self._save_api_key(val_claude, provider="claude")
                    saved_any = True
                if saved_any:
                    self._append_log("[OK] 設定された API Key を keyring に保存しました。")
                else:
                    self._append_log("[INFO] 入力欄が空欄のため、API Key は変更されませんでした。")
                dlg.open = False
                self.page.update()

            # 現在設定されているかどうかだけ確認（セキュリティのため値は表示しない）
            has_gemini = bool(self._load_api_key("gemini"))
            has_claude = bool(self._load_api_key("claude"))

            key_field_gemini = ft.TextField(
                label="Gemini API Key (設定済み)" if has_gemini else "Gemini API Key",
                password=True,
                can_reveal_password=True,
                value="",
                hint_text="設定済み (変更しない場合は空欄)" if has_gemini else "未設定",
                expand=True,
            )
            key_field_claude = ft.TextField(
                label="Claude (Anthropic) API Key (設定済み)" if has_claude else "Claude (Anthropic) API Key",
                password=True,
                can_reveal_password=True,
                value="",
                hint_text="設定済み (変更しない場合は空欄)" if has_claude else "未設定",
                expand=True,
            )

            dlg = ft.AlertDialog(
                title=ft.Text("API Key 設定"),
                content=ft.Column([
                    ft.Text("使用するプロバイダの API Key を設定してください。空欄は保存をスキップします。"),
                    ft.Markdown(
                        "Gemini: [Google AI Studio](https://aistudio.google.com/app/apikey)  /  "
                        "Claude: [Anthropic Console](https://console.anthropic.com/settings/keys)",
                        on_tap_link=lambda e: self.page.launch_url(e.data),
                    ),
                    key_field_gemini,
                    key_field_claude,
                    ft.Text("※ keyring は OS の資格情報マネージャーを使用します。", size=12, color=ft.Colors.GREY),
                ], tight=True, width=520),
                actions=[
                    ft.TextButton("キャンセル", on_click=close_dlg),
                    ft.ElevatedButton("保存", on_click=save_dlg),
                ],
                actions_alignment=ft.MainAxisAlignment.END,
            )

            if hasattr(self.page, "open"):
                self.page.open(dlg)
            else:
                self.page.dialog = dlg
                dlg.open = True
                self.page.update()

        except Exception as ex:
            self._append_log(f"[ERROR] ダイアログの表示に失敗しました: {repr(ex)}")
            import traceback
            traceback.print_exc()

    def _on_click_debug_reset(self, e: ft.ControlEvent):
        def reset_confirmed(e):
            # API Key 削除
            if keyring:
                for key_name in (self.K_KEY_GEMINI, self.K_KEY_CLAUDE):
                    try:
                        keyring.delete_password(APP_NAME, key_name)
                    except Exception:
                        pass
                self._append_log("[INFO] API Key を削除しました。")
            
            # Client Storage クリア
            try:
                self.page.client_storage.clear()
                self._append_log("[INFO] アプリ設定(client_storage)をクリアしました。")
            except Exception as ex:
                self._append_log(f"[ERROR] 設定クリア失敗: {ex}")
            
            # ダイアログを閉じる
            dlg.open = False
            self.page.update()
            
            # 完了通知
            self._show_completion_toast("初期化が完了しました。アプリを再起動してください。")


        dlg = ft.AlertDialog(
            title=ft.Text("初期化の確認"),
            content=ft.Text("すべての設定と履歴を削除します。よろしいですか？\n(APIキー、フォルダ履歴、トークン使用履歴などが消去されます)"),
            actions=[
                ft.TextButton("キャンセル", on_click=lambda e: setattr(dlg, 'open', False) or self.page.update()),
                ft.TextButton("初期化する", on_click=reset_confirmed, style=ft.ButtonStyle(color=ft.Colors.ERROR)),
            ],
            actions_alignment=ft.MainAxisAlignment.END,
        )
        if hasattr(self.page, "open"):
            self.page.open(dlg)
        else:
            self.page.dialog = dlg
            dlg.open = True
            self.page.update()

    # ------------------------------
    # Log & Progress
    # ------------------------------
    def _on_change_auto_scroll(self, e: ft.ControlEvent):
        self.log_view.auto_scroll = e.control.value
        self.log_view.update()

    def _on_click_copy_log(self, e: ft.ControlEvent):
        if not self._log_lines:
            return
        text = "\n".join(self._log_lines)
        self.page.set_clipboard(text)
        self._show_completion_toast("ログをクリップボードにコピーしました")

    def _append_log(self, msg: str):
        timestamp = datetime.now().strftime("%H:%M:%S")
        raw_lines = msg.splitlines() or [msg]
        indent = " " * (len(timestamp) + 3)

        new_controls = []
        for idx, raw in enumerate(raw_lines):
            content = raw.strip() if raw.strip() else raw
            line = f"[{timestamp}] {content}" if idx == 0 else f"{indent}{content}"
            self._log_lines.append(line)
            new_controls.append(ft.Text(line, selectable=True, font_family="Consolas,monospace"))
            # Mirror into rotating log file. Severity is inferred from [ERROR]/[WARN]/[INFO] markers.
            try:
                log_gui_line(content)
            except Exception:
                pass

        if len(self._log_lines) > self._max_log_lines:
            excess = len(self._log_lines) - self._max_log_lines
            self._log_lines = self._log_lines[-self._max_log_lines :]
            for _ in range(excess):
                if self.log_view.controls:
                    self.log_view.controls.pop(0)

        self.log_view.controls.extend(new_controls)
        self.log_view.update()

    def _refresh_log_file_destination(self) -> None:
        """Re-attach the rotating file handler under the current output folder.

        Called when the user picks an output folder so the log lives next to
        the resource pack instead of the home-directory fallback.
        """
        out_value = (self.output_dir.value or "").strip() if hasattr(self, "output_dir") else ""
        target_dir: Path | None = None
        if out_value:
            try:
                target_dir = Path(out_value)
            except Exception:
                target_dir = None
        try:
            log_path = configure_file_logging(target_dir)
            log_gui_line(f"[INFO] ログファイル: {log_path}")
        except Exception as ex:
            print(f"[WARN] ログファイルの初期化に失敗しました: {ex}")

    def _set_progress(self, ratio: float, text: str = ""):
        self.progress.value = max(0.0, min(1.0, ratio))
        self.progress.update()
        self.counter.value = text or ""
        self.counter.update()

    def _set_detail_progress(self, ratio: float, text: str = ""):
        self.detail_progress.value = max(0.0, min(1.0, ratio))
        self.detail_progress.update()
        self.detail_counter.value = text or ""
        self.detail_counter.update()

    @staticmethod
    def _parse_fraction(text: str) -> tuple[int, int] | None:
        if "/" not in text:
            return None
        left, right = text.split("/", 1)
        try:
            return int(left.strip()), int(right.strip())
        except ValueError:
            return None

    def _update_extraction_progress(self, ratio: float, text: str):
        parsed = self._parse_fraction(text)
        if parsed:
            done, total = parsed
            label = f"抽出中: {done}件完了（全{total}件）"
        else:
            percent = int(max(0.0, min(1.0, ratio)) * 100)
            label = f"抽出中: {percent}%"
        self._set_progress(ratio, label)

    def _update_token_usage_ui(self, summary: TranslationSummary):
        pricing = self.model_pricing.get(summary.model)
        last_cost_total = 0.0
        if summary.usage_records:
            for prompt, completion, total in summary.usage_records:
                cost = 0.0
                if pricing:
                    cost += pricing["input"] * prompt / 1_000_000
                    cost += pricing["output"] * completion / 1_000_000
                last_cost_total += cost
                self.total_cost += cost
                record = {
                    "timestamp": datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                    "model": summary.model or "(不明)",
                    "prompt": prompt,
                    "completion": completion,
                    "total": total,
                    "cost": cost,
                }
                self.usage_history.append(record)
            # keep latest 200 entries to avoid unbounded growth
            if len(self.usage_history) > 200:
                self.usage_history = self.usage_history[-200:]
            self._persist_usage_history()
            self._persist_total_cost()

            # Update total usage stats
            self.total_usage.prompt_tokens += summary.prompt_tokens
            self.total_usage.completion_tokens += summary.completion_tokens
            self.total_usage.total_tokens += summary.total_tokens
            self._persist_total_stats()

        if summary.total_tokens > 0:
            calls = len(summary.usage_records)
            base_msg = (
                f"直近の翻訳で {summary.total_tokens} トークンを使用しました "
                f"(入力 {summary.prompt_tokens} / 出力 {summary.completion_tokens})"
            )
            if summary.model:
                base_msg += f"。モデル: {summary.model}"
            if calls:
                base_msg += f"、API コール {calls} 回"
            base_msg += "。"
            self.token_usage_summary.value = base_msg
        elif summary.translated_mods > 0:
            note = "翻訳は完了しましたが、新たに使用されたトークンは報告されませんでした。"
            if summary.aborted:
                note = "翻訳が途中で停止したため、トークン使用量は 0 として集計しています。"
            self.token_usage_summary.value = note
        else:
            self.token_usage_summary.value = "まだ翻訳の実行履歴がありません。"

        self.token_usage_prompt_text.value = f"入力トークン: {self.total_usage.prompt_tokens}"
        self.token_usage_completion_text.value = f"出力トークン: {self.total_usage.completion_tokens}"
        self.token_usage_total_text.value = f"合計トークン: {self.total_usage.total_tokens}"
        self.token_usage_cost_text.value = f"概算コスト累計: ${self.total_cost:.3f}"
        self.token_usage_updated_text.value = f"更新時刻: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"

        self._refresh_usage_history_table()

        self.token_usage_summary.update()
        self.token_usage_prompt_text.update()
        self.token_usage_completion_text.update()
        self.token_usage_total_text.update()
        self.token_usage_cost_text.update()
        self.token_usage_updated_text.update()

    def _show_completion_toast(self, message: str, *, is_error: bool = False):
        color = ft.Colors.ERROR if is_error else None
        snack = ft.SnackBar(ft.Text(message), bgcolor=color)
        if hasattr(self.page, "open"):
            self.page.open(snack)
        else:
            snack.open = True
            self.page.snack_bar = snack
            self.page.update()


    # ------------------------------
    # 抽出フロー
    # ------------------------------
    def on_extract(self, e: ft.ControlEvent):
        mods_dir_value = self.mods_dir_path.value.strip()
        mods_dir = Path(mods_dir_value) if mods_dir_value else None
        out_dir_value = self.output_dir.value.strip()
        out_dir = Path(out_dir_value) if out_dir_value else None
        if not mods_dir or not mods_dir.exists() or not mods_dir.is_dir():
            self._append_log("[ERROR] Mods フォルダが見つかりません。")
            return
        if out_dir is None:
            self._append_log("[ERROR] 出力フォルダを指定してください。")
            return
        if not out_dir.exists():
            try:
                out_dir.mkdir(parents=True, exist_ok=True)
                self._append_log("[INFO] 出力フォルダを作成しました。")
            except Exception as ex:
                self._append_log(f"[ERROR] 出力フォルダを作成できません: {repr(ex)}")
                return
        self._remember_dir(self.K_DIR_MODS, mods_dir)
        self._remember_dir(self.K_DIR_OUTPUT, out_dir)
        self._save_value(self.K_LAST_MODS_PATH, str(mods_dir))
        self._save_value(self.K_LAST_OUTPUT_PATH, str(out_dir))
        self.btn_extract.disabled = True
        self.progress_panel.visible = True
        self.btn_extract.update()
        self.progress_panel.update()
        self._set_progress(0.0, "抽出準備中")

        def _work():
            temp_dir_obj: tempfile.TemporaryDirectory[str] | None = None
            temp_dir_path: Path | None = None
            toast_message = "処理が完了しました。"
            toast_is_error = False
            self._set_detail_progress(0.0, "")
            try:
                temp_dir_obj = tempfile.TemporaryDirectory(prefix="mc_localizer_")
                temp_dir_path = Path(temp_dir_obj.name)
                self._append_log("[INFO] 一時作業フォルダを準備しました。")
                self._append_log("[RUN] Mods フォルダから抽出を開始します。")
                result: ExtractionResult = extract_localizations(
                    mods_dir,
                    temp_dir_path,
                    log=self._append_log,
                    progress=self._update_extraction_progress,
                )
                targets: list[tuple[str, Path, dict[str, str]]] = []
                for modid in result.mod_maps.keys():
                    en_path = temp_dir_path / modid / "en_us.json"
                    if en_path.exists():
                        existing_ja = result.existing_ja_maps.get(modid, {})
                        targets.append((modid, en_path, existing_ja))
                if targets:
                    self._append_log("[RUN] 抽出が完了したため、翻訳を開始します。")
                    summary = self._run_translation_with_auto_resume(
                        targets, mods_dir, temp_dir_path, out_dir
                    )
                    self._update_token_usage_ui(summary)
                    if summary.rate_limited:
                        toast_message = (
                            "レート制限のため中断しました。制限解除後に再実行してください。"
                        )
                        toast_is_error = False
                    elif summary.aborted:
                        toast_message = "翻訳が停止されました。"
                        toast_is_error = False
                    elif summary.had_error:
                        toast_message = "翻訳処理でエラーが発生しました。ログを確認してください。"
                        toast_is_error = True
                    elif summary.translated_mods == 0:
                        toast_message = "翻訳対象の ja_jp.json は生成されませんでした。"
                    else:
                        mod_part = f"{summary.total_mods} Mod 中 {summary.translated_mods} Mod"
                        if summary.total_entries:
                            entry_part = f"、{summary.total_entries} 件中 {summary.translated_entries} 件"
                        else:
                            entry_part = ""
                        toast_message = f"翻訳が完了しました ({mod_part}{entry_part})。"
                else:
                    self._set_progress(1.0, "抽出完了")
                    self._append_log("[WARN] 翻訳対象となる en_us.json が見つかりませんでした。")
                    toast_message = "抽出完了 (翻訳対象なし)。"
            except Exception as ex:
                self._append_log("[ERROR] 抽出処理で例外: " + repr(ex))
                self._append_log(traceback.format_exc())
                toast_message = "処理中にエラーが発生しました。ログを確認してください。"
                toast_is_error = True
            finally:
                if temp_dir_obj:
                    temp_dir_obj.cleanup()
                    if temp_dir_path:
                        self._append_log("[INFO] 一時作業フォルダを削除しました。")
                self.btn_extract.disabled = False
                self.progress_panel.visible = False
                self.btn_extract.update()
                self.progress_panel.update()
                self._show_completion_toast(toast_message, is_error=toast_is_error)

        threading.Thread(target=_work, daemon=True).start()

    def _run_translation_with_auto_resume(
        self,
        targets: list[tuple[str, Path, dict[str, str]]],
        source_path: Path,
        temp_dir: Path,
        output_dir: Path,
    ) -> TranslationSummary:
        """`_translate_targets` を呼び、未完了 Mod が残っていれば自動で再ループする。

        Claude 定額プランのレート制限などで途中失敗した Mod の進捗は resume_path に
        保存されているため、再実行で続きから処理される。最後までしっかり訳すため
        最大 max_passes 回までリトライする。ユーザーが停止 (stop_event) した場合は
        即座に抜ける。
        """
        import time as _time

        max_passes = 4
        cooldown_seconds = 90
        resume_root = output_dir / ".resume"

        last_summary: TranslationSummary | None = None
        accumulated_records: list[tuple[int, int, int]] = []
        accumulated_prompt = 0
        accumulated_completion = 0
        accumulated_total = 0
        produced_modids: set[str] = set()

        def _has_pending() -> bool:
            if not resume_root.exists():
                return False
            for child in resume_root.iterdir():
                if child.is_dir() and (child / "ja_jp.json").exists():
                    return True
            return False

        for pass_idx in range(1, max_passes + 1):
            if pass_idx > 1:
                self._append_log(
                    f"[RUN] 未完成の翻訳が残っているため、再試行ループ {pass_idx}/{max_passes} を開始します。"
                )
            summary = self._translate_targets(targets, source_path, temp_dir, output_dir)
            last_summary = summary
            accumulated_records.extend(summary.usage_records)
            accumulated_prompt += summary.prompt_tokens
            accumulated_completion += summary.completion_tokens
            accumulated_total += summary.total_tokens
            # produced は再ループ時に重複し得るため modid 単位で集計する
            # (translated_mods は最終 pass の値を信頼する形にしてもよいが、
            #  ここでは累積した訳完了 Mod 数を使う)

            if summary.rate_limited:
                self._append_log(
                    "[STOP] レート制限のため翻訳を中断しました。再試行ループはスキップします。"
                    " 制限解除後（通常 5 時間〜）に「抽出 / リソースパック生成」を再度押してください。"
                )
                break
            if summary.aborted and self.stop_event.is_set():
                self._append_log("[INFO] ユーザー停止を検出したため、再試行ループを終了します。")
                break
            if not _has_pending():
                if pass_idx > 1:
                    self._append_log("[OK] 全ての Mod の翻訳が完了しました（自動再試行成功）。")
                break
            # 1 パスで 1 件も新規翻訳できていない場合は、再試行しても同じ結果になるだけ。
            # (全 Mod がパック既存・スキップされる、もしくは全 Mod がエラーで飛ばされる等)
            # 90 秒待機 → 同じ空打ちを繰り返すループから抜けるための最終ガード。
            if summary.translated_mods == 0:
                self._append_log(
                    "[INFO] このパスでは新規翻訳が発生しませんでした。再試行ループを終了します。"
                    " (.resume/ に古い残骸がある場合は手動で削除してください)"
                )
                break
            if pass_idx == max_passes:
                self._append_log(
                    f"[WARN] 自動再試行を {max_passes} 回行いましたが、まだ未完成の Mod があります。"
                    " 「抽出 / リソースパック生成」を再度押すと続きから処理できます。"
                )
                break
            self._append_log(
                f"[INFO] {cooldown_seconds} 秒待機してから再試行します（レート制限のクールダウン）。"
            )
            # stop_event を確認しながら細かく待機
            for _ in range(cooldown_seconds):
                if self.stop_event.is_set():
                    self._append_log("[INFO] 待機中にユーザー停止を検出しました。")
                    break
                _time.sleep(1)
            if self.stop_event.is_set():
                break

        if last_summary is None:
            return TranslationSummary(
                translated_mods=0,
                total_mods=len(targets),
                translated_entries=0,
                total_entries=0,
                aborted=False,
                had_error=True,
                pack_dir=None,
            )
        # 累積トークン情報を最終 summary にマージして返す
        return TranslationSummary(
            translated_mods=last_summary.translated_mods,
            total_mods=last_summary.total_mods,
            translated_entries=last_summary.translated_entries,
            total_entries=last_summary.total_entries,
            aborted=last_summary.aborted,
            had_error=last_summary.had_error,
            pack_dir=last_summary.pack_dir,
            prompt_tokens=accumulated_prompt,
            completion_tokens=accumulated_completion,
            total_tokens=accumulated_total,
            model=last_summary.model,
            usage_records=accumulated_records,
            rate_limited=last_summary.rate_limited,
        )

    def _translate_targets(
        self,
        targets: list[tuple[str, Path, dict[str, str]]],
        source_path: Path,
        temp_dir: Path,
        output_dir: Path,
    ) -> TranslationSummary:
        total_targets = len(targets)

        provider = self._load_value(self.K_PROVIDER) or self.current_provider or self.default_provider
        if provider not in {p for p, _ in self.provider_choices}:
            provider = self.default_provider

        model = self.model_field.value or self._load_value(self.K_MODEL) or self.default_model
        model = model.strip()
        provider_models = self.models_by_provider.get(provider, [])
        # Guard: keep model and provider in sync even if storage drifted.
        if self._provider_for_model(model) != provider or model not in provider_models:
            fallback = self.default_model_by_provider.get(provider)
            if fallback and fallback in provider_models:
                model = fallback
            elif provider_models:
                model = provider_models[0]
            self._append_log(f"[INFO] Provider={provider} に合わせてモデルを {model} に切替えました。")

        api_key = self._load_api_key(provider)

        if not api_key:
            self._append_log(f"[ERROR] {provider} の API キーが未設定です。設定タブで保存してください。")
            self.tabs.selected_index = 1
            self.tabs.update()
            return TranslationSummary(
                translated_mods=0,
                total_mods=total_targets,
                translated_entries=0,
                total_entries=0,
                aborted=False,
                had_error=True,
                pack_dir=None,
                prompt_tokens=0,
                completion_tokens=0,
                total_tokens=0,
                model="",
                usage_records=[],
            )
        # model load moved up
        self._append_log("[INFO] リソースパック出力先を確認しました。")
        self.stop_event.clear()
        self.btn_stop.disabled = False
        self.btn_stop.update()
        produced: list[tuple[str, Path]] = []
        aborted = False
        had_error = False
        rate_limited = False
        total_entries = 0
        translated_entries = 0
        pack_dir_path: Path | None = None
        pack_generated_once = False
        self._set_progress(0.0, "翻訳準備中")
        self._set_detail_progress(0.0, "")
        total_prompt_tokens = 0
        total_completion_tokens = 0
        total_token_count = 0
        usage_records: list[tuple[int, int, int]] = []
        existing_pack_translations = self._collect_pack_translations(output_dir)
        resume_root = output_dir / ".resume"
        skipped_existing = 0

        def _register_pack_contents(pack_root: Path | None):
            if not pack_root:
                return
            for mod_name, lang_path in self._collect_pack_translations(pack_root).items():
                existing_pack_translations[mod_name] = lang_path

        try:
            for idx, (modid, in_path, existing_ja) in enumerate(targets, start=1):
                if self.stop_event.is_set():
                    aborted = True
                    break
                if not in_path.exists():
                    self._append_log(f"[WARN] en_us.json が見つかりません: {modid}")
                    continue
                out_path = in_path.parent / "ja_jp.json"
                completed_mods = idx - 1
                overall_ratio = completed_mods / total_targets if total_targets else 0.0
                self._set_progress(
                    overall_ratio,
                    f"翻訳中: {completed_mods}件完了（全{total_targets}件）",
                )
                self._append_log(f"[RUN] 翻訳を開始します: {modid}")
                self._set_detail_progress(0.0, f"{modid}: 0%")

                def _progress_wrapper(ratio: float, text: str, *, _modid: str = modid):
                    parsed = self._parse_fraction(text.strip())
                    if parsed:
                        done, total = parsed
                        label = f"{_modid}: {done}件完了（全{total}件）"
                    else:
                        percent = int(max(0.0, min(1.0, ratio)) * 100)
                        label = f"{_modid}: {percent}%"
                    self._set_detail_progress(ratio, label)

                pack_lang_path = existing_pack_translations.get(modid)
                resume_path = resume_root / modid / "ja_jp.json"
                resume_exists = resume_path.exists()
                if resume_exists:
                    self._append_log(
                        f"[INFO] 中断済みの翻訳ファイルを検出しました（{modid}）。未訳を引き継ぎます。"
                    )
                if (
                    pack_lang_path
                    and pack_lang_path.exists()
                    and resume_root not in pack_lang_path.parents
                ):
                    skipped_existing += 1
                    self._append_log(
                        f"[INFO] mods_ja_resource に既存の翻訳が見つかったためスキップします（{modid}）。"
                    )
                    # スキップする場合は対応する .resume/<modid>/ を掃除して
                    # 自動再ループが「未完了あり」と誤検知し続けるのを防ぐ。
                    try:
                        if resume_path.exists():
                            resume_path.unlink()
                        if resume_path.parent.exists() and not any(resume_path.parent.iterdir()):
                            resume_path.parent.rmdir()
                    except Exception:
                        pass
                    overall_ratio = idx / total_targets if total_targets else 1.0
                    self._set_progress(
                        overall_ratio,
                        f"翻訳中: {idx}件完了（全{total_targets}件）",
                    )
                    self._set_detail_progress(1.0, f"{modid}: 既存訳を使用")
                    continue

                try:
                    result = translate_localizations(
                        api_key=api_key,
                        model=model,
                        in_path=in_path,
                        out_path=out_path,
                        existing_translations=existing_ja,
                        log=self._append_log,
                        progress=_progress_wrapper,
                        should_stop=self.stop_event.is_set,
                        resume_path=resume_path,
                        provider=provider,
                    )
                    total_entries += result.total
                    translated_entries += result.created
                    total_prompt_tokens += result.prompt_tokens
                    total_completion_tokens += result.completion_tokens
                    total_token_count += result.total_tokens
                    for usage in result.usages:
                        prompt = usage.prompt_tokens
                        completion = usage.completion_tokens
                        total_tok = usage.total_tokens or (prompt + completion)
                        usage_records.append((prompt, completion, total_tok))
                    remaining = max(0, result.total - result.created)
                    if result.stopped:
                        if remaining:
                            self._append_log(
                                f"[INFO] 未翻訳 {remaining} 件の進捗を保存しました。再開時は自動的に続きから処理します。"
                            )
                        elif resume_exists:
                            self._append_log(
                                "[INFO] 停止時点の翻訳は保存済みです。再開時に利用されます。"
                            )
                        self._append_log("[INFO] ユーザーによって翻訳が停止されました。")
                        aborted = True
                        break
                    if out_path.exists():
                        produced.append((modid, out_path))
                    self._append_log(f"[OK] ja_jp.json を作成しました（{modid}）。")
                    pack_dir = self._generate_resource_pack(source_path, temp_dir, output_dir, produced)
                    if pack_dir:
                        pack_dir_path = pack_dir
                        pack_generated_once = True
                        self._append_log(f"[OK] リソースパックを更新しました（{modid}）。")
                        _register_pack_contents(pack_dir)
                    if not aborted:
                        overall_ratio = idx / total_targets if total_targets else 1.0
                        self._set_progress(
                            overall_ratio,
                            f"翻訳中: {idx}件完了（全{total_targets}件）",
                        )
                except RateLimitExceeded as ex:
                    # レート制限はリトライしても数時間は復旧しないので、全 Mod の処理を打ち切る。
                    # 進捗は resume_path に保存されているので、ユーザーが時間を空けて再実行すれば
                    # 続きから処理される。
                    rate_limited = True
                    aborted = True
                    self._append_log(
                        f"[STOP] レート制限を検出しました（{modid}）。翻訳を中断します。"
                        " 制限解除後（通常 5 時間〜）に「抽出 / リソースパック生成」を再度押してください。"
                    )
                    break
                except Exception as ex:
                    had_error = True
                    self._append_log(f"[ERROR] 翻訳処理で例外 ({modid}): {repr(ex)}")
                    self._append_log(traceback.format_exc())
                    # 1 Mod の失敗で全停止せず次の Mod に進む。
                    # 部分的な進捗は resume_path に保存されているので、
                    # 後段の自動再実行ループ (on_extract) で続きから処理される。
                    self._append_log(
                        f"[INFO] {modid} はスキップして次の Mod に進みます。未訳分は再実行で続きから処理されます。"
                    )
                    continue
        finally:
            self.stop_event.clear()
            self.btn_stop.disabled = True
            self.btn_stop.update()
        if produced and not aborted and not pack_generated_once:
            try:
                pack_dir = self._generate_resource_pack(source_path, temp_dir, output_dir, produced)
                if pack_dir:
                    pack_dir_path = pack_dir
                    self._append_log(f"[OK] リソースパックを更新しました（{pack_dir.name}）。")
                    pack_png = pack_dir / "pack.png"
                    if not pack_png.exists():
                        self._append_log(f"[INFO] pack.png は手動で配置してください（{pack_dir.name}）。")
                    _register_pack_contents(pack_dir)
            except Exception as ex:
                had_error = True
                self._append_log(f"[ERROR] リソースパックの生成に失敗しました: {repr(ex)}")
                self._append_log(traceback.format_exc())
        elif aborted:
            self._append_log("[WARN] 翻訳が完了しなかったため、リソースパックの作成をスキップしました。")
        elif skipped_existing:
            self._append_log(
                f"[INFO] {skipped_existing} 件の Mod は mods_ja_resource に既存の翻訳があったため処理をスキップしました。"
            )
        else:
            self._append_log("[WARN] ja_jp.json が生成されなかったため、リソースパックの作成をスキップしました。")
        return TranslationSummary(
            translated_mods=len(produced),
            total_mods=total_targets,
            translated_entries=translated_entries,
            total_entries=total_entries,
            aborted=aborted,
            had_error=had_error,
            pack_dir=pack_dir_path,
            prompt_tokens=total_prompt_tokens,
            completion_tokens=total_completion_tokens,
            total_tokens=total_token_count,
            model=model,
            usage_records=usage_records,
            rate_limited=rate_limited,
        )

    def on_stop(self, e: ft.ControlEvent):
        self.stop_event.set()
        self._append_log("[INFO] 停止要求を送信しました。現在のバッチ終了後に停止します。")

    def _get_bundled_asset_path(self, filename: str) -> Path:
        if not hasattr(sys, "_MEIPASS"):
            return BASE_DIR / "assets" / filename

        base = Path(sys._MEIPASS)
        return base / "assets" / filename

    def _generate_resource_pack(
        self,
        source_path: Path,
        temp_dir: Path,
        output_dir: Path,
        produced: list[tuple[str, Path]],
    ) -> Path | None:
        if not produced:
            return None
        self._append_log("[INFO] リソースパック生成を開始します。")
        
        # リソースパック名は「出力先(resourcepacks)の親フォルダ名_localize」とする
        # 例: .../MyModPack/resourcepacks -> MyModPack_localize
        try:
            base_name = output_dir.parent.name
        except Exception:
            base_name = "ModPack"
        
        if not base_name:
            base_name = "ModPack"

        pack_name = f"{base_name}_localize"
        pack_dir = output_dir / pack_name
        pack_dir.mkdir(parents=True, exist_ok=True)
        self._apply_template_files(pack_dir)
        assets_dir = pack_dir / "assets"
        assets_dir.mkdir(parents=True, exist_ok=True)
        for modid, ja_path in produced:
            target_dir = assets_dir / modid / "lang"
            target_dir.mkdir(parents=True, exist_ok=True)
            target_dir.mkdir(parents=True, exist_ok=True)
            shutil.copy2(ja_path, target_dir / "ja_jp.json")
        self._write_pack_mcmeta(pack_dir, base_name)
        
        # アイコンの確認・コピー
        pack_png = pack_dir / "pack.png"
        if not pack_png.exists():
            default_icon = self._get_bundled_asset_path("icon.png")
            if not default_icon.exists():
                self._append_log(f"[WARN] icon.png が見つかりません: {default_icon}")
            if default_icon.exists():
                try:
                    shutil.copy2(default_icon, pack_png)
                except Exception as e:
                    self._append_log(f"[WARN] アイコンのコピーに失敗しました: {e}")
            
        return pack_dir

    def _write_pack_mcmeta(self, pack_dir: Path, mod_name: str):
        latest_pack_format = 99
        description = "Generated by MCModLocalizer"
        pack_meta = {
            "pack": {
                "pack_format": latest_pack_format,
                "supported_formats": {
                    "edition": "java",
                    "min_inclusive": 1,
                    "max_inclusive": latest_pack_format,
                },
                "description": description,
            }
        }
        pack_dir.mkdir(parents=True, exist_ok=True)
        mcmeta_path = pack_dir / "pack.mcmeta"
        mcmeta_path.write_text(json.dumps(pack_meta, ensure_ascii=False, indent=2), encoding="utf-8")

    def _apply_template_files(self, pack_dir: Path):
        if not RESOURCE_TEMPLATE_DIR.exists():
            return
        for item in RESOURCE_TEMPLATE_DIR.iterdir():
            if item.name in {"pack.mcmeta", "pack.png"}:
                continue
            target = pack_dir / item.name
            if item.is_dir():
                shutil.copytree(item, target, dirs_exist_ok=True)
            elif item.is_file():
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(item, target)

    def _collect_pack_translations(self, base_dir: Path) -> dict[str, Path]:
        translations: dict[str, Path] = {}
        if not base_dir.exists() or not base_dir.is_dir():
            return translations

        try:
            for candidate in base_dir.rglob("ja_jp.json"):
                if not candidate.is_file():
                    continue
                parent = candidate.parent
                if parent.name == "lang" and parent.parent != parent:
                    translations[parent.parent.name] = candidate
                else:
                    translations[parent.name] = candidate
        except Exception:
            return translations

        return translations


def main(page: ft.Page):
    LocalizeApp(page)
