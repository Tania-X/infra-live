import pytest
from pydantic import ValidationError

from reconciler.models import Rule, SecurityGroup, load_security_group_yaml, parse_port_range


def make_rule(**overrides):
    payload = {
        "direction": "ingress",
        "protocol": "tcp",
        "port_range": "443/443",
        "source_cidr": "0.0.0.0/0",
    }
    payload.update(overrides)
    return Rule(**payload)


# ---------- 端口归一化 ----------

def test_single_port_normalized_to_pair():
    assert make_rule(port_range="443").port_range == "443/443"


def test_port_range_kept():
    assert make_rule(port_range="8000/9000").port_range == "8000/9000"


def test_all_ports_alias():
    assert make_rule(port_range="-1/-1").port_range == "-1/-1"


@pytest.mark.parametrize("bad", ["443/80", "0/80", "70000", "80-90", ""])
def test_illegal_port_range_rejected(bad):
    with pytest.raises(ValidationError):
        make_rule(port_range=bad)


# ---------- 网段归一化 ----------

def test_cidr_masked_to_network():
    # 203.0.113.7/24 与 203.0.113.0/24 必须视为同一条规则，否则 diff 会出假变更
    assert make_rule(source_cidr="203.0.113.7/24").source_cidr == "203.0.113.0/24"


def test_bare_ip_becomes_slash_32():
    assert make_rule(source_cidr="198.51.100.7").source_cidr == "198.51.100.7/32"


def test_ipv6_version_inferred():
    assert make_rule(source_cidr="::/0").ip_version == "ipv6"


def test_ip_version_mismatch_rejected():
    with pytest.raises(ValidationError):
        make_rule(source_cidr="::/0", ip_version="ipv4")


# ---------- 字段白名单 ----------

def test_unknown_field_rejected():
    with pytest.raises(ValidationError):
        make_rule(nic_type="internet")


def test_description_newline_rejected():
    with pytest.raises(ValidationError):
        make_rule(description="第一行\n第二行")


def test_description_too_long_rejected():
    with pytest.raises(ValidationError):
        make_rule(description="x" * 65)


# ---------- 整体校验 ----------

def test_duplicate_rules_rejected():
    rule = make_rule()
    with pytest.raises(ValidationError):
        SecurityGroup(id="sg-bp1a2b3c4d5e", name="web-sg", region="cn-hangzhou", rules=[rule, rule])


def test_illegal_security_group_id_rejected():
    with pytest.raises(ValidationError):
        SecurityGroup(id="not-a-sg", name="web-sg", region="cn-hangzhou")


def test_parse_port_range_helper():
    assert parse_port_range("443") == (443, 443)
    assert parse_port_range("-1/-1") == (1, -1)


# ---------- 读取 ----------

def test_load_yaml_from_disk(tmp_path):
    path = tmp_path / "sg.yaml"
    path.write_text(
        "kind: security_group\n"
        "id: sg-bp1a2b3c4d5e\n"
        "name: web-sg\n"
        "region: cn-hangzhou\n"
        "rules:\n"
        "  - direction: ingress\n"
        "    protocol: tcp\n"
        "    port_range: '443'\n"
        "    source_cidr: 198.51.100.7/32\n",
        encoding="utf-8",
    )
    sg = load_security_group_yaml(path)
    assert sg.rules[0].port_range == "443/443"
