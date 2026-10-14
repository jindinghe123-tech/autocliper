"""Asset search: provider parsing (mocked HTTP), license rules, filtering and rendition choice."""
import asyncio

import httpx
import pytest

from autostudio.media import stock

PEXELS_VIDEOS = {"videos": [
    {"id": 1, "width": 1080, "height": 1920, "duration": 12, "url": "https://www.pexels.com/video/waves-on-beach-1/",
     "image": "https://images.pexels.com/1.jpg", "user": {"name": "Ann"},
     "video_files": [{"quality": "uhd", "file_type": "video/mp4", "width": 2160, "height": 3840, "link": "https://v/4k.mp4"},
                     {"quality": "hd", "file_type": "video/mp4", "width": 1080, "height": 1920, "link": "https://v/1080.mp4"},
                     {"quality": "sd", "file_type": "video/mp4", "width": 540, "height": 960, "link": "https://v/540.mp4"}]},
    {"id": 2, "width": 1920, "height": 1080, "duration": 3, "url": "https://www.pexels.com/video/x-2/", "image": "t",
     "user": {"name": "Bo"}, "video_files": [{"file_type": "video/mp4", "width": 1920, "height": 1080, "link": "https://v/b.mp4"}]},
]}
PIXABAY_VIDEOS = {"hits": [{"id": 9, "pageURL": "https://pixabay.com/videos/9/", "tags": "sea, waves", "duration": 20, "user": "pix",
                            "videos": {"large": {"url": "", "width": 0, "height": 0},
                                       "medium": {"url": "https://p/720.mp4", "width": 1280, "height": 720, "thumbnail": "https://p/t.jpg"},
                                       "small": {"url": "https://p/540.mp4", "width": 960, "height": 540}}}]}
FREESOUND = {"results": [{"id": 5, "name": "whoosh", "tags": ["air"], "duration": 1.2, "username": "u",
                          "license": "http://creativecommons.org/publicdomain/zero/1.0/", "url": "https://freesound.org/s/5/",
                          "previews": {"preview-hq-mp3": "https://cdn.freesound.org/p/5-hq.mp3"}},
                         {"id": 6, "name": "nc", "duration": 2, "license": "http://creativecommons.org/licenses/by-nc/4.0/",
                          "previews": {"preview-hq-mp3": "https://x"}}]}
JAMENDO = {"headers": {"status": "success"}, "results": [
    {"id": "7", "name": "Sunny", "duration": 150, "artist_name": "A", "audiodownload": "https://j/7.mp3", "audiodownload_allowed": True,
     "license_ccurl": "http://creativecommons.org/licenses/by-sa/3.0/", "shareurl": "https://jamen.do/t/7",
     "musicinfo": {"tags": {"genres": ["pop"], "vartags": ["happy"]}}},
    {"id": "8", "name": "Locked", "duration": 100, "audiodownload": "", "audiodownload_allowed": False,
     "license_ccurl": "http://creativecommons.org/licenses/by/3.0/"},
    {"id": "9", "name": "NC", "duration": 100, "audiodownload": "https://j/9.mp3", "audiodownload_allowed": True,
     "license_ccurl": "http://creativecommons.org/licenses/by-nc-sa/3.0/"}]}


@pytest.fixture()
def fake_http(monkeypatch):
    seen: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        u = str(req.url)
        if "api.pexels.com/videos" in u:
            assert req.headers["Authorization"] == "PK"
            return httpx.Response(200, json=PEXELS_VIDEOS)
        if "pixabay.com/api/videos" in u:
            return httpx.Response(200, json=PIXABAY_VIDEOS)
        if "freesound.org" in u:
            return httpx.Response(200, json=FREESOUND)
        if "jamendo.com" in u:
            return httpx.Response(200, json=JAMENDO)
        if "wikimedia.org" in u:
            return httpx.Response(200, json={"query": {"pages": {}}})
        if "openverse" in u:
            return httpx.Response(200, json={"results": []})
        return httpx.Response(404)

    real = httpx.AsyncClient
    monkeypatch.setattr(stock, "client", lambda cfg, timeout=25: real(transport=httpx.MockTransport(handler)))
    return seen


CFG = {"pexels_key": "PK", "pixabay_key": "XK", "freesound_key": "FK", "jamendo_client_id": "JC"}


def test_video_search_merges_and_filters(fake_http):
    items, warnings = asyncio.run(stock.search(CFG, "video", "waves", limit=8, orientation="portrait", min_duration=5))
    assert warnings == []
    # only the 12 s portrait Pexels clip survives (Pixabay's is landscape, the other Pexels one is 3 s)
    assert [i["id"] for i in items] == ["pexels:v1"]
    it = items[0]
    assert it["title"] == "waves on beach" and it["license"] == "Pexels License" and not it["attribution"]
    assert stock.pick_file(it, 1080)["url"] == "https://v/1080.mp4"
    assert stock.pick_file(it, 720)["url"] == "https://v/540.mp4"
    assert stock.pick_file(it, 4000)["url"] == "https://v/4k.mp4"
    assert stock.lookup("pexels:v1") is it


def test_pixabay_skips_empty_renditions(fake_http):
    items, _ = asyncio.run(stock.search(CFG, "video", "sea", sources=["pixabay"]))
    assert items[0]["width"] == 1280 and items[0]["thumb"] == "https://p/t.jpg"
    assert stock.pick_file(items[0], 1080)["url"] == "https://p/720.mp4"


def test_sfx_and_music_respect_licenses(fake_http):
    sfx, _ = asyncio.run(stock.search(CFG, "sfx", "whoosh", sources=["freesound"]))
    assert [i["id"] for i in sfx] == ["freesound:5"] and sfx[0]["license"].startswith("CC0")
    flt = [r for r in fake_http if "freesound" in str(r.url)][0].url.params["filter"]
    assert "Creative Commons 0" in flt and "duration:[0 TO 30]" in flt

    music, _ = asyncio.run(stock.search(CFG, "music", "happy", sources=["jamendo"]))
    assert [i["title"] for i in music] == ["Sunny"]           # download-locked and NC tracks are dropped
    assert music[0]["attribution"] and music[0]["tags"] == ["pop", "happy"]
    music_nc, _ = asyncio.run(stock.search(CFG, "music", "happy", sources=["jamendo"], commercial=False))
    assert [i["title"] for i in music_nc] == ["Sunny", "NC"]


def test_keyless_by_default_and_errors_become_warnings(monkeypatch):
    assert stock.available({}, "video") == ["wikimedia"]
    assert stock.available({}, "music") == ["openverse"]
    assert stock.available(CFG, "sfx") == ["freesound", "openverse"]

    real = httpx.AsyncClient
    monkeypatch.setattr(stock, "client", lambda cfg, timeout=25: real(transport=httpx.MockTransport(
        lambda req: httpx.Response(401, json={"detail": "Invalid token"}) if req.url.host == "freesound.org"
        else httpx.Response(200, json={"results": []}))))
    items, warnings = asyncio.run(stock.search(CFG, "sfx", "click"))
    assert items == [] and warnings == ["Freesound：API key 无效"]


@pytest.mark.parametrize("code,expected", [
    ("cc0", ("CC0 / 公有领域", True, True, False)),
    ("by-nc-sa", ("CC BY-NC-SA", False, True, True)),
    ("http://creativecommons.org/licenses/by-nd/4.0/", ("CC BY-ND 4.0", True, False, True)),
])
def test_cc_info(code, expected):
    i = stock.cc_info(code)
    assert (i["license"], i["commercial"], i["modify"], i["attribution"]) == expected


def test_url_ext_and_credit():
    assert stock.url_ext("https://upload.wikimedia.org/a/b/Clip.ogv?utm_source=x&a=b.c") == "ogv"
    assert stock.url_ext("https://prod-1.storage.jamendo.com/?trackid=1&format=mp32", "mp3") == "mp3"
    line = stock.credit_line({"title": "T", "author": "A", "license": "CC BY 4.0", "source": "Openverse",
                              "source_site": "freesound", "page": "https://f/1"}, "x.mp3")
    assert line == "《T》 by A · CC BY 4.0 · Openverse（Freesound） https://f/1"


def test_trending_risk_flags():
    from autostudio.tools.research import _flag

    assert _flag("老人被气枪击中身亡") == "含「身亡」"
    assert _flag("机器狗进哀牢山拍到冥界之花") is None
    assert _flag("某地新闻", ["taiwan"]) == "分类：taiwan"
