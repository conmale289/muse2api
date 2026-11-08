"""Deployment artefacts: container hardening (R13) and proxy rate limiting (R8).

These assert the properties the audit asked for, so a future edit cannot silently drop
the non-root user, the healthcheck, or the auth throttle.
"""
import re
import sys
from pathlib import Path

root = Path(__file__).resolve().parents[1]


def _read(relative: str) -> str:
    return (root / relative).read_text(encoding="utf-8")


# ------------------------------ R13: Dockerfile -----------------------------
def test_dockerfile_switches_to_a_non_root_user():
    text = _read("Dockerfile")
    user_lines = re.findall(r"^USER\s+(.+)$", text, re.M)
    assert user_lines, "the image must not run as root"
    assert user_lines[-1].strip() != "root"
    assert "root" not in user_lines[-1].split(), "the final USER must not be root"


def test_dockerfile_creates_the_user_it_switches_to():
    text = _read("Dockerfile")
    assert ("useradd" in text) or ("adduser" in text), \
        "the runtime user must be created in the image"
    assert re.search(r"\bgroupadd\b|\baddgroup\b", text), \
        "the runtime group must be created in the image"


def test_dockerfile_uid_and_gid_are_build_args():
    """The uid must be overridable so the ./data bind mount stays writable."""
    text = _read("Dockerfile")
    assert re.search(r"^ARG\s+UID", text, re.M)
    assert re.search(r"^ARG\s+GID", text, re.M)
    assert "${UID}" in text and "${GID}" in text


def test_dockerfile_declares_a_healthcheck_on_the_service_port():
    text = _read("Dockerfile")
    assert re.search(r"^HEALTHCHECK\s", text, re.M)
    assert "/healthz" in text
    exposed = re.findall(r"^EXPOSE\s+(\d+)", text, re.M)
    assert exposed, "the image must declare its port"
    healthcheck_url = re.search(r"/healthz", text)
    assert healthcheck_url
    assert f":{exposed[0]}/healthz" in text, \
        "the healthcheck must hit the exposed port"


def test_dockerfile_owns_the_writable_paths():
    text = _read("Dockerfile")
    assert "chown" in text, "the runtime user must own /app and the data dirs"


# ------------------------------ R13: compose --------------------------------
def test_compose_declares_a_healthcheck():
    text = _read("docker-compose.yml")
    assert "healthcheck:" in text
    assert "/healthz" in text


def test_compose_passes_the_uid_build_args():
    text = _read("docker-compose.yml")
    assert "UID:" in text and "GID:" in text


# ------------------------------ R8: nginx -----------------------------------
def test_nginx_example_rate_limits_authentication():
    text = _read("deploy/nginx.example.conf")
    assert "limit_req_zone" in text, "no brute-force throttle is configured"
    assert re.search(r"limit_req\s+zone=", text), "the zone is declared but never applied"
    assert "limit_req_status" in text, "clients should get 429, not nginx's default 503"
