"""Social publishing: content rules, account matching, the review queue and YouTube's resumable upload."""
import asyncio
import json
import os
import tempfile
from pathlib import Path
from urllib.parse import urlparse

os.environ.setdefault("AUTOSTUDIO_HOME", tempfile.mkdtemp(prefix="autostudio_test_"))

import httpx  # noqa: E402
import pytest  # noqa: E402

from autostudio import social, state  # noqa: E402
from autostudio.social import youtube as yt  # noqa: E402
from autostudio.tools.registry import ToolContext  # noqa: E402
from autostudio.tools.social import social_publish  # noqa: E402


@pytest.fixture()
def media(tmp_path: Path) -> dict[str, str]:
    files = {"video": tmp_path / "clip.mp4", "img1": tmp_path / "a.jpg", "img2": tmp_path / "b.png"}
    for p in files.values():
        p.write_bytes(b"x" * 1024)
    return {k: str(v) for k, v in files.items()}


@pytest.fixture()
def accounts():
    for a in state.db.list_accounts():
        state.db.delete_account(a["id"])
    made = [state.db.create_account("xiaohongshu", "美食号"), state.db.create_account("xiaohongshu", "旅行号"),
            state.db.create_account("youtube", "My Channel", {"channel_id": "UC1"}, {"refresh_token": "r"})]
    yield made
    for a in made:
        state.db.delete_account(a["id"])


def test_prepare_rules(media):
    w: list[str] = []
    c = social.prepare("xiaohongshu", {"video": media["video"], "title": "一" * 30, "tags": ["#美食", " 探店 ", ""]}, w)
    assert len(c["title"]) == 20 and c["tags"] == ["美食", "探店"] and w
    c = social.prepare("xiaohongshu", {"images": [media["img1"], media["img2"]], "title": "图文"}, [])
    assert len(c["images"]) == 2 and c["video"] is None
    with pytest.raises(ValueError, match="二选一"):
        social.prepare("xiaohongshu", {"video": media["video"], "images": [media["img1"]], "title": "x"}, [])
    with pytest.raises(ValueError, match="只能发布视频"):
        social.prepare("youtube", {"images": [media["img1"]], "title": "x"}, [])
    c = social.prepare("youtube", {"video": media["video"]}, [])
    assert c["title"] == "clip" and c["privacy"] == "public"
    with pytest.raises(ValueError, match="不存在"):
        social.prepare("youtube", {"video": media["video"] + ".missing"}, [])


def test_resolve_accounts(accounts):
    xhs1, xhs2, ytc = accounts
    assert [a["id"] for a in social.resolve_accounts(["小红书"])] == [xhs1["id"], xhs2["id"]]
    assert [a["id"] for a in social.resolve_accounts(["旅行", "YouTube"])] == [xhs2["id"], ytc["id"]]
    assert len(social.resolve_accounts(["全部"])) == 3
    assert social.resolve_accounts([ytc["id"]])[0]["name"] == "My Channel"
    with pytest.raises(ValueError, match="找不到"):
        social.resolve_accounts(["抖音号"])


def test_fit_tags():
    assert yt.fit_tags(["#shorts", "a,b", "<x>"]) == ["shorts", "a b", "＜x＞"]
    assert sum(len(t) + 1 for t in yt.fit_tags(["t" * 100] * 10)) <= yt.TAGS_MAX_CHARS + 1


def test_publish_tool_queues_for_review(accounts, media, tmp_path):
    session = state.db.create_session("t")

    async def emit(_ev):
        return None

    ctx = ToolContext(session_id=session["id"], workspace=tmp_path, emit=emit, publish_mode="queue")
    out = asyncio.run(social_publish(ctx, accounts=["小红书", "youtube"], video=media["video"], title="今天吃什么",
                                     tags=["美食"], per_platform={"youtube": {"title": "What to eat today"}}))
    assert out["ok"] and len(out["results"]) == 3 and {r["status"] for r in out["results"]} == {"待确认"}
    posts = state.db.list_posts("pending", session_id=session["id"])
    assert {p["content"]["title"] for p in posts} == {"今天吃什么", "What to eat today"}


class FakeYouTube:
    """Just enough of Google's resumable upload protocol, including one transient 503."""

    def __init__(self, size: int):
        self.size, self.got, self.failed_once, self.body = size, 0, False, None

    def __call__(self, req: httpx.Request) -> httpx.Response:
        if req.method == "POST" and "/videos" in req.url.path:
            self.body = json.loads(req.content)
            assert req.headers["X-Upload-Content-Length"] == str(self.size)
            return httpx.Response(200, headers={"Location": "https://upload.example/session1"})
        assert req.method == "PUT" and str(req.url) == "https://upload.example/session1"
        cr = req.headers["Content-Range"]
        if cr.startswith("bytes */"):
            return httpx.Response(308, headers={"Range": f"bytes=0-{self.got - 1}"} if self.got else {})
        start, end = (int(x) for x in cr.split(" ")[1].split("/")[0].split("-"))
        assert start == self.got
        if not self.failed_once and self.got > 0:
            self.failed_once = True
            return httpx.Response(503)
        self.got = end + 1
        if self.got >= self.size:
            return httpx.Response(200, json={"id": "vid123", "status": {"privacyStatus": "private"}})
        return httpx.Response(308, headers={"Range": f"bytes=0-{end}"})


def test_youtube_resumable_upload(monkeypatch, tmp_path):
    video = tmp_path / "v.mp4"
    video.write_bytes(os.urandom(256 * 1024 * 3 + 1000))
    fake = FakeYouTube(video.stat().st_size)
    real_client = httpx.AsyncClient
    monkeypatch.setattr(yt, "CHUNK", 256 * 1024)
    monkeypatch.setattr(yt.httpx, "AsyncClient", lambda **kw: real_client(transport=httpx.MockTransport(fake), **kw))
    real_sleep = asyncio.sleep
    monkeypatch.setattr(yt.asyncio, "sleep", lambda _s: real_sleep(0))
    seen: list[float] = []

    async def token() -> str:
        return "tok"

    async def progress(f: float, _t: str) -> None:
        seen.append(f)

    res = asyncio.run(yt.upload(token, {"video": str(video), "title": "<hi>", "tags": ["#a"], "privacy": "public"}, progress))
    assert res["url"] == "https://youtu.be/vid123" and fake.got == fake.size and fake.failed_once
    assert fake.body["snippet"]["title"] == "＜hi＞" and fake.body["snippet"]["tags"] == ["a"]
    assert res["warnings"] and "私享" in res["warnings"][0]   # requested public, came back private (unaudited project)
    assert seen == sorted(seen)


def test_task_auto_publish_column():
    t = state.db.create_task(name="n", prompt="p", cron="0 9 * * *", auto_publish=True)
    assert state.db.get_task(t["id"])["auto_publish"] == 1
    state.db.update_task(t["id"], auto_publish=False)
    assert state.db.get_task(t["id"])["auto_publish"] == 0
    state.db.delete_task(t["id"])


# ---------------------------------------------------------------------------
# 小红书 login: the real polling loop in a headless browser, against a stand-in creator site

REDNOTE = "https://creator.rednote.com"
LOGIN_PAGE = """<html><body>短信登录<script>
  setTimeout(() => fetch('/__signed_in').then(() => { %s }), 1500);
</script></body></html>"""


@pytest.fixture()
def fake_creator(monkeypatch: pytest.MonkeyPatch):
    from autostudio.social import browser, xiaohongshu as xhs

    # overseas: the account was registered abroad, so once signed in the platform moves it to creator.rednote.com
    # (where its session lives) and that page reloads itself once
    site = {"signed": False, "after": "", "overseas": False}
    real_launch = browser.launch

    def handle(route) -> None:
        u = urlparse(route.request.url)
        here = f"{u.scheme}://{u.netloc}"
        home = REDNOTE if site["overseas"] else xhs.CREATOR
        if u.path == "/__signed_in":
            site["signed"] = True
            return route.fulfill(body="ok")
        if u.path.startswith("/login"):
            return route.fulfill(content_type="text/html", body=LOGIN_PAGE % site["after"])
        if site["signed"] and here != home:
            if u.path.startswith("/api/"):
                return route.fulfill(status=401, json={"code": -1, "msg": "登录已过期"})
            return route.fulfill(status=302, headers={"location": home + u.path + (f"?{u.query}" if u.query else "")})
        if u.path == "/api/galaxy/user/info":
            if site["signed"]:
                return route.fulfill(json={"data": {"userName": "测试号", "userId": "u1", "redId": "42"}})
            return route.fulfill(status=401, json={"code": -1, "msg": "登录已过期"})
        if not site["signed"]:
            return route.fulfill(status=302, headers={"location": here + "/login?redirectReason=401"})
        reload_once = "<script>if(!sessionStorage.r){sessionStorage.r=1;setTimeout(()=>location.reload(),300)}</script>"
        return route.fulfill(content_type="text/html", body=f"<html><body>首页{reload_once}</body></html>")

    def launch(p, profile_dir, headless):
        try:
            ctx = real_launch(p, profile_dir, True)
        except Exception as e:  # noqa: BLE001
            pytest.skip(f"no browser to drive: {e}")
        ctx.route("https://creator.xiaohongshu.com/**", handle)
        ctx.route(f"{REDNOTE}/**", handle)
        return ctx

    monkeypatch.setattr(browser, "launch", launch)
    return site


@pytest.mark.parametrize("after", ["location.href = '/new/home'", ""], ids=["site-navigates", "url-stays-on-login"])
def test_xhs_login_is_detected(fake_creator, tmp_path: Path, after: str) -> None:
    pytest.importorskip("playwright.sync_api")
    from autostudio.social import xiaohongshu as xhs

    fake_creator["after"] = after
    steps: list[str] = []
    prof = xhs.login(tmp_path / "profile", timeout=40, progress=lambda f, t: steps.append(t))
    assert prof["nickname"] == "测试号" and prof["user_id"] == "u1"
    assert prof["site"] == "https://creator.xiaohongshu.com"
    assert any("已登录" in s for s in steps)


def test_xhs_overseas_account_follows_to_rednote(fake_creator, tmp_path: Path) -> None:
    pytest.importorskip("playwright.sync_api")
    from autostudio.social import browser, xiaohongshu as xhs

    fake_creator.update(overseas=True, after="location.href = '/new/home'")
    prof = xhs.login(tmp_path / "profile", timeout=40)
    assert prof["nickname"] == "测试号" and prof["site"] == REDNOTE        # not "登录窗口已关闭"
    assert xhs.check(tmp_path / "p2")["site"] == REDNOTE                   # an account saved before this knew no site
    assert xhs.check(tmp_path / "p2", REDNOTE)["site"] == REDNOTE
    landed = browser.run(lambda _ctx, page: xhs._open(page, xhs.CREATOR, xhs.PUBLISH_PATH.format(target="video")),
                         tmp_path / "p3", headless=True)
    assert landed == REDNOTE


def test_xhs_login_notices_the_window_going_away(fake_creator, tmp_path: Path) -> None:
    pytest.importorskip("playwright.sync_api")
    from autostudio.social import xiaohongshu as xhs

    fake_creator["after"] = "location.href = 'about:blank'"
    fake_creator["signed"] = False
    with pytest.raises(xhs.LoginCancelled, match="窗口已关闭"):     # not 登录超时
        xhs.login(tmp_path / "profile", timeout=40)
