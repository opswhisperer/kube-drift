import unittest

from app.upstream import split_image
from app.versions import cmp_versions, is_floating, pick_latest


class SplitImage(unittest.TestCase):
    def test_variants(self):
        self.assertEqual(split_image("mongo:8"), ("docker.io", "library/mongo", "8", None))
        self.assertEqual(split_image("traefik/whoami"), ("docker.io", "traefik/whoami", "latest", None))
        self.assertEqual(split_image("10.0.0.5:5000/api:87540e3"), ("10.0.0.5:5000", "api", "87540e3", None))
        self.assertEqual(split_image("ghcr.io/a/b:v1@sha256:abc"), ("ghcr.io", "a/b", "v1", "sha256:abc"))
        self.assertEqual(split_image("lscr.io/linuxserver/syncthing:latest")[0], "lscr.io")


class Versions(unittest.TestCase):
    def test_floating(self):
        for t in ("latest", "main", "main-stable", "nightly", "87540e3", "dev"):
            self.assertTrue(is_floating(t), t)
        for t in ("v1.2.3", "1.31.1", "9.0-alpine", "distroless-v1.39.1", "3.6.6-0", "0.0.161-589c824437-r1", "8"):
            self.assertFalse(is_floating(t), t)

    def test_cmp(self):
        self.assertLess(cmp_versions("v1.20.2", "1.21.2"), 0)
        self.assertEqual(cmp_versions("1.2", "1.2.0"), 0)
        self.assertGreater(cmp_versions("2.41.6", "2.34.6"), 0)

    def test_pick_latest_shape(self):
        tags = ["v1.35.9", "v1.36.3", "v1.37.1", "v1.37.2-rc.0", "latest", "v1.35.10"]
        self.assertEqual(pick_latest("v1.35.9", tags, "minor"), {"latest": "v1.35.10", "latest_any": "v1.37.1", "outdated": True})
        self.assertEqual(pick_latest("v1.35.10", tags, "minor")["outdated"], False)

    def test_suffix_and_precision(self):
        tags = ["9.0-alpine", "9.2-alpine", "9.2.1-alpine", "9.2.1", "10.0-alpine"]
        r = pick_latest("9.0-alpine", tags)
        self.assertEqual(r["latest"], "10.0-alpine")
        r = pick_latest("9.0-alpine", tags, "major")
        self.assertEqual(r["latest"], "9.2-alpine")
        # a bare build number must never beat a 3-part version
        self.assertEqual(pick_latest("12.4.2", ["12.4.2", "9799770991", "13.2.3"])["latest"], "13.2.3")
        self.assertEqual(pick_latest("8", ["8", "8.0.14", "9", "9.0.1"], "major")["latest"], "8")

    def test_loose_suffix_first_party(self):
        tags = ["0.0.161-589c824437-r1", "0.0.162-aaaaaaaaaa-r1", "0.0.162-aaaaaaaaaa-rc1"]
        self.assertEqual(pick_latest("0.0.161-589c824437-r1", tags, loose_suffix=True)["latest"], "0.0.162-aaaaaaaaaa-r1")


if __name__ == "__main__":
    unittest.main()
