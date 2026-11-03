import unittest
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from validate_model_spec import validate_model_spec


def valid_spec():
    return {
        "schema_version": "1.0",
        "status": "ready",
        "problem_statement": "Complete severe repairs before the deadline, then minimize lost revenue.",
        "data": {
            "blade_loss": 1200.0,
            "bearing_loss": 900.0,
            "severe_deadline": 15.0,
        },
        "variables": [
            {"id": "start_blade", "type": "binary", "lower_bound": 0, "upper_bound": 1, "unit": "1"},
            {"id": "start_bearing", "type": "binary", "lower_bound": 0, "upper_bound": 1, "unit": "1"},
        ],
        "objectives": [
            {
                "id": "economic_loss",
                "priority": 2,
                "sense": "minimize",
                "expression": {
                    "constant": 0,
                    "terms": [
                        {"variable": "start_blade", "coefficient": {"data": "blade_loss"}},
                        {"variable": "start_bearing", "coefficient": {"data": "bearing_loss"}},
                    ],
                },
            }
        ],
        "constraints": [
            {
                "id": "blade_deadline",
                "kind": "linear_row",
                "meaning": "Schedule the blade repair within the severe-fault window.",
                "lhs": {"constant": 0, "terms": [{"variable": "start_blade", "coefficient": 1}]},
                "sense": ">=",
                "rhs": 1,
            },
            {
                "id": "bearing_deadline",
                "kind": "linear_row",
                "meaning": "Schedule the bearing repair within the severe-fault window.",
                "lhs": {"constant": 0, "terms": [{"variable": "start_bearing", "coefficient": 1}]},
                "sense": ">=",
                "rhs": 1,
            },
        ],
        "unresolved_data": [],
        "assumptions": [],
        "provenance": [],
    }


class ModelSpecValidationTests(unittest.TestCase):
    def test_accepts_new_linear_constraint_composed_from_declared_variables(self):
        spec = valid_spec()
        spec["constraints"].append(
            {
                "id": "combined_severe_priority",
                "kind": "linear_row",
                "meaning": "At least one severe repair starts in the July window.",
                "lhs": {
                    "constant": 0,
                    "terms": [
                        {"variable": "start_blade", "coefficient": 1},
                        {"variable": "start_bearing", "coefficient": 1},
                    ],
                },
                "sense": ">=",
                "rhs": 1,
            }
        )

        self.assertEqual(validate_model_spec(spec), [])

    def test_rejects_decision_variable_used_as_another_variables_coefficient(self):
        spec = valid_spec()
        spec["constraints"][0]["lhs"]["terms"][0]["coefficient"] = {
            "variable": "start_bearing"
        }

        errors = validate_model_spec(spec)

        self.assertTrue(any("coefficient" in error.lower() for error in errors))

    def test_rejects_undeclared_decision_variable(self):
        spec = valid_spec()
        spec["constraints"][0]["lhs"]["terms"][0]["variable"] = "start_gearbox"

        errors = validate_model_spec(spec)

        self.assertTrue(any("undeclared variable" in error.lower() for error in errors))

    def test_rejects_unbounded_integer_variable(self):
        spec = valid_spec()
        spec["variables"][0]["type"] = "integer"
        spec["variables"][0]["upper_bound"] = None

        errors = validate_model_spec(spec)

        self.assertTrue(any("finite" in error.lower() for error in errors))

    def test_rejects_non_linear_expression_operator(self):
        spec = valid_spec()
        spec["constraints"][0]["lhs"] = {
            "op": "multiply",
            "left": {"variable": "start_blade"},
            "right": {"variable": "start_bearing"},
        }

        errors = validate_model_spec(spec)

        self.assertTrue(any("linear" in error.lower() for error in errors))

    def test_rejects_unknown_data_parameter(self):
        spec = valid_spec()
        spec["objectives"][0]["expression"]["terms"][0]["coefficient"] = {
            "data": "missing_price"
        }

        errors = validate_model_spec(spec)

        self.assertTrue(any("unknown data" in error.lower() for error in errors))

    def test_blocks_solver_ready_status_when_clarification_is_needed(self):
        spec = valid_spec()
        spec["status"] = "clarification_required"
        spec["unresolved_data"] = ["blade stock arrival date"]

        errors = validate_model_spec(spec)

        self.assertTrue(any("solver" in error.lower() for error in errors))

    def test_rejects_binary_variables_with_non_binary_bounds(self):
        spec = valid_spec()
        spec["variables"][0]["upper_bound"] = 2

        errors = validate_model_spec(spec)

        self.assertTrue(any("binary" in error.lower() for error in errors))

    def test_blocks_extension_proposal_from_solver(self):
        spec = valid_spec()
        spec["status"] = "extension_proposal"

        errors = validate_model_spec(spec)

        self.assertTrue(any("solver dispatch" in error.lower() for error in errors))

    def test_rejects_non_finite_parameter(self):
        spec = valid_spec()
        spec["data"]["blade_loss"] = float("inf")

        errors = validate_model_spec(spec)

        self.assertTrue(any("finite number" in error.lower() for error in errors))

    def test_rejects_duplicate_lexicographic_priorities(self):
        spec = valid_spec()
        second = dict(spec["objectives"][0])
        second["id"] = "makespan"
        spec["objectives"].append(second)

        errors = validate_model_spec(spec)

        self.assertTrue(any("priorities must be unique" in error.lower() for error in errors))


if __name__ == "__main__":
    unittest.main()
