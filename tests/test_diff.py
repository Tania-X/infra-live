import pytest

from reconciler.diff import compute_diff
from reconciler.models import Rule, SecurityGroup


def rule(port, cidr, *, direction="ingress", protocol="tcp", action="accept", priority=1):
    return Rule(
        direction=direction,
        protocol=protocol,
        port_range=port,
        source_cidr=cidr,
        action=action,
        priority=priority,
    )


def sg(rules, *, sg_id="sg-bp1a2b3c4d5e", name="web-sg", region="cn-hangzhou"):
    return SecurityGroup(id=sg_id, name=name, region=region, rules=tuple(rules))


def test_no_change():
    same = [rule("22/22", "198.51.100.7/32"), rule("443/443", "203.0.113.0/24")]
    diff = compute_diff(sg(same), sg(list(reversed(same))))
    assert diff.is_empty
    assert diff.summary == "无变更"


def test_add_rule():
    diff = compute_diff(sg([rule("443/443", "203.0.113.0/24")]), sg([]))
    assert [change.op for change in diff.changes] == ["add"]
    assert diff.changes[0].before is None
    assert diff.changes[0].after.source_cidr == "203.0.113.0/24"
    assert "1 处新增" in diff.summary


def test_remove_rule():
    diff = compute_diff(sg([]), sg([rule("6379/6379", "0.0.0.0/0")]))
    assert [change.op for change in diff.changes] == ["remove"]
    assert diff.changes[0].after is None


def test_replacement_paired_into_one_change():
    # 同方向同端口、只换源网段 → 一条「修改」，而不是一删一加两条
    desired = sg([rule("443/443", "203.0.113.0/24")])
    actual = sg([rule("443/443", "0.0.0.0/0")])
    diff = compute_diff(desired, actual)
    assert [change.op for change in diff.changes] == ["replace"]
    assert diff.changes[0].before.source_cidr == "0.0.0.0/0"
    assert diff.changes[0].after.source_cidr == "203.0.113.0/24"
    assert "1 处修改" in diff.summary


def test_mixed_ops_order_is_stable():
    desired = sg([rule("443/443", "203.0.113.0/24"), rule("8080/8080", "10.0.0.0/8")])
    actual = sg([rule("443/443", "0.0.0.0/0"), rule("6379/6379", "0.0.0.0/0")])
    diff = compute_diff(desired, actual)
    assert [change.op for change in diff.changes] == ["replace", "add", "remove"]
    assert diff.counts == {"add": 1, "remove": 1, "replace": 1}


def test_equivalent_spellings_do_not_produce_diff():
    # 归一化之后，「443」和「443/443」、带上位 host 位的网段都不算变更
    desired = sg([rule("443", "203.0.113.7/24")])
    actual = sg([rule("443/443", "203.0.113.0/24")])
    assert compute_diff(desired, actual).is_empty


def test_id_mismatch_rejected():
    with pytest.raises(ValueError, match="资源 ID 不一致"):
        compute_diff(sg([]), sg([], sg_id="sg-bp1zzzzzzzzz"))


def test_region_mismatch_rejected():
    with pytest.raises(ValueError, match="资源地域不一致"):
        compute_diff(sg([]), sg([], region="cn-beijing"))
