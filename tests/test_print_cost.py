"""Tariffs and captured slice estimates: no manual total costs or invented grams."""
import copy
import math
import tempfile
import unittest

import test_workflow
from AIAssistant.session import SessionStore, ApiError
from AIAssistant.print_cost import PrintPricing, estimate, slice_mass


class PrintCostTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = SessionStore(self.temp.name)
        self.sid = self.store.listing()["active_session_id"]
        self.pricing = PrintPricing(self.store)
        self.snapshot = {"context": {"printer": {"id": "p1"}, "mode": {"id": "classic"}, "profile": {},
            "extruders": [{"id": "e1", "position": "0", "nozzle_mm": .4, "material": {"id": "pla", "name": "PLA"}}]},
            "objects": [{"dimensions_mm": [20,20,20]}], "settings": [{"target": {"scope": "extruder", "extruder_id": "e1"}, "values": {"material_diameter": 1.75, "material_density": 1.24, "speed_print": 50}}]}
        self.operation = {"id": "slice-main", "kind": "slice", "status": "completed", "created_at": "1", "plan_id": "main", "print_snapshot": copy.deepcopy(self.snapshot),
            "result": {"time": {"value": 7200, "unit": "s"}, "material_mass": {"values": [100.], "unit": "g"}, "material": {"values": [30.], "unit": "m"}}}
        self.rates = {"materials": {"pla": 2.5}, "printers": {"p1": 50.}}

    def test_price_from_grams_and_seconds_and_explicit_zero_rates(self):
        cost = estimate(self.operation, self.rates)
        self.assertEqual(cost["cost"], 350.)
        self.assertEqual(cost["grams"], 100.)
        self.assertEqual(cost["hours"], 2.)
        self.assertEqual(estimate(self.operation, {"materials": {"pla": 0}, "printers": {"p1": 0}})["cost"], 0)
        self.assertIsNone(estimate(self.operation, {"materials": {"pla": 2.5}, "printers": {}})["cost"])
        self.assertIsNone(estimate(None, self.rates)["cost"])

    def test_old_length_uses_captured_diameter_and_density_only(self):
        self.operation["result"].pop("material_mass")
        expected = 30 * math.pi * (.875 ** 2) * 1.24
        self.assertAlmostEqual(slice_mass(self.operation)[0]["grams"], expected)
        self.operation["print_snapshot"]["settings"][0]["values"].pop("material_density")
        self.assertIsNone(estimate(self.operation, self.rates)["cost"])
        self.assertIsNone(estimate(self.operation, self.rates)["grams"])

    def test_rates_persist_per_material_and_printer_and_empty_clears(self):
        self.pricing.save(self.snapshot, {"printer_id": "p1", "printer_hour": 50., "material_prices": {"pla": 2.5}})
        reopened = PrintPricing(SessionStore(self.store.root))
        self.assertEqual(reopened.tariffs(), self.rates)
        other = copy.deepcopy(self.snapshot)
        other["context"]["printer"]["id"] = "p2"
        other["context"]["extruders"][0]["material"]["id"] = "petg"
        self.pricing.save(other, {"printer_id": "p2", "printer_hour": 20, "material_prices": {"petg": 3}})
        self.assertEqual(self.pricing.tariffs()["materials"]["pla"], 2.5)
        self.pricing.save(self.snapshot, {"printer_id": "p1", "printer_hour": None, "material_prices": {"pla": None}})
        self.assertNotIn("p1", self.pricing.tariffs()["printers"])
        self.assertEqual(self.pricing.tariffs()["materials"], {"petg": 3})

    def test_invalid_rates_and_stale_context_are_rejected(self):
        for value in (-1, float("nan"), True, "2.5"):
            with self.assertRaises(ApiError):
                self.pricing.save(self.snapshot, {"printer_id": "p1", "printer_hour": value, "material_prices": {"pla": 2.5}})
        with self.assertRaises(ApiError):
            self.pricing.save(self.snapshot, {"printer_id": "p2", "printer_hour": 50, "material_prices": {"pla": 2.5}})

    def test_tariff_command_works_before_a_plan_exists(self):
        import types
        from AIAssistant.calibration import Calibration
        from AIAssistant.experience import ExperienceStore
        bridge = types.SimpleNamespace(call=lambda *args: copy.deepcopy(self.snapshot))
        server = types.SimpleNamespace(store=self.store, bridge=bridge, print_feedback=types.SimpleNamespace(db=ExperienceStore(self.store.root)))
        calibration = Calibration(server)
        result = calibration.command(self.sid, "tariffs", {"tariffs": {"printer_id": "p1", "printer_hour": 50, "material_prices": {"pla": 2.5}}})
        self.assertEqual(result["pricing"]["printer_hour"], 50)
        self.assertEqual(result["pricing"]["materials"][0]["price"], 2.5)
        self.assertIsNone(result["pricing"]["main"]["cost"])

    def test_pricing_matches_slice_conditions_and_separates_main_and_test(self):
        self.pricing.save(self.snapshot, {"printer_id": "p1", "printer_hour": 50, "material_prices": {"pla": 2.5}})
        session = self.store.internal(self.sid)
        trial = copy.deepcopy(self.operation)
        trial.update(id="slice-test", plan_id="test", created_at="2")
        trial["result"]["material_mass"]["values"] = [10.]
        trial["result"]["time"]["value"] = 360
        trial["print_snapshot"]["objects"][0]["dimensions_mm"] = [10,8,20]
        session.update(calibration_decision={"plan_id": "main"}, intelligent_calibration={"state": "review", "main_plan_id": "main", "plan_ids": ["test"], "test": {"id": "cube"}},
                       operations={"main": self.operation, "test": trial})
        value = self.pricing.view(session, self.snapshot, {"id": "cube"})
        self.assertEqual(value["costs"], {"main_loss": 350., "test_cost": 30.})
        trial["status"] = "stale"
        self.assertNotIn("test_cost", self.pricing.view(session, self.snapshot, {"id": "cube"})["costs"])
        trial["status"] = "completed"
        trial["print_snapshot"]["settings"][0]["values"]["speed_print"] = 60
        self.assertNotIn("test_cost", self.pricing.view(session, self.snapshot, {"id": "cube"})["costs"])

    def test_multiextruder_mass_uses_positions_and_individual_material_tariffs(self):
        self.operation["print_snapshot"]["context"]["extruders"].append({"id": "e2", "position": "1", "nozzle_mm": .4, "material": {"id": "petg"}})
        self.operation["result"]["material_mass"]["values"] = [100., 20.]
        self.rates["materials"]["petg"] = 3
        self.assertEqual(estimate(self.operation, self.rates)["cost"], 410)
        self.operation["result"]["material_mass"]["values"].append(5.)
        self.assertIsNone(estimate(self.operation, self.rates)["cost"])


if __name__ == "__main__":
    unittest.main()
