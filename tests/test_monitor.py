"""Service monitoring and version check (offline)."""

import asyncio

import httpx

from arrbot import health, version


def test_monitor_alerts_after_two_failures_and_on_recovery():
    m = health.Monitor()
    bad = [health.Check("Radarr", False, "nicht erreichbar"), health.Check("TMDB", True, "ok")]
    assert m.update(bad, now=0) == []  # one hiccup stays quiet
    assert m.update(bad, now=300) == ["🚨 Radarr ist nicht erreichbar: nicht erreichbar"]
    assert m.update(bad, now=600) == []  # told once
    assert "Radarr weg seit" in m.status()
    assert m.update([health.Check("Radarr", True, "ok")], now=300 + 3 * 3600) == [
        "✅ Radarr läuft wieder (war 3 Std. 0 Min. weg)."]
    assert m.status() == "🛡 Überwachung: alle Dienste laufen"


def test_version_compare_and_github_check(tmp_path):
    assert version.newer("1.10.0", "1.9.3") and not version.newer("1.7.0", "1.7.0")

    def handler(req):
        assert req.url.path == "/me/bot/main/arrbot/version.py"
        return httpx.Response(200, text='__version__ = "9.0.0"\n')

    latest = asyncio.run(version.latest("me/bot", transport=httpx.MockTransport(handler)))
    assert latest == "9.0.0"
    assert "neu verfügbar: 9.0.0" in version.line(latest)
    assert version.line(version.__version__).endswith("aktuell ✅")
    assert asyncio.run(version.latest("")) is None
    assert version.first_start_of_version(str(tmp_path)) == "neu"
    assert version.first_start_of_version(str(tmp_path)) is None
    (tmp_path / "version.txt").write_text("1.0.0")
    assert version.first_start_of_version(str(tmp_path)) == "1.0.0"


def test_private_repo_uses_token():
    def handler(req):
        assert req.url.host == "api.github.com" and req.headers["Authorization"] == "Bearer t"
        assert req.url.path == "/repos/me/bot/contents/arrbot/version.py"
        return httpx.Response(200, text='__version__ = "2.0.0"')

    assert asyncio.run(version.latest("me/bot", transport=httpx.MockTransport(handler), token="t")) == "2.0.0"
