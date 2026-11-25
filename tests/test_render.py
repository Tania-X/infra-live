from reconciler.diff import compute_diff
from reconciler.models import Rule, SecurityGroup
from reconciler.render import _cell, render_markdown
from reconciler.risk import assess


def rule(port, cidr, *, direction="ingress", protocol="tcp", action="accept"):
    return Rule(
        direction=direction,
        protocol=protocol,
        port_range=port,
        source_cidr=cidr,
        action=action,
    )


def sg(rules):
    return SecurityGroup(id="sg-bp1a2b3c4d5e", name="web-sg", region="cn-hangzhou", rules=tuple(rules))


def build(desired_rules, actual_rules, **kwargs):
    diff = compute_diff(sg(desired_rules), sg(actual_rules))
    return render_markdown(diff, assess(diff), **kwargs)


def test_no_change_rendering():
    same = [rule("443/443", "203.0.113.0/24")]
    md = build(same, same, resource_label="web-sg · cn-hangzhou")
    assert "无变更" in md
    assert "web-sg · cn-hangzhou" in md


def test_replacement_table_row():
    md = build([rule("443/443", "203.0.113.0/24")], [rule("443/443", "0.0.0.0/0")])
    assert "修改" in md
    assert "0.0.0.0/0" in md
    assert "203.0.113.0/24" in md


def test_blocked_banner_and_badge():
    md = build([rule("22/22", "0.0.0.0/0")], [])
    assert "⛔ BLOCKED" in md
    assert "禁止合并" in md


def test_high_badge():
    md = build([rule("80/80", "0.0.0.0/0")], [])
    assert "🔴 HIGH" in md


def test_medium_badge():
    md = build([rule("443/443", "203.0.113.0/24")], [])
    assert "🟡 MEDIUM" in md


def test_always_has_provenance_note():
    md = build([], [])
    assert "确定性计算" in md
    assert "审批请在 GitHub 完成" in md


def test_cell_escaping():
    # 竖线会拆散表格列，换行会拆散行
    assert _cell("a|b") == "a\\|b"
    assert _cell("x\ny") == "x y"
