import json
import tempfile
import unittest
from pathlib import Path

from dev.keycloak import ROLES, provision, realm


class KeycloakSetupTest(unittest.TestCase):
    def test_realm_has_exact_callback_pkce_and_no_password_grant(self):
        configuration = realm({role: "test-only" for role in ROLES})
        client = configuration["clients"][0]
        self.assertEqual(
            client["redirectUris"], ["http://127.0.0.1:5173/api/auth/callback"]
        )
        self.assertEqual(client["attributes"]["pkce.code.challenge.method"], "S256")
        self.assertFalse(client["directAccessGrantsEnabled"])
        self.assertFalse(client["implicitFlowEnabled"])
        self.assertFalse(configuration["registrationAllowed"])
        for user in configuration["users"]:
            self.assertEqual(user["realmRoles"], [user["username"]])

    def test_provisioning_is_private_and_preserves_existing_passwords(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "keycloak"
            first = provision(root)
            self.assertEqual(first, provision(root))
            self.assertEqual(len(set(first.values())), 4)
            self.assertEqual(root.stat().st_mode & 0o777, 0o700)
            for path in [
                root / "accounts.json",
                root / "data/import/target-realm.json",
            ]:
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)
                self.assertIsInstance(json.loads(path.read_text()), dict)
