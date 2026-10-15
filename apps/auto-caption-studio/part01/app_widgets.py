from __future__ import annotations

import math

from PySide6.QtCore import Qt, QUrl, QSize, Signal
from PySide6.QtGui import QDesktopServices, QPixmap
from PySide6.QtWidgets import QLabel, QSlider, QSizePolicy


class TimelineSlider(QSlider):
    """Horizontal slider that jumps to the exact clicked/dragged timeline position."""

    def __init__(self, orientation: Qt.Orientation, parent=None):
        super().__init__(orientation, parent)
        self.setTracking(True)
        self.setSingleStep(1)
        self.setPageStep(25)

    def _set_value_from_event(self, event) -> None:
        if self.orientation() != Qt.Orientation.Horizontal or self.width() <= 0:
            return
        try:
            x = float(event.position().x())
        except AttributeError:
            x = float(event.pos().x())
        ratio = max(0.0, min(1.0, x / max(1, self.width())))
        value = self.minimum() + round((self.maximum() - self.minimum()) * ratio)
        self.setValue(int(value))
        try:
            self.sliderMoved.emit(int(value))
        except Exception:
            pass

    def mousePressEvent(self, event) -> None:  # type: ignore[override]
        if event.button() == Qt.MouseButton.LeftButton:
            self._set_value_from_event(event)
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:  # type: ignore[override]
        if event.buttons() & Qt.MouseButton.LeftButton:
            self._set_value_from_event(event)
            event.accept()
            return
        super().mouseMoveEvent(event)


class AspectRatioPreviewLabel(QLabel):
    """Preview label that keeps the studio canvas at 16:9 and supports mouse layer dragging."""

    canvasPositionChanged = Signal(float, float)
    canvasDragFinished = Signal(float, float)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._canvas_dragging = False
        self.setMouseTracking(True)
        self.setCursor(Qt.CursorShape.OpenHandCursor)
        self.setToolTip("CapCut-style canvas: choose a layer below, then drag directly on the preview to adjust X/Y.")

    def hasHeightForWidth(self) -> bool:  # type: ignore[override]
        return True

    def heightForWidth(self, width: int) -> int:  # type: ignore[override]
        return max(180, int(width * 9 / 16))

    def sizeHint(self) -> QSize:  # type: ignore[override]
        return QSize(854, 480)

    def minimumSizeHint(self) -> QSize:  # type: ignore[override]
        return QSize(360, 202)

    def _event_to_canvas_percent(self, event) -> tuple[float, float]:
        try:
            pos = event.position()
            x = float(pos.x())
            y = float(pos.y())
        except AttributeError:
            pos = event.pos()
            x = float(pos.x())
            y = float(pos.y())

        content_x = 0.0
        content_y = 0.0
        content_w = max(1.0, float(self.width()))
        content_h = max(1.0, float(self.height()))
        pix = self.pixmap()
        if pix is not None and not pix.isNull():
            content_w = max(1.0, float(pix.width()))
            content_h = max(1.0, float(pix.height()))
            content_x = max(0.0, (float(self.width()) - content_w) / 2.0)
            content_y = max(0.0, (float(self.height()) - content_h) / 2.0)

        px = max(0.0, min(100.0, ((x - content_x) / content_w) * 100.0))
        py = max(0.0, min(100.0, ((y - content_y) / content_h) * 100.0))
        return px, py

    def mousePressEvent(self, event) -> None:  # type: ignore[override]
        if event.button() == Qt.MouseButton.LeftButton:
            self._canvas_dragging = True
            self.setCursor(Qt.CursorShape.ClosedHandCursor)
            x, y = self._event_to_canvas_percent(event)
            self.canvasPositionChanged.emit(x, y)
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:  # type: ignore[override]
        if self._canvas_dragging and (event.buttons() & Qt.MouseButton.LeftButton):
            x, y = self._event_to_canvas_percent(event)
            self.canvasPositionChanged.emit(x, y)
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:  # type: ignore[override]
        if self._canvas_dragging and event.button() == Qt.MouseButton.LeftButton:
            self._canvas_dragging = False
            self.setCursor(Qt.CursorShape.OpenHandCursor)
            x, y = self._event_to_canvas_percent(event)
            self.canvasDragFinished.emit(x, y)
            event.accept()
            return
        super().mouseReleaseEvent(event)


