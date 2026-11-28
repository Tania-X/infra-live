#!/usr/bin/env python3
"""把关键行为固定下来。不依赖 pytest。

    python tests.py

每条测试只回答一个问题：这个行为以后会不会被我改坏。
"""

import model
import plan


def ruleset(*rules):
    return model.parse_ruleset({"name": "web-sg", "rules": list(rules)})


def rule(ports, peer, action="accept", direction="ingress", protocol="tcp"):
    return {
        "direction": direction,
        "protocol": protocol,
        "ports": ports,
        "peer": peer,
        "action": action,
    }


def kinds(changes):
    return [change["kind"] for change in changes]


def level(changes):
    return plan.judge(changes)[0]


# 1. 收窄：两条规则各算一处修改，而不是删两条加两条
def test_narrowing_is_two_modifies_and_low():
    desired = ruleset(rule("22", "198.51.100.7"), rule("443", "203.0.113.0/24"))
    actual = ruleset(rule("22", "0.0.0.0/0"), rule("443", "0.0.0.0/0"))
    changes = model.diff(desired, actual)
    assert kinds(changes) == ["modify", "modify"]
    assert level(changes) == "低"


# 2. 在最前面插一条，后面的规则不该跟着"变"
def test_insert_does_not_touch_the_rest():
    desired = ruleset(rule("8080", "10.0.0.0/8"), rule("80", "0.0.0.0/0"))
    actual = ruleset(rule("80", "0.0.0.0/0"))
    changes = model.diff(desired, actual)
    assert kinds(changes) == ["add"]
    assert level(changes) == "中"


# 3. 调换先后是变更，但不是新增、也不是删除
def test_swap_is_one_reorder():
    desired = ruleset(rule("80", "0.0.0.0/0"), rule("443", "0.0.0.0/0"))
    actual = ruleset(rule("443", "0.0.0.0/0"), rule("80", "0.0.0.0/0"))
    assert kinds(model.diff(desired, actual)) == ["reorder"]


# 4. 只改动作也算修改，方向不同风险不同
def test_action_change_is_modify():
    tighter = model.diff(
        ruleset(rule("22", "10.0.0.0/8", "drop")),
        ruleset(rule("22", "10.0.0.0/8", "accept")),
    )
    assert kinds(tighter) == ["modify"]
    assert level(tighter) == "低"

    looser = model.diff(
        ruleset(rule("22", "10.0.0.0/8", "accept")),
        ruleset(rule("22", "10.0.0.0/8", "drop")),
    )
    assert kinds(looser) == ["modify"]
    assert level(looser) == "高"


# 5. 改完之后仍然对全网开放管理端口 → 禁止合并
def test_widening_back_to_world_on_ssh_is_blocked():
    changes = model.diff(
        ruleset(rule("22", "0.0.0.0/0")),
        ruleset(rule("22", "198.51.100.7/32")),
    )
    assert level(changes) == "禁止合并"


# 6. 全端口的各种写法等价，不能报假变更
def test_all_port_spellings_are_equal():
    assert model.diff(
        ruleset(rule("-1/-1", "10.0.0.0/8")), ruleset(rule("any", "10.0.0.0/8"))
    ) == []
    assert model.diff(
        ruleset(rule("1/65535", "10.0.0.0/8")), ruleset(rule("any", "10.0.0.0/8"))
    ) == []


# 7. 写错了要报错，不能猜
def test_bad_input_raises():
    for payload in (
        {"name": "x", "rules": [rule("22", "0.0.0.0/0"), rule("22", "0.0.0.0/0")]},
        {"name": "x", "rules": [{"ports": "22", "peer": "0.0.0.0/0", "nic": "x"}]},
        {"name": "x", "rules": [rule("70000", "0.0.0.0/0")]},
        {"name": "x"},
    ):
        try:
            model.parse_ruleset(payload)
        except ValueError:
            continue
        raise AssertionError(f"应该报错：{payload}")


def main():
    tests = [value for name, value in sorted(globals().items())
             if name.startswith("test_")]
    for test in tests:
        test()
        print(f"✓ {test.__name__}")
    print(f"\n{len(tests)} 条通过")


if __name__ == "__main__":
    main()
