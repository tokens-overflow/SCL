"""网络好友（互联网真人聊天）—— 用 GitHub 私有仓库当中转/邮箱。

设计（与 AI 好友/任务完全独立，纯新增）：
- `GithubMailbox`：用标准库 `urllib` 调 GitHub Contents API 收发消息文件（零依赖）。
- `NetChatStore`：配置 `data/netchat.json`（owner/repo/token/handle/friends/seen_cids）
  + 历史 `data/net_msgs/<peer>.jsonl`。落盘复用 `stores.atomic_write_text`。
- `NetChatService`：后台轮询线程 + 轻量 SSE 扇出(subscribe/emit，仿 task_service)
  + `setup/friends/add_friend/send/history`。

消息 = 仓库里一个小文件：`chat/<收件人>/<epoch_ms>-<发件人>-<rand6>.json`，
内容 `{v,from,to,text,ts,cid}`。收方轮询自己的 `chat/<handle>/` 目录，读到就 emit 给前端并**删除**该文件
（靠删除避免重复+仓库膨胀；再用本地 seen_cids 兜底去重）。

**只有配置齐全(owner/repo/token/handle)时才起后台线程**；否则完全待命，不发任何网络请求＝对现有功能零影响。
PAT 只在后端使用，绝不通过 `public_state()` 返回给前端、不打日志。
"""
from __future__ import annotations

import base64
import json
import queue
import random
import re
import string
import threading
import time
import urllib.error
import urllib.request
from copy import deepcopy
from pathlib import Path
from typing import Any

from .stores import atomic_write_text

GITHUB_API = "https://api.github.com"
_SEEN_CAP = 800          # 本地去重记录上限
_EVENTS_CAP = 500        # SSE 重放缓冲上限
_FAST_SECONDS = 2.0      # 活跃对话时的快轮询间隔（更快收到对方消息）
_ACTIVE_WINDOW = 40.0    # 最近一次收发/打开会话后，这么多秒内都算“活跃”走快轮询


def _rand6() -> str:
    return "".join(random.choices(string.ascii_lowercase + string.digits, k=6))


def _safe_key(h: str) -> str:
    """把“号”规整成 URL/路径安全的标识：只留 字母/数字/_/-/.，其余去掉。
    号是收件目录名，会拼进 GitHub API 的 URL（不能含空格等控制字符）。
    个性/中文/表情请放到 头像 和 个性签名 里（那两个是自由的、对方也能看到）。"""
    return re.sub(r"[^A-Za-z0-9_.\-]", "", (h or "")).strip("._-")


class GithubMailbox:
    """对一个 GitHub 仓库的收发原语（Contents API）。所有网络错误向上抛。"""

    def __init__(self, owner: str, repo: str, token: str):
        self.owner = owner
        self.repo = repo
        self.token = token

    def _req(self, method: str, path: str, body: dict | None = None) -> Any:
        url = f"{GITHUB_API}/repos/{self.owner}/{self.repo}{path}"
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Authorization", f"token {self.token}")
        req.add_header("Accept", "application/vnd.github+json")
        req.add_header("X-GitHub-Api-Version", "2022-11-28")
        if data is not None:
            req.add_header("Content-Type", "application/json")
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read()
            return json.loads(raw.decode("utf-8")) if raw else {}

    def check(self) -> None:
        """验证 owner/repo/token 是否可用（拿仓库信息）；失败抛异常。"""
        self._req("GET", "")

    def put_message(self, to: str, sender: str, text: str, cid: str,
                    av: str = "", sign: str = "", nick: str = "") -> dict[str, Any]:
        ts = int(time.time() * 1000)
        fname = f"{ts}-{_safe_key(sender)}-{_rand6()}.json"
        path = f"chat/{_safe_key(to)}/{fname}"
        payload = {"v": 1, "from": sender, "to": to, "text": text, "ts": ts, "cid": cid,
                   "av": av, "sign": sign, "nick": nick}
        content = base64.b64encode(
            json.dumps(payload, ensure_ascii=False).encode("utf-8")
        ).decode("ascii")
        self._req("PUT", f"/contents/{path}", {"message": f"msg {sender}->{to}", "content": content})
        return payload

    def list_inbox(self, handle: str) -> list[dict[str, Any]]:
        """列出 chat/<handle>/ 下的消息文件；目录不存在(404)视为空。"""
        try:
            items = self._req("GET", f"/contents/chat/{_safe_key(handle)}")
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return []
            raise
        if not isinstance(items, list):
            return []
        files = [it for it in items if isinstance(it, dict)
                 and it.get("type") == "file" and str(it.get("name", "")).endswith(".json")]
        files.sort(key=lambda it: it.get("name", ""))   # 文件名以 epoch_ms 开头，按时间序
        return files

    def fetch(self, item: dict[str, Any]) -> dict[str, Any]:
        data = self._req("GET", f"/contents/{item['path']}")
        raw = base64.b64decode(data.get("content", ""))
        return json.loads(raw.decode("utf-8"))

    def delete(self, item: dict[str, Any]) -> None:
        self._req("DELETE", f"/contents/{item['path']}",
                  {"message": "read", "sha": item["sha"]})

    def read_json(self, path: str) -> tuple[Any, str | None]:
        """读取仓库里某个 JSON 文件 → (对象, sha)；不存在(404)→(None, None)。"""
        try:
            data = self._req("GET", f"/contents/{path}")
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return None, None
            raise
        raw = base64.b64decode(data.get("content", ""))
        try:
            return json.loads(raw.decode("utf-8")), data.get("sha")
        except Exception:
            return None, data.get("sha")

    def write_json(self, path: str, obj: Any, sha: str | None = None, message: str = "update") -> None:
        content = base64.b64encode(
            json.dumps(obj, ensure_ascii=False).encode("utf-8")).decode("ascii")
        body: dict[str, Any] = {"message": message, "content": content}
        if sha:
            body["sha"] = sha
        self._req("PUT", f"/contents/{path}", body)


class NetChatStore:
    """网络聊天的本地配置 + 历史。线程安全。"""

    def __init__(self, data_dir: Path | str):
        self.data_dir = Path(data_dir)
        self.path = self.data_dir / "netchat.json"
        self.msg_dir = self.data_dir / "net_msgs"
        self.lock = threading.RLock()
        self._data = self._load()

    def _load(self) -> dict[str, Any]:
        if self.path.exists():
            try:
                return json.loads(self.path.read_text(encoding="utf-8"))
            except Exception:
                pass
        return {"owner": "", "repo": "", "token": "", "handle": "", "nickname": "",
                "avatar": "🧑", "sign": "", "friends": [], "seen_cids": [], "peer_meta": {}}

    def _save_locked(self) -> None:
        atomic_write_text(self.path, json.dumps(self._data, ensure_ascii=False, indent=2))

    def configured(self) -> bool:
        with self.lock:
            d = self._data
            return bool(d.get("owner") and d.get("repo") and d.get("token") and d.get("handle"))

    def creds(self) -> tuple[str, str, str, str]:
        with self.lock:
            d = self._data
            return d.get("owner", ""), d.get("repo", ""), d.get("token", ""), d.get("handle", "")

    def avatar(self) -> str:
        with self.lock:
            return self._data.get("avatar") or "🧑"

    def sign(self) -> str:
        with self.lock:
            return self._data.get("sign") or ""

    def nickname(self) -> str:
        with self.lock:
            return self._data.get("nickname") or self._data.get("handle") or ""

    def token(self) -> str:
        with self.lock:
            return self._data.get("token") or ""

    def public_state(self) -> dict[str, Any]:
        """给前端看的状态——**绝不含 token**。"""
        with self.lock:
            d = self._data
            return {"configured": self.configured(),
                    "id": d.get("handle", ""), "handle": d.get("handle", ""),
                    "nickname": d.get("nickname", "") or d.get("handle", ""),
                    "avatar": d.get("avatar", "🧑"), "sign": d.get("sign", ""),
                    "owner": d.get("owner", ""), "repo": d.get("repo", ""),
                    "friends": list(d.get("friends", [])),
                    "peer_meta": dict(d.get("peer_meta", {}))}

    def set_config(self, owner: str, repo: str, token: str, handle: str,
                   avatar: str = "🧑", sign: str = "", nickname: str = "") -> None:
        with self.lock:
            self._data.update(owner=owner, repo=repo, token=token, handle=handle,
                              avatar=(avatar or "🧑"), sign=(sign or ""),
                              nickname=(nickname or handle))
            self._save_locked()

    def set_peer_meta(self, peer: str, av: str = "", sign: str = "", nick: str = "") -> None:
        if not peer:
            return
        with self.lock:
            pm = self._data.setdefault("peer_meta", {})
            cur = dict(pm.get(peer, {}))
            if av:
                cur["av"] = av
            if sign or "sign" not in cur:
                cur["sign"] = sign
            if nick:
                cur["nick"] = nick
            if pm.get(peer) != cur:
                pm[peer] = cur
                self._save_locked()

    def friends(self) -> list[str]:
        with self.lock:
            return list(self._data.get("friends", []))

    def add_friend(self, handle: str) -> bool:
        handle = _safe_key(handle)
        if not handle:
            return False
        with self.lock:
            fs = self._data.setdefault("friends", [])
            if handle in fs:
                return False
            fs.append(handle)
            self._save_locked()
            return True

    def del_friend(self, handle: str) -> None:
        handle = _safe_key(handle)
        with self.lock:
            fs = self._data.setdefault("friends", [])
            if handle in fs:
                fs.remove(handle)
            self._data.setdefault("peer_meta", {}).pop(handle, None)
            self._save_locked()

    def seen(self, cid: str) -> bool:
        with self.lock:
            return cid in self._data.get("seen_cids", [])

    def mark_seen(self, cid: str) -> None:
        with self.lock:
            seen = self._data.setdefault("seen_cids", [])
            seen.append(cid)
            if len(seen) > _SEEN_CAP:
                del seen[: len(seen) - _SEEN_CAP]
            self._save_locked()

    def append_history(self, peer: str, record: dict[str, Any]) -> None:
        self.msg_dir.mkdir(parents=True, exist_ok=True)
        line = json.dumps(record, ensure_ascii=False) + "\n"
        with self.lock:
            with open(self.msg_dir / f"{peer}.jsonl", "a", encoding="utf-8") as f:
                f.write(line)

    def clear_history(self, peer: str) -> None:
        try:
            (self.msg_dir / f"{peer}.jsonl").unlink(missing_ok=True)
        except OSError:
            pass

    def history(self, peer: str, limit: int = 300) -> list[dict[str, Any]]:
        p = self.msg_dir / f"{peer}.jsonl"
        if not p.exists():
            return []
        out: list[dict[str, Any]] = []
        for ln in p.read_text(encoding="utf-8").splitlines():
            ln = ln.strip()
            if not ln:
                continue
            try:
                out.append(json.loads(ln))
            except Exception:
                pass
        return out[-limit:]


class NetChatService:
    """网络聊天服务：后台轮询 GitHub 邮箱 + SSE 推前端 + 收发/好友。"""

    def __init__(self, data_dir: Path | str, poll_seconds: float = 5.0):
        self.store = NetChatStore(data_dir)
        self.poll_seconds = poll_seconds       # 空闲时的慢轮询间隔
        self._last_active = 0.0                # 最近收发/打开会话的时间戳（决定快/慢轮询）
        self.lock = threading.RLock()
        self._subs: set[queue.Queue[dict[str, Any]]] = set()
        self._events: list[dict[str, Any]] = []
        self._seq = 0
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        if self.store.configured():
            self._start_thread()

    # ---------- SSE 轻量扇出（仿 task_service.subscribe/emit） ----------
    def subscribe(self) -> queue.Queue[dict[str, Any]]:
        ch: queue.Queue[dict[str, Any]] = queue.Queue()
        with self.lock:
            self._subs.add(ch)
        return ch

    def unsubscribe(self, ch: queue.Queue[dict[str, Any]]) -> None:
        with self.lock:
            self._subs.discard(ch)

    def replay_after(self, since: int) -> list[dict[str, Any]]:
        with self.lock:
            return [e for e in self._events if int(e.get("_seq") or 0) > since]

    def _emit(self, event: dict[str, Any]) -> None:
        with self.lock:
            self._seq += 1
            event = {**event, "_seq": self._seq}
            self._events.append(event)
            if len(self._events) > _EVENTS_CAP:
                del self._events[: len(self._events) - _EVENTS_CAP]
            subs = list(self._subs)
        for ch in subs:
            ch.put(event)

    # ---------- 对外操作 ----------
    def public_state(self) -> dict[str, Any]:
        return self.store.public_state()

    def setup(self, owner: str, repo: str, token: str, handle: str,
              avatar: str = "🧑", sign: str = "", nickname: str = "") -> dict[str, Any]:
        owner = (owner or "").strip()
        repo = (repo or "").strip()
        handle = _safe_key(handle)          # Q-CC ID 必须 URL 安全（字母/数字/_/-）
        avatar = (avatar or "🧑").strip() or "🧑"
        sign = (sign or "").strip()
        nickname = (nickname or "").strip() or handle
        token = (token or "").strip()
        prev_id = self.store.creds()[3]
        # token 留空且已配置 → 沿用已存的 token（这样能只改 ID/昵称/头像/个签 而不必重输 PAT）
        if not token:
            token = self.store.token()
        if not handle:
            return {"ok": False, "error": "Q-CC ID 只能用 字母/数字/_/-（昵称/头像/个签 可随意）"}
        if not (owner and repo and token):
            return {"ok": False, "error": "仓库(owner/repo)和 PAT 都要填"}
        mb = GithubMailbox(owner, repo, token)
        try:
            mb.check()
        except urllib.error.HTTPError as exc:
            return {"ok": False, "error": f"GitHub 拒绝({exc.code})：检查仓库名/PAT 权限(Contents 读写)"}
        except Exception as exc:
            return {"ok": False, "error": f"连不上 GitHub：{exc}"}
        # Q-CC ID 唯一性：members.json 里登记所有 ID；换新 ID 时若已被占用则拒绝
        if handle != prev_id:
            try:
                members, sha = mb.read_json("members.json")
                ids = list(members.get("ids", [])) if isinstance(members, dict) else []
                if handle in ids:
                    return {"ok": False, "error": f"Q-CC ID「{handle}」已被占用，换一个"}
                ids.append(handle)
                mb.write_json("members.json", {"ids": ids}, sha, f"register {handle}")
            except urllib.error.HTTPError as exc:
                return {"ok": False, "error": f"登记 ID 失败({exc.code})：检查 PAT 的 Contents 写权限"}
            except Exception as exc:
                return {"ok": False, "error": f"登记 ID 失败：{exc}"}
        self.store.set_config(owner, repo, token, handle, avatar, sign, nickname)
        self._restart_thread()
        return {"ok": True, **self.store.public_state()}

    def friends(self) -> list[str]:
        return self.store.friends()

    def add_friend(self, handle: str) -> dict[str, Any]:
        self.store.add_friend(handle)
        self._emit({"type": "net-friends"})
        return {"ok": True, "friends": self.store.friends()}

    def del_friend(self, handle: str) -> dict[str, Any]:
        self.store.del_friend(handle)
        self._emit({"type": "net-friends"})
        return {"ok": True, "friends": self.store.friends()}

    def send(self, to: str, text: str) -> dict[str, Any]:
        to = (to or "").strip()
        text = (text or "")
        if not to or not text.strip():
            return {"ok": False, "error": "收件人/内容不能为空"}
        if not self.store.configured():
            return {"ok": False, "error": "还没配置网络好友（点⚙设置）"}
        owner, repo, token, handle = self.store.creds()
        my_av = self.store.avatar()
        my_sign = self.store.sign()
        my_nick = self.store.nickname()
        cid = _rand6() + _rand6()
        ts = int(time.time() * 1000)
        rec = {"v": 1, "from": handle, "to": to, "text": text, "ts": ts, "cid": cid,
               "av": my_av, "sign": my_sign, "nick": my_nick, "dir": "out"}
        self.store.append_history(to, rec)
        self.store.mark_seen(cid)                 # 自己发的也记 seen，避免回读自己
        self.mark_active()                        # 发完进入快轮询，尽快收到对方回复
        self._emit({"type": "net-msg", "peer": to, "msg": rec})   # 本地立即回显给 UI
        try:
            GithubMailbox(owner, repo, token).put_message(to, handle, text, cid, my_av, my_sign, my_nick)
        except Exception as exc:
            self._emit({"type": "net-error", "peer": to, "text": f"发送失败：{exc}"})
            return {"ok": False, "error": str(exc)}
        return {"ok": True, "cid": cid}

    def history(self, peer: str) -> list[dict[str, Any]]:
        return self.store.history(peer)

    def clear_history(self, peer: str) -> dict[str, Any]:
        self.store.clear_history(peer)
        self._emit({"type": "net-cleared", "peer": peer})
        return {"ok": True}

    # ---------- 后台轮询 ----------
    def _start_thread(self) -> None:
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True, name="qcc-netchat")
        self._thread.start()

    def _restart_thread(self) -> None:
        self._stop.set()
        self._thread = None
        if self.store.configured():
            self._start_thread()

    def mark_active(self) -> None:
        """有收发或前端打开了网络会话 → 进入快轮询窗口（收对方消息更快）。"""
        self._last_active = time.time()

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self._poll_once()
            except Exception:
                pass          # 网络抖动/限流：静默重试，不打断
            active = (time.time() - self._last_active) < _ACTIVE_WINDOW
            self._stop.wait(_FAST_SECONDS if active else self.poll_seconds)

    def _poll_once(self) -> None:
        if not self.store.configured():
            return
        owner, repo, token, handle = self.store.creds()
        mb = GithubMailbox(owner, repo, token)
        for item in mb.list_inbox(handle):
            try:
                msg = mb.fetch(item)
            except Exception:
                continue
            cid = msg.get("cid") or item.get("name")
            peer = msg.get("from") or "?"
            if not self.store.seen(cid):
                self.store.mark_seen(cid)
                self.mark_active()               # 收到就保持快轮询，连续对话更跟手
                # 记住对方的头像 + 个签 + 昵称（双向可见）
                self.store.set_peer_meta(peer, msg.get("av", ""), msg.get("sign", ""), msg.get("nick", ""))
                rec = {**msg, "dir": "in"}
                self.store.append_history(peer, rec)
                if peer not in self.store.friends():
                    self.store.add_friend(peer)
                    self._emit({"type": "net-friends"})
                self._emit({"type": "net-msg", "peer": peer, "msg": rec})
            # 读过就删（保持收件箱干净、避免重复）
            try:
                mb.delete(item)
            except Exception:
                pass

    def shutdown(self) -> None:
        self._stop.set()
        self._thread = None
