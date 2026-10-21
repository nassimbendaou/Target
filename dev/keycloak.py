"""Provision and run a private, localhost-only Keycloak development realm."""

import json
import os
import secrets
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ROLES = ("viewer", "operator", "admin")


def realm(accounts):
    return {
        "realm": "target",
        "enabled": True,
        "sslRequired": "none",
        "registrationAllowed": False,
        "resetPasswordAllowed": False,
        "bruteForceProtected": True,
        "accessTokenLifespan": 300,
        "ssoSessionIdleTimeout": 900,
        "roles": {"realm": [{"name": role} for role in ROLES]},
        "clients": [
            {
                "clientId": "target",
                "enabled": True,
                "publicClient": True,
                "standardFlowEnabled": True,
                "directAccessGrantsEnabled": False,
                "implicitFlowEnabled": False,
                "redirectUris": ["http://127.0.0.1:5173/api/auth/callback"],
                "webOrigins": ["http://127.0.0.1:5173"],
                "attributes": {"pkce.code.challenge.method": "S256"},
                "protocolMappers": [
                    {
                        "name": "target-roles",
                        "protocol": "openid-connect",
                        "protocolMapper": "oidc-usermodel-realm-role-mapper",
                        "config": {
                            "claim.name": "target_roles",
                            "multivalued": "true",
                            "jsonType.label": "String",
                            "id.token.claim": "true",
                            "access.token.claim": "true",
                        },
                    }
                ],
            }
        ],
        "users": [
            {
                "username": role,
                "enabled": True,
                "email": f"{role}@target.invalid",
                "emailVerified": True,
                "firstName": role.title(),
                "lastName": "Local",
                "realmRoles": [role],
                "credentials": [
                    {"type": "password", "value": accounts[role], "temporary": False}
                ],
            }
            for role in ROLES
        ],
    }


def provision(directory):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    directory.chmod(0o700)
    accounts_file = directory / "accounts.json"
    if not accounts_file.exists():
        accounts = {
            role: secrets.token_urlsafe(24) for role in (*ROLES, "bootstrap-admin")
        }
        with open(
            accounts_file, "x", opener=lambda path, flags: os.open(path, flags, 0o600)
        ) as stream:
            json.dump(accounts, stream, indent=2)
    accounts_file.chmod(0o600)
    accounts = json.loads(accounts_file.read_text())
    imports = directory / "data" / "import"
    imports.mkdir(parents=True, exist_ok=True, mode=0o700)
    realm_file = imports / "target-realm.json"
    with open(
        realm_file, "w", opener=lambda path, flags: os.open(path, flags, 0o600)
    ) as stream:
        json.dump(realm(accounts), stream)
    realm_file.chmod(0o600)
    return accounts


def main():
    binary = shutil.which("kc.sh")
    if not binary:
        raise SystemExit("Run with nix develop --command python -m dev.keycloak")
    directory = ROOT / ".data" / "keycloak"
    accounts = provision(directory)
    package = Path(binary).resolve().parent.parent
    for name in ("lib", "providers"):
        path = directory / name
        if not path.exists():
            path.symlink_to(package / name, target_is_directory=True)
    environment = os.environ.copy()
    environment.update(
        KC_HOME_DIR=str(directory),
        KC_CONF_DIR=str(package / "conf"),
        KC_BOOTSTRAP_ADMIN_USERNAME="bootstrap-admin",
        KC_BOOTSTRAP_ADMIN_PASSWORD=accounts["bootstrap-admin"],
    )
    print(
        f"Local credentials: {directory / 'accounts.json'} (not printed or committed)",
        flush=True,
    )
    subprocess.run(
        [
            binary,
            "start",
            "--optimized",
            "--http-enabled=true",
            "--http-host=127.0.0.1",
            "--cache=local",
            "--http-port=8180",
            "--hostname=http://127.0.0.1:8180",
            "--import-realm",
        ],
        env=environment,
        check=True,
    )


if __name__ == "__main__":
    main()
