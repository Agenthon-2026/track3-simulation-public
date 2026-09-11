"""
Track 3 scoring package — semantic-preserving accelerated market simulation.

Exposes:
    build_verifier: PRODUCTION factory for the HierarchicalVerifier the QFBench2 driver calls.
                    Requires the trusted C1 plan and C2 run record; it has no participant-rate
                    fallback and cannot produce a score without a host measurement.
    build_developer_verifier: local practice factory. Everything it emits is rankable=false.
    LEADERBOARD_SORT: Ranking direction ("desc" — higher events/sec wins).
    cluster_key: Bootstrap grouping for the shared scorer (scenario family, not as-of date).
"""

from .scoring import (
    LEADERBOARD_SORT,
    build_developer_verifier,
    build_verifier,
    cluster_key,
)

__all__ = [
    "LEADERBOARD_SORT",
    "build_developer_verifier",
    "build_verifier",
    "cluster_key",
]

# The shared scorer version (see scoring.SCORER_VERSION). Re-exported so a
# participant can read it without importing the scoring module.
from .scoring import SCORER_VERSION, scorer_identity  # noqa: E402

__version__ = SCORER_VERSION
