"""Seite mit Abschnitten (Karten) aus Bezeichnung/Wert-Paaren – für Details- und Verbindung-Tab."""

from __future__ import annotations

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QFont
from PyQt6.QtWidgets import (QFormLayout, QFrame, QGroupBox, QHBoxLayout, QLabel, QPlainTextEdit,
                             QPushButton, QScrollArea, QVBoxLayout, QWidget)


class InfoPage(QScrollArea):
    def __init__(self, placeholder: str, raw: bool = False, parent=None):
        super().__init__(parent)
        self.setWidgetResizable(True)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        body = QWidget()
        self.setWidget(body)
        lay = QVBoxLayout(body)
        lay.setContentsMargins(10, 8, 10, 10)
        lay.setSpacing(8)
        self.title = QLabel(placeholder)
        f = self.title.font()
        f.setPointSizeF(f.pointSizeF() * 1.35)
        f.setBold(True)
        self.title.setFont(f)
        self.title.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        lay.addWidget(self.title)
        self.cols_w = QWidget()
        self.cols = QHBoxLayout(self.cols_w)
        self.cols.setContentsMargins(0, 0, 0, 0)
        self.cols.setSpacing(10)
        self.col_lay = []
        for _ in range(2):
            w = QWidget()
            v = QVBoxLayout(w)
            v.setContentsMargins(0, 0, 0, 0)
            v.setSpacing(10)
            v.addStretch()
            self.cols.addWidget(w, 1, Qt.AlignmentFlag.AlignTop)
            self.col_lay.append(v)
        lay.addWidget(self.cols_w)

        self.raw_btn = self.raw_edit = None
        if raw:
            self.raw_btn = QPushButton("Rohdaten (iw) anzeigen")
            self.raw_btn.setCheckable(True)
            self.raw_edit = QPlainTextEdit()
            self.raw_edit.setReadOnly(True)
            # nicht QFontDatabase.systemFont(FixedFont): verstellt sonst die Schrift der Graphen
            self.raw_edit.setStyleSheet("font-family: monospace;")
            self.raw_edit.setMinimumHeight(300)
            self.raw_edit.hide()
            self.raw_btn.toggled.connect(self.raw_edit.setVisible)
            lay.addWidget(self.raw_btn, 0, Qt.AlignmentFlag.AlignLeft)
            lay.addWidget(self.raw_edit)
            self.raw_btn.hide()
        lay.addStretch()

        self._shape: list | None = None
        self._values: list[QLabel] = []
        self._boxes: list[QGroupBox] = []

    def set_content(self, title: str, sections: list, raw_text: str = "") -> None:
        self.title.setText(title)
        shape = [(t, [l for l, _ in rows]) for t, rows in sections]
        if shape != self._shape:
            self._rebuild(sections)
            self._shape = shape
        else:
            vals = [v for _, rows in sections for _, v in rows]
            for lbl, v in zip(self._values, vals):
                if lbl.text() != v:
                    lbl.setText(v)
        if self.raw_btn is not None:
            self.raw_btn.setVisible(bool(raw_text))
            if raw_text != self.raw_edit.toPlainText():
                pos = self.raw_edit.verticalScrollBar().value()
                self.raw_edit.setPlainText(raw_text)
                self.raw_edit.verticalScrollBar().setValue(pos)

    def _rebuild(self, sections: list) -> None:
        for b in self._boxes:
            b.setParent(None)
            b.deleteLater()
        self._boxes, self._values = [], []
        heights = [0, 0]
        bold = QFont(self.font())
        bold.setBold(True)
        normal = QFont(self.font())
        normal.setBold(False)        # explizit, sonst erbt der Wert die fette Rahmenüberschrift
        for title, rows in sections:
            box = QGroupBox(title.replace("&", "&&"))   # & wäre sonst ein Tastenkürzel
            box.setFont(bold)
            form = QFormLayout(box)
            form.setHorizontalSpacing(14)
            form.setVerticalSpacing(5)
            form.setLabelAlignment(Qt.AlignmentFlag.AlignLeft)
            form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
            for label, value in rows:
                name = QLabel(label)
                name.setFont(bold)
                val = QLabel(value)
                val.setFont(normal)
                val.setWordWrap(True)
                val.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
                form.addRow(name, val)
                self._values.append(val)
            col = 0 if heights[0] <= heights[1] else 1
            heights[col] += len(rows) + 2
            v = self.col_lay[col]
            v.insertWidget(v.count() - 1, box)
            self._boxes.append(box)
