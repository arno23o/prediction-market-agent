"""Identifier helpers — spec §5."""

from betting_agent.ids import (
    CLIENT_ORDER_ID_RE,
    attempt_id_from_seq,
    bet_id,
    group_db_id,
)


def test_attempt_id_from_seq():
    assert attempt_id_from_seq(1) == "A-0001"
    assert attempt_id_from_seq(42) == "A-0042"
    assert attempt_id_from_seq(9999) == "A-9999"


def test_bet_id_is_one_based_two_digit():
    assert bet_id("A-0042", 1) == "A-0042-B01"
    assert bet_id("A-0042", 3) == "A-0042-B03"
    assert bet_id("A-0042", 12) == "A-0042-B12"


def test_group_db_id():
    assert group_db_id("A-0042", 1) == "A-0042-G1"
    assert group_db_id("A-0007", 2) == "A-0007-G2"


def test_client_order_id_regex():
    assert CLIENT_ORDER_ID_RE.match("A-0042-B03")
    assert CLIENT_ORDER_ID_RE.match(bet_id("A-0001", 1))
    # Rejected forms. "A-0042-B003" moved OUT of this list with CI-4: the formatters
    # zero-pad but never truncate, so a 3-digit bet index is a real id the pattern has
    # to accept or misclassify as a personal fill.
    for bad in ["A-42-B03", "A-0042-B3", "a-0042-b03", "A-0042-B03x", "B-0042-B03"]:
        assert not CLIENT_ORDER_ID_RE.match(bad), bad


def test_client_order_id_regex_has_format_headroom():
    """CI-4: the id format grows past 4-digit attempts / 2-digit bet indices."""
    assert bet_id(attempt_id_from_seq(10000), 100) == "A-10000-B100"
    for good in ["A-10000-B100", "A-0042-B003", "A-123456-B99", bet_id("A-99999", 7)]:
        assert CLIENT_ORDER_ID_RE.match(good), good
    # Still bounded: the pattern is a discriminator, not a wildcard.
    for bad in ["A-1234567-B01", "A-0042-B0001", "CANARY-1754000000"]:
        assert not CLIENT_ORDER_ID_RE.match(bad), bad
