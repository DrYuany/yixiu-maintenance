from __future__ import annotations

import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

from model_artifact import build_model_artifact  # noqa: E402
from product_data import load_project  # noqa: E402
from product_planner import build_model, candidates  # noqa: E402


class ModelArtifactTest(unittest.TestCase):
    def test_model_artifact_uses_actual_solver_structure(self) -> None:
        data = load_project(ROOT, "基座01")
        options = {task["任务编号"]: candidates(data, task) for task in data.tasks}
        model, choices, stages, _ = build_model(data, options)

        artifact = build_model_artifact(
            data=data,
            options=options,
            model=model,
            choices=choices,
            stages=stages,
            strategy="高风险优先抢修",
            mode="分层",
            weights=None,
            limits=None,
            partial=False,
            solver_parameters={
                "num_search_workers": 1,
                "random_seed": 17,
                "max_time_in_seconds": 30,
            },
            model_fingerprint="test-model-fingerprint",
        )

        self.assertEqual(artifact["variables"]["total"], len(model.Proto().variables))
        self.assertEqual(artifact["constraints"]["total"], len(model.Proto().constraints))
        self.assertGreater(artifact["variables"]["total"], 0)
        self.assertGreater(artifact["constraints"]["total"], 0)
        self.assertTrue(artifact["objectives"]["stages"])
        self.assertTrue(artifact["objectives"]["stages"][0]["expressionPreview"])
        self.assertGreater(artifact["constraints"]["samples"][0]["variableCount"], 0)
        self.assertIn("x_", artifact["constraints"]["samples"][0]["expression"])
        self.assertEqual(artifact["dimensions"]["tasks"], len(data.tasks))
        self.assertTrue(any(item["label"] == "船租" for item in artifact["parameters"]))
        self.assertEqual(artifact["modelFingerprint"], "test-model-fingerprint")


if __name__ == "__main__":
    unittest.main()
