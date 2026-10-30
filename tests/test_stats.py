import datetime as dt
import json

import pytest

from jarvis_code import stats

TODAY = dt.date(2026, 10, 29)


def stamp(days_ago):
    local = dt.datetime.combine(TODAY - dt.timedelta(days=days_ago), dt.time(12)).astimezone()
    return local.astimezone(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def reply(mid, model, days_ago, **usage):
    return json.dumps({"type": "assistant", "timestamp": stamp(days_ago), "sessionId": "s1",
                       "message": {"id": mid, "model": model, "content": [], "usage": usage}}) + "\n"


@pytest.fixture
def claude(tmp_path):
    (tmp_path / "projects" / "p").mkdir(parents=True)
    return tmp_path


def write_logs(claude, *lines):
    (claude / "projects" / "p" / "s1.jsonl").write_text("".join(lines))


def test_prices_match_by_family_and_version():
    assert stats.price_of("claude-opus-5-5") == (4, 20, 0.2, 5, 8)
    assert stats.price_of("claude-opus-5-5[1m]") == stats.price_of("claude-opus-5-5")
    assert stats.price_of("claude-opus-4-5-20251101") == (5, 25, 0.5, 6.25, 10)
    assert stats.price_of("claude-opus-5-20260101") == (5, 25, 0.5, 6.25, 10)
    assert stats.price_of("claude-haiku-4-5-20251001") == (1, 5, 0.1, 1.25, 2)
    assert stats.price_of("claude-sonnet-5-5") == (2, 10, 0.1, 2.5, 4)
    assert stats.price_of("claude-opus-9-9") is None and stats.price_of("gpt-5") is None


def test_usage_by_kind_with_cache_write_split(claude):
    write_logs(
        claude,
        reply("a", "claude-opus-5-5", 0, input_tokens=1_000_000, output_tokens=1_000_000,
              cache_read_input_tokens=1_000_000, cache_creation_input_tokens=2_000_000,
              cache_creation={"ephemeral_5m_input_tokens": 1_000_000, "ephemeral_1h_input_tokens": 1_000_000}),
        reply("a", "claude-opus-5-5", 0, input_tokens=1_000_000, output_tokens=1_000_000),  # same reply again
        reply("b", "claude-opus-5-5", 0, cache_creation_input_tokens=1_000_000),  # no split: all 5 minutes
        reply("c", "<synthetic>", 0, input_tokens=99))
    s = stats.load(str(claude), TODAY)
    usage = s.days["2026-10-29"].usage["claude-opus-5-5"]
    assert usage == {"input": 1_000_000, "output": 1_000_000, "cache_read": 1_000_000,
                     "cache_write": 3_000_000, "cache_write_1h": 1_000_000}
    # 4 + 20 + 0.2 + 2 * 5 (5m writes) + 8 (1h write)
    assert stats.api_cost(s, 1)["total"] == pytest.approx(4 + 20 + 0.2 + 10 + 8)


def test_ranges_are_rolling(claude):
    write_logs(claude, *(reply(f"m{n}", "claude-sonnet-5-5", n, output_tokens=1_000_000) for n in (0, 6, 7, 29, 30)))
    s = stats.load(str(claude), TODAY)
    totals = {k: v["total"] for k, v in stats.api_costs(s).items()}
    assert totals == {"1d": pytest.approx(10), "7d": pytest.approx(20), "30d": pytest.approx(40)}
    assert [r[2] for r in stats.COST_RANGES] == [1, 7, 30]


def test_unknown_models_are_unpriced_not_free(claude):
    write_logs(claude, reply("a", "claude-opus-5-5", 0, output_tokens=1_000_000),
               reply("b", "claude-newthing-1", 0, output_tokens=500))
    cost = stats.api_cost(stats.load(str(claude), TODAY), 1)
    assert cost["total"] == pytest.approx(20) and list(cost["models"]) == ["claude-opus-5-5"]
    assert cost["unpriced"] == {"claude-newthing-1": 500}


def test_days_the_cache_only_has_a_total_for_are_estimated(claude):
    (claude / "stats-cache.json").write_text(json.dumps({
        "lastComputedDate": (TODAY - dt.timedelta(days=1)).isoformat(),
        "dailyModelTokens": [{"date": "2026-10-27", "tokensByModel": {"claude-opus-5-5": 2_000_000}}],
        # overall: half output, half cache reads
        "modelUsage": {"claude-opus-5-5": {"inputTokens": 0, "outputTokens": 1_000_000,
                                           "cacheReadInputTokens": 1_000_000, "cacheCreationInputTokens": 0}}}))
    s = stats.load(str(claude), TODAY)
    entry = stats.api_cost(s, 7)["models"]["claude-opus-5-5"]
    assert entry["estimated"] == 2_000_000 and entry["tokens"] == 2_000_000
    assert entry["cost"] == pytest.approx(20 + 0.2)
    assert stats.api_cost(s, 1)["total"] == 0


def test_no_files_at_all(claude):
    s = stats.load(str(claude), TODAY)
    assert stats.api_costs(s)["30d"] == {"total": 0, "models": {}, "unpriced": {}}
