"""Local OIDC login using Authlib, with opaque, expiring server-side sessions."""

import hmac
import json
import secrets
import time
from hashlib import sha256
from http.cookies import CookieError, SimpleCookie
from urllib.parse import parse_qs

import requests
from authlib.integrations.requests_client import OAuth2Session
from authlib.oidc.core import CodeIDToken
from joserfc import jwt
from joserfc.jwk import KeySet

ISSUER = "http://127.0.0.1:8180/realms/target"
ORIGIN = "http://127.0.0.1:5173"
CLIENT_ID = "target"
SESSION_COOKIE = "target_session"
FLOW_COOKIE = "target_login"
ROLES = frozenset({"viewer", "operator", "admin"})


class AuthenticationError(ValueError):
    pass


def digest(value):
    return sha256(value.encode()).hexdigest()


def cookie(name, value, max_age):
    jar = SimpleCookie()
    jar[name] = value
    jar[name]["httponly"] = True
    jar[name]["samesite"] = "Lax"
    jar[name]["path"] = "/api"
    jar[name]["max-age"] = max_age
    # shortcut: HTTP cookies are localhost-only; require HTTPS + Secure before any deployment.
    return jar[name].OutputString()


def cookie_value(header, name):
    jar = SimpleCookie()
    try:
        jar.load(header or "")
        return jar[name].value if name in jar else ""
    except CookieError:
        return ""


class AuthManager:
    def __init__(self, store):
        self.store = store
        with store.connect() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS login_flows (
                id TEXT PRIMARY KEY, state TEXT NOT NULL, nonce TEXT NOT NULL,
                verifier TEXT NOT NULL, expires_at REAL NOT NULL
            )""")
            db.execute("""CREATE TABLE IF NOT EXISTS auth_sessions (
                id TEXT PRIMARY KEY, identity TEXT NOT NULL, csrf TEXT NOT NULL,
                expires_at REAL NOT NULL
            )""")

    def metadata(self):
        with requests.Session() as session:
            session.trust_env = False
            response = session.get(
                ISSUER + "/.well-known/openid-configuration",
                timeout=5,
                allow_redirects=False,
            )
            response.raise_for_status()
            metadata = response.json()
        if metadata.get("issuer") != ISSUER or any(
            not isinstance(metadata.get(name), str)
            or not metadata[name].startswith(ISSUER + "/")
            for name in ("authorization_endpoint", "token_endpoint", "jwks_uri")
        ):
            raise AuthenticationError("Invalid identity-provider configuration")
        return metadata

    def client(self):
        client = OAuth2Session(
            CLIENT_ID,
            scope="openid profile",
            redirect_uri=ORIGIN + "/api/auth/callback",
            code_challenge_method="S256",
            token_endpoint_auth_method="none",
        )
        client.trust_env = False
        return client

    def begin_login(self):
        metadata = self.metadata()
        flow, nonce, verifier = (secrets.token_urlsafe(32) for _ in range(3))
        with self.client() as client:
            url, state = client.create_authorization_url(
                metadata["authorization_endpoint"],
                nonce=nonce,
                code_verifier=verifier,
            )
        with self.store.connect() as db:
            db.execute("DELETE FROM login_flows WHERE expires_at <= ?", (time.time(),))
            if db.execute("SELECT COUNT(*) FROM login_flows").fetchone()[0] >= 100:
                raise AuthenticationError("Too many pending logins; try again later")
            db.execute(
                "INSERT INTO login_flows VALUES (?, ?, ?, ?, ?)",
                (digest(flow), state, nonce, verifier, time.time() + 300),
            )
        return url, cookie(FLOW_COOKIE, flow, 300)

    def complete_login(self, query, cookies):
        parameters = parse_qs(query)
        if any(len(parameters.get(name, [])) != 1 for name in ("code", "state")):
            raise AuthenticationError("Missing or repeated callback parameters")
        flow = cookie_value(cookies, FLOW_COOKIE)
        with self.store.connect() as db:
            row = db.execute(
                "DELETE FROM login_flows WHERE id = ? RETURNING *", (digest(flow),)
            ).fetchone()
        if (
            not row
            or row["expires_at"] <= time.time()
            or not hmac.compare_digest(row["state"], parameters["state"][0])
        ):
            raise AuthenticationError("Login expired or invalid state; sign in again")
        metadata = self.metadata()
        with self.client() as client:
            token = client.fetch_token(
                metadata["token_endpoint"],
                code=parameters["code"][0],
                code_verifier=row["verifier"],
                timeout=5,
                allow_redirects=False,
            )
        with requests.Session() as session:
            session.trust_env = False
            response = session.get(
                metadata["jwks_uri"], timeout=5, allow_redirects=False
            )
            response.raise_for_status()
            keys = KeySet.import_key_set(response.json())
        decoded = jwt.decode(token["id_token"], keys, algorithms=["RS256"])
        claims = CodeIDToken(
            decoded.claims,
            decoded.header,
            {
                "iss": {"essential": True, "value": ISSUER},
                "aud": {"essential": True, "value": CLIENT_ID},
            },
            {
                "nonce": row["nonce"],
                "client_id": CLIENT_ID,
                "access_token": token.get("access_token"),
            },
        )
        claims.validate(leeway=0)
        roles = claims.get("target_roles", [])
        if (
            not isinstance(claims["sub"], str)
            or not claims["sub"]
            or not isinstance(roles, list)
        ):
            raise AuthenticationError("Invalid identity claims")
        roles = sorted(
            ROLES.intersection(role for role in roles if isinstance(role, str))
        )
        if not roles:
            raise AuthenticationError("No Target role assigned")
        identity = {
            "sub": claims["sub"],
            "issuer": ISSUER,
            "name": claims.get("preferred_username") or claims["sub"],
            "roles": roles,
        }
        session_id, _ = self.create_session(
            identity, min(claims["exp"], time.time() + 300)
        )
        return cookie(SESSION_COOKIE, session_id, 300)

    def create_session(self, identity, expires_at):
        session_id, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        with self.store.connect() as db:
            db.execute(
                "DELETE FROM auth_sessions WHERE expires_at <= ?", (time.time(),)
            )
            db.execute(
                "INSERT INTO auth_sessions VALUES (?, ?, ?, ?)",
                (digest(session_id), json.dumps(identity), csrf, expires_at),
            )
        return session_id, csrf

    def session(self, cookies):
        session_id = cookie_value(cookies, SESSION_COOKIE)
        if not session_id:
            return None
        with self.store.connect() as db:
            row = db.execute(
                "SELECT * FROM auth_sessions WHERE id = ? AND expires_at > ?",
                (digest(session_id), time.time()),
            ).fetchone()
        return (
            {"user": json.loads(row["identity"]), "csrf": row["csrf"]} if row else None
        )

    def logout(self, cookies):
        with self.store.connect() as db:
            db.execute(
                "DELETE FROM auth_sessions WHERE id = ?",
                (digest(cookie_value(cookies, SESSION_COOKIE)),),
            )
        return cookie(SESSION_COOKIE, "", 0)


def can_operate(session):
    return bool(
        session and {"operator", "admin"}.intersection(session["user"]["roles"])
    )
