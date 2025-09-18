from __future__ import annotations

import os
import threading
import traceback
from pathlib import Path

import flet as ft

from processing import ExtractionResult, translate_localizations, extract_localizations

APP_NAME = "MC Localizer"

try:
    import keyring  # type: ignore
except Exception:
    keyring = None


class LocalizeApp:
    def __init__(self, page: ft.Page):
        self.page = page
        self.stop_event = threading.Event()
        self.worker: threading.Thread | None = None
        # 保存キー
        self.K_API = "openai_api_key"
        self.K_MODEL = "openai_model"
        self.K_GLOSS = "glossary_path"
        self.K_SAVE_MODE = "save_mode"  # "keyring" or "local"
        self.K_DIR_JAR = "dir_mod_jar"
        self.K_DIR_EXTRACT = "dir_extract_root"
        self.K_DIR_INPUT = "dir_input_json"
        self.K_DIR_OUTPUT = "dir_output_json"
        self.K_DIR_GLOSSARY = "dir_glossary_csv"
        # 既定値
        self.default_model = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
        # -------------- UI 構築 --------------
        page.title = f"{APP_NAME} (Flet)"
        page.padding = 16
        page.window_width = 1000
        page.window_height = 820
        page.theme_mode = "light"
        # ログ & 進捗
        self.log = ft.TextField(label="ログ", multiline=True, read_only=True, expand=True,
                                min_lines=12, max_lines=9999, border=ft.InputBorder.OUTLINE)
        self.progress = ft.ProgressBar(width=420, value=0)
        self.counter = ft.Text("待機中")
        # -------- 抽出タブ UI --------
        self.mod_jar_path = ft.TextField(label="Mod JAR（必須）", dense=True, expand=True)
        self.extract_dir = ft.TextField(label="作業フォルダ（en_us.json / skeleton 出力先）", dense=True, expand=True)
        self.cb_auto_translate = ft.Checkbox(label="抽出後すぐ翻訳（ja_jp.json を作成）", value=True)
        self.fp_jar = ft.FilePicker(on_result=self._on_pick_jar)
        self.fp_dir = ft.FilePicker(on_result=self._on_pick_dir)
        self.page.overlay.extend([self.fp_jar, self.fp_dir])
        pick_jar_btn = ft.IconButton(icon=ft.Icons.FOLDER_OPEN, tooltip="Mod JAR を選択",
                                     on_click=self._open_jar_picker)
        pick_dir_btn = ft.IconButton(icon=ft.Icons.FOLDER_OPEN, tooltip="作業フォルダを選択",
                                     on_click=self._open_extract_dir_picker)
        self.btn_extract = ft.ElevatedButton("抽出 / ひな形生成", icon=ft.Icons.DOWNLOAD, on_click=self.on_extract)
        extract_tab = ft.Column(
            controls=[
                ft.Text("ステップ1: JAR から en_us.json を抽出し、ひな形 (ja_jp.skeleton.json) を作成します。", weight=ft.FontWeight.BOLD),
                ft.Row([self.mod_jar_path, pick_jar_btn], alignment=ft.MainAxisAlignment.START),
                ft.Row([self.extract_dir, pick_dir_btn], alignment=ft.MainAxisAlignment.START),
                ft.Row([self.cb_auto_translate, self.btn_extract, self.progress, self.counter], spacing=16, vertical_alignment=ft.CrossAxisAlignment.CENTER),
                self.log,
            ],
            expand=True,
            spacing=12,
        )
        # -------- 翻訳タブ UI --------
        self.input_path = ft.TextField(label="入力 en_us.json（抽出済み）", dense=True, expand=True)
        self.output_path = ft.TextField(label="出力 ja_jp.json", dense=True, expand=True)
        self.glossary_path = ft.TextField(label="用語集 CSV（任意）", dense=True, expand=True)
        self.fp_open = ft.FilePicker(on_result=self._on_pick_input)
        self.fp_save = ft.FilePicker(on_result=self._on_save_output)
        self.fp_gloss = ft.FilePicker(on_result=self._on_pick_glossary)
        self.page.overlay.extend([self.fp_open, self.fp_save, self.fp_gloss])
        pick_in_btn = ft.IconButton(icon=ft.Icons.FOLDER_OPEN, tooltip="en_us.json を選択",
                                    on_click=self._open_input_picker)
        pick_out_btn = ft.IconButton(icon=ft.Icons.SAVE, tooltip="ja_jp.json の保存先",
                                     on_click=self._open_output_picker)
        pick_gloss_btn = ft.IconButton(icon=ft.Icons.FOLDER_OPEN, tooltip="用語集 CSV を選択",
                                       on_click=self._open_glossary_picker)
        self.btn_start = ft.ElevatedButton("翻訳開始", icon=ft.Icons.PLAY_ARROW, on_click=self.on_start)
        self.btn_stop = ft.OutlinedButton("停止", icon=ft.Icons.STOP, on_click=self.on_stop, disabled=True)
        translate_tab = ft.Column(
            controls=[
                ft.Text("ステップ2: en_us.json を OpenAI で翻訳し、ja_jp.json を生成します。", weight=ft.FontWeight.BOLD),
                ft.Row([self.input_path, pick_in_btn], alignment=ft.MainAxisAlignment.START),
                ft.Row([self.output_path, pick_out_btn], alignment=ft.MainAxisAlignment.START),
                ft.Row([self.glossary_path, pick_gloss_btn], alignment=ft.MainAxisAlignment.START),
                ft.Row([self.btn_start, self.btn_stop], spacing=12),
            ],
            expand=True,
            spacing=12,
        )
        # -------- 設定タブ UI --------
        saved_model = self._load_value(self.K_MODEL) or self.default_model
        self.api_key_field = ft.TextField(
            label="OpenAI API キー",
            password=True, can_reveal_password=True, dense=True, expand=True,
            hint_text="例: sk-...", value=self._load_api_key() or "",
        )
        self.model_field = ft.Dropdown(
            label="モデル",
            value=saved_model,
            options=[
                ft.dropdown.Option("gpt-4o-mini"),
                ft.dropdown.Option("gpt-4o"),
                ft.dropdown.Option("o4-mini"),
                ft.dropdown.Option("o4"),
            ],
            dense=True, expand=False, width=220,
        )
        save_mode = (self._load_value(self.K_SAVE_MODE) or ("keyring" if keyring else "local"))
        self.save_mode_switch = ft.Dropdown(
            label="APIキーの保存先", value=save_mode,
            options=[ft.dropdown.Option("keyring"), ft.dropdown.Option("local")],
            helper_text="keyring: OS の資格情報（推奨） / local: この端末のユーザーストレージ",
            width=220,
        )
        self.btn_save_settings = ft.ElevatedButton("保存", icon=ft.Icons.SAVE, on_click=self.on_save_settings)
        settings_tab = ft.Column(
            controls=[
                ft.Text("OpenAI 設定", weight=ft.FontWeight.BOLD),
                ft.Row([self.api_key_field, self.model_field, self.save_mode_switch, self.btn_save_settings], spacing=12),
                ft.Text("ヒント：環境変数 OPENAI_MODEL / OPENAI_API_KEY を設定している場合は、それらも自動的に参照します。"),
            ],
            expand=True,
            spacing=12,
        )
        # -------- Tabs --------
        self.tabs = ft.Tabs(
            selected_index=0,
            tabs=[
                ft.Tab(text="抽出", icon=ft.Icons.DOWNLOAD, content=extract_tab),
                ft.Tab(text="翻訳", icon=ft.Icons.TRANSLATE, content=translate_tab),
                ft.Tab(text="設定", icon=ft.Icons.SETTINGS, content=settings_tab),
            ],
            expand=True,
        )
        page.add(self.tabs)
        self._append_log("準備完了。JAR を抽出 → ひな形生成 → 翻訳の順で実行してください。")

    # ------------------------------
    # FilePicker launchers
    # ------------------------------
    def _open_jar_picker(self, e: ft.ControlEvent):
        init_dir = self._get_initial_directory(self.K_DIR_JAR)
        self.fp_jar.pick_files(initial_directory=init_dir, allowed_extensions=["jar"], allow_multiple=False)

    def _open_extract_dir_picker(self, e: ft.ControlEvent):
        init_dir = self._get_initial_directory(self.K_DIR_EXTRACT)
        self.fp_dir.get_directory_path(initial_directory=init_dir)

    def _open_input_picker(self, e: ft.ControlEvent):
        init_dir = self._get_initial_directory(self.K_DIR_INPUT)
        self.fp_open.pick_files(initial_directory=init_dir, allowed_extensions=["json"], allow_multiple=False)

    def _open_output_picker(self, e: ft.ControlEvent):
        init_dir = self._get_initial_directory(self.K_DIR_OUTPUT)
        current = (self.output_path.value or "").strip()
        file_name = Path(current).name if current else "ja_jp.json"
        self.fp_save.save_file(initial_directory=init_dir, file_name=file_name)

    def _open_glossary_picker(self, e: ft.ControlEvent):
        init_dir = self._get_initial_directory(self.K_DIR_GLOSSARY)
        self.fp_gloss.pick_files(initial_directory=init_dir, allowed_extensions=["csv"], allow_multiple=False)

    # ------------------------------
    # FilePicker handlers
    # ------------------------------
    def _on_pick_jar(self, e: ft.FilePickerResultEvent):
        if e.files:
            selected = Path(e.files[0].path)
            self.mod_jar_path.value = str(selected)
            self.mod_jar_path.update()
            self._remember_dir(self.K_DIR_JAR, selected)

    def _on_pick_dir(self, e: ft.FilePickerResultEvent):
        if e.path:
            selected = Path(e.path)
            self.extract_dir.value = str(selected)
            self.extract_dir.update()
            self._remember_dir(self.K_DIR_EXTRACT, selected)

    def _on_pick_input(self, e: ft.FilePickerResultEvent):
        if e.files:
            selected = Path(e.files[0].path)
            self.input_path.value = str(selected)
            self.input_path.update()
            self._remember_dir(self.K_DIR_INPUT, selected)

    def _on_save_output(self, e: ft.FilePickerResultEvent):
        if e.path:
            selected = Path(e.path)
            self.output_path.value = str(selected)
            self.output_path.update()
            self._remember_dir(self.K_DIR_OUTPUT, selected)

    def _on_pick_glossary(self, e: ft.FilePickerResultEvent):
        if e.files:
            selected = Path(e.files[0].path)
            self.glossary_path.value = str(selected)
            self.glossary_path.update()
            self._remember_dir(self.K_DIR_GLOSSARY, selected)

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

    def _load_api_key(self) -> str | None:
        if keyring:
            try:
                v = keyring.get_password(APP_NAME, "OPENAI_API_KEY")
                if v:
                    return v
            except Exception:
                pass
        v = self._load_value(self.K_API)
        if v:
            return v
        return os.getenv("OPENAI_API_KEY")

    def _save_api_key(self, value: str, mode: str):
        if mode == "keyring" and keyring:
            try:
                keyring.set_password(APP_NAME, "OPENAI_API_KEY", value)
                self.page.client_storage.remove(self.K_API)
                return
            except Exception:
                self._append_log("[WARN] keyring への保存に失敗。ローカル保存にフォールバックします。")
        self._save_value(self.K_API, value)

    def on_save_settings(self, e: ft.ControlEvent):
        key = self.api_key_field.value.strip()
        model = self.model_field.value.strip()
        mode = self.save_mode_switch.value
        if not key:
            self._append_log("[ERROR] API キーが空です。")
            return
        self._save_api_key(key, mode)
        self._save_value(self.K_MODEL, model)
        self._save_value(self.K_SAVE_MODE, mode)
        self._append_log(f"[OK] 設定を保存しました（保存先: {mode}、モデル: {model}）。")

    # ------------------------------
    # Log & Progress
    # ------------------------------
    def _append_log(self, msg: str):
        self.log.value = (self.log.value + ("\n" if self.log.value else "")) + msg
        self.log.update()

    def _set_progress(self, ratio: float, text: str = ""):
        self.progress.value = max(0.0, min(1.0, ratio))
        self.progress.update()
        if text:
            self.counter.value = text
            self.counter.update()

    # ------------------------------
    # 抽出フロー
    # ------------------------------
    def on_extract(self, e: ft.ControlEvent):
        jar_path = Path(self.mod_jar_path.value.strip())
        out_dir_value = self.extract_dir.value.strip()
        out_dir = Path(out_dir_value) if out_dir_value else None
        if not jar_path.exists():
            self._append_log(f"[ERROR] Mod JAR が見つかりません: {jar_path}")
            return
        if out_dir is None:
            self._append_log("[ERROR] 作業フォルダを指定してください。")
            return
        self.btn_extract.disabled = True
        self.btn_extract.update()
        self._set_progress(0, "抽出開始")

        def _work():
            try:
                self._append_log(f"[RUN] 抽出: {jar_path}")
                result: ExtractionResult = extract_localizations(
                    jar_path,
                    out_dir,
                    log=self._append_log,
                    progress=self._set_progress,
                )
                if self.cb_auto_translate.value and result.primary_en_path:
                    self.input_path.value = str(result.primary_en_path)
                    out_path = result.primary_en_path.parent / "ja_jp.json"
                    self.output_path.value = str(out_path)
                    self.input_path.update()
                    self.output_path.update()
                    self.tabs.selected_index = 1
                    self.tabs.update()
                    self._append_log("[RUN] 抽出が完了したため、翻訳を開始します。")
                    self._start_translate_internal()
                else:
                    self._set_progress(1.0, "抽出完了")
            except Exception as ex:
                self._append_log("[ERROR] 抽出処理で例外: " + repr(ex))
                self._append_log(traceback.format_exc())
            finally:
                self.btn_extract.disabled = False
                self.btn_extract.update()

        threading.Thread(target=_work, daemon=True).start()

    # ------------------------------
    # 翻訳フロー
    # ------------------------------
    def on_start(self, e: ft.ControlEvent):
        self._start_translate_internal()

    def _start_translate_internal(self):
        if self.worker and self.worker.is_alive():
            self._append_log("[WARN] すでに実行中です。停止後に再度お試しください。")
            return
        in_path_value = self.input_path.value.strip()
        out_path_value = self.output_path.value.strip()
        gloss_path_value = self.glossary_path.value.strip()
        if not in_path_value:
            self._append_log("[ERROR] 入力 en_us.json を指定してください。")
            return
        in_path = Path(in_path_value)
        out_path = Path(out_path_value) if out_path_value else None
        gloss_path = Path(gloss_path_value) if gloss_path_value else None
        model = (self._load_value(self.K_MODEL) or self.default_model)
        api_key = self._load_api_key()
        if not api_key:
            self._append_log("[ERROR] API キーが未設定です。設定タブで保存してください。")
            self.tabs.selected_index = 2
            self.tabs.update()
            return
        if not in_path.exists():
            self._append_log(f"[ERROR] 入力 en_us.json が見つかりません: {in_path}")
            return
        if out_path is None:
            out_path = in_path.parent / "ja_jp.json"
            self.output_path.value = str(out_path)
            self.output_path.update()
        elif out_path.is_dir():
            out_path = out_path / "ja_jp.json"
            self.output_path.value = str(out_path)
            self.output_path.update()
        self.stop_event.clear()
        self.btn_start.disabled = True
        self.btn_stop.disabled = False
        self.btn_start.update()
        self.btn_stop.update()
        self._set_progress(0, "翻訳開始")

        def _work():
            try:
                translate_localizations(
                    api_key=api_key,
                    model=model,
                    in_path=in_path,
                    out_path=out_path,
                    gloss_path=gloss_path,
                    log=self._append_log,
                    progress=self._set_progress,
                    should_stop=self.stop_event.is_set,
                )
            except Exception as ex:
                self._append_log("[ERROR] 予期せぬエラー: " + repr(ex))
                self._append_log(traceback.format_exc())
            finally:
                self.btn_start.disabled = False
                self.btn_stop.disabled = True
                self.btn_start.update()
                self.btn_stop.update()

        self.worker = threading.Thread(target=_work, daemon=True)
        self.worker.start()

    def on_stop(self, e: ft.ControlEvent):
        self.stop_event.set()
        self._append_log("[INFO] 停止要求を送信しました。現在のバッチ終了後に停止します。")


def main(page: ft.Page):
    LocalizeApp(page)
