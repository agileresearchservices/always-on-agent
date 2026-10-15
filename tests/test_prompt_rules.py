from aoa.prompt import OPERATING_RULES, build_sections


def test_sign_off_is_logged_rule():
    # evals/accepted/flywheel_goodnight_is_logged.json: a goodnight gets a daily-log line, then a reply.
    rules = build_sections("tools", "facts")[1]
    assert rules.name == "operating_rules" and rules.text == OPERATING_RULES
    assert "goodnight" in OPERATING_RULES
    assert 'memory_write kind "log"' in OPERATING_RULES
    assert 'send_user kind "reply" (not a reaction)' in OPERATING_RULES
