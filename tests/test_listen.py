"""Wrongly tagged German tracks and 🎧 listening in (offline, no ffmpeg, no OpenAI)."""

import asyncio

import httpx

from arrbot import german, listen, manage


def ep_file(i, audio="eng", series=4):
    return {"id": i, "seriesId": series, "path": f"/tv/Dinos/S01E{i:02d}.mkv",
            "languages": [{"name": "German"}], "mediaInfo": {"audioLanguages": audio}}


def test_confirmed_title_and_file(tmp_path):
    c = german.confirmed
    c.load(str(tmp_path))
    assert german.file_check(ep_file(1)) == "mislabelled"
    c.add_file("s", 1)
    assert german.file_check(ep_file(1)) == "german" and german.file_check(ep_file(2)) == "mislabelled"
    c.add("s", 4)
    assert german.file_check(ep_file(2)) == "german"
    c.load(str(tmp_path))  # survives a restart
    assert german.file_check(ep_file(3)) == "german"
    c.remove("s", 4)
    assert german.file_check(ep_file(3)) == "mislabelled"
    c.keys.clear()


def test_plan_says_probably_only_mistagged():
    plan = manage.FixPlan(4, "Dinosaurs", checked=3)
    plan.mislabelled = plan.other_lang = [
        {"id": i, "seasonNumber": 1, "episodeNumber": i, "hasFile": True, "episodeFile": ep_file(i)} for i in (1, 2, 3)]
    assert manage.likely_mistagged(plan)
    text, buttons = manage.render_plan(plan)
    assert "oft ist nur die Markierung falsch" in text
    assert ("✅ Ist Deutsch (nur falsch markiert)", "ok") in buttons


def test_paths_and_speech_detection():
    lst = listen.Listener("k", "/tv=/media/tv; /tv/kids=/kids")
    assert lst.local("/tv/kids/a.mkv") == "/kids/a.mkv" and lst.local("/tv/b.mkv") == "/media/tv/b.mkv"
    assert lst.local("/movies/c.mkv") == "/movies/c.mkv"

    answers = iter([{"language": "german", "text": "Baby: Nicht die Mama! Nicht die Mama!"},
                    {"language": "english", "text": "♪"}])

    def handler(req):
        assert req.headers["Authorization"] == "Bearer k"
        return httpx.Response(200, json=next(answers))

    lst.transport = httpx.MockTransport(handler)
    assert asyncio.run(lst.language(b"mp3"))[0] == "German"
    assert asyncio.run(lst.language(b"mp3"))[0] is None  # music only: no verdict
    assert round(lst.spent * 100, 2) == 0.4


class FakeListener(listen.Listener):
    def __init__(self, spoken):
        super().__init__("k")
        self.spoken, self.asked = spoken, []

    async def hear(self, path):
        self.asked.append(path)
        self.spent += 0.002
        lang = self.spoken(path)
        return listen.Heard(path, [listen.Track(0, "eng", lang)])


def test_whole_series_decided_by_three_samples(tmp_path):
    german.confirmed.load(str(tmp_path))
    files = [ep_file(i) for i in range(1, 66)]
    fake = FakeListener(lambda p: "German")
    text, bad = asyncio.run(listen.hear_files(fake, "s", 4, files, "Dinosaurs", True))
    assert len(fake.asked) == 3 and not bad
    assert "3 Stichproben angehört – überall wird Deutsch gesprochen ✅" in text
    assert german.file_check(ep_file(40)) == "german"
    german.confirmed.keys.clear()


def test_mixed_series_finds_the_english_episodes(tmp_path):
    german.confirmed.load(str(tmp_path))
    files = [{**ep_file(i), "_eps": f"S01E{i:02d}"} for i in range(1, 7)]
    fake = FakeListener(lambda p: "English" if p.endswith(("02.mkv", "05.mkv")) else "German")
    text, bad = asyncio.run(listen.hear_files(fake, "s", 4, files, "Eine schrecklich nette Familie", True))
    assert len(fake.asked) == 6  # samples were mixed: every episode is checked
    assert [f["id"] for f in bad] == [2, 5]
    assert "✅ 4 wirklich Deutsch" in text and "🇬🇧 2 wirklich nicht Deutsch (English): S01E02, S01E05" in text
    assert german.file_check(files[0]) == "german" and german.file_check(files[1]) == "mislabelled"
    german.confirmed.keys.clear()


class SpotListener(listen.Listener):
    """Fake ffmpeg + speech recognition: answers per spot."""

    def __init__(self, by_spot):
        super().__init__("k")
        self.by_spot, self.spots = by_spot, []

    async def clip(self, path, track, start):
        return str(start).encode()

    async def language(self, audio):
        share = round(float(audio.decode()) / 1000, 2)
        self.spots.append(share)
        return self.by_spot.get(share), ""


def test_spots_skip_intro_and_credits_and_vote():
    two_agree = SpotListener({0.25: "German", 0.5: "German", 0.72: "English"})
    assert asyncio.run(two_agree._track_language("x", 0, 1000)) == "German"
    assert two_agree.spots == [0.25, 0.5]  # third clip not needed
    tie = SpotListener({0.25: "German", 0.5: "English", 0.72: "German"})  # e.g. an English song in between
    assert asyncio.run(tie._track_language("x", 0, 1000)) == "German" and tie.spots == [0.25, 0.5, 0.72]
    music = SpotListener({0.72: "English"})
    assert asyncio.run(music._track_language("x", 0, 1000)) == "English"
    assert asyncio.run(SpotListener({})._track_language("x", 0, 1000)) is None
    assert all(0.2 <= s <= 0.75 for s in listen.SPOTS)
