"""Contact-email validation: the fail-fast gate SEC compliance depends on."""

from govfacts.server import _is_real_contact


def test_real_looking_address_is_accepted():
    assert _is_real_contact("jane.doe@realcompany.com") is True


def test_missing_value_is_rejected():
    assert _is_real_contact("") is False


def test_malformed_value_is_rejected():
    assert _is_real_contact("not-an-email") is False
    assert _is_real_contact("missing-at-sign.com") is False
    assert _is_real_contact("no-domain@") is False


def test_generated_config_sentinel_is_rejected():
    assert _is_real_contact("REPLACE_WITH_YOUR_EMAIL@example.org") is False


def test_common_placeholder_domains_are_rejected():
    assert _is_real_contact("someone@example.com") is False
    assert _is_real_contact("someone@example.org") is False
    assert _is_real_contact("someone@example.net") is False


def test_generic_placeholder_phrasing_is_rejected():
    assert _is_real_contact("you@realcompany.com") is False
    assert _is_real_contact("yourname@realcompany.com") is False
