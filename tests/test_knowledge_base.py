"""Knowledge status, provenance, filtering and exclusions use real SQLite evidence."""
import copy
import os
import tempfile
import unittest

import test_workflow
from AIAssistant.experience import ExperienceStore
from AIAssistant.knowledge_base import KnowledgeBase, derive_conclusions, knowledge_conditions
from AIAssistant.knowledge_view import projection
from AIAssistant.print_diagnostics import catalog
from AIAssistant.session import uid, now


class KnowledgeBaseTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db = ExperienceStore(self.temp.name)
        self.snapshot = {"context": {"printer": {"id": "p1", "name": "Принтер 1"}, "mode": {"id": "classic"},
                         "profile": {"quality": {"id": "normal"}}, "extruders": [{"id": "e1", "nozzle_mm": .4,
                         "material": {"id": "proto_pla", "name": "PLA"}}]},
                         "objects": [{"id": "node", "name": "Деталь", "geometry_revision": "mesh-a", "dimensions_mm": [20, 20, 20]}],
                         "settings": [{"target": {"scope": "extruder", "extruder_id": "e1"}, "values": {"material_print_temperature": 210, "speed_print": 50}}]}

    def printed(self, result="good", snapshot=None, defects=None):
        operation = {"id": uid(), "print_snapshot": copy.deepcopy(snapshot or self.snapshot)}
        data = {"result": result, "text": "Наблюдение", "defects": defects or [], "kind": "user_observation", "printed": True,
                "measurements": {}, "photos": [], "answers": {}}
        return self.db.record("chat", operation, data, uid())

    def trial(self, outcome="resolved", after=205):
        original = self.printed("defects", defects=["stringing"])
        applied = copy.deepcopy(self.snapshot)
        applied["settings"][0]["values"]["material_print_temperature"] = after
        data = {"defect": "stringing", "choice": "0", "key": "material_print_temperature", "target": self.snapshot["settings"][0]["target"],
                "before": 210, "after": after, "unit": "°C", "reason": "Снизить температуру", "state": "proposed", "snapshot_ref": {}, "outcome": None}
        trial = self.db.propose(original["id"], original["observations"][0]["id"], data)
        repeated = self.printed("good" if outcome == "resolved" else "defects", applied, [] if outcome == "resolved" else ["stringing"])
        self.db.update_adjustment(trial["id"], {"outcome": outcome, "repeat_print_id": repeated["id"], "state": "evaluated", "applied_snapshot": applied}, "observation")
        return original, repeated, trial

    def conclusions(self):
        return derive_conclusions(self.db.library(limit=None))

    def correction(self):
        return next(c for c in self.conclusions() if c["type"] == "correction")

    def revise(self, observation, **changes):
        data = copy.deepcopy(observation["data"])
        data.update(changes)
        self.db.revise_observation(observation["id"], data, False, "Исправлена ошибочная оценка")

    def test_one_success_is_observation_two_disjoint_trials_are_confirmed(self):
        self.trial()
        self.assertEqual(self.correction()["status"], "observation")
        self.assertEqual(self.db.find(self.snapshot)["lessons"], [])
        self.trial()
        conclusion = self.correction()
        self.assertEqual((conclusion["status"], conclusion["confirmations"]), ("confirmed", 2))
        self.assertEqual(len(conclusion["print_ids"]), 4)
        self.assertTrue(self.db.find(self.snapshot)["lessons"])

    def test_same_physical_repeat_cannot_count_twice(self):
        original, repeated, trial = self.trial()
        data = copy.deepcopy(trial["data"])
        data.update(choice="1", reason="Вторая формулировка той же проверки")
        duplicate = self.db.propose(original["id"], original["observations"][0]["id"], data)
        old = self.db.adjustment(trial["id"])["data"]
        self.db.update_adjustment(duplicate["id"], {"repeat_print_id": repeated["id"], "applied_snapshot": old["applied_snapshot"], "outcome": "resolved", "state": "evaluated"})
        self.assertEqual(self.correction()["confirmations"], 1)

    def test_exclusion_downgrades_and_restoring_recalculates_without_extra_proof(self):
        self.trial()
        original, repeated, trial = self.trial()
        observation = repeated["observations"][0]
        self.db.revise_observation(observation["id"], observation["data"], True, "Ошибка измерения")
        self.assertEqual(self.correction()["status"], "observation")
        self.assertEqual(self.db.find(self.snapshot)["lessons"], [])
        reopened = ExperienceStore(self.temp.name)
        self.assertTrue(reopened.get(repeated["id"])["observations"][0]["excluded"])
        self.db.revise_observation(observation["id"], observation["data"], False, "Измерение проверено")
        self.assertEqual(self.correction()["confirmations"], 2)
        self.assertEqual(len(self.db.get(repeated["id"])["observations"][0]["revisions"]), 2)

    def test_contradiction_blocks_reuse_and_editing_invalid_result_removes_evidence(self):
        self.trial()
        self.trial()
        original, repeated, trial = self.trial("unchanged")
        self.assertEqual(self.correction()["status"], "conflicting")
        self.assertEqual(self.db.find(self.snapshot)["lessons"], [])
        self.db.revise_observation(repeated["observations"][0]["id"], repeated["observations"][0]["data"], True, "Использован другой пластик")
        self.assertEqual(self.correction()["status"], "confirmed")
        first = next(p for p in self.db.library(limit=None)["prints"] if p["observations"][-1]["data"]["result"] == "good")
        self.revise(first["observations"][-1], result="defects", defects=["stringing"])
        self.assertEqual(self.correction()["status"], "observation")

    def test_working_configurations_and_problems_come_from_checked_results(self):
        self.printed("not_checked")
        self.assertEqual(self.conclusions(), [])
        self.printed()
        self.printed()
        self.assertEqual(next(c for c in self.conclusions() if c["type"] == "configuration")["status"], "confirmed")
        self.printed("defects", defects=["stringing"])
        self.assertTrue(all(c["status"] == "conflicting" for c in self.conclusions()))

    def test_similar_other_mesh_can_match_but_other_equipment_or_geometry_cannot(self):
        self.trial()
        self.trial()
        similar = copy.deepcopy(self.snapshot)
        similar["objects"][0].update(geometry_revision="another-stl", name="Другая деталь", dimensions_mm=[21, 21, 21])
        self.assertEqual(knowledge_conditions(self.snapshot), knowledge_conditions(similar))
        self.assertTrue(self.db.find(similar)["lessons"])
        for change in (lambda s: s["context"]["extruders"][0].update(nozzle_mm=.6),
                       lambda s: s["context"]["mode"].update(id="cylindrical"),
                       lambda s: s["objects"][0].update(dimensions_mm=[200, 20, 2])):
            other = copy.deepcopy(similar)
            change(other)
            self.assertEqual(self.db.find(other)["lessons"], [])

    def test_projection_filters_groups_and_links_both_directions(self):
        original, repeated, trial = self.trial()
        library = self.db.library(limit=None)
        data = dict(library, conclusions=self.conclusions(), defects=catalog())
        view = projection(data, {"tab": "prints", "search": "PLA", "printer": "Принтер 1", "type": "ordinary", "status": "good", "group_by": "material", "selection": repeated["id"]})
        self.assertEqual(len(view["rows"]), 1)
        self.assertEqual(view["rows"][0]["group_label"], "PLA")
        self.assertTrue(view["detail"]["related"])
        conclusion = self.correction()
        detail = projection(data, {"tab": "conclusions", "selection": conclusion["id"]})["detail"]
        self.assertEqual(set(r["id"] for r in detail["related"]), {original["id"], repeated["id"]})

    def test_catalog_is_shared_structured_and_all_illustrations_are_local(self):
        entries = catalog()
        self.assertEqual(len(entries), 18)
        root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "plugin", "AIAssistant", "qml"))
        for row in entries:
            self.assertEqual(len(row["choices"]), 3)
            self.assertTrue(row["description"] and row["warnings"])
            self.assertTrue(os.path.isfile(os.path.join(root, row["illustration"]["path"])))

    def test_calibration_import_is_idempotent_and_unknown_settings_are_not_invented(self):
        record = {"id": "old-cube", "created_at": now(), "x_mm": 20., "y_mm": 20., "z_mm": 20., "printed": True,
                  "adhesion_ok": True, "layers_ok": False, "surface_ok": False, "process_signature": "old-process"}
        class Sessions:
            def listing(self):
                return {"sessions": [{"id": "cube-chat"}]}
            def internal(self, sid):
                return {"id": sid, "calibration_results": [record]}
        base = KnowledgeBase(Sessions(), self.db)
        first, second = base.snapshot(), base.snapshot()
        self.assertEqual(len(second["prints"]), 1)
        self.assertEqual(first["prints"][0]["id"], second["prints"][0]["id"])
        self.assertEqual(second["prints"][0]["snapshot"]["settings"], [])
        self.assertEqual(second["prints"][0]["observations"][0]["data"]["defects"], [])
