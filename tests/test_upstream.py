import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app import upstream
from app.http import HttpError
from app.upstream import Registry, label_link, reference_url

CHALLENGE = {"www-authenticate": 'Bearer realm="https://reg.example/token",service="reg.example",scope="aws"'}


class Resp:
    def __init__(self, tags):
        self.headers = {}
        self._tags = tags

    def json(self):
        return {"tags": self._tags}


class ExpiredToken(unittest.TestCase):
    def setUp(self):
        upstream._tokens.clear()
        self.addCleanup(upstream._tokens.clear)

    def fake_registry(self, expired_status):
        """A registry that accepts only token 'fresh' and answers a stale token with expired_status."""
        def request(url, method="GET", headers=None, insecure=False):
            auth = (headers or {}).get("Authorization")
            if auth == "Bearer fresh":
                return Resp(["v1.1.2"])
            if auth is None:
                raise HttpError(401, url, CHALLENGE)
            raise HttpError(expired_status, url, {}, b'{"errors":[{"code":"DENIED"}]}')
        return request

    def test_reauth_after_expiry(self):
        # ECR Public says 400 for an expired token; most registries say 401.
        for status in (400, 401, 403):
            with self.subTest(status=status):
                upstream._tokens["reg.example/a/b"] = "stale"
                with mock.patch.object(upstream, "request", self.fake_registry(status)), \
                     mock.patch.object(upstream, "get_json", return_value={"token": "fresh"}):
                    self.assertEqual(Registry()._call("reg.example", "a/b", "tags/list").json()["tags"], ["v1.1.2"])
                self.assertEqual(upstream._tokens["reg.example/a/b"], "fresh")

    def test_real_400_still_raises(self):
        def request(url, method="GET", headers=None, insecure=False):
            if (headers or {}).get("Authorization") is None:
                raise HttpError(401, url, CHALLENGE)
            raise HttpError(400, url, {}, b"bad request")
        upstream._tokens["reg.example/a/b"] = "stale"
        with mock.patch.object(upstream, "request", request), \
             mock.patch.object(upstream, "get_json", return_value={"token": "fresh"}):
            with self.assertRaises(HttpError) as cm:
                Registry()._call("reg.example", "a/b", "tags/list")
        self.assertEqual(cm.exception.status, 400)


class ReferenceUrl(unittest.TestCase):
    def test_ghcr_links_the_package_not_a_guessed_repo(self):
        self.assertEqual(reference_url("ghcr.io", "immich-app/immich-server"), "https://ghcr.io/immich-app/immich-server")
        self.assertEqual(reference_url("ghcr.io", "kagent-dev/kagent/controller"), "https://ghcr.io/kagent-dev/kagent/controller")

    def test_unknown_registry_has_no_link(self):
        # The /v2/ API needs a token, so linking it only shows the user a 401.
        self.assertEqual(reference_url("cr.example.dev", "acme/app"), "")
        self.assertEqual(reference_url("10.0.0.5:5000", "app"), "")

    def test_known_registries(self):
        self.assertEqual(reference_url("docker.io", "library/mongo"), "https://hub.docker.com/_/mongo/tags")
        self.assertEqual(reference_url("public.ecr.aws", "acme/app"), "https://gallery.ecr.aws/acme/app")


class LabelLink(unittest.TestCase):
    def test_source_becomes_releases_page(self):
        self.assertEqual(label_link({"source": "https://github.com/acme/widget"}, "acme/widget"),
                         "https://github.com/acme/widget/releases")
        self.assertEqual(label_link({"source": "https://github.com/Acme/Widget.git"}, "acme/widget"),
                         "https://github.com/Acme/Widget/releases")
        self.assertEqual(label_link({"source": "https://github.com/acme/widget/tree/nightly"}, "acme/widget"),
                         "https://github.com/acme/widget/releases")
        self.assertEqual(label_link({"source": "https://gitlab.com/acme/widget"}, "acme/widget"),
                         "https://gitlab.com/acme/widget/-/releases")
        self.assertEqual(label_link({"source": "https://git.acme.example/widget"}, "acme/widget"),
                         "https://git.acme.example/widget")

    def test_image_named_differently_from_its_repo(self):
        # same owner (punctuation aside), or a shared word of the name
        self.assertEqual(label_link({"source": "https://github.com/acme-labs/gizmo"}, "acmelabs/widget"),
                         "https://github.com/acme-labs/gizmo/releases")
        self.assertEqual(label_link({"source": "https://github.com/acme-app/widget"}, "acme-app/widget-server"),
                         "https://github.com/acme-app/widget/releases")
        self.assertEqual(label_link({"source": "https://github.com/acme/widget"}, "acme/widget/controller"),
                         "https://github.com/acme/widget/releases")

    def test_labels_inherited_from_a_base_image_are_ignored(self):
        base = {"source": "https://github.com/distroco/base-images", "url": "https://distroco.example/base"}
        self.assertIsNone(label_link(base, "acme/widget"))
        self.assertIsNone(label_link({"source": "https://github.com/acme/images"}, "library/widget"))

    def test_falls_back_to_docs_then_home_page(self):
        labels = {"source": "https://github.com/distroco/base", "documentation": "https://widget.example/docs",
                  "url": "https://widget.example/"}
        self.assertEqual(label_link(labels, "acme/widget"), "https://widget.example/docs")
        self.assertEqual(label_link({"url": "https://widget.example/"}, "acme/widget"), "https://widget.example/")
        self.assertIsNone(label_link({"source": "not a link widget"}, "acme/widget"))
        self.assertIsNone(label_link({}, "acme/widget"))


class Labels(unittest.TestCase):
    def setUp(self):
        upstream._labels.clear()
        upstream._tokens.clear()
        self.addCleanup(upstream._labels.clear)
        self.calls = []

    def fake(self, status=None):
        index = {"mediaType": upstream.INDEX_TYPES[0], "manifests": [
            {"digest": "sha256:att", "platform": {"os": "unknown", "architecture": "unknown"}},
            {"digest": "sha256:arm", "platform": {"os": "linux", "architecture": "arm64"}},
            {"digest": "sha256:amd", "platform": {"os": "linux", "architecture": "amd64"}}]}
        docs = {"manifests/sha256:idx": index,
                "manifests/sha256:amd": {"config": {"digest": "sha256:cfg"}},
                "blobs/sha256:cfg": {"config": {"Labels": {
                    "org.opencontainers.image.source": "https://github.com/acme/widget",
                    "org.label-schema.url": "https://widget.example/", "maintainer": "x"}}}}

        def call(host, repo, path, method="GET", accept=None):
            self.calls.append(path)
            if status:
                raise HttpError(status, path)
            r = mock.Mock()
            r.json.return_value = docs[path]
            return r
        return call

    def test_reads_a_platform_image_from_an_index_and_caches_by_digest(self):
        reg = Registry()
        with mock.patch.object(reg, "_call", self.fake()):
            want = {"source": "https://github.com/acme/widget", "url": "https://widget.example/"}
            self.assertEqual(reg.labels("reg.example", "acme/widget", "sha256:idx"), want)
            self.assertEqual(reg.labels("reg.example", "acme/widget", "sha256:idx"), want)
        self.assertEqual(self.calls, ["manifests/sha256:idx", "manifests/sha256:amd", "blobs/sha256:cfg"])

    def test_private_image_cached_as_no_labels_rate_limit_retried(self):
        reg = Registry()
        with mock.patch.object(reg, "_call", self.fake(401)):
            self.assertEqual(reg.labels("reg.example", "acme/private", "sha256:p"), {})
        with mock.patch.object(reg, "_call", self.fake(429)):
            self.assertEqual(reg.labels("reg.example", "acme/busy", "sha256:b"), {})
        self.assertIn("sha256:p", upstream._labels)
        self.assertNotIn("sha256:b", upstream._labels)

    def test_save_prunes_to_digests_seen_and_load_restores(self):
        upstream._labels.update({"sha256:a": {"source": "s"}, "sha256:old": {}})
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "image-labels.json"
            upstream.save_labels(f, {"sha256:a"})
            self.assertEqual(json.loads(f.read_text()), {"sha256:a": {"source": "s"}})
            upstream._labels.clear()
            upstream.load_labels(f)
            upstream.load_labels(Path(d) / "missing.json")
        self.assertEqual(upstream._labels, {"sha256:a": {"source": "s"}})


class ChartMeta(unittest.TestCase):
    """kubeVersion of a chart in an OCI registry, read from its config blob."""

    def setUp(self):
        upstream._labels.clear()
        self.addCleanup(upstream._labels.clear)
        self.calls = []

    def fake(self, docs, status=None):
        def call(host, repo, path, method="GET", accept=None):
            self.calls.append(path)
            if status:
                raise HttpError(status, path)
            r = mock.Mock()
            r.json.return_value = docs[path]
            return r
        return call

    def chart(self, kube_version):
        return {"manifests/sha256:m": {"config": {"mediaType": "application/vnd.cncf.helm.config.v1+json", "digest": "sha256:c"}},
                "blobs/sha256:c": {"name": "widget", "version": "2.0.0", "kubeVersion": kube_version}}

    def test_reads_kube_version_and_caches_by_digest(self):
        reg = Registry()
        with mock.patch.object(reg, "_call", self.fake(self.chart(">=1.30.0-0"))):
            self.assertEqual(reg.chart_meta("reg.example", "charts/widget", "sha256:m"), {"kube_version": ">=1.30.0-0"})
            self.assertEqual(reg.chart_meta("reg.example", "charts/widget", "sha256:m"), {"kube_version": ">=1.30.0-0"})
        self.assertEqual(self.calls, ["manifests/sha256:m", "blobs/sha256:c"])

    def test_no_range_cached_unreadable_not(self):
        reg = Registry()
        with mock.patch.object(reg, "_call", self.fake(self.chart(None))):
            self.assertEqual(reg.chart_meta("reg.example", "charts/widget", "sha256:m"), {"kube_version": ""})
        upstream._labels.clear()
        with mock.patch.object(reg, "_call", self.fake({}, 429)):
            self.assertEqual(reg.chart_meta("reg.example", "charts/widget", "sha256:m"), {})
        self.assertNotIn("sha256:m", upstream._labels)
        image = {"manifests/sha256:m": {"config": {"mediaType": "application/vnd.oci.image.config.v1+json", "digest": "sha256:c"}}}
        with mock.patch.object(reg, "_call", self.fake(image)):
            self.assertEqual(reg.chart_meta("reg.example", "charts/widget", "sha256:m"), {})


if __name__ == "__main__":
    unittest.main()
