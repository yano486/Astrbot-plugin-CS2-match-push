"""
web_panel.py — Web 管理面板
与内嵌 pages/management/ 共用页面，提供基于 aiohttp 的轻量 WebPanel 服务器。

访问地址：http://<host>:<port>/
"""

import asyncio
from collections import deque
import hmac
import ipaddress
import json
from datetime import datetime, timezone, timedelta
from pathlib import Path

from aiohttp import web
from yarl import URL

from astrbot.api import logger

from .card_layout import CARD_TYPES, catalog, default_layout, validate_card_layout
from .config import ConfigError, SOURCE_DEFAULTS, integer, source_id, sources_configured, string, validate_config
from .data_sources import SourceRouter
from .delivery import DeliveryError

CST = timezone(timedelta(hours=8))


# Resolve resources from the plugin location, independent of AstrBot's cwd.
PANEL_DIR = Path(__file__).parent / "pages" / "management"



WEB_PANEL_LOGIN_HTML = r"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>CS2 比赛推送 · 登录</title>
<style>
:root{color-scheme:light dark;--bg:#f7f8fa;--surface:#fff;--text:#212121;--muted:#667085;--border:#e2e5e9;--primary:#1976d2}
@media(prefers-color-scheme:dark){:root{--bg:#1a1a1a;--surface:#242424;--text:#eee;--muted:#aaa;--border:#3b3b3b;--primary:#64b5f6}}
*{box-sizing:border-box}body{margin:0;min-height:100vh;display:grid;place-items:center;background:var(--bg);color:var(--text);font-family:system-ui,-apple-system,"Segoe UI",sans-serif}
form{width:min(420px,calc(100vw - 32px));padding:32px;background:var(--surface);border:1px solid var(--border);border-radius:20px}h1{font-size:22px;margin:0 0 12px}p{color:var(--muted);font-size:14px;line-height:1.7;margin:0 0 24px}label{display:block;font-size:13px;margin-bottom:8px}input{width:100%;padding:12px;border:1px solid var(--border);border-radius:8px;background:var(--surface);color:var(--text);font:inherit}input:focus-visible,button:focus-visible{outline:3px solid var(--primary);outline-offset:3px}button{width:100%;padding:12px;margin-top:20px;border:0;border-radius:8px;background:var(--primary);color:#fff;font:600 14px system-ui;cursor:pointer}.error{color:#d32f2f;font-size:13px;min-height:20px;margin-top:12px}.note{margin:20px 0 0;font-size:12px}
</style>
</head>
<body>
<form id="login-form">
<h1>CS2 比赛推送</h1><p>使用管理令牌进入独立面板。</p>
<label for="token">管理令牌</label><input id="token" type="password" autocomplete="off" required autofocus>
<button type="submit">进入管理面板</button><div class="error" id="error" role="alert"></div>
<p class="note">管理员私聊机器人发送 <code>/cs面板</code> 获取令牌。也可直接从 AstrBot 插件详情打开内嵌页面。</p>
</form>
<script>
document.getElementById('login-form').addEventListener('submit', event => {
 event.preventDefault();
 const token = document.getElementById('token').value.trim();
 if (!token) { document.getElementById('error').textContent = '请输入管理令牌'; return; }
 location.href = '/?token=' + encodeURIComponent(token);
});
</script>
</body></html>"""


# ─────────────────────────────────────────
# Web 面板服务器
# ─────────────────────────────────────────

class WebPanel:
    def __init__(self, plugin: "CSMatchPlugin"):
        self.plugin = plugin
        self.app = web.Application(middlewares=[self._auth_middleware, self._input_middleware])
        self._runner = None
        self._site   = None
        self._bind_host = None
        self._bind_port = None
        self._reconfigure_lock = asyncio.Lock()
        self._config_lock = asyncio.Lock()
        self._logs = deque(maxlen=500)
        self._setup_routes()

    @web.middleware
    async def _auth_middleware(self, req, handler):
        path = req.path
        if path == "/" or path.startswith("/api/"):
            token = self._request_token(req)
            token_ok = self._token_matches(token)
            if token_ok or self._is_local_request(req):
                if req.method not in {"GET", "HEAD", "OPTIONS"}:
                    origin = req.headers.get("Origin")
                    if origin and not self._same_origin(req, origin):
                        # Browser cookies are ambient credentials, including
                        # across localhost ports. Cross-origin writes need an
                        # explicitly supplied token rather than a session cookie.
                        auth = req.headers.get("Authorization", "")
                        explicit_token = auth[7:].strip() if auth.lower().startswith("bearer ") else req.rel_url.query.get("token", "")
                        if not self._token_matches(explicit_token):
                            return self._json({"ok": False, "msg": "cross-origin request denied"}, 403)
                resp = await handler(req)
                if token_ok:
                    resp.set_cookie(
                        "cs_panel_token",
                        token,
                        httponly=True,
                        samesite="Lax",
                        max_age=30 * 24 * 3600,
                    )
                return resp
            if path == "/":
                return web.Response(text=WEB_PANEL_LOGIN_HTML, content_type="text/html", charset="utf-8")
            return self._json({"ok": False, "msg": "unauthorized"}, 401)
        return await handler(req)

    @web.middleware
    async def _input_middleware(self, req, handler):
        try:
            return await handler(req)
        except ConfigError as exc:
            return self._json({"ok": False, "msg": str(exc)}, 400)

    async def _body(self, req, *, allow_empty=False) -> dict:
        if allow_empty and not req.can_read_body:
            return {}
        try:
            body = await req.json()
        except (ValueError, UnicodeDecodeError) as exc:
            raise ConfigError("invalid json") from exc
        if not isinstance(body, dict):
            raise ConfigError("request body must be a JSON object")
        return body

    def _request_token(self, req) -> str:
        auth = req.headers.get("Authorization", "")
        if auth.lower().startswith("bearer "):
            return auth[7:].strip()
        query_token = req.rel_url.query.get("token", "")
        if query_token:
            return query_token.strip()
        return (req.cookies.get("cs_panel_token") or "").strip()

    def _token_matches(self, token: str) -> bool:
        expected = str(self.plugin.store.get("web_panel_token") or "").strip()
        return bool(token and expected and hmac.compare_digest(token.encode(), expected.encode()))

    def _same_origin(self, req, origin: str) -> bool:
        try:
            return URL(origin).origin() == URL(f"{req.scheme}://{req.host}").origin()
        except ValueError:
            return False

    def _is_local_request(self, req) -> bool:
        remote = req.remote or ""
        if remote in {"localhost", "127.0.0.1", "::1"}:
            return True
        try:
            return ipaddress.ip_address(remote).is_loopback
        except ValueError:
            return False

    def _setup_routes(self):
        a = self.app.router
        a.add_get("/",                          self.index)
        a.add_get("/app.js",                    self.asset)
        a.add_get("/setup.js",                  self.asset)
        a.add_get("/source-ui.js",              self.asset)
        a.add_get("/style.css",                 self.asset)
        a.add_get("/api/config",                self.get_config)
        a.add_patch("/api/config",              self.patch_config)
        a.add_get("/api/config/export",         self.export_config)
        a.add_post("/api/config/import",        self.import_config)
        a.add_post("/api/setup/check",          self.check_setup)
        a.add_post("/api/sources/check",        self.check_sources)
        a.add_post("/api/sources/verify",       self.verify_source)
        a.add_get("/api/matches",               self.get_matches)
        a.add_post("/api/refresh",              self.refresh)
        a.add_post("/api/push_now",             self.push_now)
        a.add_post("/api/test",                 self.send_test)
        a.add_post("/api/groups",               self.add_group)
        a.add_delete("/api/groups/{gid}",       self.remove_group)
        a.add_get("/api/teams/search",          self.search_teams)   # 必须在 {name} 之前
        a.add_get("/api/teams/popular",         self.popular_teams)
        a.add_post("/api/teams",                self.add_team)
        a.add_post("/api/teams/unfollow",       self.remove_team)
        a.add_delete("/api/teams/{name}",       self.remove_team)    # 保留兼容
        a.add_get("/api/logs",                  self.get_logs)
        a.add_post("/api/notified/clear",       self.clear_notified)
        a.add_post("/api/tasks/rebuild",        self.rebuild_tasks)
        # 单场比赛：自定义提醒时间 & 手动重建
        a.add_patch("/api/matches/{mid}",       self.patch_match)
        a.add_post("/api/matches/{mid}/rebuild",self.rebuild_match)
        a.add_get("/api/card-layout",           self.get_card_layout)
        a.add_post("/api/card-layout",          self.save_card_layout)
        a.add_post("/api/card-layout/preview",  self.preview_card_layout)

    def _json(self, data: dict, status=200):
        return web.Response(
            text=json.dumps(data, ensure_ascii=False),
            content_type="application/json",
            status=status
        )

    async def index(self, req):
        html = (PANEL_DIR / "index.html").read_text(encoding="utf-8")
        token = req.rel_url.query.get("token", "").strip()
        if self._token_matches(token):
            bootstrap = (
                "<script>\n"
                "history.replaceState(null, '', location.pathname);\n"
                "</script>\n"
            )
            html = html.replace("</head>", bootstrap + "</head>", 1)
        return web.Response(text=html, content_type="text/html", charset="utf-8")

    async def asset(self, req):
        # Both paths are fixed routes, never an arbitrary filesystem path.
        return web.FileResponse(PANEL_DIR / req.path.lstrip("/"))

    async def get_config(self, req):
        data = self.plugin.store.export_all()
        data["followed_teams"] = self.plugin.client.followed_teams_for_panel()
        return self._json({"ok": True, "data": data,
                           "token_configured": bool(self.plugin.store.get("pandascore_token")),
                           "proxy_configured": bool(self.plugin.store.get("hltv_proxy_url")),
                           "source_configured": sources_configured(self.plugin._source_config()),
                           "source_status": self.plugin.client.status(),
                           "team_logos": getattr(getattr(self.plugin, "team_logos", None), "web_images", {}),
                           "team_logo_dark_names": getattr(getattr(self.plugin, "team_logos", None), "web_dark_names", []),
                           "web_token_configured": bool(self.plugin.store.get("web_panel_token")),
                           "embedded_available": bool(getattr(self.plugin, "embedded_available", False))})

    async def patch_config(self, req):
        return await self._apply_config(req, imported=False)

    async def export_config(self, req):
        data = self.plugin.store.export_all()
        return self._json({"ok": True, "data": data})

    async def import_config(self, req):
        return await self._apply_config(req, imported=True)

    async def _apply_config(self, req, *, imported: bool):
        body = validate_config(await self._body(req))
        async with self._config_lock:
            previous = {key: self.plugin.store.get(key) for key in body}
            changed = {key for key, value in body.items() if previous[key] != value}
            web_changed = changed & {"web_panel_host", "web_panel_port", "web_panel_enabled"}
            self.plugin.store.import_config(body)
            web_reconfigured = False
            try:
                if web_changed:
                    web_reconfigured = await self.plugin.apply_web_panel_config()
                if changed:
                    await self.plugin.reload_runtime_config()
            except Exception as exc:
                self.plugin.store.import_config(previous)
                rollback_ok = True
                try:
                    if web_changed:
                        await self.plugin.apply_web_panel_config()
                    await self.plugin.reload_runtime_config()
                except Exception as rollback_error:
                    rollback_ok = False
                    logger.error(f"[CS] 运行配置恢复失败: {type(rollback_error).__name__}")
                logger.error(f"[CS] 配置应用失败: {type(exc).__name__}")
                return self._json({"ok": False, "msg": "配置应用失败，已恢复保存值" if rollback_ok else
                                   "配置应用失败，保存值已恢复，但运行状态需要检查或重载插件"}, 500)
            filters = {
                "min_tiers", "blacklist_teams", "blacklist_leagues",
                "notify_all_followed", "followed_teams", "hltv_min_stars",
            }
            if changed & (filters | {"remind_minutes", "custom_remind_minutes"}):
                self.plugin.rebuild_all_match_tasks()
            elif imported or changed & {"pandascore_token", "fetch_ahead_days", *SOURCE_DEFAULTS}:
                self.plugin.schedule_refresh("web_import_config" if imported else "web_patch_config")
            if changed & (filters | {"tournament_announce_enabled", "tournament_announce_hours", *SOURCE_DEFAULTS}):
                self.plugin.rebuild_tournament_tasks()
        return self._json({"ok": True, "restart_required": False, "web_reconfigured": web_reconfigured})

    async def get_matches(self, req):
        return self._json({"ok": True, "matches": self.plugin.get_panel_matches(),
                           "source_status": self.plugin.client.status()})

    async def check_setup(self, req):
        """Check candidate source settings without changing saved configuration."""
        body = await self._body(req, allow_empty=True)
        candidate = validate_config(body)
        if not candidate.get("pandascore_token"):
            candidate.pop("pandascore_token", None)
        settings = {**self.plugin._source_config(), **candidate}
        client = SourceRouter(settings, self.plugin.store)
        try:
            statuses = await client.check_sources()
        finally:
            await client.close()
        enabled = [item for item in statuses.values() if item["enabled"]]
        ready = sum(item["state"] == "ready" for item in enabled)
        source = {"ok": bool(enabled) and ready == len(enabled), "message": "数据源连接正常" if ready == len(enabled)
                  else "部分数据源可用，请检查受阻来源" if ready else "数据源检查未通过，请查看各来源状态"}

        groups = self.plugin.store.get("push_groups")
        group_count = len(groups) if isinstance(groups, (list, tuple)) else 0
        group_ok = group_count > 0
        group_status = {
            "ok": group_ok,
            "count": group_count,
            "message": "已配置推送群。" if group_ok else "尚未配置推送群，请添加接收比赛通知的群。",
        }
        platform_status = {
            "ok": None,
            "message": "请在 AstrBot 确认机器人已连接，并且已加入接收群。",
        }
        return self._json({
            "ok": True,
            "source": source,
            "sources": statuses,
            "groups": group_status,
            "platform": platform_status,
        })

    async def check_sources(self, req):
        statuses = await self.plugin.client.check_sources()
        enabled = [item for item in statuses.values() if item["enabled"]]
        ready = sum(item["state"] == "ready" for item in enabled)
        return self._json({"ok": True, "sources": statuses,
                           "source": {"ok": bool(enabled) and ready == len(enabled), "message": "数据源连接正常" if ready == len(enabled)
                                      else "部分来源受阻" if ready else "数据源检查未通过"}})

    async def verify_source(self, req):
        if "hltv" not in self.plugin.client.enabled:
            return self._json({"ok": False, "msg": "请先在高级维护启用并保存 HLTV 数据源"}, 400)
        task = self.plugin._verification_task
        if task is not None and not task.done():
            return self._json({"ok": True, "started": True, "msg": "人工验证已经在等待，请在浏览器中完成"})
        self.plugin._verification_task = self.plugin._create_background_task(
            self.plugin.client.manual_verify(), "hltv_manual_verify")
        return self._json({"ok": True, "started": True,
                           "msg": "已启动人工验证；请在运行插件的机器上查看浏览器，完成后检查数据源状态。无桌面时需预先配置私有显示与 SSH 隧道。"})

    async def refresh(self, req):
        self.plugin.schedule_refresh("web_refresh")
        return self._json({"ok": True})

    async def push_now(self, req):
        """立即推送赛程到所有群"""
        body = await self._body(req, allow_empty=True)
        days = integer(body.get("days", 1), "days", 1, 30)
        if not self.plugin.has_daily_matches_to_push(days):
            return self._json({"ok": True, "skipped": True, "msg": "no matches to push"})
        self.plugin.schedule_instant_push(days, "web_push_now")
        return self._json({"ok": True})

    async def send_test(self, req):
        body = await self._body(req, allow_empty=True)
        action = string(body.get("action", ""), "action") or "赛前"
        if not self.plugin.is_test_mode_enabled():
            return self._json({"ok": False, "msg": "测试模式未开启"}, 400)
        if not self.plugin.has_test_target():
            return self._json({"ok": False, "msg": "请先配置测试 QQ 或群号"}, 400)
        valid = {"赛前", "赛果", "变更", "开幕", "日报", "全部"}
        if action not in valid:
            return self._json({"ok": False, "msg": "未知测试动作"}, 400)
        try:
            msg = await self.plugin.run_test_push(action, 1)
        except DeliveryError as exc:
            return self._json({"ok": False, "msg": str(exc)}, 502)
        return self._json({"ok": True, "msg": msg})

    async def add_group(self, req):
        body = await self._body(req)
        gid = str(integer(body.get("gid"), "group id", 1))
        added = self.plugin.store.add_group(gid)
        return self._json({"ok": True, "added": added})

    async def remove_group(self, req):
        gid = str(integer(req.match_info["gid"], "group id", 1))
        self.plugin.store.remove_group(gid)
        return self._json({"ok": True})

    async def add_team(self, req):
        """通过精确 ID 关注战队"""
        body = await self._body(req)
        team_id = source_id(body.get("id"), "team id")
        name = string(body.get("name"), "team name", nonempty=True)
        slug = string("" if body.get("slug") is None else body["slug"], "team slug")
        image_url = string(body.get("image_url") or "", "team image URL")
        if image_url and not image_url.startswith(("https://", "http://")):
            raise ConfigError("team image URL must use http:// or https://")
        added = self.plugin.store.follow_team(team_id, name, slug, image_url)
        if added:
            self.plugin.schedule_refresh("web_add_team")
        return self._json({"ok": True, "added": added})

    async def remove_team(self, req):
        """取消关注战队 — 支持 POST /teams/unfollow {id} 和旧版 DELETE /teams/{name}"""
        team_id = None
        name = req.match_info.get("name", "")
        if req.method == "POST":
            body = await self._body(req)
            if body.get("id") is not None:
                team_id = source_id(body["id"], "team id")
            else:
                name = string(body.get("name"), "team name", nonempty=True)

        # aiohttp has already decoded the route parameter. Do not unquote twice.
        if team_id is None:
            name = string(name, "team name", nonempty=True)
            teams = self.plugin.store.get_followed_teams()
            matched = [t for t in teams if isinstance(t, dict) and
                       t.get("name", "").lower() == name.lower()]
            if matched:
                team_id = matched[0].get("id")
        if not team_id:
            if not self.plugin.store.unfollow_team_by_name(name):
                return self._json({"ok": False, "msg": "team id not found"}, 400)
        else:
            self.plugin.store.unfollow_team(team_id)
        self.plugin.schedule_refresh("web_remove_team")
        return self._json({"ok": True})

    async def search_teams(self, req):
        """搜索战队，返回精确列表供前端展示"""
        q = req.rel_url.query.get("q", "").strip()
        if not q or len(q) > 80:
            return self._json({"ok": False, "msg": "请输入 1–80 个字符的战队名称或别称"}, 400)
        results = await self.plugin.client.search_teams(q, per_page=15)
        teams = [
            {"id": t.get("id"), "name": t.get("name"), "slug": t.get("slug"),
             "image_url": t.get("image_url"), "is_main": bool(t.get("is_main")),
             "matched_alias": t.get("matched_alias", ""), "match_kind": t.get("match_kind", ""),
             "_source": t.get("_source", "pandascore")}
            for t in results if t.get("id") and t.get("name")
        ]
        # 标记已关注
        followed_ids = self.plugin.store.get_followed_team_ids()
        for t in teams:
            t["followed"] = t["id"] in followed_ids
        return self._json({"ok": True, "teams": teams})

    async def popular_teams(self, req):
        result = self.plugin.client.team_search.popular()
        followed = self.plugin.store.get_followed_team_ids()
        for team in result['teams']:
            team['followed'] = team['id'] in followed
        return self._json({'ok': True, **result})

    async def get_logs(self, req):
        return self._json({"ok": True, "logs": list(self._logs)[-100:]})

    async def clear_notified(self, req):
        self.plugin.clear_match_notifications()
        return self._json({"ok": True})

    async def rebuild_tasks(self, req):
        self.plugin.rebuild_all_match_tasks()
        return self._json({"ok": True})

    async def patch_match(self, req):
        """修改单场比赛的自定义提醒时间"""
        mid = source_id(req.match_info.get("mid", ""), "match id")
        body = await self._body(req)

        if "remind_minutes" not in body:
            return self._json({"ok": False, "msg": "no supported fields"}, 400)

        val = body["remind_minutes"]
        minutes = None if val is None else integer(val, "remind_minutes", 1, 120)

        msg = self.plugin.update_match_remind(mid, minutes)
        return self._json({"ok": True, "msg": msg})

    async def rebuild_match(self, req):
        """手动重建单场比赛的提醒任务（同时取消正在进行的结果轮询）"""
        mid = source_id(req.match_info.get("mid", ""), "match id")

        msg = self.plugin.rebuild_match_task(mid)
        if msg is None:
            return self._json({"ok": False, "msg": "match not found"}, 404)
        return self._json({"ok": True, "msg": msg})

    def push_log(self, level: str, msg: str):
        now = datetime.now(CST).strftime("%H:%M:%S")
        self._logs.append({"time": now, "level": level, "msg": msg})

    async def get_card_layout(self, req):
        layout = validate_card_layout(self.plugin.store.get("card_layout") or {})
        data = catalog()
        return self._json({
            "ok": True,
            "catalog": data.get("cards", data),
            "canvas": data.get("canvas") or {},
            "layout": layout,
            "defaults": default_layout(),
        })

    async def save_card_layout(self, req):
        body = await self._body(req)
        layout = validate_card_layout(body.get("layout") if isinstance(body.get("layout"), dict) else body)
        self.plugin.store.import_config({"card_layout": layout})
        return self._json({"ok": True, "layout": layout})

    async def preview_card_layout(self, req):
        import base64
        import os
        import tempfile
        body = await self._body(req)
        kind = string(body.get("type") or "daily", "type")
        if kind not in CARD_TYPES:
            raise ConfigError("unknown card type")
        card = body.get("card")
        saved = self.plugin.store.get("card_layout") or {}
        if isinstance(card, dict):
            saved = validate_card_layout({**validate_card_layout(saved), kind: card})
        else:
            saved = validate_card_layout(saved)
        fd, path = tempfile.mkstemp(prefix="cs2-card-", suffix=".png")
        os.close(fd)
        try:
            renderer = self.plugin._banner_renderer
            renderer.render_preview(kind, saved, path)
            data = base64.b64encode(Path(path).read_bytes()).decode("ascii")
            boxes = list(getattr(renderer, "last_boxes", []) or [])
            size = getattr(renderer, "last_size", (0, 0)) or (0, 0)
        finally:
            try:
                os.remove(path)
            except OSError:
                pass
        return self._json({
            "ok": True,
            "type": kind,
            "image": f"data:image/png;base64,{data}",
            "boxes": boxes,
            "width": size[0],
            "height": size[1],
        })

    async def start(self, host: str, port: int):

        await self.reconfigure(True, host, port)

    async def stop(self):
        # Runner cleanup belongs to final plugin shutdown. Reconfiguring a site
        # must not run Application shutdown/cleanup hooks on an active app.
        async with self._reconfigure_lock:
            runner = self._runner
            self._runner = None
            self._site = None
            self._bind_host = None
            self._bind_port = None
            if runner:
                await runner.cleanup()

    async def reconfigure(self, enabled: bool, host: str, port: int) -> bool:
        async with self._reconfigure_lock:
            if not enabled:
                if self._site:
                    await self._site.stop()
                    self._site = None
                    self._bind_host = None
                    self._bind_port = None
                    logger.info("[CS] Web 管理面板已关闭")
                    self.push_log("OK", "Web 管理面板已关闭")
                    return True
                return False

            if self._site and self._bind_host == host and self._bind_port == port:
                return False

            if self._runner is None:
                runner = web.AppRunner(self.app)
                await runner.setup()
                self._runner = runner

            new_site = web.TCPSite(self._runner, host, port)
            try:
                await new_site.start()
            except Exception:
                # A failed start can register a site with the runner. Remove it
                # while preserving the previous live site and application.
                await new_site.stop()
                raise

            old_site = self._site
            self._site = new_site
            self._bind_host = host
            self._bind_port = port
            if old_site:
                # Site.stop only stops accepting connections. The config request
                # that arrived through this site can finish sending its response.
                await old_site.stop()

            logger.info(f"[CS] Web 管理面板已切换监听：http://{host}:{port}")
            self.push_log("OK", f"Web 管理面板监听 {host}:{port}")
            return True
