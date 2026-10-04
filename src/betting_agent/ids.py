"""Identifier helpers (spec §5).

``attempt_id = "A-0042"`` (from the ``attempts`` autoincrement ``seq``);
``bet_id = "A-0042-B03"`` (1-based ticket order, also the Kalshi ``client_order_id``);
``group_id = "A-0042-G1"`` for structural bet groups.
"""

from __future__ import annotations

import re

# client_order_id == bet_id; this is the system-vs-personal discriminator (spec §10).
# The digit ranges are deliberately wider than the *formatters* below: ``attempt_id_from_seq``
# and ``bet_id`` zero-pad to 4 and 2, but neither truncates, so attempt 10000 renders
# "A-10000" and bet index 100 renders "B100" (CI-4). A too-narrow pattern here would
# classify every system order as a *personal* fill in settle and reconcile — a silent,
# catastrophic misattribution. Matching more than the formatters emit is free: nothing
# else in the system mints ids of this shape, and the canary's "CANARY-<epoch>" is
# still outside the pattern by construction.
CLIENT_ORDER_ID_RE = re.compile(r"^A-\d{4,6}-B\d{2,3}$")


def attempt_id_from_seq(seq: int) -> str:
    """``42 -> "A-0042"``."""
    return f"A-{seq:04d}"


def bet_id(attempt_id: str, ticket_index: int) -> str:
    """``("A-0042", 3) -> "A-0042-B03"`` (``ticket_index`` is 1-based)."""
    return f"{attempt_id}-B{ticket_index:02d}"


def group_db_id(attempt_id: str, n: int) -> str:
    """``("A-0042", 1) -> "A-0042-G1"``."""
    return f"{attempt_id}-G{n}"


def shadow_bet_id(attempt_id: str, candidate_id: str, index: int) -> str:
    """``("A-0042", "C2", 1) -> "A-0042-C2-S01"`` (``index`` is 1-based).

    Deliberately does NOT match ``CLIENT_ORDER_ID_RE`` — shadow bets never
    reach the exchange (Jul29 spec L16).
    """
    return f"{attempt_id}-{candidate_id}-S{index:02d}"
