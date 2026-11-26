import asyncio
import json

import httpx

from arrbot import arrhealth
from arrbot.arr import Sonarr
from arrbot.config import ArrConfig


class FakeSonarr:
    def __init__(self):
        self.health = [{"source": "IndexerStatusCheck", "type": "warning",
                        "message": "Indexers unavailable due to failures: scenenzbs", "wikiUrl": "https://wiki/x"},
                       {"source": "UpdateCheck", "type": "ok", "message": ""}]
        self.indexers = [{"id": 1, "name": "scenenzbs", "enableAutomaticSearch": True, "implementation": "Newznab",
                          "fields": [{"name": "apiKey", "value": "********"},
                                     {"name": "categories", "value": [5000, 5100]}]}]
        self.options = [{"value": 5030, "name": "SD", "parentValue": 5000},
                        {"value": 5040, "name": "HD", "parentValue": 5000},
                        {"value": 5140, "name": "HD", "parentValue": 5100},
                        {"value": 2040, "name": "HD", "parentValue": 2000}]
        self.puts, self.tests = [], 0
        self.heal_on_test = True

    def handler(self, req):
        p = req.url.path.removeprefix("/api/v3")
        body = json.loads(req.content) if req.content else None
        if p == "/health":
            return httpx.Response(200, json=self.health)
        if p == "/indexer" and req.method == "GET":
            return httpx.Response(200, json=self.indexers)
        if p == "/indexer/1" and req.method == "GET":
            return httpx.Response(200, json=json.loads(json.dumps(self.indexers[0])))
        if p == "/indexer/1" and req.method == "PUT":
            self.puts.append(body)
            self.indexers[0] = body
            return httpx.Response(202, json=body)
        if p == "/indexer/action/newznabCategories":
            if self.options is None:
                return httpx.Response(400, json={"message": "unknown action"})
            return httpx.Response(200, json={"options": self.options})
        if p == "/indexer/testall":
            self.tests += 1
            if self.heal_on_test:
                self.health = [h for h in self.health if h["source"] != "IndexerStatusCheck"]
            return httpx.Response(200, json=[{"id": 1, "isValid": True, "validationFailures": []}])
        if p == "/command" and req.method == "POST":
            return httpx.Response(201, json={"id": 5})
        if p == "/command/5":
            return httpx.Response(200, json={"id": 5, "status": "completed"})
        return httpx.Response(404, text=f"unhandled {req.method} {p}")


def sonarr_for(fake):
    return Sonarr(ArrConfig("http://s", "k"), transport=httpx.MockTransport(fake.handler))


def test_issues_include_own_category_check():
    fake = FakeSonarr()
    found = asyncio.run(arrhealth.issues(sonarr_for(fake)))
    assert [i.source for i in found] == ["IndexerStatusCheck", "IndexerCategories"]  # "ok" left out
    assert "scenenzbs" in found[1].message and found[1].indexer_id == 1
    assert "vorübergehend gesperrt" in found[0].text() and "https://wiki/x" in found[0].text()


def test_auto_fix_tests_indexers_and_rechecks():
    fake = FakeSonarr()
    svc = sonarr_for(fake)
    issue = asyncio.run(arrhealth.issues(svc))[0]
    text = asyncio.run(arrhealth.auto_fix(svc, issue, wait=5))
    assert fake.tests == 1 and "1 von 1 ok" in text and "Danach ist die Warnung weg" in text

    fake2 = FakeSonarr()
    fake2.heal_on_test = False
    svc2 = sonarr_for(fake2)
    issue2 = asyncio.run(arrhealth.issues(svc2))[0]
    assert "noch da" in asyncio.run(arrhealth.auto_fix(svc2, issue2, wait=5))
    root = arrhealth.Issue("Sonarr", "RootFolderCheck", "error", "Missing root folder: /tv")
    assert asyncio.run(arrhealth.auto_fix(svc2, root)) is None  # nothing safe to do


def test_add_subcategories_from_indexer_list_and_fallback():
    fake = FakeSonarr()
    text = asyncio.run(arrhealth.add_subcategories(sonarr_for(fake), 1))
    cats = next(f["value"] for f in fake.puts[0]["fields"] if f["name"] == "categories")
    assert cats == [5000, 5030, 5040, 5100, 5140] and "laut Indexer" in text
    assert fake.puts[0]["fields"][0]["value"] == "********"  # API key untouched (Sonarr keeps the real one)

    fake = FakeSonarr()
    fake.options = None  # older Sonarr without the action: standard sub categories
    text = asyncio.run(arrhealth.add_subcategories(sonarr_for(fake), 1))
    cats = next(f["value"] for f in fake.puts[0]["fields"] if f["name"] == "categories")
    assert 5030 in cats and 5142 in cats and "Standard-Unterkategorien" in text


def test_health_watch_reports_new_once_and_resolved(tmp_path):
    fake = FakeSonarr()
    svc = sonarr_for(fake)
    hw = arrhealth.HealthWatch(str(tmp_path))
    new, gone = asyncio.run(hw.poll([svc]))
    assert len(new) == 2 and gone == []
    new, gone = asyncio.run(hw.poll([svc]))
    assert new == [] and gone == []  # nothing twice
    assert len(arrhealth.HealthWatch(str(tmp_path)).known) == 2  # survives a restart
    fake.health = []
    new, gone = asyncio.run(hw.poll([svc]))
    assert new == [] and [g["source"] for g in gone] == ["IndexerStatusCheck"]
