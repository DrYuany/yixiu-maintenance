import json
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class CatalogRegistryTests(unittest.TestCase):
    def load(self, filename, key):
        payload = json.loads((ROOT / 'references' / filename).read_text(encoding='utf-8'))
        rows = payload[key]
        self.assertTrue(rows)
        ids = [row['编号'] for row in rows]
        self.assertEqual(len(ids), len(set(ids)), filename)
        return rows

    def test_all_four_registries_have_unique_stable_ids_and_known_status(self):
        objectives = self.load('目标指标库.json', '指标')
        constraints = self.load('约束构件库.json', '构件')
        data_fields = self.load('数据字典.json', '字段')
        combinations = self.load('目标组合规则.json', '规则')
        self.assertTrue(all(row.get('状态') for row in objectives))
        self.assertTrue(all(row.get('状态') for row in constraints))
        self.assertTrue(all(row.get('定义') and row.get('用途') for row in data_fields))
        self.assertTrue(all(row.get('语义') and row.get('输入') for row in combinations))

    def test_learned_construct_store_starts_empty_and_one_shot_is_present(self):
        payload = json.loads((ROOT / 'references' / 'user-learned-constructs.json').read_text(encoding='utf-8'))
        self.assertEqual(payload['已学习构件'], [])
        example = (ROOT / 'tests' / 'one-shot-catalog-example.md').read_text(encoding='utf-8')
        self.assertIn('One-shot', example)
        self.assertIn('clarification_required', example)


if __name__ == '__main__':
    unittest.main()
