import pytest

from reconciler.diff import compute_diff
from reconciler.models import Rule, SecurityGroup
from reconciler.risk import assess


def rule(port, cidr, *, direction="ingress", protocol="tcp", action="accept", priority=1):
    return Rule(
        direction=direction,
        protocol=protocol,
        port_range=port,
        source_cidr=cidr,
        action=action,
        priority=priority,
    )


def sg(rules):
    return SecurityGroup(id="sg-bp1a2b3c4d5e", name="web-sg", region="cn-hangzhou", rules=tuple(rules))


def report(desired_rules, actual_rules):
    return assess(compute_diff(sg(desired_rules), sg(actual_rules)))


def test_open_ssh_to_world_is_blocked():
    r = report([rule("22/22", "0.0.0.0/0")], [])
    assert r.overall == "blocked"
    assert r.blocked
    assert "禁止合并" in r.findings[0].reason


def test_open_redis_to_world_is_blocked():
    r = report([rule("6379/6379", "0.0.0.0/0")], [])
    assert r.overall == "blocked"


def test_open_all_ports_to_world_is_blocked():
    r = report([rule("-1/-1", "0.0.0.0/0")], [])
    assert r.overall == "blocked"


def test_open_non_managed_port_to_world_is_high():
    r = report([rule("80/80", "0.0.0.0/0")], [])
    assert r.overall == "high"


def test_narrow_add_is_medium():
    r = report([rule("443/443", "203.0.113.0/24")], [])
    assert r.overall == "medium"


def test_add_drop_rule_is_low():
    r = report([rule("22/22", "0.0.0.0/0", action="drop")], [])
    assert r.overall == "low"


def test_narrowing_replacement_is_low():
    r = report([rule("443/443", "203.0.113.0/24")], [rule("443/443", "0.0.0.0/0")])
    assert r.overall == "low"


def test_widening_replacement_is_high():
    r = report([rule("443/443", "0.0.0.0/0")], [rule("443/443", "203.0.113.0/24")])
    assert r.overall == "high"


def test_widening_managed_port_to_world_is_blocked():
    # 22 从固定 IP 放开到全网 → 即使「只是改一条」，也必须拦
    r = report([rule("22/22", "0.0.0.0/0")], [rule("22/22", "198.51.100.7/32")])
    assert r.overall == "blocked"


def test_removing_managed_port_rule_is_high():
    r = report([], [rule("22/22", "198.51.100.7/32")])
    assert r.overall == "high"


def test_removing_normal_rule_is_medium():
    r = report([], [rule("8080/8080", "10.0.0.0/8")])
    assert r.overall == "medium"


def test_empty_diff_is_low():
    r = report([], [])
    assert r.overall == "low"
    assert r.findings == ()


def test_overall_takes_highest_level():
    r = report(
        [rule("22/22", "0.0.0.0/0"), rule("443/443", "203.0.113.0/24")],
        [rule("6379/6379", "10.0.0.0/8")],
    )
    assert r.overall == "blocked"
    assert len(r.findings) == 3


def test_blocked_finding_identified():
    r = report([rule("3389/3389", "0.0.0.0/0")], [])
    assert len(r.of("blocked")) == 1
