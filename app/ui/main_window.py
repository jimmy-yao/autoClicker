"""主窗口：命令入口 + 布局。

职责边界
--------
这个类只做三件事：

1. **搭界面**：所有控件的创建与摆放都在这里，一处就能看全（``_build_*``）；
2. **当命令入口**：按钮、菜单、托盘、全局热键最终都调同一批方法（``start_clicking`` /
   ``pick_position`` / ``start_recording`` …），因此不会出现「按钮能做、热键不能做」；
3. **做跨界策略**：例如「录制期间禁用开始」「运行中禁止拾取坐标」，
   只有同时了解录制和回放的这里才判断得了。

具体事宜全部交给下面这些对象，它们各自持有属于自己的界面控件：

==========================  ==================================================
``ProfileController``       当前是哪个方案、有无未保存改动、落盘/改名/删除
``RunController``           工作线程里的 ``MacroEngine`` + 进度条
``RecordingController``     输入钩子、录制指示条、事件转步骤
``TrayIcon``                托盘图标与右键菜单
``LogPanel``                接 ``logging`` 的只读日志视图
==========================  ==================================================

界面结构
--------
::

    ┌ 方案 ── 下拉切换 + 新建/保存/另存为/重命名/删除 ─┐
    │        启动延迟 · 序列轮数 · 轮次间隔            │
    ├ 步骤序列（StepEditor）  ┬ 日志（可拖动分隔条）────┤
    ├─ 开始 / 停止 · 状态 · 进度条 ────────────────────┤
    ├─ 输入方式：前台 SendInput / 后台 PostMessage / UIA ─┤
    └─ 状态栏：热键提示 ───────────────────────────────┘

跨线程通信一律走信号：``MacroEngine`` 在工作线程里发信号，槽在主线程序列化执行。
"""

from __future__ import annotations

import logging

from PySide6.QtCore import Qt, Signal, Slot
from PySide6.QtGui import QAction, QCloseEvent
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QSpinBox,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from ..core.models import ClickStep, MoveStep
from ..core.recording import RecordSettings
from ..input.base import InputBackend
from ..storage.config import ProfileStore
from ..win.hotkey import HotkeyManager
from .icon import app_icon
from .log_panel import LogPanel
from .position_picker import PickCoordinator
from .profile_controller import ProfileController
from .recording_controller import RecordingController
from .run_controller import RunController
from .step_dialog import StepDialog
from .step_editor import StepEditor
from .tray_icon import TrayIcon

logger = logging.getLogger(__name__)

# 全局热键 id（进程内唯一即可）
HK_START = 1
HK_STOP = 2
HK_PICK = 3
HK_RECORD = 4

HOTKEY_SPECS = (
    (HK_START, "F9", "开始"),
    (HK_STOP, "F10", "停止/结束录制"),
    (HK_PICK, "F8", "拾取坐标"),
    (HK_RECORD, "F11", "录制"),
)

# 输入方式。只是界面上的选择，不进方案文件——同一个方案换个方式跑是常有的事。
# postmessage / uia 与对应后端的 ``name`` 一致，foreground 则不对应 sendinput——这里
# 描述的是「界面上的模式」，而前台后端本身没有「模式」这一说。
MODE_FOREGROUND = "foreground"
MODE_POSTMESSAGE = "postmessage"
MODE_UIA = "uia"


class MainWindow(QMainWindow):
    """自动点击器主窗口。"""

    # 任意线程 → 主线程：追加日志（队列连接，因此是线程安全的）
    log_append = Signal(str)

    def __init__(
        self,
        backend: InputBackend,
        hotkeys: HotkeyManager,
        store: ProfileStore | None = None,
    ) -> None:
        super().__init__()
        self._hotkeys = hotkeys
        self._picker = PickCoordinator(self)
        # 前台后端由 main.py 注入；后台/UIA 后端在用户第一次切到那个模式时才创建，
        # 免得不开那些模式也去 import 一堆 Win32 消息 / COM 相关的东西
        self._foreground_backend = backend
        self._background_backend = None
        self._uia_backend = None
        self._hotkey_ready: dict[int, bool] = {}
        self._force_quit = False
        self._shutdown_done = False

        self._build_ui()
        self._setup_controllers(backend, store)
        self._setup_hotkeys()
        self._profiles.bootstrap()

    # ================================================================ UI 构建
    def _build_ui(self) -> None:
        self.setWindowTitle("AutoClicker · 自动点击器")
        self.setWindowIcon(app_icon())
        self.resize(860, 780)

        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(14, 14, 14, 14)
        root.setSpacing(10)

        root.addWidget(self._build_profile_group())

        splitter = QSplitter(Qt.Orientation.Vertical)
        splitter.addWidget(self._build_steps_group())
        splitter.addWidget(self._build_log_group())
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 2)
        splitter.setSizes([420, 220])
        root.addWidget(splitter, 1)

        root.addWidget(self._build_control_group())

        self._build_menu()
        self.statusBar().showMessage("就绪  ·  F9 开始   F10 停止   F8 拾取坐标   F11 录制")

    def _build_menu(self) -> None:
        profile_menu = self.menuBar().addMenu("方案(&P)")

        actions = (
            ("新建方案", self.create_profile),
            ("保存", lambda: self.save_current(silent=False)),
            ("另存为…", self.save_as),
            ("重命名…", self.rename_profile),
            ("删除…", self.delete_profile),
        )
        for text, handler in actions:
            action = QAction(text, self)
            action.triggered.connect(handler)
            profile_menu.addAction(action)

        profile_menu.addSeparator()
        quit_action = QAction("退出", self)
        quit_action.triggered.connect(self.quit_app)
        profile_menu.addAction(quit_action)

        help_menu = self.menuBar().addMenu("帮助(&H)")
        about_action = QAction("关于", self)
        about_action.triggered.connect(self.show_about)
        help_menu.addAction(about_action)

    def _build_profile_group(self) -> QGroupBox:
        """方案组。控件的读写契约由 ``ProfileController`` 承担，这里只管摆放。"""
        group = QGroupBox("方案")
        root = QVBoxLayout(group)

        # ---- 第一行：方案切换与文件操作
        top = QHBoxLayout()
        top.addWidget(QLabel("当前方案:"))
        self._profile_combo = QComboBox()
        self._profile_combo.setMinimumWidth(180)
        top.addWidget(self._profile_combo, 1)

        for text, handler in (
            ("新建", self.create_profile),
            ("保存", lambda: self.save_current(silent=False)),
            ("另存为", self.save_as),
            ("重命名", self.rename_profile),
            ("删除", self.delete_profile),
        ):
            button = QPushButton(text)
            button.clicked.connect(handler)
            top.addWidget(button)
        root.addLayout(top)

        # ---- 第二行：运行参数
        bottom = QHBoxLayout()
        self._start_delay_spin = QSpinBox()
        self._start_delay_spin.setRange(0, 600_000)
        self._start_delay_spin.setSuffix(" ms")
        self._start_delay_spin.setToolTip("按下开始后的等待时间，留出切换窗口的余量")

        self._repeat_spin = QSpinBox()
        self._repeat_spin.setRange(0, 1_000_000)
        self._repeat_spin.setSpecialValueText("无限")
        self._repeat_spin.setToolTip("整个步骤序列重复多少轮，填 0 表示无限直到按 F10")

        self._round_delay_spin = QSpinBox()
        self._round_delay_spin.setRange(0, 3_600_000)
        self._round_delay_spin.setSuffix(" ms")
        self._round_delay_spin.setToolTip("每轮结束后额外等待多久再开始下一轮")

        self._minimize_check = QCheckBox("开始后最小化窗口")
        self._minimize_check.setChecked(True)
        self._minimize_check.setToolTip("避免本窗口挡住点击位置")

        bottom.addWidget(QLabel("启动延迟:"))
        bottom.addWidget(self._start_delay_spin)
        bottom.addWidget(QLabel("序列轮数:"))
        bottom.addWidget(self._repeat_spin)
        bottom.addWidget(QLabel("轮次间隔:"))
        bottom.addWidget(self._round_delay_spin)
        bottom.addWidget(self._minimize_check)
        bottom.addStretch(1)
        root.addLayout(bottom)
        return group

    def _build_steps_group(self) -> QGroupBox:
        group = QGroupBox("步骤序列（按顺序执行）")
        layout = QVBoxLayout(group)

        self._editor = StepEditor(self._picker)
        layout.addWidget(self._editor)
        return group

    def _build_log_group(self) -> QGroupBox:
        group = QGroupBox("日志")
        layout = QVBoxLayout(group)

        self._log_panel = LogPanel()
        layout.addWidget(self._log_panel)
        return group

    def _build_control_group(self) -> QWidget:
        holder = QWidget()
        root = QVBoxLayout(holder)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(6)

        row = QHBoxLayout()
        self._start_button = QPushButton("开始 (F9)")
        self._start_button.setMinimumHeight(32)
        self._start_button.clicked.connect(self.start_clicking)

        self._stop_button = QPushButton("停止 (F10)")
        self._stop_button.setMinimumHeight(32)
        self._stop_button.clicked.connect(self.stop_clicking)
        self._stop_button.setEnabled(False)

        self._pick_button = QPushButton("拾取坐标 (F8)")
        self._pick_button.setMinimumHeight(32)
        self._pick_button.clicked.connect(self.pick_position)

        self._record_button = QPushButton("录制 (F11)")
        self._record_button.setMinimumHeight(32)
        self._record_button.setToolTip(
            "录制鼠标点击、按键和鼠标轨迹，自动生成步骤序列。\n"
            "录制期间按 F10 结束。"
        )
        self._record_button.clicked.connect(self.start_recording)

        self._record_moves_check = QCheckBox("记录鼠标移动")
        self._record_moves_check.setChecked(True)
        self._record_moves_check.setToolTip(
            "记录鼠标轨迹，回放时能还原「移动到菜单上再点击」这类操作。\n"
            "关掉后只录点击和按键，步骤更精简。"
        )

        self._record_swallow_check = QCheckBox("屏蔽输入")
        self._record_swallow_check.setChecked(False)
        self._record_swallow_check.setToolTip(
            "勾选后录制期间鼠标键盘不会传到任何程序，纯粹「空录」坐标和节奏。\n\n"
            "不勾选（默认）：录制时操作是真的会发生的，因此可以录「点开菜单→再点菜单项」"
            "这类有前后依赖的流程，但请自行确认不会误触发重要操作。\n"
            "勾选：绝对不会误点，但依赖上一步结果才出现的界面就没法录。"
        )

        row.addWidget(self._start_button)
        row.addWidget(self._stop_button)
        row.addWidget(self._pick_button)
        row.addWidget(self._record_button)
        row.addWidget(self._record_moves_check)
        row.addWidget(self._record_swallow_check)
        row.addStretch(1)
        root.addLayout(row)

        root.addLayout(self._build_backend_row())

        self._state_label = QLabel("状态: 空闲")
        root.addWidget(self._state_label)

        self._progress = QProgressBar()
        self._progress.setRange(0, 100)
        self._progress.setValue(0)
        self._progress.setTextVisible(False)
        self._progress.setFixedHeight(8)
        root.addWidget(self._progress)
        return holder

    def _build_backend_row(self) -> QHBoxLayout:
        """输入方式一行：三种注入方式 + 绑定窗口。

        三者的取舍：
        - **前台 SendInput**：最通用（真实输入，什么程序都认），但运行期间占用鼠标；
        - **后台 PostMessage**：不占鼠标，但只对处理 ``WM_*`` 的经典 Win32 窗口有效；
        - **UIA 无障碍**：不移动鼠标、不抢焦点，网页控件（JCEF/Electron）也能点，
          但要求目标暴露无障碍树。

        只有「后台」必须绑定窗口；UIA 的绑定是可选的**安全校验**（坐标下的控件不属于
        绑定的窗口就跳过，免得点到遮挡者身上）；前台注入不需要绑定。
        """
        self._mode_combo = QComboBox()
        self._mode_combo.addItem("前台点击（SendInput）", MODE_FOREGROUND)
        self._mode_combo.addItem("后台点击（PostMessage，不占鼠标）", MODE_POSTMESSAGE)
        self._mode_combo.addItem("UIA 无障碍（按控件点击，不移动鼠标）", MODE_UIA)
        self._mode_combo.setToolTip(
            "前台（SendInput）：往系统输入队列里塞真实事件——最通用，但运行期间鼠标被占用。\n\n"
            "后台（PostMessage）：把 WM_* 消息投递给目标窗口——不占鼠标，但只对处理这些消息的\n"
            "经典 Win32 窗口有效；Chromium/Electron/JCEF、游戏、部分 UWP 会忽略（表现为「点了没反应」）。\n\n"
            "UIA 无障碍：按坐标取到「控件」再调用它的 Invoke——不移动鼠标、不抢焦点，网页控件\n"
            "也照点；代价是要求目标暴露无障碍树（Swing 要开 Java Access Bridge +\n"
            "IDEA 的 Support screen readers），且只能点有 Invoke 语义的控件（按钮/链接/菜单项）。\n"
            "键盘、移动、右键在 UIA 里没有对应语义，会回落到前台注入。\n\n"
            "拿不准就用 tools\\probe_background.py 跑一次，它会直接给结论。"
        )
        self._mode_combo.currentIndexChanged.connect(self._on_backend_mode_changed)

        self._bind_button = QPushButton("绑定窗口")
        self._bind_button.setToolTip(
            "点一下，然后在屏幕上点击目标窗口。\n\n"
            "后台（PostMessage）：把它设为点击的投递目标；不绑定也能用，会按每次点击的坐标\n"
            "临时识别当时最顶层的窗口。\n"
            "UIA：作为安全校验——坐标下的控件不属于绑定的窗口时跳过（被遮挡时会出现这种情况）。"
        )
        self._bind_button.setEnabled(False)
        self._bind_button.clicked.connect(self._bind_window)

        self._bind_label = QLabel()
        self._bind_label.setStyleSheet("color: #9aa4b2;")

        row = QHBoxLayout()
        row.addWidget(self._mode_combo)
        row.addWidget(self._bind_button)
        row.addWidget(self._bind_label, 1)
        self._refresh_bind_label()
        return row

    # ================================================================ 子系统
    def _setup_controllers(self, backend: InputBackend, store: ProfileStore | None) -> None:
        """建好各子系统并接线。这里也是唯一知道「谁和谁说话」的地方。"""
        self._profiles = ProfileController(
            store=store or ProfileStore(),
            window=self,
            combo=self._profile_combo,
            editor=self._editor,
            repeat_spin=self._repeat_spin,
            round_delay_spin=self._round_delay_spin,
            start_delay_spin=self._start_delay_spin,
            parent=self,
        )
        self._run = RunController(backend, self._progress, parent=self)
        self._record = RecordingController(self, parent=self)
        self._tray = TrayIcon(self)

        # 日志：任意线程 emit log_append，队列连接保证落到主线程再改控件
        self.log_append.connect(
            self._log_panel.appendPlainText, Qt.ConnectionType.QueuedConnection
        )
        self._log_panel.attach(self.log_append.emit)

        self._profiles.log.connect(self.log_append)
        self._profiles.changed.connect(self._on_profile_changed)

        self._run.log.connect(self.log_append)
        self._run.status.connect(self._set_state)
        self._run.started.connect(self._reset_control_buttons)
        self._run.stopped.connect(self._on_run_stopped)
        self._run.failed.connect(self._on_run_failed)

        self._record.log.connect(self.log_append)
        self._record.status.connect(self._set_state)
        self._record.recording_changed.connect(self._on_recording_changed)
        self._record.applied.connect(self._profiles.apply_recording)

        self._tray.restore_requested.connect(self._restore_window)
        self._tray.start_requested.connect(self.start_clicking)
        self._tray.stop_requested.connect(self.stop_clicking)
        self._tray.pick_requested.connect(self.pick_position)
        self._tray.record_toggle_requested.connect(self.toggle_recording)
        self._tray.quit_requested.connect(self.quit_app)

        # 截取模板失败（抓屏被拒之类）只可能发生在取图流程里，
        # 那里没有地方弹窗，所以统一走日志面板。
        self._picker.failed.connect(lambda message: self.log_append.emit(f"[错误] {message}"))

    def _setup_hotkeys(self) -> None:
        self._hotkeys.activated.connect(self._on_hotkey)
        for hotkey_id, name, label in HOTKEY_SPECS:
            registered = self._hotkeys.register_named(name, hotkey_id=hotkey_id) is not None
            self._hotkey_ready[hotkey_id] = registered
            if not registered:
                message = f"全局热键 {name}（{label}）注册失败，可能已被其他程序占用，请使用界面按钮操作"
                logger.warning(message)
                self.log_append.emit(f"[警告] {message}")

    # ================================================================ 方案命令
    @Slot()
    def create_profile(self) -> None:
        self._profiles.create()

    def save_current(self, silent: bool = False) -> bool:
        return self._profiles.save(silent=silent)

    @Slot()
    def save_as(self) -> None:
        self._profiles.save_as()

    @Slot()
    def rename_profile(self) -> None:
        self._profiles.rename()

    @Slot()
    def delete_profile(self) -> None:
        self._profiles.delete()

    @Slot()
    def show_about(self) -> None:
        from .. import __version__

        QMessageBox.about(
            self,
            "关于 AutoClicker",
            f"<b>AutoClicker</b> v{__version__}<br><br>"
            "基于 PySide6 + Win32 SendInput 的桌面自动点击工具。<br><br>"
            "<b>热键</b>：F9 开始 · F10 停止 · F8 拾取坐标<br><br>"
            "运行时请不要移动鼠标——点击会在光标位置真实执行。",
        )

    # ================================================================ 运行命令
    @Slot()
    def start_clicking(self) -> None:
        if self._run.running:
            self.log_append.emit("[提示] 任务已在运行中")
            return

        profile = self._profiles.collect()
        try:
            profile.validate(require_steps=True)
        except ValueError as exc:
            QMessageBox.warning(self, "参数错误", str(exc))
            self.log_append.emit(f"[错误] {exc}")
            return

        # 未保存的改动先落盘，避免辛苦编辑的内容丢掉
        self.save_current(silent=True)

        # 按界面选择决定这次用哪种注入方式。
        # 必须在 start 之前：引擎在运行中不允许换后端。
        backend = self._active_backend()
        self._run.set_backend(backend)
        mode = self._mode
        if mode == MODE_POSTMESSAGE:
            if backend.bound_window:
                self.log_append.emit(f"[后台点击] 目标窗口：{backend.bound_title}")
            else:
                self.log_append.emit("[后台点击] 未绑定窗口，将按每次点击的坐标临时识别窗口")
        elif mode == MODE_UIA:
            bound = f"，校验窗口：{backend.bound_title}" if backend.bound_window else "（未绑定校验窗口）"
            self.log_append.emit(
                f"[UIA] 按控件 Invoke 点击，不移动鼠标、不抢焦点{bound}；"
                "键盘/移动/右键回落到前台注入"
            )

        if self._minimize_check.isChecked():
            self.hide()

        self._run.start(profile)

    @Slot()
    def stop_clicking(self) -> None:
        if not self._run.running:
            self.log_append.emit("[提示] 当前没有正在运行的任务")
            return
        self._stop_button.setEnabled(False)
        self.log_append.emit("正在停止…")
        self._run.stop()

    @Slot()
    def pick_position(self) -> None:
        """F8 / 按钮：把拾取到的坐标写回选中的「点击」或「移动」步骤。"""
        if self._record.recording:
            self.log_append.emit("[提示] 正在录制，请先按 F10 结束录制")
            return
        if self._run.running:
            self.log_append.emit("[提示] 任务运行中，请先按 F10 停止后再拾取坐标")
            return

        row = self._editor.current_row()
        if not isinstance(self._editor.step_at(row), (ClickStep, MoveStep)):
            self.log_append.emit(
                "[提示] 请先选中一个「点击」或「移动」步骤，再按 F8 拾取坐标"
            )
            return

        self._picker.request(
            lambda x, y: self._on_picked_for_row(row, x, y),
            on_canceled=lambda: self.log_append.emit("已取消拾取"),
            hide_widget=self,
        )

    # ================================================================ 录制命令
    @Slot()
    def start_recording(self) -> None:
        if self._record.recording:
            self.log_append.emit("[提示] 已经在录制中，按 F10 结束")
            return
        if self._run.running:
            self.log_append.emit("[提示] 任务运行中，请先按 F10 停止后再录制")
            return
        if self._picker.busy:
            self.log_append.emit("[提示] 正在拾取坐标，请先完成或按 Esc 取消")
            return

        self._record.start(
            settings=RecordSettings(record_moves=self._record_moves_check.isChecked()),
            swallow=self._record_swallow_check.isChecked(),
            name=self._profiles.effective_name,
            stop_hotkey_ready=self._hotkey_ready.get(HK_STOP, False),
        )

    @Slot()
    def stop_recording(self) -> None:
        self._record.stop()

    @Slot()
    def toggle_recording(self) -> None:
        """托盘菜单用：一个入口同时兼顾开始和结束。"""
        if self._record.recording:
            self.stop_recording()
        else:
            self.start_recording()

    def _on_picked_for_row(self, row: int, x: int, y: int) -> None:
        if self._editor.update_click_coords(row, x, y):
            self.log_append.emit(f"已把坐标 ({x}, {y}) 写入第 {row + 1} 步")
        else:
            self.log_append.emit("[提示] 该步骤没有「坐标」可写，已忽略")

    # ================================================================ 输入方式
    @property
    def _mode(self) -> str:
        """界面上当前选的输入方式。"""
        return self._mode_combo.currentData()

    def _ensure_backend(self, mode: str):
        """按需创建某个后端。

        延迟创建是为了「不开那个模式就完全不碰它」：PostMessage 要 import 一整套
        Win32 消息代码，UIA 要 import COM 那一层。
        """
        if mode == MODE_POSTMESSAGE:
            if self._background_backend is None:
                from ..input.postmessage import PostMessageBackend

                self._background_backend = PostMessageBackend()
            return self._background_backend
        if mode == MODE_UIA:
            if self._uia_backend is None:
                from ..input.uia import UiaBackend

                # 移动/按键/右键在 UIA 里没有对应语义，交回前台后端
                self._uia_backend = UiaBackend(fallback=self._foreground_backend)
            return self._uia_backend
        return self._foreground_backend

    def _active_backend(self):
        """按界面上的选择返回本次运行要用的后端。"""
        return self._ensure_backend(self._mode)

    def _bindable_backend(self):
        """「绑定窗口」该作用在哪个后端上；前台模式没有可绑的东西，返回 ``None``。"""
        if self._mode == MODE_FOREGROUND:
            return None
        return self._ensure_backend(self._mode)

    @Slot(int)
    def _on_backend_mode_changed(self, _index: int) -> None:
        mode = self._mode
        if mode == MODE_POSTMESSAGE:
            self.log_append.emit(
                "[提示] 已切换到后台点击（PostMessage）：不移动光标、不要求目标窗口在前台；"
                "建议先点「绑定窗口」选定目标，否则按每次点击的坐标临时识别窗口。"
            )
        elif mode == MODE_UIA:
            self.log_append.emit(
                "[提示] 已切换到 UIA 无障碍点击：按控件 Invoke，不移动鼠标、不抢焦点，"
                "网页控件也能点；需要目标暴露无障碍树（Swing 要开 Support screen readers）。"
                "键盘/移动/右键会回落到前台注入。"
            )
        else:
            self.log_append.emit("[提示] 已切换到前台点击（SendInput）")
        self._refresh_bind_label()
        self._sync_backend_controls()

    def _refresh_bind_label(self) -> None:
        mode = self._mode
        if mode == MODE_FOREGROUND:
            self._bind_label.setText("无需绑定：前台注入按光标位置点击")
            return
        backend = self._background_backend if mode == MODE_POSTMESSAGE else self._uia_backend
        if backend is not None and backend.bound_window:
            prefix = "校验窗口" if mode == MODE_UIA else "目标窗口"
            self._bind_label.setText(f"{prefix}：{backend.bound_title}")
        elif mode == MODE_UIA:
            self._bind_label.setText("未绑定（可选：绑定后只点该窗口里的控件）")
        else:
            self._bind_label.setText("未绑定（将按点击坐标自动识别窗口）")

    def _sync_backend_controls(self) -> None:
        """按「是否在跑 / 是否在录」刷新这一行控件的可用性。"""
        busy = self._run.running or self._record.recording
        self._mode_combo.setEnabled(not busy)
        self._bind_button.setEnabled(self._mode != MODE_FOREGROUND and not busy)

    @Slot()
    def _bind_window(self) -> None:
        """走一次取点流程，把屏幕上点到的那个窗口设为目标。

        后台模式下它是消息投递目标；UIA 模式下它是安全校验（不属于它的控件跳过）。
        """
        if self._run.running or self._record.recording:
            self.log_append.emit("[提示] 任务运行或录制中，请结束后再绑定窗口")
            return
        if self._picker.busy:
            self.log_append.emit("[提示] 正在拾取坐标，请先完成或按 Esc 取消")
            return
        self._picker.request(
            self._bind_window_at,
            on_canceled=lambda: self.log_append.emit("已取消绑定窗口"),
            hide_widget=self,
        )

    def _bind_window_at(self, x: int, y: int) -> None:
        backend = self._bindable_backend()
        if backend is None:
            return
        if backend.bind_point(x, y):
            what = "校验窗口" if self._mode == MODE_UIA else "目标窗口"
            self.log_append.emit(f"已绑定{what}：{backend.bound_title}")
        else:
            self.log_append.emit(f"[错误] 坐标 ({x}, {y}) 处没有可用的窗口，绑定失败")
        self._refresh_bind_label()

    # ================================================================ 跨界状态
    @Slot()
    def _reset_control_buttons(self) -> None:
        """按「是否在录制 / 是否在回放」统一刷新按钮可用性。

        录制和回放都会禁用「开始/拾取/录制」，只有同时了解两边的这里才判断得了，
        因此这几个按钮留在 MainWindow，不下放到控制器里。
        """
        self._sync_backend_controls()

        if self._record.recording:
            self._start_button.setEnabled(False)
            self._pick_button.setEnabled(False)
            self._record_button.setEnabled(False)
            self._stop_button.setEnabled(True)
            return

        busy = self._run.running
        self._start_button.setEnabled(not busy)
        self._pick_button.setEnabled(not busy)
        self._record_button.setEnabled(not busy)
        self._stop_button.setEnabled(busy)

    @Slot(str)
    def _set_state(self, text: str) -> None:
        """状态牌由运行和录制共用，所以「状态: 」这个前缀由视图统一加。"""
        self._state_label.setText(f"状态: {text}")

    @Slot(str, bool)
    def _on_profile_changed(self, name: str, dirty: bool) -> None:
        marker = " *" if dirty else ""
        self.setWindowTitle(f"AutoClicker · {name or '未命名方案'}{marker}")

    @Slot(bool)
    def _on_recording_changed(self, active: bool) -> None:
        self._reset_control_buttons()

    @Slot(int)
    def _on_run_stopped(self, actions: int) -> None:
        self._reset_control_buttons()
        if not self.isVisible():
            self._tray.notify(f"任务已结束，共执行 {actions} 次动作")

    @Slot(str)
    def _on_run_failed(self, message: str) -> None:
        QMessageBox.critical(self, "执行失败", message)

    # ================================================================ 通用槽
    @Slot()
    def _restore_window(self) -> None:
        self.show()
        self.raise_()
        self.activateWindow()

    @Slot(int)
    def _on_hotkey(self, hotkey_id: int) -> None:
        # 录制中：F10 结束录制，其它热键一律不理会（此时输入是被屏蔽的）
        if self._record.recording:
            if hotkey_id == HK_STOP:
                self.stop_recording()
            return

        # 步骤编辑对话框开着时按 F8，交给对话框自己处理（它需要临时隐藏自己）
        dialog = QApplication.activeModalWidget()
        if hotkey_id == HK_PICK and isinstance(dialog, StepDialog):
            dialog.start_pick()
            return

        if self._picker.busy:
            # 遮罩还挂着：先收掉，避免它一直挡着
            self._picker.cancel()
            if hotkey_id == HK_PICK:
                return

        if hotkey_id == HK_START:
            self.start_clicking()
        elif hotkey_id == HK_STOP:
            self.stop_clicking()
        elif hotkey_id == HK_PICK:
            self.pick_position()
        elif hotkey_id == HK_RECORD:
            self.start_recording()

    # ================================================================ 生命周期
    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802 - Qt 命名
        """点关闭按钮时最小化到托盘，而不是退出。"""
        if self._force_quit or not self._tray.available:
            self._shutdown()
            event.accept()
            return

        event.ignore()
        self.hide()
        self._tray.notify_once(
            "tray-hint",
            "已最小化到托盘，程序仍在后台运行。\nF9 开始 / F10 停止 / F8 拾取坐标",
        )

    @Slot()
    def quit_app(self) -> None:
        self._force_quit = True
        self._shutdown()
        QApplication.quit()

    def _shutdown(self) -> None:
        """停引擎、收钩子、注销热键、自动保存。可重复调用。"""
        if self._shutdown_done:
            return
        self._shutdown_done = True

        logger.info("正在退出…")

        # 录制中退出：钩子会随进程消失，但主动摘掉更干净，也能保证屏蔽立刻解除
        self._record.shutdown()
        self._run.shutdown()

        try:
            # 未保存的编辑内容自动落盘，避免下次启动丢失
            self.save_current(silent=True)
        except Exception:  # noqa: BLE001 - 退出流程不允许抛异常
            logger.exception("退出前自动保存失败")

        try:
            self._hotkeys.shutdown()
        except Exception:  # noqa: BLE001
            logger.exception("注销热键时出错")

        self._log_panel.detach()
        self._picker.cancel()
        for backend in (self._background_backend, self._uia_backend):
            if backend is not None:
                backend.close()
        self._tray.hide()
