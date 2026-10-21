import http.client
import json
import tempfile
import threading
import time
import unittest
from http.cookies import SimpleCookie
from http.server import ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlencode, urlsplit

from joserfc import jwt
from joserfc.errors import JoseError
from joserfc.jwk import RSAKey

from backend.api import make_handler
from backend.auth import (
    AuthManager,
    AuthenticationError,
    CLIENT_ID,
    ISSUER,
    SESSION_COOKIE,
)
from backend.storage import JobStore


class OIDCTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.key = RSAKey.generate_key(auto_kid=True)

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.store = JobStore(Path(self.directory.name) / "jobs.sqlite3")
        self.auth = AuthManager(self.store)
        self.metadata = {
            "issuer": ISSUER,
            "authorization_endpoint": ISSUER + "/auth",
            "token_endpoint": ISSUER + "/token",
            "jwks_uri": ISSUER + "/certs",
        }
        self.metadata_patch = patch.object(
            self.auth, "metadata", return_value=self.metadata
        )
        self.metadata_patch.start()

    def tearDown(self):
        self.metadata_patch.stop()
        self.directory.cleanup()

    def login(self, changes=None, key=None):
        url, cookie = self.auth.begin_login()
        parameters = parse_qs(urlsplit(url).query)
        self.assertEqual(parameters["code_challenge_method"], ["S256"])
        self.assertNotIn("code_verifier", parameters)
        claims = {
            "iss": ISSUER,
            "sub": "oidc-subject",
            "aud": CLIENT_ID,
            "iat": int(time.time()),
            "exp": int(time.time()) + 300,
            "nonce": parameters["nonce"][0],
            "target_roles": ["operator"],
        }
        claims.update(changes or {})
        signed = jwt.encode(
            {"alg": "RS256", "kid": (key or self.key).kid}, claims, key or self.key
        )
        response = Mock()
        response.json.return_value = {"keys": [self.key.as_dict(private=False)]}
        self.network = patch("backend.auth.requests.Session.get", return_value=response)
        self.token = patch(
            "backend.auth.OAuth2Session.fetch_token", return_value={"id_token": signed}
        )
        self.network.start()
        self.token.start()
        self.addCleanup(self.network.stop)
        self.addCleanup(self.token.stop)
        query = urlencode({"code": "one-time-code", "state": parameters["state"][0]})
        return query, cookie

    def test_signature_claims_pkce_session_hashes_and_callback_replay(self):
        query, flow_cookie = self.login()
        session_cookie = self.auth.complete_login(query, flow_cookie)
        session = self.auth.session(session_cookie)
        self.assertEqual(session["user"]["roles"], ["operator"])
        self.assertEqual(session["user"]["sub"], "oidc-subject")
        jar = SimpleCookie(session_cookie)
        self.assertTrue(jar[SESSION_COOKIE]["httponly"])
        self.assertEqual(jar[SESSION_COOKIE]["samesite"], "Lax")
        with self.store.connect() as db:
            row = db.execute("SELECT * FROM auth_sessions").fetchone()
            self.assertNotEqual(row["id"], jar[SESSION_COOKIE].value)
            self.assertNotIn("id_token", row["identity"])
        with self.assertRaises(AuthenticationError):
            self.auth.complete_login(query, flow_cookie)

    def test_wrong_issuer_audience_nonce_expiry_and_unknown_roles_are_rejected(self):
        for changes in [
            {"iss": "https://attacker.invalid"},
            {"aud": "other-client"},
            {"nonce": "wrong"},
            {"nonce": "wrong", "nonce_supported": False},
            {"exp": int(time.time()) - 100},
            {"target_roles": ["root"]},
            {"target_roles": "admin"},
            {"sub": ""},
        ]:
            with self.subTest(changes=changes):
                query, cookie = self.login(changes)
                with self.assertRaises((JoseError, AuthenticationError)):
                    self.auth.complete_login(query, cookie)
                self.assertIsNone(self.auth.session(cookie))

    def test_wrong_signing_key_and_state_and_browser_cookie_are_rejected(self):
        query, cookie = self.login(key=RSAKey.generate_key(auto_kid=True))
        with self.assertRaises(JoseError):
            self.auth.complete_login(query, cookie)
        for mode in ("state", "cookie", "expired", "duplicate"):
            query, cookie = self.login()
            if mode == "state":
                query = "code=code&state=attacker"
            elif mode == "cookie":
                cookie = ""
            elif mode == "duplicate":
                query += "&state=attacker"
            else:
                with self.store.connect() as db:
                    db.execute("UPDATE login_flows SET expires_at = 0")
            with self.subTest(mode=mode), self.assertRaises(AuthenticationError):
                self.auth.complete_login(query, cookie)

    def test_expired_and_logged_out_sessions_are_rejected(self):
        identity = {"sub": "subject", "name": "User", "roles": ["viewer"]}
        expired, _ = self.auth.create_session(identity, time.time() - 1)
        self.assertIsNone(self.auth.session(f"target_session={expired}"))
        current, _ = self.auth.create_session(identity, time.time() + 100)
        header = f"target_session={current}"
        self.assertIsNotNone(self.auth.session(header))
        self.auth.logout(header)
        self.assertIsNone(self.auth.session(header))


class AuthenticatedAPITest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.store = JobStore(Path(self.directory.name) / "jobs.sqlite3")
        self.auth = AuthManager(self.store)
        self.app = SimpleNamespace(
            jobs=Mock(return_value=[]),
            job=Mock(return_value=None),
            catalog=SimpleNamespace(describe=Mock(return_value={})),
            launch=Mock(return_value={"id": "new"}),
            cancel=Mock(return_value=(202, {})),
        )
        self.server = ThreadingHTTPServer(
            ("127.0.0.1", 0), make_handler(self.app, self.auth)
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.directory.cleanup()

    def headers(self, role):
        identifier, csrf = self.auth.create_session(
            {"sub": role, "issuer": ISSUER, "name": role, "roles": [role]},
            time.time() + 100,
        )
        return {
            "Cookie": f"target_session={identifier}",
            "X-CSRF-Token": csrf,
            "Origin": f"http://127.0.0.1:{self.server.server_port}",
        }

    def request(self, method, path, headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port)
        connection.request(method, path, headers=headers or {})
        response = connection.getresponse()
        result = response.status, json.loads(response.read())
        connection.close()
        return result

    def test_anonymous_and_forged_identity_headers_cannot_read_or_mutate(self):
        for method, path in [
            ("GET", "/api/jobs"),
            ("GET", "/api/catalog"),
            ("GET", "/api/jobs/secret"),
            ("POST", "/api/jobs"),
            ("POST", "/api/jobs/secret/cancel"),
        ]:
            with self.subTest(path=path, method=method):
                self.assertEqual(
                    self.request(
                        method,
                        path,
                        {
                            "Origin": "http://127.0.0.1:5173",
                            "X-Role": "admin",
                            "X-Forwarded-User": "admin",
                            "Authorization": "Bearer fake",
                        },
                    )[0],
                    401,
                )
        self.app.jobs.assert_not_called()
        self.app.launch.assert_not_called()
        self.app.cancel.assert_not_called()

    def test_viewer_reads_but_cannot_launch_or_cancel_and_operators_can(self):
        for role in ("viewer", "operator", "admin"):
            headers = self.headers(role)
            with self.subTest(role=role):
                self.assertEqual(self.request("GET", "/api/jobs", headers)[0], 200)
                identity = self.request("GET", "/api/auth/session", headers)[1]
                self.assertEqual(identity["can_operate"], role != "viewer")
                self.assertEqual(
                    self.request("POST", "/api/jobs", headers)[0],
                    403 if role == "viewer" else 201,
                )
                self.assertEqual(
                    self.request("POST", "/api/jobs/id/cancel", headers)[0],
                    403 if role == "viewer" else 202,
                )

    def test_missing_bad_csrf_and_cross_origin_are_rejected_before_launch(self):
        for overrides in [
            {"X-CSRF-Token": ""},
            {"X-CSRF-Token": "é"},
            {"Origin": "https://attacker.invalid"},
            {"Origin": ""},
        ]:
            headers = {**self.headers("operator"), **overrides}
            self.assertEqual(self.request("POST", "/api/jobs", headers)[0], 403)
        self.app.launch.assert_not_called()

    def test_logout_revokes_server_side_session(self):
        headers = self.headers("viewer")
        self.assertEqual(self.request("POST", "/api/auth/logout", headers)[0], 200)
        self.assertEqual(self.request("GET", "/api/jobs", headers)[0], 401)

    def test_callback_codes_are_not_logged(self):
        with (
            patch.object(self.auth, "complete_login", side_effect=AuthenticationError),
            patch("sys.stderr") as log,
        ):
            self.request(
                "GET", "/api/auth/callback?code=SECRET-CODE&state=SECRET-STATE"
            )
        output = "".join(call.args[0] for call in log.write.call_args_list)
        self.assertNotIn("SECRET-CODE", output)
        self.assertNotIn("SECRET-STATE", output)
