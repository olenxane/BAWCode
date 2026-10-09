# -*- coding: utf-8 -*-
"""工作流编辑器（PySide6 最小原型）。

用法: python -m gui.workflow_editor [工作流名]

交互：
- 左侧面板双击（或拖拽到画布）添加节点
- 悬停节点出现右侧输出端口，从端口拖出连线、落到目标节点松开即建立 A→B；连线决定执行顺序（必须构成一条单链）
- 单击选中节点/连线（连线变绿加粗）；Delete 键/工具栏删除选中；双击节点编辑属性；右键菜单同
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
    "skill": [("list", "技能白名单（逗号分隔，空=不注入）", LIST, None)],
    "understand": [
        ("max_rounds", "理解循环轮数（ask_user 澄清）", INT, (1, 100)),
        ("capture", "捕获输出为变量名（可空）", STR, None),
    ],
    "analyze": [("default_level", "判定失败/不可解析时默认", CHOICE, ["low", "high"])],
    "plan": [("confirm", "计划生成后需用户确认", BOOL, None), ("steps", "执行前强制拆解步骤（主 LLM 经 generate_steps 工具）", BOOL, None), ("retry_times", "计划生成失败重试次数", INT, (0, 10))],
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
    "plan": {"confirm": True, "steps": True, "retry_times": 2},
    "execute": {"max_rounds": 12, "model_role": "code", "prompt_files": [], "capture": ""},
    "review": {"prompt_files": ["review.md"], "capture": "review_result", "max_rounds": 8, "model_role": "review"},
    "llm": {"prompt_files": [], "capture": "", "model_role": "plan", "max_rounds": 0},
}

NODE_W, NODE_H = 168, 64

# 简洁清新浅色风格：米白暖底 #FAF9F5 + 绿色主色 #16A34A + 深墨文字
STYLESHEET = """
* { font-family: "Segoe UI", "Microsoft YaHei", sans-serif; font-size: 13px; }
QMainWindow, QDialog { background: #FAF9F5; }
QToolBar {
    background: #FAF9F5; border: none; border-bottom: 1px solid #E9E5DB;
    spacing: 4px; padding: 6px 10px;
}
QToolButton {
    background: transparent; color: #3D3B35; border: 1px solid transparent;
    border-radius: 7px; padding: 6px 14px;
}
QToolButton:hover { background: #F0EDE5; }
QToolButton:pressed { background: #E8E4DA; }
QListWidget {
    background: #F6F4EE; border: none; border-right: 1px solid #E9E5DB;
    outline: 0; padding: 8px;
}
QListWidget::item {
    background: #FFFFFF; border: 1px solid #E7E3D9; border-radius: 8px;
    padding: 10px 12px; margin: 3px 0; color: #1F1E1C;
}
QListWidget::item:hover { border-color: #16A34A; background: #F4FAF6; }
QListWidget::item:selected { background: #E3F3EA; border-color: #16A34A; color: #1F1E1C; }
QDockWidget { color: #6B675E; font-weight: bold; }
QDockWidget::title {
    background: #F6F4EE; border-left: 1px solid #E9E5DB; border-bottom: 1px solid #E9E5DB;
    padding: 10px 12px; text-align: left;
}
QLineEdit, QSpinBox, QComboBox, QPlainTextEdit {
    background: #FFFFFF; border: 1px solid #DEDACF; border-radius: 7px;
    padding: 6px 9px; color: #1F1E1C; selection-background-color: #BFE6D2;
}
QLineEdit:focus, QSpinBox:focus, QComboBox:focus, QPlainTextEdit:focus { border: 1px solid #16A34A; }
QComboBox::drop-down { border: none; width: 26px; }
QComboBox::down-arrow {
    width: 0; height: 0; margin-right: 10px;
    border-left: 4px solid transparent; border-right: 4px solid transparent;
    border-top: 5px solid #8A857B;
}
QComboBox QAbstractItemView {
    background: #FFFFFF; border: 1px solid #E7E3D9; outline: 0;
    selection-background-color: #E3F3EA; selection-color: #1F1E1C;
}
QCheckBox { color: #3D3B35; spacing: 6px; }
QLabel { color: #3D3B35; background: transparent; }
QStatusBar { background: #F6F4EE; color: #6B675E; border-top: 1px solid #E9E5DB; }
QStatusBar::item { border: none; }
QMenu { background: #FFFFFF; border: 1px solid #E7E3D9; border-radius: 8px; padding: 5px; }
QMenu::item { padding: 7px 26px 7px 14px; border-radius: 5px; color: #1F1E1C; }
QMenu::item:selected { background: #E3F3EA; }
QMenu::separator { height: 1px; background: #EDE9DF; margin: 5px 8px; }
QScrollArea { background: #FAF9F5; border: none; }
QScrollBar:vertical { background: transparent; width: 10px; margin: 2px; }
QScrollBar::handle:vertical { background: #D8D3C7; border-radius: 5px; min-height: 30px; }
QScrollBar::handle:vertical:hover { background: #C3BDB0; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
QScrollBar:horizontal { background: transparent; height: 10px; margin: 2px; }
QScrollBar::handle:horizontal { background: #D8D3C7; border-radius: 5px; min-width: 30px; }
QScrollBar::handle:horizontal:hover { background: #C3BDB0; }
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal { width: 0; }
QPushButton {
    background: #FFFFFF; border: 1px solid #DEDACF; border-radius: 7px;
    padding: 7px 16px; color: #1F1E1C;
}
QPushButton:hover { border-color: #16A34A; background: #F4FAF6; }
QPushButton:pressed, QPushButton:default { background: #16A34A; border-color: #16A34A; color: #FFFFFF; }
QToolTip { background: #1F1E1C; color: #FAF9F5; border: none; padding: 6px 8px; }
"""


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
        # 柔和投影让白卡片从米白画布上浮起；boundingRect 已外扩给投影留位
        shadow = QtWidgets.QGraphicsDropShadowEffect()
        shadow.setBlurRadius(20)
        shadow.setXOffset(0)
        shadow.setYOffset(3)
        shadow.setColor(QtGui.QColor(60, 55, 45, 28))
        self.setGraphicsEffect(shadow)
        self.setAcceptHoverEvents(True)
        self._hover = False
        self._port_hover = False

    def _port_hit(self, pos: QtCore.QPointF) -> bool:
        """输出端口命中区：右边缘中点，比可视圆略大方便点按"""
        return QtCore.QRectF(NODE_W - 11, NODE_H / 2 - 11, 22, 22).contains(pos)

    def boundingRect(self) -> QtCore.QRectF:
        # 外扩覆盖选中描边与投影：高亮画在 rect.adjusted(-3,-3,3,3)，投影模糊半径
        # 20+偏移 3；超出边界的部分不会被重绘裁剪，拖动/切换选中会留下残影
        return QtCore.QRectF(-24, -24, NODE_W + 48, NODE_H + 48)

    def paint(self, painter: QtGui.QPainter, option, widget=None):
        label, color = TYPE_LABELS.get(self.node["type"], (self.node["type"], "#8A857B"))
        rect = QtCore.QRectF(0, 0, NODE_W, NODE_H)
        painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
        # 白底卡片
        border = "#D3CEC2" if self._hover else "#E5E1D8"
        painter.setPen(QtGui.QPen(QtGui.QColor(border), 1))
        painter.setBrush(QtGui.QColor("#FFFFFF"))
        painter.drawRoundedRect(rect, 10, 10)
        # 左侧类型色条
        painter.setPen(QtCore.Qt.PenStyle.NoPen)
        painter.setBrush(QtGui.QColor(color))
        painter.drawRoundedRect(QtCore.QRectF(0, 12, 4, NODE_H - 24), 2, 2)
        if self.isSelected():
            painter.setPen(QtGui.QPen(QtGui.QColor("#16A34A"), 2))
            painter.setBrush(QtCore.Qt.BrushStyle.NoBrush)
            painter.drawRoundedRect(rect.adjusted(-3, -3, 3, 3), 12, 12)
        # 悬停（或正在从本节点拖线）时浮现输出端口
        if self._hover or self._port_hover or self.editor._drag_src is self:
            r = 7 if self._port_hover else 5
            painter.setPen(QtGui.QPen(QtGui.QColor("#16A34A"), 2))
            painter.setBrush(QtGui.QColor("#16A34A") if self._port_hover else QtGui.QColor("#FFFFFF"))
            painter.drawEllipse(QtCore.QPointF(NODE_W, NODE_H / 2), r, r)
        painter.setPen(QtGui.QPen(QtGui.QColor("#1F1E1C")))
        painter.setFont(QtGui.QFont("Microsoft YaHei", 10, QtGui.QFont.Weight.Bold))
        painter.drawText(rect.adjusted(14, 6, -8, -30), QtCore.Qt.AlignmentFlag.AlignLeft, label)
        painter.setFont(QtGui.QFont("Microsoft YaHei", 8))
        painter.setPen(QtGui.QPen(QtGui.QColor("#8A857B")))
        sub = f"{self.node.get('id', '')}"
        if self.node.get("enabled") is False:
            sub += " · 已停用"
        painter.drawText(rect.adjusted(14, 30, -8, -6), QtCore.Qt.AlignmentFlag.AlignLeft, sub)

    def itemChange(self, change, value):
        if change == QtWidgets.QGraphicsItem.GraphicsItemChange.ItemPositionHasChanged:
            self.editor.update_edges()
        return super().itemChange(change, value)

    def mousePressEvent(self, event):
        if event.button() == QtCore.Qt.MouseButton.LeftButton and self._port_hit(event.pos()):
            # 从输出端口按下 = 开始拖拽连线，不进移动/选中逻辑
            self.editor.begin_edge_drag(self, event.scenePos())
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self.editor._drag_src is self:
            self.editor.update_edge_drag(event.scenePos())
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if self.editor._drag_src is self:
            self.editor.end_edge_drag(event.scenePos())
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def hoverEnterEvent(self, event):
        self._hover = True
        self.update()
        super().hoverEnterEvent(event)

    def hoverMoveEvent(self, event):
        hovering_port = self._port_hit(event.pos())
        if hovering_port != self._port_hover:
            self._port_hover = hovering_port
            self.setCursor(
                QtCore.Qt.CursorShape.CrossCursor if hovering_port else QtCore.Qt.CursorShape.ArrowCursor
            )
            self.update()
        super().hoverMoveEvent(event)

    def hoverLeaveEvent(self, event):
        self._hover = False
        self._port_hover = False
        self.unsetCursor()
        self.update()
        super().hoverLeaveEvent(event)

    def mouseDoubleClickEvent(self, event):
        self.editor.focus_param_panel(self)

    def contextMenuEvent(self, event):
        menu = QtWidgets.QMenu(self.editor)  # 挂父级以继承窗口样式表
        menu.addAction("参数面板…", lambda: self.editor.focus_param_panel(self))
        menu.addAction("删除节点", lambda: self.editor.delete_nodes([self]))
        menu.exec(event.screenPos())


def _rotate(p: QtCore.QPointF, degrees: float) -> QtCore.QPointF:
    rad = math.radians(degrees)
    return QtCore.QPointF(
        p.x() * math.cos(rad) - p.y() * math.sin(rad), p.x() * math.sin(rad) + p.y() * math.cos(rad)
    )


EDGE_COLOR = "#C6C1B5"
EDGE_ACTIVE = "#16A34A"


def _edge_geometry(start: QtCore.QPointF, end: QtCore.QPointF):
    """贝塞尔连线路径 + 终点箭头多边形（正式边与拖拽橡皮筋共用）"""
    path = QtGui.QPainterPath(start)
    # 反向连线（目标在源左侧）时控制点跟随方向，避免画出生硬的交叉大回环
    dx = (end.x() - start.x()) / 2
    if abs(dx) < 40:
        dx = 40 if dx >= 0 else -40
    c1 = QtCore.QPointF(start.x() + dx, start.y())
    c2 = QtCore.QPointF(end.x() - dx, end.y())
    path.cubicTo(c1, c2, end)
    line = QtCore.QLineF(start, end)
    angle = line.angle() if line.length() > 1 else 0
    head = QtGui.QPolygonF()
    head << end
    head << end + _rotate(QtCore.QPointF(-12, 5), angle)
    head << end + _rotate(QtCore.QPointF(-12, -5), angle)
    return path, head


class EdgeItem(QtWidgets.QGraphicsPathItem):
    """节点连线：右中 → 左中，带箭头；可选中（绿色加粗），路径随节点移动刷新"""

    def __init__(self, src: NodeItem, dst: NodeItem):
        super().__init__()
        self.src = src
        self.dst = dst
        self.setFlag(QtWidgets.QGraphicsItem.GraphicsItemFlag.ItemIsSelectable)
        self.setZValue(-1)
        self._arrow = QtWidgets.QGraphicsPolygonItem(self)
        self._arrow.setPen(QtGui.QPen(QtCore.Qt.PenStyle.NoPen))
        # 箭头是子图元，不接受鼠标事件，让点击穿透到边本体统一处理选中
        self._arrow.setAcceptedMouseButtons(QtCore.Qt.MouseButton.NoButton)
        self.update_path()

    def update_path(self):
        sp, dp = self.src.scenePos(), self.dst.scenePos()
        start = QtCore.QPointF(sp.x() + NODE_W, sp.y() + NODE_H / 2)
        end = QtCore.QPointF(dp.x(), dp.y() + NODE_H / 2)
        path, head = _edge_geometry(start, end)
        self.setPath(path)
        self._arrow.setPolygon(head)

    def paint(self, painter: QtGui.QPainter, option, widget=None):
        selected = self.isSelected()
        color = QtGui.QColor(EDGE_ACTIVE if selected else EDGE_COLOR)
        painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
        painter.setPen(QtGui.QPen(color, 3 if selected else 2))
        painter.setBrush(QtCore.Qt.BrushStyle.NoBrush)
        painter.drawPath(self.path())
        self._arrow.setBrush(QtGui.QBrush(color))

    def shape(self) -> QtGui.QPainterPath:
        # 曲线默认命中只有笔宽 2px，很难点中；加宽到 10px
        stroker = QtGui.QPainterPathStroker()
        stroker.setWidth(10)
        return stroker.createStroke(self.path())

    def contextMenuEvent(self, event):
        menu = QtWidgets.QMenu(self.src.editor)
        menu.addAction("删除连线", lambda: self.src.editor.delete_edges([self]))
        menu.exec(event.screenPos())


class RubberEdge(QtWidgets.QGraphicsPathItem):
    """拖拽连线过程中的橡皮筋预览：绿色虚线 + 箭头，从源端口跟随光标"""

    def __init__(self, start: QtCore.QPointF):
        super().__init__()
        self._start = start
        self.setZValue(10)  # 压过节点卡片，拖拽过程始终可见
        self.setPen(QtGui.QPen(QtGui.QColor(EDGE_ACTIVE), 2, QtCore.Qt.PenStyle.DashLine))
        self._arrow = QtWidgets.QGraphicsPolygonItem(self)
        self._arrow.setPen(QtGui.QPen(QtCore.Qt.PenStyle.NoPen))
        self._arrow.setBrush(QtGui.QBrush(QtGui.QColor(EDGE_ACTIVE)))
        self.set_end(start)

    def set_end(self, end: QtCore.QPointF) -> None:
        path, head = _edge_geometry(self._start, end)
        self.setPath(path)
        self._arrow.setPolygon(head)


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
        self.setFrameShape(QtWidgets.QFrame.Shape.NoFrame)
        # 场景大于视口时锚定左上角（默认 AlignCenter 会把初始视野滚到场景中央）
        self.setAlignment(
            QtCore.Qt.AlignmentFlag.AlignLeft | QtCore.Qt.AlignmentFlag.AlignTop
        )

    def drawBackground(self, painter: QtGui.QPainter, rect: QtCore.QRectF) -> None:
        """米白底 + 细点阵网格（对齐整数格，避免滚动时点的位置抖动）"""
        painter.fillRect(rect, QtGui.QColor("#F7F5F0"))
        pen = QtGui.QPen(QtGui.QColor("#E4E0D4"), 3)
        pen.setCapStyle(QtCore.Qt.PenCapStyle.RoundCap)
        painter.setPen(pen)
        step = 26
        left = int(rect.left()) - (int(rect.left()) % step)
        top = int(rect.top()) - (int(rect.top()) % step)
        points = []
        x = left
        while x <= rect.right():
            y = top
            while y <= rect.bottom():
                points.append(QtCore.QPointF(x, y))
                y += step
            x += step
        painter.drawPoints(points)

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
        self._drag_src: NodeItem | None = None  # 正在从其端口拖线的源节点
        self._rubber: RubberEdge | None = None  # 拖拽中的橡皮筋预览
        self._model_names = self._list_model_names()
        self._panel_item: NodeItem | None = None

        self.setWindowTitle(f"BAWCode 工作流编辑器 · {name or '（未保存）'}")
        self.resize(1240, 700)
        self.setStyleSheet(STYLESHEET)

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
        self.param_panel.setMinimumWidth(280)
        self.addDockWidget(QtCore.Qt.DockWidgetArea.RightDockWidgetArea, self.param_panel)
        # 默认宽度 360：QDockWidget 无 setWidth，resizeDocks 在布局后生效
        QtCore.QTimer.singleShot(
            0, lambda: self.resizeDocks([self.param_panel], [360], QtCore.Qt.Orientation.Horizontal)
        )
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
            "双击/拖拽面板添加节点 · 从节点右侧端口拖出连线到目标节点 · 单击选中节点/连线 · Delete 删除选中"
        )
        if name:
            self.load_from_file(workflow_mod.workflow_path(None, name))
        # 大场景下视图默认居中到场景中心；显示后滚回左上角
        QtCore.QTimer.singleShot(0, self._scroll_home)

    def _scroll_home(self) -> None:
        self.view.horizontalScrollBar().setValue(0)
        self.view.verticalScrollBar().setValue(0)

    @staticmethod
    def _list_model_names() -> list[str]:
        """从主配置读取可用模型（model_name = provider_id-model_id）；配置损坏时
        与主程序一致地抛错退出，不静默降级为空列表"""
        from core.config import Config

        return [
            str(r.get("model_name"))
            for r in Config().list_models()
            if r.get("model_name")
        ]

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
        holder.setStyleSheet("background: #FAF9F5;")  # 默认控件底色偏灰，与 dock 米白统一
        lay = QtWidgets.QVBoxLayout(holder)
        tip = QtWidgets.QLabel("单击节点在此处查看 / 编辑参数\n（修改即时写回节点数据）")
        tip.setWordWrap(True)
        tip.setStyleSheet("color: #8A857B;")
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
        defaults = {
            "enabled": True, "confirm": True, "steps": True, "retry_times": 2,
            "default_level": "low",
            "max_rounds": {"understand": 8, "execute": 12, "review": 8}.get(ntype, 0),
            "model_role": {"plan": "plan", "execute": "code", "review": "review"}.get(ntype, ""),
        }
        label, _ = TYPE_LABELS.get(ntype, (ntype, "#555555"))
        holder = QtWidgets.QWidget()
        holder.setStyleSheet("background: #FAF9F5;")  # 默认控件底色偏灰，与 dock 米白统一
        form = QtWidgets.QFormLayout(holder)
        # 标签在上、字段在下的换行布局：dock 变窄时标签换行、字段占满可用宽，
        # 不再出现长中文标签把字段挤出可视区 / "类型"行被挤成两段的排版事故
        form.setRowWrapPolicy(QtWidgets.QFormLayout.RowWrapPolicy.WrapAllRows)
        form.setLabelAlignment(QtCore.Qt.AlignmentFlag.AlignLeft | QtCore.Qt.AlignmentFlag.AlignVCenter)
        form.setFieldGrowthPolicy(QtWidgets.QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        form.setContentsMargins(14, 12, 14, 12)
        form.setHorizontalSpacing(8)
        form.setVerticalSpacing(4)

        def _cap(title: str) -> QtWidgets.QLabel:
            cap = QtWidgets.QLabel(title)
            cap.setWordWrap(True)  # 长标签换行而不是撑出横向滚动条
            cap.setStyleSheet("color: #6B675E; margin-top: 6px;")
            return cap

        def _fit(widget):
            # 允许字段随面板收窄到任意宽度（默认 minimumSizeHint 会顶出横向滚动条）
            widget.setMinimumWidth(0)
            widget.setSizePolicy(
                QtWidgets.QSizePolicy.Policy.Expanding, widget.sizePolicy().verticalPolicy()
            )
            return widget

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
            form.addRow(_cap(title), _fit(edit))

        def add_bool(key: str, title: str) -> None:
            box = QtWidgets.QCheckBox()
            box.setChecked(bool(node.get(key, defaults.get(key, False))))

            def _write(state: int) -> None:
                node[key] = state == QtCore.Qt.CheckState.Checked.value
                item.update()

            box.stateChanged.connect(_write)
            form.addRow(_cap(title), box)

        def add_int(key: str, title: str, lo: int, hi: int) -> None:
            spin = QtWidgets.QSpinBox()
            spin.setRange(lo, hi)
            try:
                spin.setValue(int(node.get(key, defaults.get(key, lo))))
            except (TypeError, ValueError):
                spin.setValue(defaults.get(key, lo))

            def _write(value: int) -> None:
                node[key] = int(value)
                item.update()

            spin.valueChanged.connect(_write)
            form.addRow(_cap(title), _fit(spin))

        def add_choice(key: str, title: str, options: list[str]) -> None:
            combo = QtWidgets.QComboBox()
            combo.addItem("（默认）", "")
            for opt in options:
                combo.addItem(opt, opt)
            cur = str(node.get(key, defaults.get(key, "")) or "")
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
            form.addRow(_cap(title), _fit(combo))

        def add_prompt_block() -> None:
            edit = QtWidgets.QPlainTextEdit(str(node.get("prompt") or ""))
            edit.setPlaceholderText("内联提示词（优先于提示词文件；支持 ^{var}^ 变量）")
            edit.setFixedHeight(120)

            def _write() -> None:
                node["prompt"] = edit.toPlainText()
                item.update()

            edit.textChanged.connect(_write)
            form.addRow(_cap("提示词（内联）"), _fit(edit))
            add_bool("override_system", "替换系统提示词")
            add_text("prompt_files", "提示词文件（逗号分隔）", "逗号分隔", to_list=True)

        type_label = QtWidgets.QLabel(label)
        type_label.setStyleSheet(
            f"color: {TYPE_LABELS.get(ntype, ('', '#888'))[1]}; font-weight: bold; font-size: 15px;"
        )
        form.addRow(_cap("类型"), type_label)

        id_edit = QtWidgets.QLineEdit(str(node.get("id") or ""))

        def _write_id() -> None:
            node["id"] = id_edit.text().strip() or ntype
            item.update()

        id_edit.textEdited.connect(_write_id)
        form.addRow(_cap("节点 id"), _fit(id_edit))
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
            form.addRow(_cap("模型"), _fit(model_combo))
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
        # 内容随面板宽度自适应（标签换行+字段弹性），永不出现横向滚动裁切
        scroll.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
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

    def add_node(self, ntype: str, pos: QtCore.QPointF | None = None, node_data: dict | None = None) -> NodeItem:
        if ntype not in TYPE_LABELS:
            return None
        if node_data is None:
            node = {"id": self._unique_id(ntype), "type": ntype, "enabled": True}
            node.update(json.loads(json.dumps(NODE_DEFAULTS.get(ntype, {}))))
        else:
            # 加载保留缺省字段，面板按运行时默认展示
            node = json.loads(json.dumps(node_data))
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
            if self._drag_src is item:
                self.cancel_edge_drag()
            # 顺带从场景移除关联边（只移出列表不移场景会留下僵尸连线）
            for edge in [e for e in self._edges if e.src is item or e.dst is item]:
                self.scene.removeItem(edge)
                self._edges.remove(edge)
            if self._panel_item is item:
                removed_panel = True
            self.scene.removeItem(item)
            if item in self._nodes:
                self._nodes.remove(item)
        if removed_panel:
            self._panel_clear()
        self.update_edges()

    def delete_edges(self, edges: list["EdgeItem"]) -> None:
        for edge in edges:
            if edge in self._edges:
                self._edges.remove(edge)
                self.scene.removeItem(edge)
        if edges:
            self.statusBar().showMessage("已删除连线", 3000)

    def delete_selected(self) -> None:
        edges = [e for e in self._edges if e.isSelected()]
        nodes = [n for n in self._nodes if n.isSelected()]
        if not nodes and not edges:
            self.statusBar().showMessage("未选中节点或连线", 3000)
            return
        self.delete_edges(edges)
        self.delete_nodes(nodes)

    # --- 端口拖拽连线 ---------------------------------------------------

    @staticmethod
    def _port_scene_pos(item: NodeItem) -> QtCore.QPointF:
        pos = item.scenePos()
        return QtCore.QPointF(pos.x() + NODE_W, pos.y() + NODE_H / 2)

    def begin_edge_drag(self, src: NodeItem, scene_pos: QtCore.QPointF) -> None:
        self.cancel_edge_drag()
        self._drag_src = src
        self._rubber = RubberEdge(self._port_scene_pos(src))
        self._rubber.set_end(scene_pos)
        self.scene.addItem(self._rubber)
        src.update()  # 拖拽期间让源节点端口保持可见

    def update_edge_drag(self, scene_pos: QtCore.QPointF) -> None:
        if self._rubber is not None:
            self._rubber.set_end(scene_pos)

    def cancel_edge_drag(self) -> None:
        if self._rubber is not None:
            self.scene.removeItem(self._rubber)
            self._rubber = None
        if self._drag_src is not None:
            self._drag_src.update()
            self._drag_src = None

    def end_edge_drag(self, scene_pos: QtCore.QPointF) -> None:
        src = self._drag_src
        self.cancel_edge_drag()
        if src is None:
            return
        target = self._node_at(scene_pos)
        if target is None:
            self.statusBar().showMessage("连线已取消（落点不在节点上）", 3000)
            return
        if target is src:
            self.statusBar().showMessage("不允许节点自连", 3000)
            return
        # 两点间任一方向已有边 → 拒绝重复，提示而非静默改动
        for edge in self._edges:
            if {id(edge.src), id(edge.dst)} == {id(src), id(target)}:
                self.statusBar().showMessage(
                    f"{src.node['id']} 与 {target.node['id']} 间已存在连线（选中边后 Delete 可删除）", 5000
                )
                return
        edge = EdgeItem(src, target)
        self.scene.addItem(edge)
        self._edges.append(edge)
        self.statusBar().showMessage(f"连线 {src.node['id']} → {target.node['id']}", 5000)

    def _node_at(self, scene_pos: QtCore.QPointF) -> NodeItem | None:
        for item in reversed(self._nodes):  # 后添加的在上层，优先命中
            if item.sceneBoundingRect().contains(scene_pos):
                return item
        return None

    def update_edges(self) -> None:
        for edge in self._edges:
            edge.update_path()

    # --- 链计算与校验 ---------------------------------------------------

    def compute_chain(self) -> tuple:
        """由连线推导执行顺序（单链）；返回 (顺序|None, 错误列表)，失败时顺序为 None"""
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
            item = self.add_node(node["type"], node_data=node)
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
        self.cancel_edge_drag()
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
    app.setStyleSheet(STYLESHEET)  # 应用级兜底：独立运行时菜单/弹窗也能取到样式
    window = EditorWindow(name)
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
