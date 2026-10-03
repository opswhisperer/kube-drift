import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app import main
from app.orphans import OrphanScanner
from app.scan import Scanner
from tests.fake_k8s import FakeK8s
from tests.fake_orphans import FakeKor, FakeOrphanK8s

ROOT = Path(__file__).resolve().parents[1]


def load(path):
    with mock.patch.dict(os.environ, {"CONFIG": str(path)}):
        return main.load_config()


class Config(unittest.TestCase):
    def test_missing_file_means_defaults(self):
        self.assertEqual(load("/nonexistent/config.yaml"), {})

    def test_example_loads_and_runs(self):
        cfg = load(ROOT / "config" / "example.yaml")
        self.assertNotIn("helm", cfg)  # only commented examples → dropped, not null
        self.assertNotIn("resources", cfg["orphans"])
        self.assertEqual(cfg["cluster_name"], "my-cluster")
        self.assertGreater(len(Scanner(FakeK8s(), cfg).inventory()), 50)
        items = OrphanScanner(FakeOrphanK8s(), cfg, FakeKor(), self_namespace="apps").run()["items"]
        self.assertGreater(len(items), 10)

    def test_json_still_works(self):
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            f.write('{"cluster_name": "old", "helm": null}')
        try:
            self.assertEqual(load(f.name), {"cluster_name": "old"})
        finally:
            os.unlink(f.name)

    def test_not_a_mapping(self):
        with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as f:
            f.write("- a\n- b\n")
        try:
            with self.assertRaises(ValueError):
                load(f.name)
        finally:
            os.unlink(f.name)

    def test_self_namespace_is_protected(self):
        res = OrphanScanner(FakeOrphanK8s(), {}, FakeKor(), self_namespace="apps").run()
        self.assertEqual(next(i for i in res["items"] if i["id"] == "Secret/apps/kube-drift-tls")["status"], "protected")
        res = OrphanScanner(FakeOrphanK8s(), {}, FakeKor(), self_namespace="kube-drift").run()
        self.assertNotEqual(next(i for i in res["items"] if i["id"] == "Secret/apps/kube-drift-tls")["status"], "protected")


if __name__ == "__main__":
    unittest.main()
