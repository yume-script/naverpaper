#!/usr/bin/env python3
"""naverpaper MCP 서버 (LiteLLM 등 MCP 클라이언트에서 연결)

노출하는 도구는 모두 이름이 `naverpaper_` 로 시작합니다.

  naverpaper_status        계정별 쿠키 상태
  naverpaper_totals        적립 합계(오늘/7일/30일/전체) + 최근 실행 내역
  naverpaper_run           수집을 백그라운드로 시작 (즉시 반환)
  naverpaper_link          링크 하나 수동 적립을 백그라운드로 시작 (즉시 반환)
  naverpaper_run_status    진행 중인 작업 상태 + 마지막 기록된 실행
  naverpaper_logs          로그 마지막 N줄 (비밀 값은 가림)
  naverpaper_notify        적립 합계를 디스코드로 전송
  naverpaper_login_start   캡차 수동 로그인 시작 (캡차 이미지 반환)
  naverpaper_login_submit  캡차 글자 제출

실행 예
  # LiteLLM 연결용 (streamable HTTP) - 토큰은 config.json 의 mcp_token 에서 읽음
  python3 naverpaper_mcp.py --host 0.0.0.0 --port 5934
  # 로컬 stdio
  python3 naverpaper_mcp.py --transport stdio

보안: 비밀번호, 쿠키 값, 웹훅 주소는 어떤 도구도 돌려주지 않으며 계정 아이디는 마스킹합니다.
"""
import argparse
import asyncio
import base64
import hmac
import json
import logging
import os
import re
import sqlite3
import sys
import threading
from datetime import datetime
from logging.handlers import WatchedFileHandler
from pathlib import Path

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)

import naverpaper as nv  # noqa: E402

PREFIX = "naverpaper_"
LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "::1")
LOGIN_TIMEOUT_SECONDS = 300
MAX_LOGIN_ATTEMPTS = 3

INSTRUCTIONS = (
    "네이버 폐지줍기(naverpaper) 관리 도구입니다. 수집은 몇 분 걸릴 수 있어 naverpaper_run / naverpaper_link 는 "
    "즉시 반환하며, 진행 상황은 naverpaper_run_status 로 확인하세요. 캡차 로그인이 필요하면 "
    "naverpaper_login_start 로 캡차 이미지를 받고 naverpaper_login_submit 으로 글자를 제출합니다."
)

logger = logging.getLogger("naverpaper")

_CONFIG_PATH = nv.DEFAULT_CONFIG
_NP = None  # source_naverpaper 모듈 (지연 로딩)

_JOB = {"state": "idle", "kind": None, "started_at": None, "finished_at": None, "result": None, "error": None}
_JOB_LOCK = threading.Lock()

_LOGIN = {}  # user_id -> {"lock": RunLock, "timer": Timer, "attempts": int}
_LOGIN_LOCK = threading.Lock()


# ---------------------------------------------------------------- 공통 도우미
def _now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _cfg():
    return nv.load_config(_CONFIG_PATH)


def _np():
    global _NP
    if _NP is None:
        import source_naverpaper

        _NP = source_naverpaper
    return _NP


def _paths(cfg):
    base = cfg["data_dir"]
    return {
        "db": os.path.join(base, "naverpaper.sqlite"),
        "cookies": os.path.join(base, "cookies"),
        "lock": os.path.join(base, "naverpaper.lock"),
        "log": os.path.join(base, "naverpaper.log"),
    }


def _mask(user_id):
    return nv._mask_id(user_id)


def _resolve_account(cfg, ref):
    """전체 아이디, 마스킹된 표기(abc•••), 1부터 시작하는 번호 중 어느 것이든 받는다."""
    ref = str(ref or "").strip()
    accounts = cfg["accounts"]
    for acc in accounts:
        if acc["user_id"] == ref:
            return acc
    for acc in accounts:
        if _mask(acc["user_id"]) == ref:
            return acc
    if ref.isdigit() and 1 <= int(ref) <= len(accounts):
        return accounts[int(ref) - 1]
    return None


def _scrub(text, cfg=None):
    """도구가 돌려주는 문자열에서 비밀 값과 계정 아이디를 가린다."""
    text = str(text or "")
    text = re.sub(r"(NID_(?:AUT|SES|JST)\s*[=:]\s*)[^\s;,\"']+", r"\1***", text, flags=re.I)
    text = re.sub(r"https://discord(?:app)?\.com/api/webhooks/\S+", "[webhook]", text)
    if cfg:
        for acc in cfg["accounts"]:
            if acc["password"] and len(acc["password"]) >= 4:
                text = text.replace(acc["password"], "***")
        for acc in cfg["accounts"]:
            if len(acc["user_id"]) >= 3:
                text = text.replace(acc["user_id"], _mask(acc["user_id"]))
    return text


def _engine_log(message):
    logger.info("%s", message)


def _readonly_db(path):
    return sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True, timeout=5)


def _summarize(result, cfg):
    accounts = []
    for ar in getattr(result, "account_results", []) or []:
        accounts.append(
            {
                "account": _mask(getattr(ar, "user_id", "")),
                "visited": int(getattr(ar, "visited_url_count", 0) or 0),
                "skipped": int(getattr(ar, "skipped_url_count", 0) or 0),
                "points": int(getattr(ar, "estimated_points", 0) or 0),
            }
        )
    return {
        "status": result.status,
        "message": _scrub(getattr(result, "message", ""), cfg),
        "collected_urls": int(getattr(result, "collected_url_count", 0) or 0),
        "skipped_urls": int(getattr(result, "skipped_url_count", 0) or 0),
        "visited_urls": int(getattr(result, "visited_url_count", 0) or 0),
        "earned_points": int(getattr(result, "estimated_points", 0) or 0),
        "accounts": accounts,
    }


# ---------------------------------------------------------------- 백그라운드 작업
def _login_active():
    with _LOGIN_LOCK:
        return bool(_LOGIN)


def _start_job(kind, fn):
    if _login_active():
        return {"started": False, "reason": "캡차 로그인이 진행 중입니다. 완료하거나 만료(최대 5분)된 뒤 다시 시도하세요."}
    with _JOB_LOCK:
        if _JOB["state"] == "running":
            return {"started": False, "reason": f"이미 {_JOB['kind']} 작업이 실행 중입니다.", "started_at": _JOB["started_at"]}
        _JOB.update(state="running", kind=kind, started_at=_now(), finished_at=None, result=None, error=None)
        started_at = _JOB["started_at"]

    def worker():
        result, error, state = None, None, "finished"
        try:
            result = fn()
        except Exception as e:  # noqa: BLE001
            logger.exception("%s 작업 오류", kind)
            state, error = "error", _scrub(str(e))[:300]
        with _JOB_LOCK:
            _JOB.update(state=state, finished_at=_now(), result=result, error=error)

    threading.Thread(target=worker, daemon=True, name="naverpaper-job").start()
    return {"started": True, "kind": kind, "started_at": started_at, "next": "naverpaper_run_status 로 진행 상황을 확인하세요."}


def _do_run():
    cfg = _cfg()
    np = _np()
    p = _paths(cfg)
    os.makedirs(cfg["data_dir"], exist_ok=True)
    lock = nv.RunLock(p["lock"])
    if not lock.acquire():
        return {"status": "skipped", "message": "다른 수집 또는 로그인이 이미 진행 중이라 건너뜁니다."}
    try:
        nv.seed_cookies(np, cfg, cfg["accounts"])
        run_cfg = np.RunConfig(
            db_path=p["db"],
            cookie_dir=p["cookies"],
            accounts=nv.build_accounts(np, cfg["accounts"]),
            reward_proxy_url=cfg["reward_proxy_url"],
            login_proxy_url=cfg["login_proxy_url"],
            no_paper_record=cfg["no_paper_record"],
            keep_campaign_days=cfg["keep_campaign_days"],
            keep_user_days=cfg["keep_user_days"],
        )
        result = np.run_sync(run_cfg, log=_engine_log)
        logger.info("수집 종료 status=%s 예상포인트=%s", result.status, result.estimated_points)
        try:
            nv.notify_run(cfg, result, p["db"])
        except Exception:  # noqa: BLE001
            logger.exception("디스코드 알림 실패")
        return _summarize(result, cfg)
    finally:
        lock.release()


def _do_link(url):
    cfg = _cfg()
    np = _np()
    p = _paths(cfg)
    os.makedirs(cfg["data_dir"], exist_ok=True)
    lock = nv.RunLock(p["lock"])
    if not lock.acquire():
        return {"status": "skipped", "message": "다른 수집 또는 로그인이 이미 진행 중이라 건너뜁니다."}
    try:
        nv.seed_cookies(np, cfg, cfg["accounts"])
        result = np.run_manual_link_sync(
            p["db"],
            p["cookies"],
            nv.build_accounts(np, cfg["accounts"]),
            url,
            cfg["reward_proxy_url"],
            log=_engine_log,
            login_proxy_url=cfg["login_proxy_url"],
        )
        return _summarize(result, cfg)
    finally:
        lock.release()


# ---------------------------------------------------------------- 조회 도우미
def _status_data():
    cfg = _cfg()
    np = _np()
    cookie_dir = _paths(cfg)["cookies"]
    rows = []
    for idx, acc in enumerate(cfg["accounts"], 1):
        ok, detail = np.check_cookie_status(acc["user_id"], cookie_dir)
        rows.append(
            {
                "index": idx,
                "account": _mask(acc["user_id"]),
                "cookie_ok": bool(ok),
                "detail": _scrub(detail, cfg),
                "password_configured": bool(acc["password"]),
            }
        )
    return {"accounts": rows}


def _recent_runs(db_path, limit):
    if not nv._db_exists(db_path):
        return []
    conn = _readonly_db(db_path)
    try:
        runs = conn.execute(
            "SELECT id, started_at, finished_at, status, visited_url_count, estimated_points "
            "FROM run_history ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        out = []
        for run_id, started, finished, status, visited, points in runs:
            per = conn.execute(
                "SELECT user_id, COALESCE(estimated_points, 0) FROM run_account_summary WHERE run_id=? ORDER BY id",
                (run_id,),
            ).fetchall()
            out.append(
                {
                    "run_id": run_id,
                    "started_at": str(started)[:19],
                    "finished_at": str(finished)[:19] if finished else None,
                    "status": status,
                    "visited_urls": int(visited or 0),
                    "earned_points": int(points or 0),
                    "accounts": [{"account": _mask(u), "points": int(p or 0)} for u, p in per],
                }
            )
        return out
    finally:
        conn.close()


def _totals_data(runs):
    cfg = _cfg()
    db_path = _paths(cfg)["db"]
    ids = [a["user_id"] for a in cfg["accounts"]]
    zero = {"today": 0, "d7": 0, "d30": 0, "all": 0}
    if nv._db_exists(db_path):
        stats, first, run_count = nv.points_report(db_path, ids)
    else:
        stats, first, run_count = {uid: dict(zero) for uid in ids}, None, 0
    total = dict(zero)
    accounts = []
    for uid in sorted(stats):
        e = stats[uid]
        for k in total:
            total[k] += e[k]
        accounts.append(
            {
                "account": _mask(uid),
                "today": e["today"],
                "last_7_days": e["d7"],
                "last_30_days": e["d30"],
                "all_time": e["all"],
            }
        )
    return {
        "first_record": str(first)[:10] if first else None,
        "run_count": run_count,
        "accounts": accounts,
        "total": {"today": total["today"], "last_7_days": total["d7"], "last_30_days": total["d30"], "all_time": total["all"]},
        "recent_runs": _recent_runs(db_path, runs),
        "note": "적립액은 수집 전후 네이버페이 포인트 잔액 차이로 계산한 추정값입니다.",
    }


def _latest_run(db_path):
    if not nv._db_exists(db_path):
        return None
    conn = _readonly_db(db_path)
    try:
        row = conn.execute(
            "SELECT id, started_at, finished_at, status, visited_url_count, estimated_points, message "
            "FROM run_history ORDER BY id DESC LIMIT 1"
        ).fetchone()
    finally:
        conn.close()
    if not row:
        return None
    return {
        "run_id": row[0],
        "started_at": str(row[1])[:19],
        "finished_at": str(row[2])[:19] if row[2] else None,
        "status": row[3],
        "visited_urls": int(row[4] or 0),
        "earned_points": int(row[5] or 0),
        "message": _scrub(row[6] or ""),
    }


def _tail_log(lines):
    cfg = _cfg()
    path = _paths(cfg)["log"]
    if not os.path.exists(path):
        return ""
    with open(path, "rb") as f:
        f.seek(0, os.SEEK_END)
        size = f.tell()
        f.seek(max(0, size - 262144))
        data = f.read().decode("utf-8", "replace")
    return _scrub("\n".join(data.splitlines()[-lines:]), cfg)


def _notify(test):
    cfg = _cfg()
    url = cfg["discord_webhook_url"]
    if not url:
        return {"sent": False, "message": "config.json 에 discord_webhook_url 이 설정되어 있지 않습니다."}
    if test:
        return {"sent": bool(nv.send_discord(url, {"content": "naverpaper MCP 테스트 메시지입니다."}))}
    db_path = _paths(cfg)["db"]
    ids = [a["user_id"] for a in cfg["accounts"]]
    if nv._db_exists(db_path):
        stats, _first, _count = nv.points_report(db_path, ids)
    else:
        stats = {uid: {"today": 0, "d7": 0, "d30": 0, "all": 0} for uid in ids}
    embed = nv.build_embed(cfg, "네이버 폐지줍기 적립 합계", stats)
    return {"sent": bool(nv.send_discord(url, {"embeds": [embed]}))}


# ---------------------------------------------------------------- 캡차 로그인
def _decode_shot(data_url):
    raw = data_url.split(",", 1)[1] if "," in data_url else data_url
    return base64.b64decode(raw)


def _login_cleanup(user_id):
    with _LOGIN_LOCK:
        entry = _LOGIN.pop(user_id, None)
    if not entry:
        return
    entry["timer"].cancel()
    try:
        _np().close_login_session_sync(user_id)
    except Exception:  # noqa: BLE001
        pass
    entry["lock"].release()


def _login_expire(user_id):
    logger.info("캡차 로그인 대기 시간이 지나 세션을 닫습니다.")
    _login_cleanup(user_id)


def _arm_timer(user_id, entry):
    entry["timer"].cancel()
    timer = threading.Timer(LOGIN_TIMEOUT_SECONDS, _login_expire, args=(user_id,))
    timer.daemon = True
    entry["timer"] = timer
    timer.start()


def _login_payload(user_id, result, entry, cfg):
    """(응답 dict, 캡차 이미지 bytes 또는 None)"""
    if result.get("cookies"):
        _login_cleanup(user_id)
        return {"status": "success", "account": _mask(user_id), "message": "로그인 성공, 쿠키 저장 완료"}, None
    shot = result.get("screenshot")
    if shot and entry["attempts"] < MAX_LOGIN_ATTEMPTS:
        _arm_timer(user_id, entry)
        image = _decode_shot(shot)
        payload = {
            "status": "captcha_required",
            "account": _mask(user_id),
            "attempt": entry["attempts"] + 1,
            "max_attempts": MAX_LOGIN_ATTEMPTS,
            "expires_in_seconds": LOGIN_TIMEOUT_SECONDS,
            "message": _scrub(result.get("msg") or "캡차 이미지를 확인하고 naverpaper_login_submit 으로 글자를 제출하세요.", cfg),
        }
        if cfg.get("discord_webhook_url"):
            try:
                sent = nv.notify_captcha(
                    cfg, user_id, image, f"naverpaper_login_submit 으로 {LOGIN_TIMEOUT_SECONDS // 60}분 안에 제출하세요."
                )
            except Exception:  # noqa: BLE001
                logger.exception("캡차 이미지 디스코드 전송 실패")
                sent = False
            payload["discord_sent"] = bool(sent)
        return payload, image
    _login_cleanup(user_id)
    return {"status": "failed", "account": _mask(user_id), "message": _scrub(result.get("msg") or "로그인에 실패했습니다.", cfg)}, None


def _login_start(account):
    cfg = _cfg()
    np = _np()
    target = _resolve_account(cfg, account)
    if not target:
        return {"status": "error", "message": "해당 계정을 찾을 수 없습니다. naverpaper_status 의 index 또는 account 값을 사용하세요."}, None
    if not target["password"]:
        return {"status": "error", "message": "config.json 에 이 계정의 비밀번호가 없어 자동 로그인을 시작할 수 없습니다."}, None
    with _JOB_LOCK:
        if _JOB["state"] == "running":
            return {"status": "error", "message": f"{_JOB['kind']} 작업이 실행 중입니다. 끝난 뒤 다시 시도하세요."}, None

    user_id = target["user_id"]
    _login_cleanup(user_id)  # 이전 세션이 남아 있으면 정리
    p = _paths(cfg)
    os.makedirs(cfg["data_dir"], exist_ok=True)
    lock = nv.RunLock(p["lock"])
    if not lock.acquire():
        return {"status": "error", "message": "다른 수집 또는 로그인이 진행 중입니다."}, None

    entry = {"lock": lock, "timer": threading.Timer(0, lambda: None), "attempts": 0}
    with _LOGIN_LOCK:
        _LOGIN[user_id] = entry
    try:
        account_obj = np.AccountConfig(user_id=user_id, password=target["password"], keep_login=target["keep_login"])
        result = np.open_login_screen_sync(account_obj, p["cookies"], cfg["login_proxy_url"], log=_engine_log)
    except Exception as e:  # noqa: BLE001
        logger.exception("로그인 화면 열기 실패")
        _login_cleanup(user_id)
        return {"status": "error", "message": _scrub(str(e), cfg)[:300]}, None
    return _login_payload(user_id, result, entry, cfg)


def _login_submit(account, captcha_text):
    cfg = _cfg()
    np = _np()
    target = _resolve_account(cfg, account)
    if not target:
        return {"status": "error", "message": "해당 계정을 찾을 수 없습니다."}, None
    user_id = target["user_id"]
    with _LOGIN_LOCK:
        entry = _LOGIN.get(user_id)
    if not entry:
        return {"status": "error", "message": "진행 중인 로그인이 없거나 만료되었습니다. naverpaper_login_start 부터 다시 시작하세요."}, None
    entry["attempts"] += 1
    try:
        result = np.submit_login_captcha_sync(user_id, str(captcha_text or "").strip(), _paths(cfg)["cookies"], log=_engine_log)
    except Exception as e:  # noqa: BLE001
        logger.exception("캡차 제출 실패")
        _login_cleanup(user_id)
        return {"status": "error", "message": _scrub(str(e), cfg)[:300]}, None
    return _login_payload(user_id, result, entry, cfg)


def _with_image(payload, image):
    if not image:
        return payload
    from mcp.server.fastmcp import Image

    return [json.dumps(payload, ensure_ascii=False), Image(data=image, format="png")]


# ---------------------------------------------------------------- MCP 도구 (이름은 모두 naverpaper_ 로 시작)
async def naverpaper_status() -> dict:
    """계정별 쿠키 유효 여부를 확인합니다. 아이디는 마스킹되어 표시되며 index 번호를 다른 도구의 account 값으로 쓸 수 있습니다."""
    return await asyncio.to_thread(_status_data)


async def naverpaper_totals(runs: int = 10) -> dict:
    """계정별 적립 합계(오늘/7일/30일/전체)와 최근 실행 내역을 돌려줍니다. runs 는 최근 실행 표시 개수(1~50)입니다."""
    runs = max(1, min(int(runs), 50))
    return await asyncio.to_thread(_totals_data, runs)


async def naverpaper_run() -> dict:
    """네이버 폐지줍기 수집을 백그라운드로 시작하고 즉시 반환합니다. 몇 분 걸릴 수 있으니 naverpaper_run_status 로 진행 상황을 확인하세요."""
    return _start_job("run", _do_run)


async def naverpaper_link(url: str) -> dict:
    """링크 하나를 수동으로 적립합니다(백그라운드 실행). 적립 가능한 네이버 리워드 링크만 허용됩니다."""
    np = _np()
    url = str(url or "").strip()
    if not url.startswith("https://") or not (np.is_reward_url(url) or np.is_pincrux_reward_candidate(url)):
        return {"started": False, "reason": "지원하지 않는 링크입니다. 적립 가능한 https 리워드 링크만 사용할 수 있습니다."}
    return _start_job("link", lambda: _do_link(url))


async def naverpaper_run_status() -> dict:
    """실행 중이거나 마지막으로 끝난 작업(run/link)의 상태와 결과, 그리고 DB에 기록된 마지막 실행을 돌려줍니다."""
    with _JOB_LOCK:
        job = dict(_JOB)
    cfg = _cfg()
    latest = await asyncio.to_thread(_latest_run, _paths(cfg)["db"])
    return {"job": job, "latest_recorded_run": latest, "login_in_progress": _login_active()}


async def naverpaper_logs(lines: int = 50) -> dict:
    """로그의 마지막 N줄(1~300)을 돌려줍니다. 쿠키 값, 웹훅 주소, 비밀번호는 가려지고 아이디는 마스킹됩니다."""
    lines = max(1, min(int(lines), 300))
    text = await asyncio.to_thread(_tail_log, lines)
    return {"lines": lines, "log": text}


async def naverpaper_notify(test: bool = False) -> dict:
    """적립 합계를 설정된 디스코드 웹훅으로 전송합니다. test=true 면 테스트 메시지만 보냅니다."""
    return await asyncio.to_thread(_notify, bool(test))


async def naverpaper_login_start(account: str):
    """캡차 등으로 자동 로그인이 막힌 계정의 수동 로그인을 시작합니다. 캡차 이미지가 함께 반환되며 5분 안에 naverpaper_login_submit 으로 글자를 제출해야 합니다. account 는 naverpaper_status 의 index 번호 또는 마스킹된 아이디입니다."""
    payload, image = await asyncio.to_thread(_login_start, account)
    return _with_image(payload, image)


async def naverpaper_login_submit(account: str, captcha_text: str):
    """naverpaper_login_start 로 받은 캡차 이미지의 글자를 제출합니다. 캡차가 없는 화면이면 빈 문자열을 제출하세요. 최대 3회까지 시도할 수 있습니다."""
    payload, image = await asyncio.to_thread(_login_submit, account, captcha_text)
    return _with_image(payload, image)


# (함수, 읽기 전용 여부)
TOOLS = [
    (naverpaper_status, True),
    (naverpaper_totals, True),
    (naverpaper_run_status, True),
    (naverpaper_logs, True),
    (naverpaper_run, False),
    (naverpaper_link, False),
    (naverpaper_notify, False),
    (naverpaper_login_start, False),
    (naverpaper_login_submit, False),
]


# ---------------------------------------------------------------- 인증 / 서버 구성
class BearerAuth:
    """Authorization: Bearer <토큰> 또는 X-API-Key: <토큰> 헤더를 요구하는 ASGI 미들웨어."""

    def __init__(self, app, token):
        self.app = app
        self.token = token

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            headers = {k.lower(): v for k, v in scope.get("headers", [])}
            bearer = headers.get(b"authorization", b"").decode("latin-1")
            api_key = headers.get(b"x-api-key", b"").decode("latin-1")
            ok = hmac.compare_digest(bearer, f"Bearer {self.token}") or hmac.compare_digest(api_key, self.token)
            if not ok:
                body = b'{"error":"unauthorized"}'
                await send(
                    {
                        "type": "http.response.start",
                        "status": 401,
                        "headers": [
                            (b"content-type", b"application/json"),
                            (b"www-authenticate", b"Bearer"),
                            (b"content-length", str(len(body)).encode()),
                        ],
                    }
                )
                await send({"type": "http.response.body", "body": body})
                return
        await self.app(scope, receive, send)


def build_server(host, port, allowed_hosts):
    from mcp.server.fastmcp import FastMCP

    kwargs = {"host": host, "port": port, "instructions": INSTRUCTIONS}
    try:
        from mcp.server.transport_security import TransportSecuritySettings

        if allowed_hosts:
            origins = [f"{scheme}://{h}" for h in allowed_hosts for scheme in ("http", "https")]
            kwargs["transport_security"] = TransportSecuritySettings(
                enable_dns_rebinding_protection=True, allowed_hosts=list(allowed_hosts), allowed_origins=origins
            )
        elif host not in LOOPBACK_HOSTS:
            # 외부 바인딩은 토큰 인증이 필수이므로 Host 헤더 검사는 끈다 (LiteLLM 이 다른 호스트명으로 접속 가능)
            kwargs["transport_security"] = TransportSecuritySettings(enable_dns_rebinding_protection=False)
    except ImportError:
        pass

    while True:
        try:
            mcp = FastMCP("naverpaper", **kwargs)
            break
        except TypeError:
            if "transport_security" in kwargs:
                kwargs.pop("transport_security")
            elif "instructions" in kwargs:
                kwargs.pop("instructions")
            else:
                raise

    try:
        from mcp.types import ToolAnnotations
    except ImportError:
        ToolAnnotations = None  # noqa: N806

    for fn, read_only in TOOLS:
        name = fn.__name__
        assert name.startswith(PREFIX), name
        kw = {"name": name}
        if ToolAnnotations is not None:
            kw["annotations"] = ToolAnnotations(
                readOnlyHint=read_only, destructiveHint=False, idempotentHint=read_only, openWorldHint=not read_only
            )
        try:
            mcp.tool(**kw)(fn)
        except TypeError:
            mcp.tool(name=name)(fn)
    return mcp


def _setup_logging(data_dir, verbose=False):
    """stdio 전송은 stdout 이 프로토콜 채널이라 로그는 파일과 stderr 로만 보낸다."""
    os.makedirs(data_dir, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", "%Y-%m-%d %H:%M:%S")
    logger.setLevel(logging.DEBUG if verbose else logging.INFO)
    logger.handlers.clear()
    for handler in (WatchedFileHandler(os.path.join(data_dir, "naverpaper.log"), encoding="utf-8"), logging.StreamHandler(sys.stderr)):
        handler.setFormatter(fmt)
        logger.addHandler(handler)


def main():
    global _CONFIG_PATH
    parser = argparse.ArgumentParser(description="naverpaper MCP 서버")
    parser.add_argument("--transport", choices=["streamable-http", "sse", "stdio"], default="streamable-http")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=5934)
    parser.add_argument("-c", "--config", default=os.environ.get("NAVERPAPER_CONFIG") or nv.DEFAULT_CONFIG)
    parser.add_argument("--token", default="", help="인증 토큰 (기본: 환경변수 NAVERPAPER_MCP_TOKEN, 없으면 config.json 의 mcp_token)")
    parser.add_argument("--allowed-host", action="append", default=[], help="허용할 Host 헤더 (예: 192.168.0.10:5934), 여러 번 지정 가능")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    _CONFIG_PATH = os.path.abspath(args.config)
    try:
        cfg = _cfg()
    except Exception as e:  # noqa: BLE001
        print(f"[설정 오류] {e}", file=sys.stderr)
        return nv.EXIT_CONFIG
    _setup_logging(cfg["data_dir"], args.verbose)

    token = args.token or os.environ.get("NAVERPAPER_MCP_TOKEN", "") or cfg.get("mcp_token", "")
    if args.transport != "stdio" and args.host not in LOOPBACK_HOSTS and not token:
        print("[설정 오류] 외부 주소에 바인딩하려면 토큰이 필요합니다 (config.json 의 mcp_token, 환경변수 NAVERPAPER_MCP_TOKEN 또는 --token).", file=sys.stderr)
        return nv.EXIT_CONFIG

    try:
        mcp = build_server(args.host, args.port, args.allowed_host)
    except ImportError as e:
        print(f"[패키지 오류] {e}\npip install -r requirements.txt", file=sys.stderr)
        return nv.EXIT_CONFIG

    logger.info("naverpaper MCP 시작 transport=%s host=%s port=%s auth=%s", args.transport, args.host, args.port, "token" if token else "none")
    if args.transport == "stdio":
        mcp.run(transport="stdio")
        return nv.EXIT_OK

    import uvicorn

    app = mcp.streamable_http_app() if args.transport == "streamable-http" else mcp.sse_app()
    if token:
        app = BearerAuth(app, token)
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")
    return nv.EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
