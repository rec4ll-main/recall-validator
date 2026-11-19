"""The v3 evidence fetch must allow a slow (but answering) API inside the publish window."""

import v3_metrics_client as vmc


def test_default_timeout_allows_slow_evidence_query():
    # One full attempt plus one retry must fit the ~144 s publish window.
    assert 30 <= vmc.DEFAULT_TIMEOUT_SECONDS <= 70


def test_fetch_passes_default_timeout_to_opener():
    seen = {}

    def opener(req, timeout):
        seen["timeout"] = timeout
        raise OSError("stop after recording the timeout")

    try:
        vmc.fetch_v3_metrics("https://api.example.invalid", "k", [(1, "hk")], 31, 100, urlopen=opener)
    except vmc.V3MetricsError:
        pass
    assert seen["timeout"] == vmc.DEFAULT_TIMEOUT_SECONDS
