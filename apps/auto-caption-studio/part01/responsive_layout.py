"""Responsive desktop-window layout controller.

Targets narrow Windows/RDP windows down to roughly 640 px. It does not pretend
to be a native mobile application; it safely reflows the existing desktop UI.
"""
from __future__ import annotations

from PySide6.QtCore import Qt


class ResponsiveLayoutController:
    WIDE = "wide"
    COMPACT = "compact"
    NARROW = "narrow"

    def __init__(self, window):
        self.window = window
        self.mode = ""

    @staticmethod
    def mode_for_width(width: int) -> str:
        if width < 900:
            return ResponsiveLayoutController.NARROW
        if width < 1350:
            return ResponsiveLayoutController.COMPACT
        return ResponsiveLayoutController.WIDE

    def apply(self, width: int, *, force: bool = False) -> str:
        w = self.window
        mode = self.mode_for_width(max(1, int(width)))
        if mode == self.mode and not force:
            return mode
        self.mode = mode

        splitter = getattr(w, "main_splitter", None)
        settings = getattr(w, "settings_panel", None)
        studio = getattr(w, "studio_panel", None)
        narrow_workspace = getattr(w, "narrow_workspace", None)
        studio_scroll = getattr(w, "narrow_studio_scroll", None)
        narrow_actions = getattr(w, "narrow_action_bar", None)
        if splitter is not None and settings is not None and studio is not None:
            settings.setMinimumWidth(0)
            studio.setMinimumWidth(0)
            settings.setMaximumWidth(16777215)
            studio.setMaximumWidth(16777215)
            if mode == self.NARROW and narrow_workspace is not None and studio_scroll is not None:
                # Two full desktop panels cannot be meaningfully squeezed into
                # a 600 px-high vertical splitter. Use a compact page switcher;
                # the preview page scrolls vertically and the editor keeps its
                # own per-tab scroll behavior.
                splitter.setVisible(False)
                if narrow_workspace.indexOf(settings) < 0:
                    narrow_workspace.insertTab(0, settings, "Editor / Queue")
                if studio_scroll.widget() is not studio:
                    studio_scroll.setWidget(studio)
                if narrow_workspace.indexOf(studio_scroll) < 0:
                    narrow_workspace.insertTab(1, studio_scroll, "Preview")
                restore_index = int(narrow_workspace.property("restore_index") or narrow_workspace.currentIndex() or 0)
                narrow_workspace.setCurrentIndex(max(0, min(1, restore_index)))
                narrow_workspace.setVisible(True)
                if narrow_actions is not None:
                    narrow_actions.setVisible(True)
            else:
                if narrow_workspace is not None:
                    try:
                        narrow_workspace.setProperty("restore_index", max(0, narrow_workspace.currentIndex()))
                    except Exception:
                        pass
                    while narrow_workspace.indexOf(settings) >= 0:
                        narrow_workspace.removeTab(narrow_workspace.indexOf(settings))
                    while studio_scroll is not None and narrow_workspace.indexOf(studio_scroll) >= 0:
                        narrow_workspace.removeTab(narrow_workspace.indexOf(studio_scroll))
                    narrow_workspace.setVisible(False)
                if studio_scroll is not None and studio_scroll.widget() is studio:
                    studio_scroll.takeWidget()
                splitter.setOrientation(Qt.Orientation.Horizontal)
                splitter.insertWidget(0, settings)
                splitter.insertWidget(1, studio)
                splitter.setSizes([690, 560] if mode == self.COMPACT else [820, 740])
                splitter.setVisible(True)
                if narrow_actions is not None:
                    narrow_actions.setVisible(False)

        subtitle = getattr(w, "header_subtitle", None)
        if subtitle is not None:
            subtitle.setVisible(mode != self.NARROW)
        for name in ("btn_open", "btn_save", "btn_check_ffmpeg"):
            button = getattr(w, name, None)
            if button is not None:
                button.setVisible(mode != self.NARROW)
        more = getattr(w, "btn_header_more", None)
        if more is not None:
            more.setVisible(mode == self.NARROW)

        # Queue tables become a useful three-column list instead of a squeezed
        # spreadsheet. Full columns return automatically on wider windows.
        table = getattr(w, "queue_table", None)
        if table is not None:
            compact_columns = {0, 1, 3}
            for column in range(table.columnCount()):
                table.setColumnHidden(column, mode == self.NARROW and column not in compact_columns)

        # Reflow action buttons without creating duplicate widgets/signals.
        preview_grid = getattr(w, "preview_controls_layout", None)
        preview_buttons = [
            getattr(w, "btn_preview_play", None),
            getattr(w, "btn_render_now", None),
            getattr(w, "btn_open_output", None),
        ]
        if preview_grid is not None:
            for button in preview_buttons:
                if button is not None:
                    preview_grid.removeWidget(button)
            if mode == self.NARROW:
                for index, button in enumerate([b for b in preview_buttons if b is not None]):
                    preview_grid.addWidget(button, index // 2, index % 2)
            else:
                for index, button in enumerate([b for b in preview_buttons if b is not None]):
                    preview_grid.addWidget(button, 0, index)

        toolbar = getattr(w, "queue_toolbar_layout", None)
        queue_buttons = list(getattr(w, "queue_toolbar_buttons", []) or [])
        if toolbar is not None and queue_buttons:
            for button in queue_buttons:
                toolbar.removeWidget(button)
            columns = 2 if mode == self.NARROW else 3
            for index, button in enumerate(queue_buttons):
                toolbar.addWidget(button, index // columns, index % columns)
            for column in range(3):
                toolbar.setColumnStretch(column, 1 if column < columns else 0)


        # Long text-template button labels otherwise force a hidden horizontal
        # overflow in the nested Text tab. Reflow the existing buttons instead
        # of creating a second mobile-only widget tree.
        effects_grid = getattr(w, "premium_text_effects_layout", None)
        effect_buttons_map = getattr(w, "premium_text_template_buttons", {}) or {}
        effect_buttons = list(effect_buttons_map.values())
        if effects_grid is not None and effect_buttons:
            for button in effect_buttons:
                effects_grid.removeWidget(button)
            effect_columns = 2 if mode == self.NARROW else (3 if mode == self.COMPACT else 4)
            for index, button in enumerate(effect_buttons):
                effects_grid.addWidget(button, index // effect_columns, index % effect_columns)
            for column in range(4):
                effects_grid.setColumnStretch(column, 1 if column < effect_columns else 0)

        preview = getattr(w, "preview_label", None)
        if preview is not None:
            preview.setMinimumWidth(260 if mode == self.NARROW else 320)
            preview.setMaximumWidth(16777215 if mode != self.WIDE else 760)

        timeline = getattr(w, "timeline_box", None)
        if timeline is not None and mode == self.NARROW:
            timeline.setMinimumHeight(230)

        tabs = getattr(w, "tabs", None)
        if tabs is not None:
            tabs.tabBar().setExpanding(False)
            tabs.setUsesScrollButtons(True)

        try:
            w.user_settings.setValue("responsive_layout_mode", mode)
        except Exception:
            pass
        return mode
