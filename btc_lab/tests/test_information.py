"""Information collection is independent of execution and all tests are offline."""
import ast
from datetime import datetime, timezone
import inspect
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

from btc_lab import information as info


NOW = datetime(2026, 9, 24, 12, tzinfo=timezone.utc).timestamp()


def article(name="policy", *, published=NOW - 3600, **extra):
    return {"headline": "Federal Reserve issues statement", "url": f"https://www.federalreserve.gov/newsevents/{name}.htm",
            "source": info.SOURCE, "published_at": published, **extra}


def rss(name="policy", date="Thu, 24 Sep 2026 11:00:00 GMT"):
    return (f'<rss version="2.0"><channel><item><title>Federal Reserve &amp; policy</title>'
            f'<link>https://www.federalreserve.gov/newsevents/{name}.htm</link>'
            f'<pubDate>{date}</pubDate></item></channel></rss>').encode()


def valid_digest(source_ids):
    return {"summary": "연준 공식 정책 관련 헤드라인입니다.", "event_type": "monetary_policy",
            "affected_assets": ["UNKNOWN"], "facts": ["공식 보도자료 제목이 수집됐습니다."],
            "uncertainties": ["본문과 시장 영향은 확인하지 않았습니다."], "source_ids": source_ids}


class FakeClient:
    def __init__(self):
        self.calls = []

    def __call__(self, **kwargs):
        self.calls.append(kwargs)
        inputs = json.loads(kwargs["prompt"])["source_articles"]
        return valid_digest([a["id"] for a in inputs])


def test_default_is_deterministic_information_without_gemini(tmp_path):
    client = FakeClient()
    report = info.run_once(tmp_path, now=NOW, fixture_articles=[article()], gemini_client=client)
    assert report["mode"] == "information_only"
    assert report["status"] == "ok"
    assert report["ai"] == {"status": "disabled", "digest": None}
    assert not client.calls
    assert report["articles"][0]["first_seen"] == info._iso(NOW)
    assert report["articles"][0]["observed_at"] == info._iso(NOW)
    assert report["articles"][0]["published_at"] == info._iso(NOW - 3600)
    assert (tmp_path / "information.json").exists()
    assert len(list((tmp_path / "history").glob("*.json"))) == 1


def test_rss_parsing_preserves_attribution_and_bounds_xml():
    result = info.parse_fed_rss(rss())
    assert result[0]["headline"] == "Federal Reserve & policy"
    assert result[0]["published_at"] == "Thu, 24 Sep 2026 11:00:00 GMT"
    assert result[0]["source"] == info.SOURCE
    with pytest.raises(ValueError):
        info.parse_fed_rss(b"x" * (info.MAX_BYTES + 1))
    with pytest.raises(ValueError):
        info.parse_fed_rss(b"<html></html>")


@pytest.mark.parametrize("encoding", ["utf-8", "utf-16", "utf-32"])
def test_xml_dtd_and_external_entities_are_rejected(encoding):
    payload = '<!DOCTYPE rss [<!ENTITY x SYSTEM "file:///private">]><rss><channel><item><title>&x;</title></item></channel></rss>'
    with pytest.raises(ValueError, match="DTD"):
        info.parse_fed_rss(payload.encode(encoding))


def test_public_fetch_is_get_allowlisted_and_size_and_timeout_bounded(monkeypatch):
    seen = {}
    class Response:
        def __enter__(self): return self
        def __exit__(self, *_): pass
        def read(self, limit):
            seen["read_limit"] = limit
            return rss()
    class Opener:
        def open(self, request, timeout):
            seen.update(url=request.full_url, method=request.method, timeout=timeout, headers=request.headers)
            return Response()
    monkeypatch.setattr(info, "build_opener", lambda *_: Opener())
    assert info.fetch_fed_rss() == rss()
    assert seen["url"] == info.FEED_URL and seen["method"] == "GET"
    assert seen["timeout"] == 15 and seen["read_limit"] == info.MAX_BYTES + 1
    assert not any("key" in k.lower() or "authorization" in k.lower() for k in seen["headers"])
    with pytest.raises(ValueError):
        info.fetch_fed_rss(timeout=16)
    with pytest.raises(ValueError):
        info._FedRedirects().redirect_request(None, None, 302, "", {}, "https://other.example/feed")


def test_duplicates_stable_ids_and_first_seen_survive_feed_rotation(tmp_path):
    first = info.run_once(tmp_path, now=NOW, fixture_articles=[article("a"), article("a")])
    assert len(first["articles"]) == 1
    original = first["articles"][0]
    info.run_once(tmp_path, now=NOW + 3600, fixture_articles=[article("b")])
    returned = info.run_once(tmp_path, now=NOW + 7200, fixture_articles=[article("a")])["articles"][0]
    assert returned["id"] == original["id"]
    assert returned["first_seen"] == original["first_seen"]
    assert returned["observed_at"] == info._iso(NOW + 7200)


def test_successful_feed_cache_keeps_original_observed_time(tmp_path):
    calls = []
    def fetch(**kwargs):
        calls.append(kwargs)
        return rss()
    first = info.run_once(tmp_path, now=NOW, feed_fetcher=fetch)
    second = info.run_once(tmp_path, now=NOW + 60, feed_fetcher=fetch)
    assert len(calls) == 1
    assert second["source_cache_hit"]
    assert second["articles"] == [{**first["articles"][0], "quality": {**first["articles"][0]["quality"],
                                               "age_sec": 3660, "observed_age_sec": 60}}]
    assert second["source_last_success_at"] == first["source_last_success_at"]


def test_fetch_failure_never_rebrands_stale_cache_as_fresh(tmp_path):
    client = FakeClient()
    first = info.run_once(tmp_path, now=NOW, feed_fetcher=lambda **_: rss(), gemini=True,
                          model="fixture-model", gemini_client=client)
    def fail(**_):
        raise RuntimeError("secret-token-in-exception")
    second = info.run_once(tmp_path, now=NOW + 901, feed_fetcher=fail, gemini=True,
                           model="fixture-model", gemini_client=client)
    assert second["source_status"] == "stale" and second["status"] == "information_unavailable"
    assert not second["eligible_source_ids"]
    assert second["source_last_success_at"] == first["source_last_success_at"]
    assert second["articles"][0]["observed_at"] == first["articles"][0]["observed_at"]
    assert second["articles"][0]["quality"]["status"] == "stale"
    assert second["ai"]["status"] == "unavailable" and len(client.calls) == 1
    assert "secret-token" not in (tmp_path / "information.json").read_text(encoding="utf-8")


def test_initial_failure_reports_unavailable_without_provider_call(tmp_path):
    client = FakeClient()
    def fail(**_): raise TimeoutError("private provider exception")
    report = info.run_once(tmp_path, now=NOW, feed_fetcher=fail, gemini=True, model="fixture", gemini_client=client)
    assert report["status"] == "information_unavailable" and report["source_status"] == "unavailable"
    assert report["articles"] == [] and not client.calls


@pytest.mark.parametrize("published,status", [(None, "unknown"), ("bad time", "unknown"),
                                                (float("nan"), "unknown"), (NOW - 800000, "stale"),
                                                (NOW + 1000, "unknown")])
def test_unknown_expired_and_future_publication_times_cannot_enter_model(tmp_path, published, status):
    client = FakeClient()
    report = info.run_once(tmp_path, now=NOW, fixture_articles=[article(published=published)],
                          gemini=True, model="fixture", gemini_client=client)
    assert report["articles"][0]["quality"]["status"] == status
    assert report["status"] == "information_unavailable" and not client.calls


@pytest.mark.parametrize("bad", [0, -1, float("nan"), float("inf"), True, None])
def test_max_age_must_be_explicit_finite_positive(bad):
    with pytest.raises(ValueError): info.InformationConfig(max_age_sec=bad)
    with pytest.raises(ValueError): info.article_quality({}, NOW, bad)


def test_nonfinite_observation_time_and_metadata_are_rejected(tmp_path):
    with pytest.raises(ValueError): info.run_once(tmp_path, now=float("nan"), fixture_articles=[])
    with pytest.raises(ValueError): info.normalize_articles([article(observed_at=float("inf"))], NOW)
    with pytest.raises(ValueError): info.article_quality({}, float("nan"), 3600)


def test_gemini_one_call_per_distinct_article_content_model_and_prompt(tmp_path):
    client = FakeClient()
    first = info.run_once(tmp_path, now=NOW, fixture_articles=[article()], gemini=True,
                          model="fixture-a", gemini_client=client)
    second = info.run_once(tmp_path, now=NOW + 1000, fixture_articles=[article()], gemini=True,
                           model="fixture-a", gemini_client=client)
    assert len(client.calls) == 1 and second["ai"]["cache_hit"]
    assert first["ai"]["created_at"] == second["ai"]["created_at"]
    assert first["ai"]["input_hash"] == second["ai"]["input_hash"]
    assert first["ai"]["model"] == "fixture-a" and first["ai"]["prompt_version"] == info.PROMPT_VERSION
    info.run_once(tmp_path, now=NOW + 1100, fixture_articles=[article("new")], gemini=True,
                  model="fixture-a", gemini_client=client)
    info.run_once(tmp_path, now=NOW + 1200, fixture_articles=[article("new")], gemini=True,
                  model="fixture-b", gemini_client=client)
    assert len(client.calls) == 3
    assert set(json.loads(client.calls[0]["prompt"])["source_articles"][0]) == {
        "id", "headline", "url", "source", "published_at", "first_seen"}


@pytest.mark.parametrize("field", ["action", "direction", "quantity", "qty", "leverage", "target_weight", "score", "orders"])
def test_provider_extra_trading_fields_are_rejected(field):
    invalid = {**valid_digest(["id"]), field: "BUY"}
    with pytest.raises(ValueError, match="schema"):
        info.validate_digest(invalid, {"id"})


def test_provider_unknown_sources_duplicate_keys_and_nonfinite_json_are_rejected():
    with pytest.raises(ValueError): info.validate_digest(valid_digest(["unprovided"]), {"id"})
    with pytest.raises(ValueError): info.validate_digest('{"summary":"a","summary":"b"}', {"id"})
    with pytest.raises(ValueError): info.validate_digest(json.dumps({**valid_digest(["id"]), "summary": float("nan")}), {"id"})
    with pytest.raises(ValueError): info.validate_digest({**valid_digest(["id"]), "facts": [float("inf")]}, {"id"})


def test_provider_errors_are_sanitized_and_not_retried_on_identical_inputs(tmp_path, capsys):
    calls = []
    def fail(**kwargs):
        calls.append(kwargs)
        raise RuntimeError("https://example/?key=SECRET_API_TOKEN")
    for offset in (0, 1000):
        report = info.run_once(tmp_path, now=NOW + offset, fixture_articles=[article()],
                              gemini=True, model="fixture", gemini_client=fail)
        assert report["ai"]["status"] == "unavailable"
    assert len(calls) == 1
    for path in tmp_path.rglob("*.json"):
        assert "SECRET_API_TOKEN" not in path.read_text(encoding="utf-8")
    assert not capsys.readouterr().out and not capsys.readouterr().err


def test_fixtures_are_not_reused_as_public_feed_cache(tmp_path):
    info.run_once(tmp_path, now=NOW, fixture_articles=[article("fiction")])
    calls = []
    def fetch(**_):
        calls.append(True)
        return rss("real")
    report = info.run_once(tmp_path, now=NOW + 1, feed_fetcher=fetch)
    assert calls and report["input_kind"] == "public_rss"
    assert "real.htm" in report["articles"][0]["url"]


def test_missing_gemini_client_is_reported_without_disabling_public_information(tmp_path):
    report = info.run_once(tmp_path, now=NOW, fixture_articles=[article()], gemini=True, model="fixture")
    assert report["status"] == "ok"
    assert report["ai"]["error_code"] == "client_unavailable"
    assert report["ai"]["digest"] is None


def test_cli_default_never_reads_gemini_key_or_imports_legacy_configuration(tmp_path, monkeypatch, capsys):
    fixture = tmp_path / "input.json"
    fixture.write_text(json.dumps([article(published=info.time.time() - 60)]), encoding="utf-8")
    original = info.os.environ.get
    def guarded(key, *args):
        if key == "GEMINI_API_KEY": raise AssertionError("Default must not read key")
        return original(key, *args)
    monkeypatch.setattr(info.os.environ, "get", guarded)
    assert info.main(["--once", "--state-dir", str(tmp_path / "out"), "--fixture", str(fixture)]) == 0
    assert json.loads(capsys.readouterr().out)["ai_status"] == "disabled"


def test_explicit_gemini_cli_works_without_key(tmp_path, monkeypatch, capsys):
    fixture = tmp_path / "input.json"
    fixture.write_text(json.dumps([article(published=info.time.time() - 60)]), encoding="utf-8")
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    assert info.main(["--once", "--state-dir", str(tmp_path / "out"), "--fixture", str(fixture),
                      "--gemini", "--model", "fixture-model"]) == 0
    assert json.loads(capsys.readouterr().out)["ai_status"] == "unavailable"


def test_optional_sdk_adapter_passes_only_information_schema_without_tools(monkeypatch):
    seen = {}
    class Client:
        def __init__(self, **kwargs):
            seen["client"] = kwargs
            self.models = self
        def generate_content(self, **kwargs):
            seen["request"] = kwargs
            return SimpleNamespace(text='{"information":"fixture"}')
        def close(self): seen["closed"] = True
    fake_genai = SimpleNamespace(Client=Client)
    monkeypatch.setitem(sys.modules, "google", SimpleNamespace(genai=fake_genai))
    adapter = info.GeminiInformationClient("unit-test-placeholder")
    adapter(prompt="public headline", schema=info.OUTPUT_SCHEMA, model="explicit-model")
    assert seen["client"]["http_options"]["timeout"] == 15000
    assert seen["client"]["http_options"]["retry_options"] == {"attempts": 1}
    assert seen["request"]["model"] == "explicit-model"
    assert seen["request"]["config"]["max_output_tokens"] == 2048
    assert "tools" not in seen["request"]["config"]
    assert "unit-test-placeholder" not in seen["request"]["contents"]
    assert seen["closed"]


def test_no_execution_imports_or_callbacks_and_no_root_env_loading():
    source = Path(info.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    imports = [node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)]
    assert not any(name and (name.startswith("binance_coinm_v1") or name in {
        "config", "engine", "forward", "main", "multi_agent", "news_feed"}) for name in imports)
    assert "load_dotenv" not in source
    assert not {"order", "position", "direction", "leverage", "quantity", "action"} & set(inspect.signature(info.run_once).parameters)
    assert set(info.OUTPUT_SCHEMA["properties"]) == {
        "summary", "event_type", "affected_assets", "facts", "uncertainties", "source_ids"}
