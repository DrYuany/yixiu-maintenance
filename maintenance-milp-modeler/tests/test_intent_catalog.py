import json
import unittest
from pathlib import Path


class IntentCatalogTests(unittest.TestCase):
    def test_catalog_has_100_unique_scenarios_in_ten_balanced_groups(self):
        path = Path(__file__).with_name('intent-scenarios.json')
        cases = json.loads(path.read_text(encoding='utf-8'))
        self.assertEqual(len(cases), 100)
        self.assertEqual(len({case['id'] for case in cases}), 100)
        self.assertEqual(len({case['request'] for case in cases}), 100)
        groups = {}
        for case in cases:
            self.assertTrue(case['request'].strip())
            self.assertTrue(case['expected'].strip())
            groups[case['group']] = groups.get(case['group'], 0) + 1
        self.assertEqual(len(groups), 10)
        self.assertEqual(set(groups.values()), {10})

    def test_ambiguous_multiobjective_cases_require_clarification(self):
        cases = json.loads(Path(__file__).with_name('intent-scenarios.json').read_text(encoding='utf-8'))
        ambiguous = [case for case in cases if case['group'] == '目标关系含糊，必须澄清']
        self.assertEqual(len(ambiguous), 10)
        self.assertTrue(all(any(token in case['expected'] for token in ('澄清', '询问', 'clarification'))
                            for case in ambiguous))


if __name__ == '__main__':
    unittest.main()
