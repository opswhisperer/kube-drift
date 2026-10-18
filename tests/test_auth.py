import base64
import hashlib
import json
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from app import auth, main
from app.store import Store


def b64(obj) -> str:
    return base64.urlsafe_b64encode(json.dumps(obj).encode()).rstrip(b"=").decode()


class FakeIdP:
    """An OIDC provider on localhost: discovery, and a token endpoint that checks PKCE and the client."""

    def __init__(self):
        idp = self
        self.claims = {"sub": "u1", "name": "Ada", "email": "ada@example.test", "groups": ["drift-admins"]}
        self.codes: dict[str, dict] = {}  # code -> {nonce, challenge}

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _json(self, code, obj):
                body = json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                if self.path == "/o/kube-drift/.well-known/openid-configuration":
                    self._json(200, {"issuer": idp.issuer, "authorization_endpoint": idp.base + "/authorize",
                                     "token_endpoint": idp.base + "/token", "end_session_endpoint": idp.base + "/end"})
                else:
                    self._json(404, {})

            def do_POST(self):
                form = dict(urllib.parse.parse_qsl(self.rfile.read(int(self.headers["Content-Length"])).decode()))
                want = "Basic " + base64.b64encode(b"kube-drift:s3cret").decode()
                grant = idp.codes.pop(form.get("code"), None)
                if self.headers.get("Authorization") != want or not grant:
                    self._json(400, {"error": "invalid_grant"})
                    return
                verifier = form.get("code_verifier", "")
                if auth._b64(hashlib.sha256(verifier.encode()).digest()) != grant["challenge"]:
                    self._json(400, {"error": "invalid_grant", "error_description": "pkce"})
                    return
                claims = {"iss": idp.issuer, "aud": "kube-drift", "exp": time.time() + 300,
                          "nonce": grant["nonce"], **idp.claims}
                self._json(200, {"access_token": "at", "id_token": f"{b64({'alg': 'RS256'})}.{b64(claims)}.sig"})

        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.base = f"http://127.0.0.1:{self.srv.server_address[1]}"
        self.issuer = self.base + "/o/kube-drift/"
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def authorize(self, url: str) -> dict:
        """What the IdP does when the browser arrives: remember the request, hand back a code."""
        q = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(url).query))
        code = f"code-{len(self.codes)}"
        self.codes[code] = {"nonce": q["nonce"], "challenge": q["code_challenge"]}
        return {"code": code, "state": q["state"]}

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **kw):
        return None


OPENER = urllib.request.build_opener(NoRedirect)


class Units(unittest.TestCase):
    def test_signer(self):
        s = auth.Signer(b"k" * 32)
        tok = s.dumps({"a": 1}, 60)
        self.assertEqual(s.loads(tok)["a"], 1)
        self.assertIsNone(s.loads(tok[:-2] + "xx"))
        self.assertIsNone(auth.Signer(b"other").loads(tok))
        self.assertIsNone(s.loads(s.dumps({"a": 1}, -1)))
        self.assertIsNone(s.loads("garbage"))

    def test_safe_next(self):
        self.assertEqual(auth.safe_next("/#orphans"), "/#orphans")
        for bad in ("https://evil.test/", "//evil.test", "/\\evil.test", "", None):
            self.assertEqual(auth.safe_next(bad), "/")

    def test_keystore(self):
        with tempfile.TemporaryDirectory() as d:
            ks = auth.KeyStore(Path(d))
            token, key = ks.create("ci", "ada@example.test", None)
            self.assertTrue(token.startswith("kd_") and token.startswith(key["prefix"]))
            self.assertNotIn(token, (Path(d) / "api-keys.json").read_text())  # only the hash is kept
            self.assertEqual(ks.check(token)["id"], key["id"])
            self.assertIsNone(ks.check(token + "x"))
            self.assertIsNone(ks.check("Bearer nonsense"))
            self.assertEqual(auth.KeyStore(Path(d)).check(token)["name"], "ci")  # survives a restart
            self.assertNotIn("hash", ks.list()[0])
            self.assertTrue(ks.revoke(key["id"]))
            self.assertIsNone(ks.check(token))
            self.assertFalse(ks.revoke(key["id"]))

    def test_expired_key(self):
        with tempfile.TemporaryDirectory() as d:
            ks = auth.KeyStore(Path(d))
            token, key = ks.create("old", "ada", 1)
            ks._keys[key["id"]]["expires_at"] = "2000-01-01T00:00:00+00:00"
            self.assertIsNone(ks.check(token))

    def test_config(self):
        base = {"issuer": "https://auth.example.test/application/o/kube-drift/", "client_id": "kube-drift",
                "redirect_url": "https://drift.example.test/auth/callback"}
        with mock.patch.dict("os.environ", {"OIDC_CLIENT_SECRET": "x"}):
            c = auth.OIDCConfig.from_config({**base, "scopes": ["profile"]})
            self.assertEqual(c.scopes, ["openid", "profile"])
            with self.assertRaisesRegex(ValueError, "https"):
                auth.OIDCConfig.from_config({**base, "issuer": "http://auth.example.test/"})
            with self.assertRaisesRegex(ValueError, "client_id"):
                auth.OIDCConfig.from_config({**base, "client_id": None})
        with mock.patch.dict("os.environ", {}, clear=True), self.assertRaisesRegex(ValueError, "OIDC_CLIENT_SECRET"):
            auth.OIDCConfig.from_config(base)
        self.assertIsNone(auth.from_config({}, Path(".")))

    def test_allowed(self):
        o = auth.OIDC(auth.OIDCConfig("i", "c", "s", "r", allowed_groups=["admins"], allowed_emails=["bob@example.test"]))
        self.assertIsNone(o.allowed({"groups": ["x", "admins"]}))
        self.assertIsNone(o.allowed({"email": "Bob@example.test"}))
        self.assertIsNotNone(o.allowed({"email": "bob@example.test", "email_verified": False}))
        self.assertIsNotNone(o.allowed({"groups": ["x"], "email": "eve@example.test"}))
        self.assertIsNone(auth.OIDC(auth.OIDCConfig("i", "c", "s", "r")).allowed({}))  # no lists: anyone the IdP lets in


class Flow(unittest.TestCase):
    """The whole thing through main.Handler: sign in, use the API, make and revoke a key."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.idp = FakeIdP()
        self.saved = (main.AUTH, main.ORPHANS, main.STATE.result)
        main.ORPHANS = main.OrphanState(Store(Path(self.tmp.name)))
        main.STATE.result = {"scanned_at": None, "summary": {}, "components": []}
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), main.Handler)
        self.base = f"http://127.0.0.1:{self.srv.server_address[1]}"
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.oidc = {"issuer": self.idp.issuer, "client_id": "kube-drift", "redirect_url": self.base + "/auth/callback",
                     "allowed_groups": ["drift-admins"]}
        with mock.patch.dict("os.environ", {"OIDC_CLIENT_SECRET": "s3cret"}):
            main.AUTH = auth.from_config({"auth": {"oidc": self.oidc}}, Path(self.tmp.name))

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()
        self.idp.close()
        main.AUTH, main.ORPHANS, main.STATE.result = self.saved
        self.tmp.cleanup()

    def req(self, path, method="GET", body=None, cookie=None, bearer=None):
        h = {}
        if body is not None:
            h["Content-Type"] = "application/json"
        if cookie:
            h["Cookie"] = cookie
        if bearer:
            h["Authorization"] = f"Bearer {bearer}"
        r = urllib.request.Request(self.base + path, method=method, headers=h,
                                   data=json.dumps(body).encode() if body is not None else None)
        try:
            resp = OPENER.open(r)
        except urllib.error.HTTPError as e:
            resp = e
        with resp:
            data = resp.read()
            return resp.status, resp.headers, data

    @staticmethod
    def cookie_of(headers, name):
        for c in headers.get_all("Set-Cookie") or []:
            if c.startswith(name + "="):
                return c.split(";", 1)[0]
        return None

    def sign_in(self, next_path="/"):
        status, h, _ = self.req("/auth/login?next=" + urllib.parse.quote(next_path))
        self.assertEqual(status, 302)
        login = self.cookie_of(h, auth.LOGIN_COOKIE)
        self.assertIn("code_challenge_method=S256", h["Location"])
        got = self.idp.authorize(h["Location"])
        return self.req("/auth/callback?" + urllib.parse.urlencode(got), cookie=login)

    def test_open_paths(self):
        for p in ("/healthz", "/openapi.json", "/docs", "/auth/me"):
            self.assertEqual(self.req(p)[0], 200, p)
        self.assertEqual(json.loads(self.req("/auth/me")[2]), {"auth": "oidc", "via": None, "user": None, "key": None})

    def test_signed_out(self):
        status, h, _ = self.req("/?x=1")
        self.assertEqual((status, h["Location"]), (302, "/auth/login?next=%2F%3Fx%3D1"))
        status, h, _ = self.req("/api/drift")
        self.assertEqual(status, 401)
        self.assertIn("Bearer", h["WWW-Authenticate"])
        self.assertEqual(self.req("/api/rescan", "POST", {})[0], 401)
        self.assertEqual(self.req("/api/drift", cookie="kd_session=forged.sig")[0], 401)

    def test_sign_in_key_lifecycle(self):
        status, h, _ = self.sign_in("/#orphans")
        self.assertEqual((status, h["Location"]), (302, "/#orphans"))
        session = self.cookie_of(h, auth.SESSION_COOKIE)
        self.assertEqual(self.req("/", cookie=session)[0], 200)
        self.assertEqual(self.req("/api/drift", cookie=session)[0], 200)
        me = json.loads(self.req("/auth/me", cookie=session)[2])
        self.assertEqual((me["via"], me["user"]["email"]), ("session", "ada@example.test"))

        status, _, body = self.req("/api/keys", "POST", {"name": "ci", "days": 30}, cookie=session)
        self.assertEqual(status, 201)
        made = json.loads(body)
        token = made["token"]
        self.assertEqual(made["key"]["created_by"], "ada@example.test")
        self.assertEqual(self.req("/api/drift", bearer=token)[0], 200)
        self.assertEqual(self.req("/api/orphans/rules", "POST", {"rule": {"kind": "Secret"}}, bearer=token)[0], 200)
        self.assertEqual(json.loads(self.req("/auth/me", bearer=token)[2])["key"]["name"], "ci")
        # a key can't manage keys, nor get the dashboard page's sign-in redirect
        self.assertEqual(self.req("/api/keys", bearer=token)[0], 403)
        self.assertEqual(self.req("/api/keys", "POST", {"name": "more"}, bearer=token)[0], 403)
        keys = json.loads(self.req("/api/keys", cookie=session)[2])["keys"]
        self.assertEqual([k["name"] for k in keys], ["ci"])
        self.assertNotIn(token, json.dumps(keys))

        self.assertEqual(self.req("/api/keys/delete", "POST", {"id": made["key"]["id"]}, cookie=session)[0], 200)
        self.assertEqual(self.req("/api/drift", bearer=token)[0], 401)
        self.assertEqual(self.req("/api/keys/delete", "POST", {"id": made["key"]["id"]}, cookie=session)[0], 404)

        status, h, body = self.req("/auth/logout", "POST", {}, cookie=session)
        self.assertEqual(json.loads(body)["redirect"], self.idp.base + "/end")
        self.assertIn("Max-Age=0", [c for c in h.get_all("Set-Cookie") if c.startswith(auth.SESSION_COOKIE)][0])

    def test_key_validation(self):
        session = self.cookie_of(self.sign_in()[1], auth.SESSION_COOKIE)
        self.assertEqual(self.req("/api/keys", "POST", {"name": ""}, cookie=session)[0], 400)
        self.assertEqual(self.req("/api/keys", "POST", {"name": "x", "days": 0}, cookie=session)[0], 400)

    def test_bad_callbacks(self):
        status, h, _ = self.req("/auth/login")
        login = self.cookie_of(h, auth.LOGIN_COOKIE)
        got = self.idp.authorize(h["Location"])
        self.assertEqual(self.req("/auth/callback?" + urllib.parse.urlencode({**got, "state": "nope"}), cookie=login)[0], 400)
        self.assertEqual(self.req("/auth/callback?" + urllib.parse.urlencode(got))[0], 400)  # no login cookie
        self.assertEqual(self.req("/auth/callback?error=access_denied", cookie=login)[0], 400)

    def test_group_not_allowed(self):
        self.idp.claims["groups"] = ["someone-else"]
        status, _, body = self.sign_in()
        self.assertEqual(status, 403)
        self.assertIn(b"Not allowed", body)

    def test_auth_off(self):
        main.AUTH = None
        self.assertEqual(self.req("/api/drift")[0], 200)
        self.assertEqual(self.req("/")[0], 200)
        self.assertEqual(json.loads(self.req("/auth/me")[2])["auth"], "none")
        self.assertEqual(self.req("/api/keys")[0], 404)
        self.assertEqual(self.req("/auth/login")[0], 404)


if __name__ == "__main__":
    unittest.main()
