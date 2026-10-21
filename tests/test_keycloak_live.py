"""Opt-in OIDC integration check against the local realm; never prints credentials."""

import json
import os
import tempfile
import unittest
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlsplit

import requests

from backend.auth import AuthManager, ISSUER, ORIGIN, can_operate
from backend.storage import JobStore
from dev.keycloak import ROOT, ROLES


class LoginForm(HTMLParser):
    def __init__(self):
        super().__init__()
        self.action = None

    def handle_starttag(self, tag, attributes):
        attributes = dict(attributes)
        if tag == "form" and attributes.get("id") == "kc-form-login":
            self.action = attributes.get("action")


@unittest.skipUnless(
    os.environ.get("TARGET_TEST_KEYCLOAK") == "1", "requires local Keycloak"
)
class LocalKeycloakIntegrationTest(unittest.TestCase):
    def test_real_authorization_code_flow_for_every_role(self):
        accounts = json.loads((ROOT / ".data/keycloak/accounts.json").read_text())
        with tempfile.TemporaryDirectory() as directory:
            auth = AuthManager(JobStore(Path(directory) / "jobs.sqlite3"))
            for role in ROLES:
                with self.subTest(role=role), requests.Session() as browser:
                    browser.trust_env = False
                    url, flow_cookie = auth.begin_login()
                    page = browser.get(url, timeout=10)
                    self.assertEqual(page.status_code, 200)
                    form = LoginForm()
                    form.feed(page.text)
                    self.assertTrue(
                        form.action and form.action.startswith(ISSUER + "/")
                    )
                    # requests lacks the browser's Secure-cookie exception for trusted loopback HTTP.
                    for item in browser.cookies:
                        self.assertEqual(item.domain, "127.0.0.1")
                        item.secure = False
                    result = browser.post(
                        form.action,
                        data={
                            "username": role,
                            "password": accounts[role],
                            "credentialId": "",
                        },
                        allow_redirects=False,
                        timeout=10,
                    )
                    self.assertEqual(result.status_code, 302)
                    callback = result.headers["Location"]
                    self.assertTrue(callback.startswith(ORIGIN + "/api/auth/callback?"))
                    session_cookie = auth.complete_login(
                        urlsplit(callback).query, flow_cookie
                    )
                    session = auth.session(session_cookie)
                    self.assertEqual(session["user"]["name"], role)
                    self.assertEqual(session["user"]["roles"], [role])
                    self.assertEqual(can_operate(session), role != "viewer")
                    auth.logout(session_cookie)
                    self.assertIsNone(auth.session(session_cookie))
