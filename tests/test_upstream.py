import base64
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


class Credentials(unittest.TestCase):
    """Docker Hub won't take Basic auth on the registry: the credentials buy a bearer token."""

    HUB = {"www-authenticate": 'Bearer realm="https://auth.docker.io/token",service="registry.docker.io"'}

    def setUp(self):
        upstream._tokens.clear()
        self.addCleanup(upstream._tokens.clear)
        self.token_auth = []

    def hub(self, url, method="GET", headers=None, insecure=False):
        if (headers or {}).get("Authorization") == "Bearer private":
            return Resp(["1.0.0"])
        raise HttpError(401, url, self.HUB)

    def get_json(self, url, headers=None):
        self.token_auth.append((headers or {}).get("Authorization"))
        return {"token": "private" if headers else "anonymous"}

    def call(self, host, cfg, env):
        with mock.patch.object(upstream, "request", self.hub), mock.patch.object(upstream, "get_json", self.get_json), \
             mock.patch.dict("os.environ", env):
            return Registry(cfg)._call(host, "acme/private", "tags/list").json()["tags"]

    def test_basic_credentials_traded_for_token(self):
        cfg = {"docker.io": {"auth_env": "HUB_AUTH"}}
        self.assertEqual(self.call("docker.io", cfg, {"HUB_AUTH": "me:pat"}), ["1.0.0"])
        self.assertEqual(self.token_auth, ["Basic bWU6cGF0"])
        self.assertEqual(upstream._tokens["registry-1.docker.io/acme/private"], "private")

    def test_hub_mirror_borrows_docker_io_credentials(self):
        cfg = {"docker.io": {"auth_env": "HUB_AUTH"}}
        self.assertEqual(self.call("docker.n8n.io", cfg, {"HUB_AUTH": "me:pat"}), ["1.0.0"])
        self.assertEqual(self.token_auth, ["Basic bWU6cGF0"])

    def test_other_realms_never_get_docker_io_credentials(self):
        cfg = {"docker.io": {"auth_env": "HUB_AUTH"}}
        with mock.patch.object(upstream, "get_json", self.get_json), mock.patch.dict("os.environ", {"HUB_AUTH": "me:pat"}):
            Registry(cfg)._token(CHALLENGE["www-authenticate"], "reg.example", "a/b")
        self.assertEqual(self.token_auth, [None])

    def test_other_registries_never_get_a_docker_hub_token(self):
        # Any registry an image names can point its challenge at auth.docker.io, to collect a
        # Docker Hub token bought with docker.io's credentials (and scoped as it likes).
        cfg = {"docker.io": {"auth_env": "HUB_AUTH"}}
        self.HUB = {"www-authenticate": 'Bearer realm="https://auth.docker.io/token",service="registry.docker.io",'
                                        'scope="repository:acme/private:pull,push"'}
        with self.assertRaises(HttpError):
            self.call("evil.example", cfg, {"HUB_AUTH": "me:pat"})
        self.assertEqual(self.token_auth, [None])

    def test_docker_io_credentials_buy_a_pull_token_over_https_only(self):
        cfg = {"docker.io": {"auth_env": "HUB_AUTH"}}
        urls = []
        def get_json(url, headers=None):
            urls.append(url)
            return self.get_json(url, headers)
        with mock.patch.object(upstream, "get_json", get_json), mock.patch.dict("os.environ", {"HUB_AUTH": "me:pat"}):
            reg = Registry(cfg)
            reg._token('Bearer realm="https://auth.docker.io/token",scope="repository:x/y:push&scope=a"',
                       "docker.n8n.io", "n8nio/n8n")
            reg._token('Bearer realm="http://auth.docker.io/token"', "registry-1.docker.io", "acme/private")
        self.assertEqual(self.token_auth, ["Basic bWU6cGF0", None])
        self.assertEqual(urls[0], "https://auth.docker.io/token?scope=repository%3An8nio%2Fn8n%3Apull")

    def test_anonymous_without_credentials(self):
        with self.assertRaises(HttpError):
            self.call("docker.io", {}, {})
        self.assertEqual(self.token_auth, [None])


def b64(text):
    return base64.b64encode(text.encode()).decode()


class DockerConfig(unittest.TestCase):
    """Credentials from a mounted pull secret (kubernetes.io/dockerconfigjson), no config needed."""

    def setUp(self):
        upstream._tokens.clear()
        upstream._auth_files.clear()
        self.addCleanup(upstream._tokens.clear)
        self.addCleanup(upstream._auth_files.clear)
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.path = str(Path(self.dir.name) / ".dockerconfigjson")

    def write(self, data):
        Path(self.path).write_text(data if isinstance(data, str) else json.dumps(data))

    def test_auth_and_username_password_for_many_hosts(self):
        self.write({"auths": {
            "reg.example": {"auth": b64("robot:s3cret")},
            "https://other.example:5000/v2/": {"username": "me", "password": "pw"},
            "https://index.docker.io/v1/": {"username": "hubuser", "password": "pat", "auth": "not base64!"},
            "token-only.example": {"identitytoken": "refresh"},
        }})
        parsed = upstream.docker_config(self.path)
        self.assertEqual(parsed["reg.example"], {"": b64("robot:s3cret")})
        self.assertEqual(parsed["other.example:5000"], {"": b64("me:pw")})
        self.assertEqual(parsed["docker.io"], {"": b64("hubuser:pat")})  # bad `auth`: username + password
        self.assertNotIn("token-only.example", parsed)

    def test_docker_hub_under_any_name(self):
        self.write({"auths": {"https://index.docker.io/v1/": {"auth": b64("hubuser:pat")}}})
        reg = Registry(auth_file=self.path)
        for host in ("docker.io", "index.docker.io", "registry-1.docker.io"):
            self.assertEqual(reg._basic(host, "library/nginx"), b64("hubuser:pat"), host)
        self.assertIsNone(reg._basic("docker.n8n.io", "n8nio/n8n"))  # a mirror borrows them only for a Hub token
        self.assertIsNone(reg._basic("ghcr.io", "acme/app"))

    def test_longest_path_prefix_wins(self):
        self.write({"auths": {"reg.example": {"auth": b64("all:x")}, "reg.example/team": {"auth": b64("team:x")}}})
        reg = Registry(auth_file=self.path)
        self.assertEqual(reg._basic("reg.example", "team/app"), b64("team:x"))
        self.assertEqual(reg._basic("reg.example", "teamx/app"), b64("all:x"))
        self.assertEqual(reg._basic("reg.example", "other/app"), b64("all:x"))

    def test_path_only_entry_leaves_other_repos_anonymous(self):
        self.write({"auths": {"reg.example/team": {"auth": b64("team:x")}}})
        reg = Registry(auth_file=self.path)
        self.assertIsNone(reg._basic("reg.example", "other/app"))
        self.assertTrue(reg.has_credentials("reg.example"))

    def test_legacy_dockercfg_format(self):
        self.write({"reg.example": {"auth": b64("old:style")}})
        self.assertEqual(Registry(auth_file=self.path)._basic("reg.example", "a/b"), b64("old:style"))

    def test_missing_file_means_anonymous(self):
        reg = Registry(auth_file=self.path)
        self.assertIsNone(reg._basic("reg.example", "a/b"))
        self.assertFalse(reg.has_credentials("reg.example"))
        self.assertEqual(upstream.docker_config(""), {})  # REGISTRY_AUTH_FILE="" turns it off

    def test_malformed_file_is_ignored_without_leaking_it(self):
        for text in ('{"auths": {"reg.example": {"auth": "c2VjcmV0"', "[1, 2]", '{"auths": ["reg.example"]}'):
            with self.subTest(text=text):
                upstream._auth_files.clear()
                self.write(text)
                with self.assertLogs("kube-drift.upstream", "WARNING") as logs:
                    self.assertEqual(upstream.docker_config(self.path), {})
                self.assertNotIn("c2VjcmV0", "".join(logs.output))
                self.assertIsNone(Registry(auth_file=self.path)._basic("reg.example", "a/b"))

    def test_rotated_secret_is_reread(self):
        self.write({"auths": {"reg.example": {"auth": b64("old:x")}}})
        reg = Registry(auth_file=self.path)
        self.assertEqual(reg._basic("reg.example", "a/b"), b64("old:x"))
        self.write({"auths": {"reg.example": {"auth": b64("rotated:xyz")}}})
        self.assertEqual(reg._basic("reg.example", "a/b"), b64("rotated:xyz"))

    def test_default_path_comes_from_the_environment(self):
        self.write({"auths": {"reg.example": {"auth": b64("env:x")}}})
        with mock.patch.dict("os.environ", {"REGISTRY_AUTH_FILE": self.path}):
            self.assertEqual(Registry()._basic("reg.example", "a/b"), b64("env:x"))

    def test_auth_env_wins_over_the_file(self):
        self.write({"auths": {"reg.example": {"auth": b64("file:x")}}})
        cfg = {"reg.example": {"auth_env": "REG_AUTH"}}
        with mock.patch.dict("os.environ", {"REG_AUTH": "env:y"}):
            self.assertEqual(Registry(cfg, auth_file=self.path)._basic("reg.example", "a/b"), b64("env:y"))
        with mock.patch.dict("os.environ", {"REG_AUTH": ""}):  # named but unset: the file still counts
            self.assertEqual(Registry(cfg, auth_file=self.path)._basic("reg.example", "a/b"), b64("file:x"))

    def test_file_credentials_buy_a_bearer_token(self):
        self.write({"auths": {"https://index.docker.io/v1/": {"auth": b64("me:pat")}}})
        token_auth = []
        def get_json(url, headers=None):
            token_auth.append((headers or {}).get("Authorization"))
            return {"token": "private" if headers else "anonymous"}
        def hub(url, method="GET", headers=None, insecure=False):
            if (headers or {}).get("Authorization") == "Bearer private":
                return Resp(["1.0.0"])
            raise HttpError(401, url, Credentials.HUB)
        with mock.patch.object(upstream, "request", hub), mock.patch.object(upstream, "get_json", get_json):
            reg = Registry(auth_file=self.path)
            self.assertEqual(reg._call("docker.io", "acme/private", "tags/list").json()["tags"], ["1.0.0"])
            self.assertEqual(reg._call("docker.n8n.io", "n8nio/n8n", "tags/list").json()["tags"], ["1.0.0"])
            with self.assertRaises(HttpError):  # another registry pointing at auth.docker.io gets nothing
                reg._call("evil.example", "acme/private", "tags/list")
        self.assertEqual(token_auth, [f"Basic {b64('me:pat')}"] * 2 + [None])


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


def ah_pkg(repo, stars=0, version="1.0.0", url="", verified=False, official=False, deprecated=False, org=None):
    return {"name": "widget", "version": version, "stars": stars, "official": official, "deprecated": deprecated,
            "repository": {"name": repo, "url": url, "verified_publisher": verified, "organization_name": org}}


class ArtifactHubSearch(unittest.TestCase):
    """Several repositories carry a chart of the same name; pick the one the release came from."""

    def setUp(self):
        upstream.artifacthub_search.cache_clear()
        self.addCleanup(upstream.artifacthub_search.cache_clear)

    def search(self, pkgs, links=(), installed=""):
        upstream.artifacthub_search.cache_clear()
        with mock.patch.object(upstream, "get_json", return_value={"packages": pkgs}), \
             mock.patch.object(upstream, "artifacthub_package", return_value=None):
            return upstream.artifacthub_search("widget", links, installed)

    def test_verified_mirror_does_not_beat_the_popular_repo(self):
        r = self.search([ah_pkg("mirrorco", stars=1, verified=True, url="https://mirrorco.example/charts"),
                         ah_pkg("widget", stars=200, url="https://charts.widget.example")])
        self.assertEqual(r["repo"], "widget")
        self.assertFalse(r["ambiguous"])

    def test_the_charts_own_links_name_the_publisher(self):
        # Installed from the less popular repo: its Chart.yaml links point at that publisher.
        pkgs = [ah_pkg("widget", stars=200, url="https://widget.github.io/helm-charts"),
                ah_pkg("bundleco", stars=9, url="https://charts.bundleco.example/bundleco")]
        r = self.search(pkgs, links=("https://github.com/bundleco/charts/tree/main/widget",))
        self.assertEqual(r["repo"], "bundleco")
        self.assertFalse(r["ambiguous"])
        r = self.search(pkgs, links=("https://github.com/widget/widget", "https://widget.example/"))
        self.assertEqual(r["repo"], "widget")

    def test_publisher_from_site_name_or_org(self):
        pkgs = [ah_pkg("other", stars=50, url="https://other.github.io/charts"),
                ah_pkg("widget-charts", stars=3, url="https://charts.widget.example")]
        self.assertEqual(self.search(pkgs, links=("https://github.com/widget/widget",))["repo"], "widget-charts")
        pkgs = [ah_pkg("other", stars=50, url="https://other.github.io/charts"),
                ah_pkg("acme-widget", stars=3, url="oci://reg.example/charts", org="acme")]
        self.assertEqual(self.search(pkgs, links=("https://github.com/acme/widget",))["repo"], "acme-widget")

    def test_hosting_sites_name_nobody(self):
        pkgs = [ah_pkg("other", stars=50, url="https://other.github.io/charts"),
                ah_pkg("bucket", stars=3, url="https://storage.googleapis.com/charts")]
        self.assertEqual(self.search(pkgs, links=("https://storage.googleapis.com/widget",))["repo"], "other")

    def test_a_repo_older_than_the_installed_chart_is_not_its_source(self):
        r = self.search([ah_pkg("stale", stars=40, version="1.5.1"), ah_pkg("widget", stars=30, version="1.13.0")],
                        installed="1.12.0")
        self.assertEqual(r["repo"], "widget")

    def test_official_wins_and_deprecated_loses(self):
        r = self.search([ah_pkg("popular", stars=500), ah_pkg("widget", stars=5, official=True)])
        self.assertEqual((r["repo"], r["ambiguous"]), ("widget", False))
        r = self.search([ah_pkg("old", stars=500, deprecated=True), ah_pkg("widget", stars=5)])
        self.assertEqual(r["repo"], "widget")

    def test_ambiguous_only_without_clear_evidence(self):
        self.assertTrue(self.search([ah_pkg("a", stars=6), ah_pkg("b", stars=4)])["ambiguous"])
        self.assertTrue(self.search([ah_pkg("a", stars=3), ah_pkg("b", stars=0)])["ambiguous"])  # too few stars to say
        self.assertFalse(self.search([ah_pkg("a", stars=12), ah_pkg("b", stars=4)])["ambiguous"])
        self.assertFalse(self.search([ah_pkg("a")])["ambiguous"])
        self.assertIsNone(self.search([]))


class ChartLinks(unittest.TestCase):
    def test_release_page_from_first_repo_link(self):
        links = ("https://widget.example/", "https://github.com/acme/widget", "https://github.com/acme/other")
        self.assertEqual(upstream.chart_release_page(links), "https://github.com/acme/widget/releases")
        self.assertIsNone(upstream.chart_release_page(("https://widget.example/", "")))

    def test_source_link_must_be_named_like_the_image(self):
        links = ("https://github.com/acme/helm-charts", "https://github.com/acme-labs/widget", "")
        self.assertEqual(upstream.chart_source_link(links, "widget/widget"), "https://github.com/acme-labs/widget/releases")
        self.assertEqual(upstream.chart_source_link(links, "acme/Widget"), "https://github.com/acme-labs/widget/releases")
        self.assertIsNone(upstream.chart_source_link(links, "acme/widget-exporter"))
        self.assertIsNone(upstream.chart_source_link(("https://widget.example/",), "acme/widget"))


class GithubGuess(unittest.TestCase):
    def setUp(self):
        upstream._projects.clear()
        self.addCleanup(upstream._projects.clear)

    def test_releases_page_or_none(self):
        answers = {"acme/widget": [{"tag_name": "v1"}], "acme/gizmo": []}

        def get(url, headers=None):
            name = url.split("/repos/")[1].split("/releases")[0]
            if name not in answers:
                raise HttpError(404, url, {})
            return answers[name]
        with mock.patch.object(upstream, "get_json", side_effect=get) as g:
            self.assertEqual(upstream.github_guess("ghcr.io", "acme/widget/controller"), "https://github.com/acme/widget/releases")
            self.assertIsNone(upstream.github_guess("docker.io", "acme/gizmo"))  # no releases: likely not the project
            self.assertIsNone(upstream.github_guess("quay.io", "acme/nothing"))
            self.assertIsNone(upstream.github_guess("quay.io", "acme/nothing"))  # remembered
            self.assertEqual(g.call_count, 3)

    def test_only_names_that_can_be_a_github_repo(self):
        with mock.patch.object(upstream, "get_json") as g:
            for host, repo in (("docker.io", "library/widget"), ("ghcr.io", "widget"), ("quay.io", "acme/widget/sub"),
                               ("registry.example", "acme/widget"), ("registry.k8s.io", "widget")):
                self.assertIsNone(upstream.github_guess(host, repo), (host, repo))
            g.assert_not_called()

    def test_errors_are_not_remembered(self):
        with mock.patch.object(upstream, "get_json", side_effect=HttpError(403, "x", {})):
            with self.assertRaises(HttpError):
                upstream.github_project("acme/widget")
        self.assertNotIn("acme/widget", upstream._projects)


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
