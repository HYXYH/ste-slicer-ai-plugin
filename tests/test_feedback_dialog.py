"""Native feedback form uses the real local API and asks only selected-area questions."""
import os
import time
import unittest

import test_print_experience as experience_tests
import test_assistant_tab as tab_tests


@unittest.skipUnless(tab_tests.HAS_QT, "Install requirements-dev.txt for native feedback tests")
class FeedbackDialogTests(unittest.TestCase):
    setUpClass = classmethod(tab_tests.AssistantTabTests.setUpClass.__func__)

    def setUp(self):
        experience_tests.PrintExperienceTests.setUp(self)
        from AIAssistant.assistant_controller import AssistantController
        from AIAssistant.feedback_dialog import FeedbackDialog
        self.controller = AssistantController(self.server)
        self.controller.current = self.sid
        self.dialog = FeedbackDialog(self.controller, self.feedback.state(self.sid), self.sid)
        self.dialog.show()
        self.qt.processEvents()

    snapshot = experience_tests.PrintExperienceTests.snapshot
    add_operation = experience_tests.PrintExperienceTests.add_operation
    target = None

    def tearDown(self):
        self.dialog.close()
        self.controller.shutdown()
        self.qt.processEvents()
        experience_tests.PrintExperienceTests.tearDown(self)
        self.dialog.deleteLater()
        self.controller.deleteLater()
        self.qt.processEvents()

    def pump(self, predicate):
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            self.qt.processEvents()
            if predicate():
                return
            time.sleep(.01)
        self.fail(self.dialog.message.text())

    def test_scoped_checklist_save_and_single_trial_consent(self):
        from PyQt5.QtCore import Qt
        self.dialog.result.setCurrentIndex(self.dialog.result.findData("defects"))
        self.assertLess(self.dialog.defects.count(), 18)
        self.dialog.group.setCurrentText("Нити и мелкие элементы")
        self.dialog.defects.item(0).setCheckState(Qt.Checked)
        self.dialog.printed.setChecked(True)
        self.dialog.save()
        self.pump(lambda: self.dialog.pid is not None)
        self.assertEqual(self.dialog.symptom_defect.count(), 1)
        self.dialog.external.setChecked(True)
        self.dialog.symptom.setCurrentIndex(1)
        self.dialog.propose()
        self.pump(lambda: self.dialog.trials.count() == 1)
        self.dialog.mutate("apply")
        self.assertEqual(self.bridge.mutations, [])
        self.dialog.consent.setChecked(True)
        self.dialog.mutate("apply")
        self.pump(lambda: self.dialog.trials.currentData()["data"]["state"] == "applied")
        self.assertEqual(self.values["material_print_temperature"], 205)
        folder = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".local-assistant-data"))
        os.makedirs(folder, exist_ok=True)
        self.dialog.grab().save(os.path.join(folder, "print-feedback-preview.png"))

    def test_unknown_result_requests_photos_and_keeps_data_unknown(self):
        self.dialog.result.setCurrentIndex(self.dialog.result.findData("unknown"))
        self.dialog.printed.setChecked(True)
        self.dialog.save()
        self.pump(lambda: self.dialog.pid is not None)
        self.assertIn("фото", self.dialog.message.text().lower())
        self.assertEqual(self.dialog.symptom_defect.count(), 0)
        self.assertEqual(self.db.library()["lessons"], [])
