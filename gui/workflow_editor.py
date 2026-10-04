# -*- coding: utf-8 -*-
"""工作流编辑器（PySide6 最小原型）。

用法: python -m gui.workflow_editor [工作流名]

交互：
- 左侧面板双击（或拖拽到画布）添加节点
- 依次点击两个节点建立连线（A→B），连线决定执行顺序（必须构成一条单链）
- 双击节点编辑属性；Delete 键/工具栏删除选中节点；右键菜单同
- 保存为 data/workflows/<名称>.json（节点按链顺序写出，主程序按此顺序执行）

原型边界：仅线性链（无条件分支边/并行）、无撤销、无缩略图。
"""
from __future__ import annotations

import json
import math
import re
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

try:
    from PySide6 import QtCore, QtGui, QtWidgets
except ImportError:
    print("工作流编辑器需要 PySide6：pip install PySide6", file=sys.stderr)
    sys.exit(1)

from core import workflow as workflow_mod

MIME_NODE = "application/x-bawcode-node"

# 节点类型 → (中文名, 颜色)
TYPE_LABELS = {
    "system_prompt": ("系统提示词", "#3A6EA5"),
    "skill": ("技能清单", "#5E8C31"),
    "understand": ("任务理解", "#2C7A7B"),
    "analyze": ("复杂度判定", "#B96A1B"),
    "plan": ("任务规划", "#7D4AA0"),
    "execute": ("编码执行", "#2E7D52"),
    "review": ("审查修正", "#C0392B"),
    "llm": ("通用LLM", "#4682B4"),
}

# 支持"节点级模型"与"内联提示词/替换系统提示词"的节点集合
MODEL_TYPES = {"understand", "analyze", "plan", "execute", "review", "llm"}
PROMPT_TYPES = {"understand", "analyze", "execute", "review", "llm"}

# 属性表单 schema：type -> [(key, 标签, 控件类型, 附加参数)]（仅类型专属字段；
# model/model_role 与 prompt/override_system/prompt_files 由面板按集合统一生成）
BOOL, INT, STR, LIST, CHOICE = "bool", "int", "str", "list", "choice"
FIELD_SCHEMAS = {
    "system_prompt": [("files", "系统提示词文件（逗号分隔）", LIST, None)],
    "skill": [("list", "技能白名单（逗号分隔，空=全部）", LIST, None)],
    "understand": [
        ("max_rounds", "理解循环轮数（ask_user 澄清）", INT, (1, 100)),
        ("capture", "捕获输出为变量名（可空）", STR, None),
    ],
    "analyze": [("default_level", "判定失败/不可解析时默认", CHOICE, ["low", "high"])],
    "plan": [("confirm", "计划生成后需用户确认", BOOL, None), ("steps", "执行前强制拆解步骤（主 LLM 经 generate_steps 工具）", BOOL, None)],
    "execute": [
        ("max_rounds", "工具循环轮数", INT, (1, 200)),
        ("capture", "捕获输出为变量名（可空）", STR, None),
    ],
    "review": [
        ("max_rounds", "审查循环轮数", INT, (1, 100)),
        ("capture", "捕获输出为变量名（可空）", STR, None),
    ],
    "llm": [
        ("max_rounds", "工具循环轮数（0=单次无工具）", INT, (0, 200)),
        ("capture", "捕获输出为变量名（可空）", STR, None),
    ],
}
COMMON_FIELDS = [("enabled", "启用节点", BOOL, None)]

# 新建节点时的字段默认值（对齐 data/workflows 示例）
NODE_DEFAULTS = {
    "system_prompt": {"files": ["system_prompt.md"]},
    "skill": {"list": []},
    "understand": {"prompt_files": ["understand.md"], "capture": "understanding", "max_rounds": 8, "model_role": ""},
    "analyze": {"default_level": "low", "prompt_files": [], "prompt": "", "model_role": "plan"},
    "plan": {"confirm": True, "steps": True},
    "execute": {"max_rounds": 12, "model_role": "code", "prompt_files": [], "capture": ""},
    "review": {"prompt_files": ["review.md"], "capture": "review_result", "max_rounds": 8, "model_role": "review"},
    "llm": {"prompt_files": [], "capture": "", "model_role": "plan", "max_rounds": 0},
}

NODE_W, NODE_H = 168, 64


class NodeItem(QtWidgets.QGraphicsObject):
    """画布节点：可拖动/可选/双击改属性，携带节点数据 dict"""

    def __init__(self, node: dict, editor: "EditorWindow"):
        super().__init__()
        self.node = node
        self.editor = editor
        self.setFlags(
            QtWidgets.QGraphicsItem.GraphicsItemFlag.ItemIsMovable
            | QtWidgets.QGraphicsItem.GraphicsItemFlag.ItemIsSelectable
            | QtWidgets.QGraphicsItem.GraphicsItemFlag.ItemSendsGeometryChanges
        )
        self._press_pos = None

    def boundingRect(self) -> QtCore.QRectF:
        # 外扩覆盖选中/连线高亮框：高亮画在 rect.adjusted(-3,-3,3,3)，超出默认
        # 边界的部分不会被重绘裁剪，拖动/切换选中会留下一串虚线残影
        return QtCore.QRectF(-6, -6, NODE_W + 12, NODE_H + 12)

    def paint(self, painter: QtGui.QPainter, option, widget=None):
        label, color = TYPE_LABELS.get(self.node["type"], (self.node["type"], "#555555"))
        rect = QtCore.QRectF(0, 0, NODE_W, NODE_H)
        painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
        painter.setPen(QtGui.QPen(QtGui.QColor(color), 2))
        painter.setBrush(QtGui.QColor(color + "33"))
        painter.drawRoundedRect(rect, 8, 8)
        if self.isSelected() or self.editor.pending_node is self:
            painter.setPen(QtGui.QPen(QtGui.QColor("#E67E22"), 2, QtCore.Qt.PenStyle.DashLine))
            painter.setBrush(QtCore.Qt.BrushStyle.NoBrush)
            painter.drawRoundedRect(rect.adjusted(-3, -3, 3, 3), 10, 10)
        painter.setPen(QtGui.QPen(QtGui.QColor("#F0F0F0")))
        painter.setFont(QtGui.QFont("Microsoft YaHei", 10, QtGui.QFont.Weight.Bold))
        painter.drawText(rect.adjusted(8, 6, -8, -30), QtCore.Qt.AlignmentFlag.AlignLeft, label)
        painter.setFont(QtGui.QFont("Microsoft YaHei", 8))
        painter.setPen(QtGui.QPen(QtGui.QColor("#B0B8C0")))
        sub = f"{self.node.get('id', '')}"
        if self.node.get("enabled") is False:
            sub += " · 已停用"
        painter.drawText(rect.adjusted(8, 30, -8, -6), QtCore.Qt.AlignmentFlag.AlignLeft, sub)

    def itemChange(self, change, value):
        if change == QtWidgets.QGraphicsItem.GraphicsItemChange.ItemPositionHasChanged:
            self.editor.update_edges()
        return super().itemChange(change, value)

    def mousePressEvent(self, event):
        self._press_pos = event.pos()
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event):
        moved = self._press_pos is not None and (event.pos() - self._press_pos).manhattanLength() > 4
        super().mouseReleaseEvent(event)
        if not moved and event.button() == QtCore.Qt.MouseButton.LeftButton:
            self.editor.on_node_clicked(self)
        self._press_pos = None

    def mouseDoubleClickEvent(self, event):
        self.editor.focus_param_panel(self)

    def contextMenuEvent(self, event):
        menu = QtWidgets.QMenu()
        menu.addAction("参数面板…", lambda: self.editor.focus_param_panel(self))
        menu.addAction("删除节点", lambda: self.editor.delete_nodes([self]))
        menu.addSeparator()
        if self.editor.pending_node is not None:
            menu.addAction("取消连线选择", self.editor.clear_pending)
        menu.exec(event.screenPos())


def Qt_Dash():
    return QtCore.Qt.PenStyle.DashLine


class EdgeItem(QtWidgets.QGraphicsPathItem):
    """节点连线：右中 → 左中，带箭头；路径随节点移动刷新"""

    def __init__(self, src: NodeItem, dst: NodeItem):
        super().__init__()
        self.src = src
        self.dst = dst
        self.setPen(QtGui.QPen(QtGui.QColor("#8899AA"), 2))
        self.setZValue(-1)
        self._arrow = QtWidgets.QGraphicsPolygonItem(self)
        self._arrow.setBrush(QtGui.QBrush(QtGui.QColor("#8899AA")))
        self._arrow.setPen(QtGui.QPen(QtCore.Qt.PenStyle.NoPen))
        self.update_path()

    def update_path(self):
        sp, dp = self.src.scenePos(), self.dst.scenePos()
        start = QtCore.QPointF(sp.x() + NODE_W, sp.y() + NODE_H / 2)
        end = QtCore.QPointF(dp.x(), dp.y() + NODE_H / 2)
        path = QtGui.QPainterPath(start)
        # 反向连线（目标在源左侧）时控制点跟随方向，避免画出生硬的交叉大回环
        dx = (end.x() - start.x()) / 2
        if abs(dx) < 40:
            dx = 40 if dx >= 0 else -40
        c1 = QtCore.QPointF(start.x() + dx, start.y())
        c2 = QtCore.QPointF(end.x() - dx, end.y())
        path.cubicTo(c1, c2, end)
        self.setPath(path)
        # 箭头
        line = QtCore.QLineF(start, end)
        angle = line.angle() if line.length() > 1 else 0
        head = QtGui.QPolygonF()
        head << end
        head << end + _rotate(QtCore.QPointF(-12, 5), angle)
        head << end + _rotate(QtCore.QPointF(-12, -5), angle)
        self._arrow.setPolygon(head)


def _rotate(p: QtCore.QPointF, degrees: float) -> QtCore.QPointF:
    rad = math.radians(degrees)
    return QtCore.QPointF(
        p.x() * math.cos(rad) - p.y() * math.sin(rad), p.x() * math.sin(rad) + p.y() * math.cos(rad)
    )


class PaletteList(QtWidgets.QListWidget):
    """节点面板：双击添加 + 拖拽到画布添加"""

    def __init__(self, editor: "EditorWindow"):
        super().__init__()
        self.editor = editor
        for ntype, (label, _) in TYPE_LABELS.items():
            item = QtWidgets.QListWidgetItem(label)
            item.setData(QtCore.Qt.ItemDataRole.UserRole, ntype)
            self.addItem(item)
        self.setDragEnabled(True)

    def startDrag(self, actions):
        item = self.currentItem()
        if item is None:
            return
        drag = QtGui.QDrag(self)
        mime = QtCore.QMimeData()
        mime.setData(MIME_NODE, item.data(QtCore.Qt.ItemDataRole.UserRole).encode("utf-8"))
        drag.setMimeData(mime)
        drag.exec(QtCore.Qt.DropAction.CopyAction)

    def mouseDoubleClickEvent(self, event):
        item = self.itemAt(event.pos())
        if item is not None:
            self.editor.add_node(item.data(QtCore.Qt.ItemDataRole.UserRole))
        super().mouseDoubleClickEvent(event)


class CanvasView(QtWidgets.QGraphicsView):
    def __init__(self, scene, editor: "EditorWindow"):
        super().__init__(scene)
        self.editor = editor
        self.setAcceptDrops(True)
        self.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
        self.setBackgroundBrush(QtGui.QColor("#1B1F24"))
        # 场景大于视口时锚定左上角（默认 AlignCenter 会把初始视野滚到场景中央）
        self.setAlignment(
            QtCore.Qt.AlignmentFlag.AlignLeft | QtCore.Qt.AlignmentFlag.AlignTop
        )

    def dragEnterEvent(self, event):
        if event.mimeData().hasFormat(MIME_NODE):
            event.acceptProposedAction()

    def dragMoveEvent(self, event):
        event.acceptProposedAction()

    def dropEvent(self, event):
        ntype = bytes(event.mimeData().data(MIME_NODE)).decode("utf-8")
        pos = self.mapToScene(event.pos())
        self.editor.add_node(ntype, QtCore.QPointF(pos.x() - NODE_W / 2, pos.y() - NODE_H / 2))
        event.acceptProposedAction()

    def keyPressEvent(self, event):
        if event.key() == QtCore.Qt.Key.Key_Delete:
            self.editor.delete_selected()
            return
        super().keyPressEvent(event)


class EditorWindow(QtWidgets.QMainWindow):
    def __init__(self, name: str = ""):
        super().__init__()
        self._name = name
        self._nodes: list[NodeItem] = []
        self._edges: list[EdgeItem] = []
        self.pending_node: NodeItem | None = None
        self._model_names = self._list_model_names()
        self._panel_item: NodeItem | None = None

        self.setWindowTitle(f"BAWCode 工作流编辑器 · {name or '（未保存）'}")
        self.resize(1240, 700)

        central = QtWidgets.QWidget()
        layout = QtWidgets.QHBoxLayout(central)
        self.palette = PaletteList(self)
        self.palette.setFixedWidth(140)
        layout.addWidget(self.palette)

        self.scene = QtWidgets.QGraphicsScene(0, 0, 2400, 1600, self)
        self.view = CanvasView(self.scene, self)
        layout.addWidget(self.view, 1)
        self.setCentralWidget(central)

        # 右侧参数面板：单击节点载入配置，编辑即时写回
        self.param_panel = QtWidgets.QDockWidget("节点参数", self)
        self.param_panel.setFeatures(
            QtWidgets.QDockWidget.DockWidgetFeature.DockWidgetFloatable
            | QtWidgets.QDockWidget.DockWidgetFeature.DockWidgetMovable
        )
        self.param_panel.setAllowedAreas(
            QtCore.Qt.DockWidgetArea.RightDockWidgetArea | QtCore.Qt.DockWidgetArea.LeftDockWidgetArea
        )
        self.addDockWidget(QtCore.Qt.DockWidgetArea.RightDockWidgetArea, self.param_panel)
        self._panel_clear()
        self.scene.selectionChanged.connect(
            lambda: self._panel_load(self._selected_item())
        )

        toolbar = self.addToolBar("main")
        toolbar.setMovable(False)
        for text, slot in [
            ("新建", self.new_workflow),
            ("打开…", self.open_workflow),
            ("保存", self.save_workflow),
            ("另存为…", self.save_as),
            ("校验", self.check_chain),
            ("删除选中", self.delete_selected),
        ]:
            action = toolbar.addAction(text)
            action.triggered.connect(slot)

        self.statusBar().showMessage(
            "双击/拖拽面板添加节点 · 点击两节点连线（再次点击取消连线） · 单击节点在右侧编辑参数 · Delete 删除选中"
        )
        if name:
            self.load_from_file(workflow_mod.workflow_path(None, name))

    @staticmethod
    def _list_model_names() -> list[str]:
        """从主配置读取可用模型（model_name = provider_id-model_id）"""
        try:
            from core.config import Config

            return [
                str(r.get("model_name"))
                for r in Config().list_models()
                if r.get("model_name")
            ]
        except Exception:
            return []

    def _selected_item(self) -> NodeItem | None:
        for n in self._nodes:
            try:
                if n.isSelected():
                    return n
            except RuntimeError:
                return None  # C++ 对象已被 removeItem 删除（重建画布瞬间触发信号）
        return None

    # --- 右侧参数面板 ---------------------------------------------------

    def _panel_clear(self) -> None:
        holder = QtWidgets.QWidget()
        lay = QtWidgets.QVBoxLayout(holder)
        tip = QtWidgets.QLabel("单击节点在此处查看 / 编辑参数\n（修改即时写回节点数据）")
        tip.setWordWrap(True)
        lay.addWidget(tip)
        lay.addStretch(1)
        self.param_panel.setWidget(holder)

    def _panel_load(self, item: NodeItem | None) -> None:
        self._panel_item = item
        if item is None:
            self._panel_clear()
            return
        node = item.node
        ntype = str(node.get("type") or "")
        label, _ = TYPE_LABELS.get(ntype, (ntype, "#555555"))
        holder = QtWidgets.QWidget()
        form = QtWidgets.QFormLayout(holder)
        form.setLabelAlignment(QtCore.Qt.AlignmentFlag.AlignRight | QtCore.Qt.AlignmentFlag.AlignTop)

        def add_text(key: str, title: str, placeholder: str = "", to_list: bool = False) -> None:
            edit = QtWidgets.QLineEdit()
            val = node.get(key)
            edit.setText(", ".join(str(x) for x in val) if isinstance(val, list) else str(val or ""))
            edit.setPlaceholderText(placeholder)

            def _write() -> None:
                text = edit.text().strip()
                node[key] = [p.strip() for p in text.split(",") if p.strip()] if to_list else text
                item.update()

            edit.textEdited.connect(_write)
            form.addRow(title, edit)

        def add_bool(key: str, title: str) -> None:
            box = QtWidgets.QCheckBox()
            box.setChecked(bool(node.get(key, key == "enabled")))

            def _write(state: int) -> None:
                node[key] = state == QtCore.Qt.CheckState.Checked.value
                item.update()

            box.stateChanged.connect(_write)
            form.addRow(title, box)

        def add_int(key: str, title: str, lo: int, hi: int) -> None:
            spin = QtWidgets.QSpinBox()
            spin.setRange(lo, hi)
            try:
                spin.setValue(int(node.get(key) or lo))
            except (TypeError, ValueError):
                spin.setValue(lo)

            def _write(value: int) -> None:
                node[key] = int(value)
                item.update()

            spin.valueChanged.connect(_write)
            form.addRow(title, spin)

        def add_choice(key: str, title: str, options: list[str]) -> None:
            combo = QtWidgets.QComboBox()
            combo.addItem("（默认）", "")
            for opt in options:
                combo.addItem(opt, opt)
            cur = str(node.get(key) or "")
            idx = combo.findData(cur)
            if cur and idx < 0:
                combo.addItem(cur, cur)
                idx = combo.count() - 1
            if idx >= 0:
                combo.setCurrentIndex(idx)

            def _write(index: int) -> None:
                node[key] = str(combo.itemData(index) or "")
                item.update()

            combo.currentIndexChanged.connect(_write)
            form.addRow(title, combo)

        def add_prompt_block() -> None:
            edit = QtWidgets.QPlainTextEdit(str(node.get("prompt") or ""))
            edit.setPlaceholderText("内联提示词（优先于提示词文件；支持 ^{var}^ 变量）")
            edit.setFixedHeight(120)

            def _write() -> None:
                node["prompt"] = edit.toPlainText()
                item.update()

            edit.textChanged.connect(_write)
            form.addRow("提示词（内联）", edit)
            add_bool("override_system", "替换系统提示词")
            add_text("prompt_files", "提示词文件（逗号分隔）", "逗号分隔", to_list=True)

        type_label = QtWidgets.QLabel(label)
        type_label.setStyleSheet(f"color: {TYPE_LABELS.get(ntype, ('', '#888'))[1]}; font-weight: bold;")
        form.addRow("类型", type_label)

        id_edit = QtWidgets.QLineEdit(str(node.get("id") or ""))

        def _write_id() -> None:
            node["id"] = id_edit.text().strip() or ntype
            item.update()

        id_edit.textEdited.connect(_write_id)
        form.addRow("节点 id", id_edit)
        add_bool("enabled", "启用节点")

        if ntype in MODEL_TYPES:
            model_combo = QtWidgets.QComboBox()
            model_combo.addItem("（跟随角色/激活模型）", "")
            for name in self._model_names:
                model_combo.addItem(name, name)
            cur_model = str(node.get("model") or "")
            if cur_model and model_combo.findData(cur_model) < 0:
                model_combo.addItem(cur_model, cur_model)
            if cur_model:
                model_combo.setCurrentIndex(model_combo.findData(cur_model))

            def _write_model(index: int) -> None:
                node["model"] = str(model_combo.itemData(index) or "")
                item.update()

            model_combo.currentIndexChanged.connect(_write_model)
            form.addRow("模型", model_combo)
            add_choice("model_role", "模型角色", ["plan", "code", "review"])

        if ntype in PROMPT_TYPES:
            add_prompt_block()

        for key, title, kind, extra in FIELD_SCHEMAS.get(ntype, []):
            if kind == BOOL:
                add_bool(key, title)
            elif kind == INT:
                add_int(key, title, *(extra or (0, 999)))
            elif kind == CHOICE:
                add_choice(key, title, list(extra or []))
            else:
                add_text(key, title, "逗号分隔" if kind == LIST else "", to_list=kind == LIST)

        scroll = QtWidgets.QScrollArea()
        scroll.setWidget(holder)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QtWidgets.QFrame.Shape.NoFrame)
        self.param_panel.setWidget(scroll)

    def focus_param_panel(self, item: NodeItem) -> None:
        """选中节点并聚焦右侧参数面板（双击节点 / 右键"参数"入口）"""
        for n in self._nodes:
            n.setSelected(n is item)
        self._panel_load(item)
        self.param_panel.setVisible(True)
        self.param_panel.raise_()

    def _unique_id(self, ntype: str) -> str:
        ids = {n.node.get("id") for n in self._nodes}
        base = ntype
        if base not in ids:
            return base
        index = 2
        while f"{base}{index}" in ids:
            index += 1
        return f"{base}{index}"

    def add_node(self, ntype: str, pos: QtCore.QPointF | None = None) -> NodeItem:
        if ntype not in TYPE_LABELS:
            return None
        node = {"id": self._unique_id(ntype), "type": ntype, "enabled": True}
        node.update(json.loads(json.dumps(NODE_DEFAULTS.get(ntype, {}))))  # 深拷贝默认字段
        item = NodeItem(node, self)
        if pos is None:
            pos = QtCore.QPointF(80 + 220 * (len(self._nodes) % 8), 60 + 140 * (len(self._nodes) // 8))
        item.setPos(pos)
        self.scene.addItem(item)
        self._nodes.append(item)
        self.focus_param_panel(item)  # 新节点默认选中并载入参数面板
        return item

    def delete_nodes(self, items: list[NodeItem]) -> None:
        removed_panel = False
        for item in items:
            self._edges = [e for e in self._edges if e.src is not item and e.dst is not item]
            if self.pending_node is item:
                self.pending_node = None
            if self._panel_item is item:
                removed_panel = True
            self.scene.removeItem(item)
            if item in self._nodes:
                self._nodes.remove(item)
        if removed_panel:
            self._panel_clear()
        self.update_edges()

    def delete_selected(self) -> None:
        selected = [n for n in self._nodes if n.isSelected()]
        if not selected:
            self.statusBar().showMessage("未选中节点", 3000)
            return
        self.delete_nodes(selected)

    def on_node_clicked(self, item: NodeItem) -> None:
        if self.pending_node is None:
            self.pending_node = item
            item.update()
            self.statusBar().showMessage(f"已选起点 {item.node['id']}，点击目标节点建立连线", 5000)
            return
        if self.pending_node is item:
            self.clear_pending()
            return
        # 两节点间已存在连线（任一方向）→ 再次点击 = 取消连线
        existing = [
            e
            for e in self._edges
            if {id(e.src), id(e.dst)} == {id(self.pending_node), id(item)}
        ]
        if existing:
            for edge in existing:
                self.scene.removeItem(edge)
                self._edges.remove(edge)
            self.statusBar().showMessage(
                f"已取消连线 {self.pending_node.node['id']} ↔ {item.node['id']}", 5000
            )
            self.clear_pending()
            self.update_edges()
            return
        edge = EdgeItem(self.pending_node, item)
        self.scene.addItem(edge)
        self._edges.append(edge)
        self.statusBar().showMessage(f"连线 {self.pending_node.node['id']} → {item.node['id']}", 5000)
        self.clear_pending()
        self.update_edges()

    def clear_pending(self) -> None:
        if self.pending_node is not None:
            self.pending_node.update()
        self.pending_node = None

    def update_edges(self) -> None:
        for edge in self._edges:
            edge.update_path()

    # --- 链计算与校验 ---------------------------------------------------

    def compute_chain(self) -> tuple[list[str], list[str]]:
        """由连线推导执行顺序（单链）；返回 (顺序, 错误列表)"""
        if not self._nodes:
            return [], ["画布为空"]
        ids = [n.node["id"] for n in self._nodes]
        if len(ids) != len(set(ids)):
            return None, ["节点 id 重复"]
        outgoing: dict[str, str] = {}
        incoming: dict[str, str] = {}
        for edge in self._edges:
            s, t = edge.src.node["id"], edge.dst.node["id"]
            if s in outgoing:
                return None, [f"节点 {s} 有多条出边（必须单链）"]
            if t in incoming:
                return None, [f"节点 {t} 有多条入边（必须单链）"]
            outgoing[s] = t
            incoming[t] = s
        starts = [nid for nid in ids if nid not in incoming]
        if len(starts) != 1:
            return None, [f"起点不唯一：{len(starts)} 个无入边节点（{', '.join(starts) or '无'}）"]
        order = [starts[0]]
        cur = starts[0]
        while cur in outgoing:
            cur = outgoing[cur]
            if cur in order:
                return None, ["链中存在环"]
            order.append(cur)
        if len(order) != len(ids):
            missing = [nid for nid in ids if nid not in order]
            return None, [f"未连入主链的节点: {', '.join(missing)}"]
        return order, []

    def check_chain(self) -> list[str]:
        _, errors = self.compute_chain()
        if errors:
            QtWidgets.QMessageBox.warning(self, "校验未通过", "\n".join(errors))
            return errors
        data = self._build_data()
        errors = workflow_mod.validate_workflow(data)
        if errors:
            QtWidgets.QMessageBox.warning(self, "校验未通过", "\n".join(errors))
            return errors
        QtWidgets.QMessageBox.information(
            self, "校验通过", "单链完整，节点字段合法。\n执行顺序：\n" + " → ".join(n["id"] for n in data["nodes"])
        )
        return []

    def _build_data(self) -> dict:
        order, _ = self.compute_chain()
        by_id = {n.node["id"]: n.node for n in self._nodes}
        nodes = [dict(by_id[nid]) for nid in order]
        return {"name": self._name or "untitled", "description": "", "nodes": nodes}

    # --- 文件读写 -------------------------------------------------------

    def load_from_file(self, path: Path) -> bool:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            errors = workflow_mod.validate_workflow(data)
            if errors:
                QtWidgets.QMessageBox.warning(self, "加载失败", "\n".join(errors))
                return False
        except (OSError, json.JSONDecodeError) as e:
            QtWidgets.QMessageBox.warning(self, "加载失败", f"{path}\n{e}")
            return False
        self.new_workflow(keep_name=data.get("name") or path.stem)
        for node in data["nodes"]:
            prev = self._nodes[-1] if self._nodes else None
            item = self.add_node(node["type"])
            item.node.update(node)
            if prev is not None:
                edge = EdgeItem(prev, item)
                self.scene.addItem(edge)
                self._edges.append(edge)
        self.update_edges()
        self.view.centerOn(0, 0)
        self.setWindowTitle(f"BAWCode 工作流编辑器 · {self._name}")
        self.statusBar().showMessage(f"已加载 {path.name}（{len(data['nodes'])} 节点）", 5000)
        return True

    def new_workflow(self, keep_name: str = "") -> None:
        self.scene.clearSelection()  # 先清选区，避免 removeItem 触发 selectionChanged 访问将删对象
        for item in list(self._nodes):
            self.scene.removeItem(item)
        for edge in list(self._edges):
            self.scene.removeItem(edge)
        self._nodes.clear()
        self._edges.clear()
        self.clear_pending()
        self._panel_item = None
        self._panel_clear()
        if not keep_name:
            self._name = ""
        else:
            self._name = keep_name

    def save_workflow(self) -> None:
        if not self._name:
            self.save_as()
            return
        if self.check_chain():
            return
        path = workflow_mod.workflow_path(None, self._name)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self._build_data(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        self.setWindowTitle(f"BAWCode 工作流编辑器 · {self._name}")
        self.statusBar().showMessage(f"已保存 {path}", 5000)

    def save_as(self) -> None:
        if self.check_chain():
            return
        name, ok = QtWidgets.QInputDialog.getText(self, "另存为", "工作流名称（文件名）:", text=self._name or "")
        if not ok or not name.strip():
            return
        self._name = re.sub(r'[\\/:*?"<>|]+', "_", name.strip())
        self.save_workflow()

    def open_workflow(self) -> None:
        names = workflow_mod.list_workflows(None)
        if not names:
            QtWidgets.QMessageBox.information(self, "打开", "data/workflows 下暂无工作流文件")
            return
        name, ok = QtWidgets.QInputDialog.getItem(self, "打开工作流", "名称:", names, 0, False)
        if ok and name:
            self.load_from_file(workflow_mod.workflow_path(None, name))


def main() -> None:
    name = sys.argv[1] if len(sys.argv) > 1 else ""
    app = QtWidgets.QApplication(sys.argv)
    window = EditorWindow(name)
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
