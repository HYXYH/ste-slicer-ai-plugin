"""Small native dialogs for the first print-experience stage; chat remains the workspace."""
import json
import os

from PyQt5.QtCore import Qt
from PyQt5.QtGui import QPixmap
from PyQt5.QtWidgets import (QDialog, QVBoxLayout, QHBoxLayout, QFormLayout, QLabel, QComboBox,
                            QPlainTextEdit, QLineEdit, QCheckBox, QPushButton, QListWidget, QListWidgetItem,
                            QFileDialog, QScrollArea, QWidget, QTreeWidget, QTreeWidgetItem, QSplitter)

from .native_window import request_id, label, button, STYLE
from .print_diagnostics import DEFECTS
from .user_view import field_label

RESULTS = [("good", "Дефектов нет"), ("defects", "Есть дефекты"), ("not_checked", "Не проверялось"), ("unknown", "Не могу определить")]
OUTCOMES = [("resolved", "Дефект исчез"), ("better", "Стало лучше"), ("worse", "Стало хуже"), ("unchanged", "Без изменений"),
            ("not_checked", "Не проверялось"), ("unknown", "Не могу определить")]
DEGREES = {"hypothesis": "Гипотеза", "supported": "Есть улучшение", "confirmed": "Подтверждено повторной печатью", "rejected": "Не подтвердилось"}
STATES = {"proposed": "Предложено", "applied": "Применено", "evaluated": "Проверено", "rolled_back": "Выполнен откат",
          "unknown": "Исход не подтверждён", "applying": "Применяется", "rolling_back": "Выполняется откат"}


def library_text(value, library):
    if "photo" in value:
        return value["photo"]["name"] + "\nДвойной щелчок открывает фотографию."
    snapshot = value.get("snapshot")
    data = value.get("data", {})
    if not snapshot and data.get("print_id"):
        snapshot = next((p["snapshot"] for p in library["prints"] if p["id"] == data["print_id"]), None)
    lines = []
    if value.get("degree"):
        lines.append(DEGREES.get(value["degree"], value["degree"]))
    if data.get("key"):
        lines += [data["reason"], "Наблюдаемый признак: " + data.get("observed_symptom", "не указан"),
                  field_label(data["key"]) + ": " + str(data["before"]) + " → " + str(data["after"]) + " " + (data.get("unit") or ""),
                  "Состояние: " + STATES.get(data["state"], data["state"])]
        if data.get("outcome"):
            lines.append("Проверка: " + dict(OUTCOMES).get(data["outcome"], data["outcome"]))
    observations = value.get("observations", [value] if data.get("kind") == "user_observation" else [])
    for observation in observations:
        report = observation["data"]
        lines += ["\nНаблюдение пользователя: " + dict(RESULTS)[report["result"]], report["text"]]
        lines += ["• " + DEFECTS[k]["label"] for k in report["defects"]]
        for key, title in (("expected_mm", "По модели"), ("actual_mm", "Измерено")):
            if key in report["measurements"]:
                lines.append(title + ": " + " × ".join(str(v) if v is not None else "не проверялось" for v in report["measurements"][key]) + " мм")
        lines.append("Фотографии: " + (", ".join(p["name"] for p in report["photos"]) or "нет"))
    if snapshot:
        context = snapshot["context"]
        lines += ["\nУсловия применимости", "Принтер: " + str((context.get("printer") or {}).get("name", "не указан")),
                  "Режим: " + str((context.get("mode") or {}).get("name", (context.get("mode") or {}).get("id", "не указан")))]
        for extruder in context.get("extruders", []):
            material = extruder.get("material") or {}
            lines.append("Материал: " + str(material.get("name", material.get("id", "не указан"))) + "; сопло: " + str(extruder.get("nozzle_mm")) + " мм")
        for model in snapshot["objects"]:
            lines.append("Модель: " + model.get("name", "Модель") + "; размеры: " + " × ".join(str(v) for v in model.get("dimensions_mm") or []) + " мм")
        lines.append("\nСнимок использованных параметров")
        for scope in snapshot["settings"]:
            lines.append("Общие" if scope["target"]["scope"] == "global" else "Экструдер")
            lines += [field_label(key) + ": " + str(v) for key, v in sorted(scope["values"].items())]
    return "\n".join(lines)


class FeedbackDialog(QDialog):
    def __init__(self, controller, state, sid):
        super().__init__()
        self.controller, self.sid, self.state = controller, sid, state
        self.pid, self.selected, self.photos, self.working = None, set(), [], False
        self.report_request = None
        self.setWindowTitle("Сообщить о результате печати")
        self.setStyleSheet(STYLE)
        self.resize(760, 790)
        outer = QVBoxLayout(self)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        content = QWidget()
        self.layout = QVBoxLayout(content)
        scroll.setWidget(content)
        outer.addWidget(scroll)
        self.message = label("Осмотрите деталь до зачистки. Сохраняются параметры выбранного расчёта, а не текущие настройки.")
        self.layout.addWidget(self.message)
        form = QFormLayout()
        self.saved = QComboBox()
        self.saved.addItem("Новый результат печати", None)
        for item in reversed(state["prints"]):
            self.saved.addItem(item["created_at"] + " · " + item["operation_id"], item["id"])
        self.saved.currentIndexChanged.connect(self.select_print)
        form.addRow("История этого чата", self.saved)
        self.operation = QComboBox()
        for operation in state["operations"]:
            self.operation.addItem(" / ".join(operation["models"]) + " · " + operation["id"][:12], operation["id"])
        if self.operation.count():
            self.operation.setCurrentIndex(self.operation.count() - 1)
        form.addRow("Напечатанный расчёт", self.operation)
        self.result = QComboBox()
        for value, title in RESULTS:
            self.result.addItem(title, value)
        self.result.currentIndexChanged.connect(self.update_visibility)
        form.addRow("Результат", self.result)
        self.layout.addLayout(form)
        self.context_label = label("")
        self.layout.addWidget(self.context_label)
        self.operation.currentIndexChanged.connect(self.show_context)
        self.show_context()
        self.problem = QWidget()
        problem_layout = QVBoxLayout(self.problem)
        problem_layout.setContentsMargins(0, 0, 0, 0)
        problem_layout.addWidget(label("Выберите область, затем отметьте подходящие проблемы. Можно выбрать несколько областей или описать дефект текстом."))
        self.group = QComboBox()
        for name in dict.fromkeys(row["group"] for row in state["catalog"]):
            self.group.addItem(name)
        problem_layout.addWidget(self.group)
        self.defects = QListWidget()
        self.defects.setMinimumHeight(145)
        self.defects.setMaximumHeight(165)
        self.defects.itemChanged.connect(self.check_defect)
        problem_layout.addWidget(self.defects)
        self.group.currentIndexChanged.connect(self.show_group)
        self.selected_label = label("")
        problem_layout.addWidget(self.selected_label)
        self.layout.addWidget(self.problem)
        self.note = QPlainTextEdit()
        self.note.setPlaceholderText("Что получилось? Где и когда появляется дефект? Если не можете определить — приложите фотографии.")
        self.note.setMaximumHeight(85)
        self.layout.addWidget(self.note)
        measures = QFormLayout()
        self.expected, self.actual = [], []
        for title, fields in (("По модели, X / Y / Z, мм", self.expected), ("Измерено, X / Y / Z, мм", self.actual)):
            row = QHBoxLayout()
            for axis in ("X", "Y", "Z"):
                field = QLineEdit()
                field.setPlaceholderText(axis + " — не проверялось")
                fields.append(field)
                row.addWidget(field)
            measures.addRow(title, row)
        self.layout.addLayout(measures)
        self.photo_label = label("Фотографии: общий вид, сбоку, снизу")
        self.layout.addWidget(self.photo_label)
        self.layout.addWidget(button("Добавить фотографии", self.upload_photos))
        self.printed = QCheckBox("Подтверждаю, что этот расчёт действительно напечатан")
        self.layout.addWidget(self.printed)
        self.save_button = button("Сохранить результат", self.save, True)
        self.save_button.setEnabled(bool(self.operation.count()))
        self.layout.addWidget(self.save_button)
        if not self.operation.count():
            self.message.setText("Пока нет завершённого расчёта со снимком параметров. Пересчитайте модель; старые параметры не будут подменены текущими.")
        self.diagnostic_panel = QWidget()
        self.layout.addWidget(self.diagnostic_panel)
        self.layout = QVBoxLayout(self.diagnostic_panel)
        self.layout.setContentsMargins(0, 0, 0, 0)
        self.external_label = label(state["external"])
        self.layout.addWidget(self.external_label)
        self.external = QCheckBox("Внешние причины проверены: пластик, стол, подача и механика")
        self.layout.addWidget(self.external)
        self.symptom_defect, self.symptom = QComboBox(), QComboBox()
        self.symptom_defect.currentIndexChanged.connect(self.show_choices)
        self.layout.addWidget(self.symptom_defect)
        self.layout.addWidget(self.symptom)
        self.propose_button = button("Проверить одну гипотезу", self.propose)
        self.layout.addWidget(self.propose_button)
        self.proposal_text = label("")
        self.layout.addWidget(self.proposal_text)
        self.trials = QComboBox()
        self.trials.currentIndexChanged.connect(self.show_trial)
        self.layout.addWidget(self.trials)
        self.consent = QCheckBox("Согласен изменить только указанный параметр")
        self.layout.addWidget(self.consent)
        self.apply_button = button("Применить изменение", lambda: self.mutate("apply"))
        self.layout.addWidget(self.apply_button)
        self.repeat_button = button("Подготовить повторную печать с этими параметрами", self.repeat)
        self.layout.addWidget(self.repeat_button)
        self.outcome = QComboBox()
        for value, title in OUTCOMES:
            self.outcome.addItem(title, value)
        self.layout.addWidget(label("После сохранения результата новой печати выберите проверяемое исправление выше и укажите эффект:"))
        self.layout.addWidget(self.outcome)
        self.evaluate_button = button("Сохранить результат проверки", self.evaluate)
        self.layout.addWidget(self.evaluate_button)
        self.rollback_button = button("Согласовать и вернуть исходное значение", lambda: self.mutate("rollback"))
        self.layout.addWidget(self.rollback_button)
        outer.addWidget(button("Закрыть", self.accept))
        self.show_group()
        self.update_visibility()
        self.refresh_trials()

    def call(self, suffix, callback, body, method="POST"):
        if self.working:
            return
        self.working = True
        def done(value):
            self.working = False
            callback(value)
        def failed(error):
            self.working = False
            self.message.setText(error)
        self.controller.client.call(self.controller.path(self.sid) + "/print-feedback" + suffix, done, method, body, failed)

    def update_visibility(self):
        self.problem.setVisible(self.result.currentData() == "defects")
        if hasattr(self, "diagnostic_panel"):
            relevant = bool(self.pid) and self.symptom_defect.count() > 0
            self.diagnostic_panel.setVisible(bool(self.pid) and (relevant or self.trials.count() > 0))
            for widget in (self.external_label, self.external, self.symptom_defect, self.symptom, self.propose_button):
                widget.setVisible(relevant)

    def show_context(self):
        if hasattr(self, "printed"):
            self.printed.setChecked(False)
        operation = next((o for o in self.state["operations"] if o["id"] == self.operation.currentData()), None)
        if not operation:
            return
        context = operation["context"]
        self.context_label.setText("Условия расчёта: " + (context.get("printer") or {}).get("name", "Принтер") + " · " +
            (context.get("mode") or {}).get("name", (context.get("mode") or {}).get("id", "")) + " · " + "; ".join(
            str((e.get("material") or {}).get("name", (e.get("material") or {}).get("id", ""))) + ", сопло " + str(e.get("nozzle_mm")) + " мм" for e in context.get("extruders", [])))

    def show_group(self):
        self.defects.blockSignals(True)
        self.defects.clear()
        for row in self.state["catalog"]:
            if row["group"] != self.group.currentText():
                continue
            item = QListWidgetItem(row["label"])
            item.setData(Qt.UserRole, row["id"])
            item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
            item.setCheckState(Qt.Checked if row["id"] in self.selected else Qt.Unchecked)
            self.defects.addItem(item)
        self.defects.blockSignals(False)
        self.selected_label.setText("Отмечено: " + "; ".join(r["label"] for r in self.state["catalog"] if r["id"] in self.selected))

    def check_defect(self, item):
        if item.checkState() == Qt.Checked:
            self.selected.add(item.data(Qt.UserRole))
        else:
            self.selected.discard(item.data(Qt.UserRole))
        self.selected_label.setText("Отмечено проблем: " + str(len(self.selected)))

    def select_print(self):
        self.pid = self.saved.currentData()
        if not self.pid:
            self.selected, self.photos = set(), []
            self.note.clear()
            for field in self.expected + self.actual:
                field.clear()
            self.printed.setChecked(False)
            self.show_group()
            self.load_questions([])
            self.update_visibility()
            return
        item = next(p for p in self.state["prints"] if p["id"] == self.pid)
        report = item["observations"][-1]["data"]
        self.operation.setCurrentIndex(self.operation.findData(item["operation_id"]))
        self.result.setCurrentIndex(self.result.findData(report["result"]))
        self.note.setPlainText(report["text"])
        self.selected = set(report["defects"])
        self.show_group()
        self.load_questions(report["defects"])
        for name, fields in (("expected_mm", self.expected), ("actual_mm", self.actual)):
            for field, value in zip(fields, report["measurements"].get(name, [None] * 3)):
                field.setText(str(value) if value is not None else "")

    def load_questions(self, defects):
        self.symptom_defect.clear()
        for key in defects:
            row = next(r for r in self.state["catalog"] if r["id"] == key)
            self.symptom_defect.addItem(row["label"], key)
        self.show_choices()
        self.update_visibility()

    def show_choices(self):
        self.symptom.clear()
        key = self.symptom_defect.currentData()
        if key:
            row = next(r for r in self.state["catalog"] if r["id"] == key)
            self.symptom.addItem("Выберите наблюдаемый признак", None)
            for choice in row["choices"]:
                self.symptom.addItem(choice["symptom"], choice["id"])
            self.symptom.addItem("Не проверялось / Не могу определить", None)

    def upload_photos(self):
        paths, _ = QFileDialog.getOpenFileNames(self, "Фото: общий вид, сбоку и снизу", "", "Фото (*.jpg *.jpeg *.png)")
        if len(paths) + len(self.photos) > 12:
            self.message.setText("Можно добавить до 12 фотографий")
            return
        queue = list(paths)
        if not queue:
            return
        self.working = True
        self.save_button.setEnabled(False)
        def next_photo():
            if not queue:
                self.working = False
                self.save_button.setEnabled(bool(self.operation.count()))
                self.photo_label.setText("Фотографий добавлено: " + str(len(self.photos)))
                return
            path = queue.pop(0)
            def failed(error):
                self.message.setText(error)
                next_photo()
            self.controller.client.call(self.controller.path(self.sid) + "/files", lambda value: saved(value), "POST", failure=failed, upload=path)
        def saved(value):
            self.photos.append(value["id"])
            next_photo()
        next_photo()

    def save(self):
        if not self.printed.isChecked():
            self.message.setText("Подтвердите фактическую печать выбранного расчёта")
            return
        try:
            measurements = {name: [float(f.text().replace(",", ".")) if f.text().strip() else None for f in fields]
                            for name, fields in (("expected_mm", self.expected), ("actual_mm", self.actual))}
        except ValueError:
            self.message.setText("Укажите размеры числами в миллиметрах")
            return
        body = {"request_id": request_id(), "operation_id": self.operation.currentData(), "printed": True,
                "result": self.result.currentData(), "defects": sorted(self.selected) if self.result.currentData() == "defects" else [],
                "text": self.note.toPlainText(), "answers": {}, "measurements": measurements, "photo_ids": self.photos}
        if self.pid:
            body["print_id"] = self.pid
        payload_key = json.dumps({k: v for k, v in body.items() if k != "request_id"}, sort_keys=True, ensure_ascii=False)
        if self.report_request and self.report_request[0] == payload_key:
            body["request_id"] = self.report_request[1]
        else:
            self.report_request = (payload_key, body["request_id"])
        def saved(value):
            item = value["print"]
            self.pid = item["id"]
            self.state["prints"] = [p for p in self.state["prints"] if p["id"] != self.pid] + [item]
            if self.saved.findData(self.pid) < 0:
                self.saved.addItem(item["created_at"] + " · " + item["operation_id"], self.pid)
            self.saved.setCurrentIndex(self.saved.findData(self.pid))
            self.message.setText("Результат сохранён. " + " ".join(q["text"] for q in value["questions"]))
            self.load_questions(item["observations"][-1]["data"]["defects"])
            self.controller.poll()
        self.call("", saved, body)

    def propose(self):
        if not self.pid or self.symptom.currentData() is None:
            self.message.setText("Сохраните результат и выберите наблюдаемый признак. Неизвестное остаётся неизвестным.")
            return
        def proposed(value):
            if value.get("manual"):
                self.proposal_text.setText(value["manual"])
                return
            adjustment = value["adjustment"]
            item = next(p for p in self.state["prints"] if p["id"] == self.pid)
            item["adjustments"] = [a for a in item["adjustments"] if a["id"] != adjustment["id"]] + [adjustment]
            self.refresh_trials(adjustment["id"])
        self.call("/" + self.pid + "/diagnose", proposed, {"defect": self.symptom_defect.currentData(), "choice": self.symptom.currentData(), "external_checked": self.external.isChecked()})

    def refresh_trials(self, selected=None):
        self.trials.clear()
        for item in self.state["prints"]:
            for adjustment in item["adjustments"]:
                self.trials.addItem(adjustment["data"]["reason"] + " · " + STATES.get(adjustment["data"]["state"], adjustment["data"]["state"]), adjustment)
        if selected:
            for i in range(self.trials.count()):
                if self.trials.itemData(i)["id"] == selected:
                    self.trials.setCurrentIndex(i)
        self.show_trial()
        self.update_visibility()

    def show_trial(self):
        trial = self.trials.currentData()
        self.consent.setChecked(False)
        state = trial["data"]["state"] if trial else ""
        self.apply_button.setEnabled(state == "proposed")
        self.repeat_button.setEnabled(state == "applied")
        self.evaluate_button.setEnabled(state == "applied")
        self.rollback_button.setEnabled(state in ("applied", "evaluated"))
        if trial:
            data = trial["data"]
            self.proposal_text.setText("Проверяемое исправление: " + data["reason"] + "\n" + field_label(data["key"]) + ": " + str(data["before"]) + " → " + str(data["after"]) +
                                      " " + (data.get("unit") or "") + "\nСостояние: " + STATES.get(data["state"], data["state"]) + ". " +
                                      ("Результат: " + dict(OUTCOMES)[data["outcome"]] if data.get("outcome") else "Одна пробная настройка; причина ещё не доказана."))

    def updated_trial(self, value):
        for item in self.state["prints"]:
            item["adjustments"] = [value if a["id"] == value["id"] else a for a in item["adjustments"]]
        self.refresh_trials(value["id"])
        self.controller.poll()

    def mutate(self, action):
        trial = self.trials.currentData()
        if not trial or not self.consent.isChecked():
            self.message.setText("Нужно согласие на указанное изменение или возврат исходного значения")
            return
        self.call("/" + trial["id"] + "/" + action, self.updated_trial, {"consent": True})

    def repeat(self):
        trial = self.trials.currentData()
        if trial:
            self.call("/" + trial["id"] + "/repeat-plan", lambda value: (self.controller.poll(), self.accept()), {})

    def evaluate(self):
        trial = self.trials.currentData()
        if not trial or not self.pid:
            self.message.setText("Сначала сохраните результат повторной печати")
            return
        self.call("/" + trial["id"] + "/evaluate", self.updated_trial, {"repeat_print_id": self.pid, "outcome": self.outcome.currentData()})


def show_library(controller, data):
    dialog = QDialog()
    dialog.setWindowTitle("Опыт печати — последние 100 записей")
    dialog.setStyleSheet(STYLE)
    dialog.resize(1000, 700)
    layout = QVBoxLayout(dialog)
    layout.addWidget(label("Наблюдения — сообщения пользователя. Гипотезы и улучшения отдельно от подтверждённых исправлений. Откройте запись для просмотра условий и параметров."))
    splitter = QSplitter()
    tree = QTreeWidget()
    tree.setHeaderLabels(["Печать / вывод", "Результат"])
    details = QPlainTextEdit()
    details.setReadOnly(True)
    splitter.addWidget(tree)
    splitter.addWidget(details)
    splitter.setSizes([430, 570])
    layout.addWidget(splitter)
    for item in data["prints"]:
        context = item["snapshot"]["context"]
        title = item["created_at"] + " · " + str((context.get("printer") or {}).get("name", "Принтер"))
        result = item["observations"][-1]["data"]
        row = QTreeWidgetItem(tree, [title, dict(RESULTS).get(result["result"], result["result"])])
        row.setData(0, Qt.UserRole, item)
        for observation in item["observations"]:
            observed = observation["data"]
            child = QTreeWidgetItem(row, ["Наблюдение: " + ", ".join(DEFECTS[k]["label"] for k in observed["defects"]), dict(RESULTS).get(observed["result"], "")])
            child.setData(0, Qt.UserRole, observation)
            for photo in observed["photos"]:
                child_photo = QTreeWidgetItem(child, [photo["name"], "Фото — двойной щелчок"])
                child_photo.setData(0, Qt.UserRole, {"photo": photo})
        for adjustment in item["adjustments"]:
            child = QTreeWidgetItem(row, [adjustment["data"]["reason"], STATES.get(adjustment["data"]["state"], adjustment["data"]["state"])])
            child.setData(0, Qt.UserRole, dict(adjustment, data=dict(adjustment["data"], print_id=item["id"])))
    for lesson in data["lessons"]:
        row = QTreeWidgetItem(tree, ["Вывод: " + lesson["data"]["reason"], DEGREES.get(lesson["degree"], lesson["degree"])])
        row.setData(0, Qt.UserRole, lesson)
    tree.currentItemChanged.connect(lambda item, previous: details.setPlainText(library_text(item.data(0, Qt.UserRole), data)) if item else None)
    def preview(item, column):
        value = item.data(0, Qt.UserRole)
        if not isinstance(value, dict) or "photo" not in value:
            return
        photo = value["photo"]
        # IDs originate from persisted validated attachments, never arbitrary local paths.
        path = os.path.join(controller.server.print_feedback.db.root, os.path.basename(photo["id"]))
        image = QPixmap(path)
        if image.isNull():
            return
        viewer = QDialog(dialog)
        viewer.setWindowTitle(photo["name"])
        box = QVBoxLayout(viewer)
        picture = QLabel()
        picture.setPixmap(image.scaledToWidth(min(850, image.width())))
        box.addWidget(picture)
        box.addWidget(button("Закрыть", viewer.accept))
        viewer.exec_()
    tree.itemDoubleClicked.connect(preview)
    layout.addWidget(button("Закрыть", dialog.accept))
    dialog.exec_()
