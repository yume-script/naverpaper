#!/usr/bin/env python3
"""네이버 폐지 줍기 - 독립 실행(cron)용 실행기

원본: https://gitlab.com/sukajds/ff_naverpaper (flaskfarm 플러그인)
수집 엔진(source_naverpaper.py)은 그대로 쓰고, flaskfarm 연동부만 CLI로 대체했습니다.

사용법
  python3 naverpaper.py                 # 1회 수집 후 종료 (cron용, 기본)
  python3 naverpaper.py run
  python3 naverpaper.py total           # 적립 합계(오늘/7일/30일/전체) + 최근 실행 내역
  python3 naverpaper.py notify          # 적립 합계를 디스코드로 전송 (--test: 테스트 메시지)
  python3 naverpaper.py set-points 번호 아이디 적립액 [--yes]  # 잘못 기록된 실행의 적립액 수정
  python3 naverpaper.py clear-visits 아이디 [--all] [--yes]  # 방문 기록 삭제(링크 재시도)
  python3 naverpaper.py pincrux-test URL [--desktop]  # 쇼핑라이브 링크 하나로 브라우저 환경/영상 재생 진단
  python3 naverpaper.py status          # 계정별 쿠키 파일 상태 확인(만료일 기준)
  python3 naverpaper.py verify [아이디]  # 네이버에 실제 접속해 로그인 상태 확인
  python3 naverpaper.py login <아이디>   # 캡차 등으로 자동 로그인이 막힐 때 수동 로그인
  python3 naverpaper.py link <URL>      # 링크 하나만 수동 적립

종료 코드: 0 정상 / 1 수집 오류 / 2 이미 실행 중이라 건너뜀 / 3 설정 오류
"""
import argparse
import base64
import json
import logging
import os
import re
import sys
import sqlite3
import threading
import time
import traceback
import urllib.parse
import uuid
from collections import deque
from datetime import datetime, timedelta, timezone
from logging.handlers import RotatingFileHandler

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)

DEFAULT_CONFIG = os.path.join(BASE_DIR, "config.json")
DEFAULT_DATA_DIR = os.path.join(BASE_DIR, "data")

EXIT_OK, EXIT_ERROR, EXIT_LOCKED, EXIT_CONFIG = 0, 1, 2, 3

logger = logging.getLogger("naverpaper")


# ---------------------------------------------------------------- 설정
# 디스코드 웹훅 주소, 또는 앱스 스크립트 게이트웨이 주소 (웹 앱으로 배포한 /exec 주소)
DISCORD_WEBHOOK_RE = re.compile(r"^https://(?:discord|discordapp)\.com/api/webhooks/")
GATEWAY_URL_RE = re.compile(r"^https://script\.google\.com/(?:a/macros/[^/?#]+|macros)/s/[\w-]+/exec(?:\?.*)?$")


def is_gateway_url(url):
    return bool(GATEWAY_URL_RE.match(str(url or "")))


def _to_bool(value, default=False):
    if isinstance(value, bool):
        return value
    if value is None or value == "":
        return default
    return str(value).strip().lower() in ("true", "1", "on", "yes")


def _to_args(value):
    """브라우저 추가 인자: 리스트 또는 공백으로 구분한 문자열."""
    if isinstance(value, (list, tuple)):
        return [str(v).strip() for v in value if str(v).strip()]
    if isinstance(value, str):
        return value.split()
    return []


def _to_int(value, default):
    try:
        return int(str(value).strip())
    except Exception:
        return default


def load_config(path):
    if not os.path.exists(path):
        raise ValueError(f"설정 파일이 없습니다: {path}  (config.example.json 을 복사해서 만드세요)")
    with open(path, "r", encoding="utf-8") as f:
        cfg = json.load(f)

    accounts = []
    for item in cfg.get("accounts", []):
        user_id = str(item.get("user_id") or "").strip()
        if not user_id:
            continue
        accounts.append(
            {
                "user_id": user_id,
                "password": str(item.get("password") or "").strip(),
                "keep_login": _to_bool(item.get("keep_login"), True),
                "nid_aut": str(item.get("nid_aut") or "").strip(),
                "nid_ses": str(item.get("nid_ses") or "").strip(),
                "nid_jst": str(item.get("nid_jst") or "").strip(),
            }
        )
    if not accounts:
        raise ValueError("config.json 의 accounts 가 비어 있습니다")

    webhook = str(os.environ.get("NAVERPAPER_DISCORD_WEBHOOK") or cfg.get("discord_webhook_url") or "").strip()
    if webhook and not (DISCORD_WEBHOOK_RE.match(webhook) or is_gateway_url(webhook)):
        raise ValueError(
            "discord_webhook_url 은 디스코드 웹훅 주소(https://discord.com/api/webhooks/...) 또는 "
            "앱스 스크립트 게이트웨이 주소(https://script.google.com/macros/s/.../exec?parentId=..&threadId=..) 여야 합니다"
        )
    gateway_key = str(os.environ.get("NAVERPAPER_DISCORD_GATEWAY_KEY") or cfg.get("discord_gateway_key") or "").strip()
    if webhook and is_gateway_url(webhook) and gateway_key and not re.search(r"[?&]key=", webhook):
        # 게이트웨이가 key 를 요구하는 경우: 주소에 key 를 붙여 두면 이후 모든 전송이 그대로 인증된다 (주소는 로그에 남기지 않는다)
        webhook += ("&" if "?" in webhook else "?") + "key=" + urllib.parse.quote(gateway_key, safe="")
    notify_mode = str(cfg.get("discord_notify_mode") or "always").strip().lower()
    if notify_mode not in ("always", "earned"):
        raise ValueError('discord_notify_mode 는 "always" 또는 "earned" 만 가능합니다')

    proxy_url = str(cfg.get("proxy_url") or "").strip()
    proxy_all = _to_bool(cfg.get("proxy_enabled"), False)
    reward_enabled = _to_bool(cfg.get("proxy_reward_enabled"), proxy_all)
    login_enabled = _to_bool(cfg.get("proxy_login_enabled"), False)

    return {
        "accounts": accounts,
        "reward_proxy_url": proxy_url if reward_enabled else "",
        "login_proxy_url": proxy_url if login_enabled else "",
        "no_paper_record": _to_bool(cfg.get("no_paper_record"), True),
        "keep_campaign_days": _to_int(cfg.get("keep_campaign_days"), 60),
        "keep_user_days": _to_int(cfg.get("keep_user_days"), 7),
        "data_dir": os.path.abspath(os.path.expanduser(cfg.get("data_dir") or DEFAULT_DATA_DIR)),
        "discord_webhook_url": webhook,
        "discord_notify_mode": notify_mode,
        "discord_mask_ids": _to_bool(cfg.get("discord_mask_ids"), True),
        "mcp_token": str(cfg.get("mcp_token") or "").strip(),
        "browser_channel": str(cfg.get("browser_channel") or "").strip(),
        "browser_headless": _to_bool(cfg.get("browser_headless"), True),
        "browser_args": _to_args(cfg.get("browser_args")),
        "pincrux_diagnostic": _to_bool(cfg.get("pincrux_diagnostic"), True),
        "pincrux_click": _to_bool(cfg.get("pincrux_click"), True),
        "pincrux_wait_seconds": max(0, _to_int(cfg.get("pincrux_wait_seconds"), 5)),
        "stall_alert_minutes": max(0, _to_int(cfg.get("stall_alert_minutes"), 15)),
        "max_run_minutes": max(0, _to_int(cfg.get("max_run_minutes"), 60)),
    }


# ---------------------------------------------------------------- 로깅 / 락
def setup_logging(data_dir, verbose=False):
    os.makedirs(data_dir, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", "%Y-%m-%d %H:%M:%S")
    logger.setLevel(logging.DEBUG if verbose else logging.INFO)
    logger.handlers.clear()
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    logger.addHandler(sh)
    fh = RotatingFileHandler(
        os.path.join(data_dir, "naverpaper.log"), maxBytes=2 * 1024 * 1024, backupCount=3, encoding="utf-8"
    )
    fh.setFormatter(fmt)
    logger.addHandler(fh)


def _parse_lock_info(text):
    parts = str(text or "").split()
    info = {}
    try:
        if parts:
            info["pid"] = int(parts[0])
        if len(parts) > 1:
            info["started"] = float(parts[1])
    except ValueError:
        pass
    return info


class RunLock:
    """cron 중복 실행 방지용 파일 락 (리눅스/맥). 락을 못 잡으면 acquire() 가 False 이고, holder 에 락을 잡은 쪽의 pid/시작 시각이 들어 있다."""

    def __init__(self, path):
        self.path = path
        self.fp = None
        self.holder = {}

    def acquire(self):
        self.holder = {}
        try:
            import fcntl
        except ImportError:  # Windows 등: 락 없이 진행
            return True
        self.fp = open(self.path, "a+")  # 락을 못 잡아도 보유자 정보를 지우지 않도록 truncate 하지 않는다
        try:
            fcntl.flock(self.fp, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.fp.seek(0)
            self.fp.truncate()
            self.fp.write(f"{os.getpid()} {int(time.time())}")
            self.fp.flush()
            return True
        except OSError:
            try:
                self.fp.seek(0)
                self.holder = _parse_lock_info(self.fp.read())
            except Exception:
                self.holder = {}
            self.fp.close()
            self.fp = None
            return False

    def release(self):
        if self.fp:
            try:
                self.fp.close()
            except Exception:
                pass
            self.fp = None


# ---------------------------------------------------------------- 쿠키 시드
def apply_engine_settings(np, cfg):
    """엔진 설정 적용: 브라우저 종류/화면 유무/추가 인자, 쇼핑라이브(live-view) 클릭·대기·진단."""
    configure_browser = getattr(np, "configure_browser", None)
    if configure_browser is not None:
        configure_browser(channel=cfg.get("browser_channel", ""), headless=cfg.get("browser_headless", True), args=cfg.get("browser_args", []))
        logger.info(
            "브라우저 설정: 종류=%s, 화면=%s%s",
            cfg.get("browser_channel") or "Playwright 내장 Chromium",
            "없음(headless)" if cfg.get("browser_headless", True) else "있음(xvfb-run 필요)",
            f", 추가 인자={cfg['browser_args']}" if cfg.get("browser_args") else "",
        )
    configure = getattr(np, "configure_pincrux", None)
    if configure is None:
        return
    configure(click=cfg.get("pincrux_click", True), wait_seconds=cfg.get("pincrux_wait_seconds", 5), diagnostic=cfg.get("pincrux_diagnostic", True))
    wait = cfg.get("pincrux_wait_seconds", 5)
    logger.info("쇼핑라이브 설정: 마우스 클릭=%s, 대기=%s", "켬" if cfg.get("pincrux_click", True) else "끔", f"{wait}초(요구 시간이 더 길면 그 시간)" if wait else "요구 시간+3초")


def seed_cookies(np, cfg, accounts_cfg):
    """config.json 에 NID_AUT/NID_SES/NID_JST 가 적혀 있고 저장된 쿠키가 유효하지 않으면 쿠키 파일을 만든다."""
    cookie_dir = os.path.join(cfg["data_dir"], "cookies")
    os.makedirs(cookie_dir, exist_ok=True)
    for acc in accounts_cfg:
        if not (acc["nid_aut"] or acc["nid_ses"] or acc["nid_jst"]):
            continue
        ok, _ = np.check_cookie_status(acc["user_id"], cookie_dir)
        if ok:
            continue
        expires = int(time.time()) + 180 * 24 * 60 * 60
        cookies = []
        for name, key in (("NID_AUT", "nid_aut"), ("NID_SES", "nid_ses"), ("NID_JST", "nid_jst")):
            if acc[key]:
                cookies.append(
                    {
                        "name": name,
                        "value": acc[key],
                        "domain": ".naver.com",
                        "path": "/",
                        "expires": expires,
                        "httpOnly": True,
                        "secure": True,
                        "sameSite": "Lax",
                    }
                )
        np.save_storage_state(cookie_dir, acc["user_id"], {"cookies": cookies, "origins": []})
        logger.info("%s: config 의 NID 쿠키로 쿠키 파일 생성", acc["user_id"])


def build_accounts(np, accounts_cfg):
    return [np.AccountConfig(user_id=a["user_id"], password=a["password"], keep_login=a["keep_login"]) for a in accounts_cfg]


# ---------------------------------------------------------------- 적립 집계
def _fmt_point(value):
    return f"{int(value or 0):,}"


def _db_exists(db_path):
    return os.path.exists(db_path) and os.path.getsize(db_path) > 0


def _new_entry():
    return {
        "today": 0, "d7": 0, "d30": 0, "all": 0,  # 적립
        "used_today": 0, "used_d7": 0, "used_d30": 0, "used_all": 0,  # 사용(잔액 감소로 추정, 소멸 포함)
        "balance": None, "balance_at": None,  # 마지막으로 확인한 잔액과 그 시각
    }


def points_report(db_path, accounts=None):
    """DB(run_account_summary)에서 계정별 오늘/7일/30일/전체 적립·사용 합계와 마지막으로 확인한 잔액을 구한다.

    적립(estimated_points)은 이벤트적립 증가분(읽지 못하면 잔액 차이), 사용(used_points)은 잔액이 줄어든 만큼의 추정값입니다.
    예전 DB에는 used_points / balance_after 열이 없을 수 있어 있을 때만 읽습니다.
    """
    now = datetime.now()
    cutoffs = {
        "today": now.replace(hour=0, minute=0, second=0, microsecond=0),
        "d7": now - timedelta(days=7),
        "d30": now - timedelta(days=30),
    }
    conn = sqlite3.connect(db_path)
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(run_account_summary)")}
        used_col = "COALESCE(used_points, 0)" if "used_points" in cols else "0"
        bal_col = "balance_after" if "balance_after" in cols else "NULL"
        rows = conn.execute(
            f"SELECT user_id, created_at, COALESCE(estimated_points, 0), {used_col}, {bal_col} FROM run_account_summary ORDER BY id"
        ).fetchall()
        first = conn.execute("SELECT MIN(started_at) FROM run_history").fetchone()[0]
        run_count = conn.execute("SELECT COUNT(*) FROM run_history").fetchone()[0]
    finally:
        conn.close()

    stats = {}
    for user_id, created_at, points, used, balance in rows:
        entry = stats.setdefault(user_id, _new_entry())
        try:
            ts = datetime.fromisoformat(str(created_at))
        except Exception:
            ts = None
        used = used or 0
        entry["all"] += points
        entry["used_all"] += used
        if ts:
            for key, cutoff in cutoffs.items():
                if ts >= cutoff:
                    entry[key] += points
                    entry["used_" + key] += used
        if balance is not None:  # id 순으로 읽으므로 마지막 값이 가장 최근
            entry["balance"] = int(balance)
            entry["balance_at"] = str(created_at)[:16]
    if accounts:
        # 설정에서 빠진 옛 계정 중 적립/사용 기록이 한 번도 없던 계정은 합계 표에 나오지 않게 한다 (기록이 있으면 유지)
        for user_id in [u for u in stats if u not in accounts and stats[u]["all"] == 0 and stats[u]["used_all"] == 0]:
            del stats[user_id]
        for user_id in accounts:
            stats.setdefault(user_id, _new_entry())
    return stats, first, run_count


def print_points_report(db_path, accounts=None, title="적립 합계"):
    if not _db_exists(db_path):
        print("아직 수집 기록이 없습니다.")
        return
    stats, first, run_count = points_report(db_path, accounts)
    total = {"today": 0, "d7": 0, "d30": 0, "all": 0, "used_d30": 0}
    total_balance, any_balance, any_used = 0, False, False
    lines = [f"== {title} (기록 시작: {str(first)[:10] if first else '-'}, 총 {run_count}회 실행) =="]
    lines.append(f"{'계정':<20}{'오늘':>10}{'7일':>10}{'30일':>10}{'전체':>12}{'사용(30일)':>12}{'잔액':>10}")
    for user_id in sorted(stats):
        e = stats[user_id]
        for k in total:
            total[k] += e.get(k, 0)
        any_used = any_used or e.get("used_all", 0) > 0
        balance = e.get("balance")
        if balance is not None:
            any_balance = True
            total_balance += balance
        lines.append(
            f"{user_id:<20}{_fmt_point(e['today']):>10}{_fmt_point(e['d7']):>10}{_fmt_point(e['d30']):>10}{_fmt_point(e['all']):>12}"
            f"{_fmt_point(e.get('used_d30', 0)):>12}{(_fmt_point(balance) if balance is not None else '-'):>10}"
        )
    if len(stats) > 1:
        lines.append(
            f"{'합계':<20}{_fmt_point(total['today']):>10}{_fmt_point(total['d7']):>10}{_fmt_point(total['d30']):>10}{_fmt_point(total['all']):>12}"
            f"{_fmt_point(total['used_d30']):>12}{(_fmt_point(total_balance) if any_balance else '-'):>10}"
        )
    if any_used:
        lines.append("* 사용은 잔액이 줄어든 만큼으로 추정한 값이며 소멸된 포인트가 포함될 수 있습니다. 잔액은 마지막 수집 때 확인한 값입니다.")
    text = "\n".join(lines)
    print(text)
    for line in lines:
        logger.info("%s", line)


def print_recent_runs(db_path, limit):
    if not _db_exists(db_path):
        return
    conn = sqlite3.connect(db_path)
    try:
        runs = conn.execute(
            "SELECT id, started_at, status, visited_url_count, estimated_points "
            "FROM run_history ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        print(f"\n== 최근 {len(runs)}회 실행 ==")
        print(f"{'번호':>5} {'시작 시각':<20}{'상태':<12}{'방문':>6}{'적립':>10}   계정별")
        for run_id, started, status, visited, points in runs:
            cols = {r[1] for r in conn.execute("PRAGMA table_info(run_account_summary)")}
            used_col = "COALESCE(used_points,0)" if "used_points" in cols else "0"
            per = conn.execute(
                f"SELECT user_id, COALESCE(estimated_points,0), {used_col} FROM run_account_summary WHERE run_id=? ORDER BY id",
                (run_id,),
            ).fetchall()
            detail = ", ".join(f"{u} +{_fmt_point(p)}" + (f" (사용 -{_fmt_point(x)})" if x else "") for u, p, x in per)
            print(f"{run_id:>5} {str(started)[:19]:<20}{str(status):<12}{visited or 0:>6}{_fmt_point(points):>10}   {detail}")
    finally:
        conn.close()


# ---------------------------------------------------------------- 디스코드 알림
def _mask_id(user_id):
    user_id = str(user_id)
    return (user_id[:3] if len(user_id) > 3 else user_id[:1]) + "•••"


def _gateway_ok(text):
    """게이트웨이 응답 본문({"ok": true, ...})을 확인한다. 배포 설정 문제로 로그인 화면(HTML)이 오면 실패로 본다."""
    try:
        data = json.loads(text)
    except ValueError:
        logger.error("게이트웨이 응답이 JSON 이 아닙니다 (배포 액세스가 '모든 사용자'인지, 최신 버전으로 배포했는지 확인): %s", text[:100].replace("\n", " "))
        return False
    if isinstance(data, dict) and data.get("ok") is True:
        return True
    error = data.get("error") if isinstance(data, dict) else data
    status = data.get("status") if isinstance(data, dict) else ""
    logger.error("게이트웨이 전송 실패 %s: %s", status or "", str(error)[:200])
    return False


def _discord_post(webhook_url, body, content_type, retries=2):
    """웹훅 POST 공통 처리 (429 재시도). 성공 True. 토큰이 들어 있으므로 URL 은 로그에 남기지 않는다."""
    import urllib.error
    import urllib.request

    for attempt in range(retries + 1):
        req = urllib.request.Request(
            webhook_url,
            data=body,
            method="POST",
            headers={"Content-Type": content_type, "User-Agent": "naverpaper/1.0"},
        )
        gateway = is_gateway_url(webhook_url)
        try:
            # 앱스 스크립트는 POST 에 302 로 응답한다 (urllib 이 결과 주소를 GET 으로 따라가 응답 JSON 을 읽는다. 전송은 첫 요청에서 이미 처리됨)
            with urllib.request.urlopen(req, timeout=40 if gateway else 20) as resp:
                if not 200 <= resp.status < 300:
                    return False
                if gateway:
                    return _gateway_ok(resp.read().decode("utf-8", "replace"))
                return True
        except urllib.error.HTTPError as e:
            err = ""
            try:
                err = e.read().decode("utf-8", "replace")
            except Exception:
                pass
            if e.code == 429 and attempt < retries:
                wait = 1.0
                try:
                    wait = float(json.loads(err).get("retry_after", 1.0))
                except Exception:
                    pass
                time.sleep(min(wait, 30))
                continue
            logger.error("디스코드 전송 실패 HTTP %s: %s", e.code, err[:200])
            return False
        except Exception as e:
            logger.error("디스코드 전송 실패: %s", e)
            if attempt < retries:
                time.sleep(2)
                continue
            return False
    return False


def send_discord(webhook_url, payload, retries=2):
    """JSON 메시지 전송."""
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    return _discord_post(webhook_url, data, "application/json", retries)


def send_discord_image(webhook_url, payload, image_bytes, filename="captcha.png", retries=2):
    """이미지 첨부 전송. 디스코드 웹훅은 multipart, 앱스 스크립트 게이트웨이는 JSON(base64 files)로 보낸다."""
    if is_gateway_url(webhook_url):
        body = dict(payload)
        body["files"] = [{"name": filename, "contentType": "image/png", "base64": base64.b64encode(image_bytes).decode("ascii")}]
        return _discord_post(webhook_url, json.dumps(body, ensure_ascii=False).encode("utf-8"), "application/json", retries)
    boundary = "----naverpaper" + uuid.uuid4().hex
    parts = [
        f'--{boundary}\r\nContent-Disposition: form-data; name="payload_json"\r\n'
        f"Content-Type: application/json\r\n\r\n".encode("utf-8"),
        json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        b"\r\n",
        f'--{boundary}\r\nContent-Disposition: form-data; name="files[0]"; filename="{filename}"\r\n'
        f"Content-Type: image/png\r\n\r\n".encode("utf-8"),
        image_bytes,
        b"\r\n",
        f"--{boundary}--\r\n".encode("utf-8"),
    ]
    return _discord_post(webhook_url, b"".join(parts), f"multipart/form-data; boundary={boundary}", retries)


def notify_captcha(cfg, user_id, image_bytes, hint="", status=""):
    """캡차 화면 이미지를 디스코드로 전송. 웹훅이 없으면 False."""
    url = cfg.get("discord_webhook_url")
    if not url or not image_bytes:
        return False
    name = _mask_id(user_id) if cfg.get("discord_mask_ids", True) else user_id
    description = f"계정 **{name}**\n로그인을 시도한 뒤의 화면입니다. 이미지에 캡차 글자가 보이면 입력해 주세요."
    if status:
        description += f"\n상태: {status}"
    if hint:
        description += f"\n{hint}"
    payload = {
        "embeds": [
            {
                "title": "네이버 로그인 캡차",
                "description": description,
                "color": 0xF1C40F,
                "image": {"url": "attachment://captcha.png"},
                "footer": {"text": "naverpaper"},
                "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z"),
            }
        ],
        "attachments": [{"id": 0, "filename": "captcha.png"}],
    }
    return send_discord_image(url, payload, image_bytes, "captcha.png")


def build_embed(cfg, title, stats, run_points=None, error_message="", notes=None, alert=False, run_used=None):
    """run_points: {계정: 이번 실행 적립}, run_used: {계정: 이번 실행에서 확인된 사용 금액}. run_points 가 None 이면 합계 요약(notify)용."""
    lines = []
    totals = {"today": 0, "d7": 0, "d30": 0, "all": 0, "used_d30": 0}
    total_balance, any_balance = 0, False
    for user_id in sorted(stats):
        e = stats[user_id]
        for k in totals:
            totals[k] += e.get(k, 0)
        name = _mask_id(user_id) if cfg["discord_mask_ids"] else user_id
        if run_points is not None:
            parts = [f"이번 +{_fmt_point(run_points.get(user_id, 0))}"]
            used = (run_used or {}).get(user_id, 0)
            if used > 0:
                parts.append(f"사용 -{_fmt_point(used)}")
            parts += [f"오늘 {_fmt_point(e['today'])}", f"전체 {_fmt_point(e['all'])}"]
        else:
            parts = [f"오늘 {_fmt_point(e['today'])}", f"7일 {_fmt_point(e['d7'])}", f"전체 {_fmt_point(e['all'])}"]
            if e.get("used_d30", 0) > 0:
                parts.append(f"사용(30일) -{_fmt_point(e['used_d30'])}")
        balance = e.get("balance")
        if balance is not None:
            any_balance = True
            total_balance += balance
            parts.append(f"잔액 {_fmt_point(balance)}")
        lines.append(f"**{name}**  " + " · ".join(parts))

    run_total = sum(run_points.values()) if run_points is not None else None
    run_used_total = sum((run_used or {}).values())
    if error_message:
        color = 0xE74C3C
        head = f"수집 중 오류: {error_message[:300]}"
    elif alert:
        color = 0xE74C3C
        head = "**계정 확인이 필요합니다.**"
    elif run_total is not None and run_total > 0:
        color = 0x2ECC71
        head = f"이번 실행 적립 **+{_fmt_point(run_total)}**"
    else:
        color = 0x95A5A6
        head = f"이번 실행 적립 **+{_fmt_point(run_total)}**" if run_total is not None else "적립 합계"
    if run_total is not None and run_used_total > 0 and not error_message and not alert:
        head += f" · 사용 -{_fmt_point(run_used_total)}"

    description = head + "\n\n" + "\n".join(lines)
    if notes:
        description += "\n\n" + "\n".join(notes)
    description = description[:4000]
    fields = [
        {"name": "오늘", "value": _fmt_point(totals["today"]), "inline": True},
        {"name": "30일", "value": _fmt_point(totals["d30"]), "inline": True},
        {"name": "전체", "value": _fmt_point(totals["all"]), "inline": True},
    ]
    if totals["used_d30"] > 0:
        fields.append({"name": "사용(30일)", "value": "-" + _fmt_point(totals["used_d30"]), "inline": True})
    if any_balance:
        fields.append({"name": "현재 잔액", "value": _fmt_point(total_balance), "inline": True})
    return {
        "title": title,
        "description": description,
        "color": color,
        "fields": fields,
        "footer": {"text": "naverpaper"},
        "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z"),
    }


def _clean_text(text, cfg):
    """디스코드로 보내는 문구에서 계정 아이디는 마스킹하고 비밀번호는 가린다."""
    text = str(text or "")
    for acc in cfg.get("accounts", []):
        if acc.get("password") and len(acc["password"]) >= 4:
            text = text.replace(acc["password"], "***")
    if cfg.get("discord_mask_ids", True):
        for acc in cfg.get("accounts", []):
            if acc.get("user_id"):
                text = text.replace(acc["user_id"], _mask_id(acc["user_id"]))
    return text


LOGIN_CAUSES = [
    ("reason=captcha", "캡차(자동입력 방지 문자) 요구", "터미널에서 `login 아이디` 를 실행하세요. 캡차 이미지가 디스코드로 전송됩니다."),
    ("reason=wrong_credentials", "아이디 또는 비밀번호 불일치", "config.json 의 아이디/비밀번호를 확인하세요. 네이버에서 비밀번호를 바꿨다면 새 비밀번호로 수정하세요."),
    ("reason=verification", "추가 인증 요구(2단계 인증, 본인 확인, 새 환경 확인)", "PC 브라우저에서 같은 계정으로 로그인해 인증을 마친 뒤 `login 아이디` 를 실행하세요."),
    ("reason=account_locked", "계정 보호조치 또는 이용 제한", "네이버에 직접 로그인해 계정 상태 안내를 확인하세요."),
    ("reason=dormant", "휴면 계정", "네이버에 직접 로그인해 휴면 해제를 하세요."),
    ("password empty", "비밀번호가 설정되어 있지 않음", "config.json 의 해당 계정에 password 를 입력하세요."),
    ("login page timeout", "로그인 페이지 접속 시간 초과", "서버 네트워크, 프록시, 부하를 확인하세요. 다음 실행에서 다시 시도됩니다."),
    ("cooldown", "최근 30분 안에 재로그인을 이미 시도해 건너뜀", "수동 로그인하거나 30분 뒤 다음 실행을 기다리세요."),
    ("waiting for locator", "로그인 화면 구조 변경 의심(입력칸을 찾지 못함)", "로그의 `login diagnostic` 줄을 확인하고 알려 주세요."),
    ("locator.", "로그인 화면 구조 변경 의심(입력칸을 찾지 못함)", "로그의 `login diagnostic` 줄을 확인하고 알려 주세요."),
    ("net::err", "네트워크 오류", "서버 네트워크와 프록시 설정을 확인하세요."),
    ("timeout", "접속 시간 초과", "서버 부하와 네트워크를 확인하세요. 다음 실행에서 다시 시도됩니다."),
    ("did not produce required cookies", "로그인 실패(원인을 특정하지 못함)", "로그의 `login diagnostic - failed final_url=... body=...` 줄을 확인하고 `login 아이디` 로 수동 로그인해 보세요."),
]


def describe_login_cause(raw):
    """엔진이 남긴 실패 사유 원문 -> (원인, 조치). 알 수 없으면 일반 안내."""
    low = str(raw or "").lower()
    for key, cause, action in LOGIN_CAUSES:
        if key in low:
            return cause, action
    return "원인을 알 수 없음", "로그를 확인하고 `login 아이디` 로 수동 로그인해 보세요."


def _cause_text(raw, cfg):
    cause, action = describe_login_cause(raw)
    text = f" 원인: {cause}. 조치: {action}"
    raw = _clean_text(raw, cfg).strip()
    if raw:
        text += f" (원문: `{raw[:150]}`)"
    return text


def account_issues(result, cfg):
    """계정별 로그인/세션 문제를 (problems, recovered) 로 돌려준다. 각 항목은 (계정 표시명, 안내 문구). 문구에는 원인과 조치가 들어 있다."""
    problems, recovered = [], []
    for ar in getattr(result, "account_results", []) or []:
        uid = getattr(ar, "user_id", "")
        name = _mask_id(uid) if cfg.get("discord_mask_ids", True) else uid
        relogin = getattr(ar, "relogin", "") or ""
        raw = getattr(ar, "login_error", "") or ""
        if getattr(ar, "pay_terms_required", False):
            problems.append(
                (
                    name,
                    "네이버페이 이용약관 동의가 필요해 링크를 처리하지 않았습니다(방문 기록 없음). 원인: 이 계정은 네이버페이 약관에 아직 동의하지 않아 포인트 화면 대신 가입 화면이 나옵니다. "
                    "조치: 해당 계정으로 pay.naver.com 에 접속해 약관에 직접 동의한 뒤 다음 실행을 기다리세요.",
                )
            )
        elif getattr(ar, "session_expired", False):
            if relogin == "skipped":
                text = "네이버 세션이 만료되었지만 최근 30분 안에 재로그인을 이미 시도해 이번에는 건너뛰었습니다. 링크는 처리하지 않았습니다(방문 기록 없음)."
                raw = raw or "cooldown"
            elif relogin == "failed":
                text = "네이버 세션이 만료되었고 자동 재로그인에 실패했습니다. 링크는 처리하지 않았습니다(방문 기록 없음)."
            else:
                text = "네이버 세션이 만료되어 링크 처리를 중단했습니다(방문 기록 없음)."
            problems.append((name, text + _cause_text(raw, cfg)))
        elif getattr(ar, "login_failed", False):
            problems.append((name, "네이버 로그인에 실패했습니다." + _cause_text(raw, cfg)))
        elif relogin == "ok":
            recovered.append((name, "세션이 만료되어 자동으로 재로그인했습니다. 정상 처리되었습니다."))
    return problems, recovered


# ---------------------------------------------------------------- 실행 감시 (멈춤 / 비정상 종료 알림)
class ProgressTracker:
    """엔진 로그가 올라올 때마다 갱신되어 '마지막 진행 시각'과 최근 로그를 보관한다."""

    def __init__(self, keep=8):
        self._lock = threading.Lock()
        self.started = time.time()
        self.last = self.started
        self.lines = deque(maxlen=keep)

    def log(self, message):
        with self._lock:
            self.last = time.time()
            self.lines.append(f"{datetime.now():%H:%M:%S} {message}")

    def snapshot(self):
        with self._lock:
            return self.started, self.last, list(self.lines)


def _fmt_duration(seconds):
    seconds = int(max(0, seconds))
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours}시간 {minutes}분"
    if minutes:
        return f"{minutes}분 {secs}초"
    return f"{secs}초"


def _proc_state(pid):
    """리눅스 /proc 에서 프로세스 상태 문자를 읽는다. 알 수 없으면 빈 문자열."""
    try:
        with open(f"/proc/{int(pid)}/stat", encoding="utf-8") as f:
            return f.read().rsplit(") ", 1)[1].split()[0]
    except Exception:
        return ""


PROC_STATE_TEXT = {"R": "실행 중", "S": "대기 중(sleeping)", "D": "디스크/IO 대기", "T": "정지됨(Ctrl+Z 등)", "Z": "좀비", "t": "추적 정지"}


def _send_alert(cfg, title, description, color):
    url = cfg.get("discord_webhook_url")
    if not url:
        return False
    embed = {
        "title": title,
        "description": description[:4000],
        "color": color,
        "footer": {"text": "naverpaper"},
        "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z"),
    }
    return send_discord(url, {"embeds": [embed]})


def _log_block(lines, cfg):
    if not lines:
        return ""
    body = "\n".join(_clean_text(x, cfg)[:180] for x in lines)[-1400:]
    return f"\n```\n{body}\n```"


ALERT_RED, ALERT_ORANGE, ALERT_GREEN = 0xE74C3C, 0xE67E22, 0x2ECC71


def notify_stall(cfg, kind, tracker, source="cron 실행", extra=""):
    """kind: stall(진행 멈춤) / overtime(실행 시간 초과) / resumed(진행 재개)"""
    started, last, lines = tracker.snapshot()
    now = time.time()
    elapsed, idle = now - started, now - last
    if kind == "stall":
        title = "네이버 폐지줍기 - 진행이 멈춘 것 같습니다"
        desc = (
            f"{source}에서 **{_fmt_duration(idle)}** 동안 새 로그가 없습니다. (시작 후 {_fmt_duration(elapsed)} 경과)\n"
            "느린 사이트를 수집 중일 수도 있지만 계속되면 프로세스를 확인하세요.\n"
            "`ps aux | grep -E \"naverpaper|chrom\" | grep -v grep`"
        )
        color = ALERT_RED
    elif kind == "overtime":
        title = "네이버 폐지줍기 - 실행 시간이 오래 걸립니다"
        desc = f"{source}이(가) 시작된 지 **{_fmt_duration(elapsed)}** 지났지만 아직 끝나지 않았습니다. 새 실행은 이 실행이 끝날 때까지 건너뜁니다."
        color = ALERT_ORANGE
    else:
        title = "네이버 폐지줍기 - 진행이 다시 시작되었습니다"
        desc = f"{source}의 로그가 다시 올라오기 시작했습니다. (시작 후 {_fmt_duration(elapsed)} 경과) 앞선 멈춤 알림은 일시적인 지연이었습니다."
        color = ALERT_GREEN
    if extra:
        desc += "\n" + extra
    desc += "\n최근 로그:" + _log_block(lines, cfg)
    return _send_alert(cfg, title, desc, color)


class RunWatchdog(threading.Thread):
    """수집이 도는 동안 별도 스레드로 감시한다. 진행(로그)이 일정 시간 없으면 '멈춤', 전체 시간이 너무 길면 '시간 초과'를 디스코드로 알린다."""

    def __init__(self, cfg, tracker, source="cron 실행", state_path=None):
        super().__init__(daemon=True, name="naverpaper-watchdog")
        self.cfg, self.tracker, self.source = cfg, tracker, source
        self.stall_seconds = cfg.get("stall_alert_minutes", 15) * 60
        self.max_seconds = cfg.get("max_run_minutes", 60) * 60
        self.interval = 30.0
        self.state_path = state_path
        self._stop_event = threading.Event()
        self.stalled = False
        self.overtime_alerted = False

    def run(self):
        while not self._stop_event.wait(self.interval):
            try:
                self._tick()
            except Exception:
                logger.exception("감시 스레드 오류")

    def _tick(self):
        now = time.time()
        started, last, lines = self.tracker.snapshot()
        self._write_state(started, lines)
        if self.stall_seconds and now - last >= self.stall_seconds and not self.stalled:
            self.stalled = True
            logger.warning("진행이 %s 동안 없습니다 (멈춤 의심)", _fmt_duration(now - last))
            notify_stall(self.cfg, "stall", self.tracker, self.source)
        elif self.stalled and self.stall_seconds and now - last < self.stall_seconds:
            self.stalled = False
            notify_stall(self.cfg, "resumed", self.tracker, self.source)
        if self.max_seconds and now - started >= self.max_seconds and not self.overtime_alerted:
            self.overtime_alerted = True
            logger.warning("실행 시간이 %s 를 넘었습니다", _fmt_duration(now - started))
            notify_stall(self.cfg, "overtime", self.tracker, self.source)

    def _write_state(self, started, lines):
        if not self.state_path:
            return
        try:
            with open(self.state_path, "w", encoding="utf-8") as f:
                json.dump({"pid": os.getpid(), "started": started, "updated": time.time(), "source": self.source, "lines": lines}, f, ensure_ascii=False)
        except OSError:
            pass

    def stop(self):
        self._stop_event.set()


def run_state_path(cfg):
    return os.path.join(cfg["data_dir"], "run_state.json")


def begin_run_state(cfg, tracker, source):
    """실행 시작 표시. 정상 종료하면 end_run_state 가 지우므로, 남아 있으면 비정상 종료다."""
    try:
        with open(run_state_path(cfg), "w", encoding="utf-8") as f:
            json.dump({"pid": os.getpid(), "started": tracker.started, "updated": time.time(), "source": source, "lines": []}, f, ensure_ascii=False)
    except OSError:
        pass


def end_run_state(cfg):
    try:
        os.remove(run_state_path(cfg))
    except OSError:
        pass


def finish_run(cfg, watchdog):
    """정상 종료 처리: 감시 스레드를 먼저 멈추고 기다린 뒤 상태 파일을 지운다 (스레드가 지운 파일을 다시 쓰는 경쟁을 막는다)."""
    watchdog.stop()
    if watchdog.is_alive():
        watchdog.join(5)
    end_run_state(cfg)


def check_previous_abnormal_exit(cfg):
    """이전 실행이 정상 종료되지 않았다면(서버 재부팅, 메모리 부족으로 강제 종료 등) 알린다. 락을 잡은 직후에 호출한다."""
    path = run_state_path(cfg)
    try:
        with open(path, encoding="utf-8") as f:
            prev = json.load(f)
    except (OSError, ValueError):
        return False
    if prev.get("pid") == os.getpid():
        return False
    try:
        os.remove(path)
    except OSError:
        pass
    started = prev.get("started")
    when = datetime.fromtimestamp(started).strftime("%m-%d %H:%M") if started else "알 수 없음"
    updated = prev.get("updated")
    last_seen = datetime.fromtimestamp(updated).strftime("%H:%M:%S") if updated else "알 수 없음"
    logger.warning("이전 실행(시작 %s, PID %s)이 정상 종료되지 않았습니다 (마지막 기록 %s)", when, prev.get("pid"), last_seen)
    desc = (
        f"이전 {prev.get('source', '실행')}(시작 {when}, PID {prev.get('pid')})이 끝나기 전에 사라졌습니다. 마지막으로 확인된 시각은 {last_seen} 입니다.\n"
        "서버 재부팅, 메모리 부족(OOM)으로 인한 강제 종료, 수동 종료 중 하나일 수 있습니다.\n"
        "`dmesg -T | grep -i -E \"killed process|out of memory\" | tail`"
        + "\n마지막 로그:" + _log_block(prev.get("lines") or [], cfg)
    )
    return _send_alert(cfg, "네이버 폐지줍기 - 이전 실행이 비정상 종료되었습니다", desc, ALERT_RED)


def notify_lock_held(cfg, lock, source="cron 실행"):
    """락을 못 잡았을 때: 잡고 있는 실행이 너무 오래됐다면(멈춤 의심) 알린다. 한 시간에 한 번만."""
    if not cfg.get("discord_webhook_url") or not (cfg.get("max_run_minutes") or cfg.get("stall_alert_minutes")):
        return False
    holder = getattr(lock, "holder", {}) or {}
    started = holder.get("started")
    if not started:
        try:
            started = os.path.getmtime(lock.path)
        except OSError:
            return False
    age = time.time() - started
    if age < (cfg.get("max_run_minutes") or 60) * 60:
        return False
    marker = os.path.join(cfg["data_dir"], "lock_alert.marker")
    try:
        if time.time() - os.path.getmtime(marker) < 3600:
            return False
    except OSError:
        pass
    pid = holder.get("pid")
    state = PROC_STATE_TEXT.get(_proc_state(pid), "") if pid else ""
    desc = (
        f"이전 실행{f'(PID {pid})' if pid else ''}이 **{_fmt_duration(age)}** 째 끝나지 않아 이번 {source}은 건너뛰었습니다."
        + (f"\n프로세스 상태: {state}" if state else "")
        + "\n멈춘 것이 맞다면 종료하세요: `kill " + (str(pid) if pid else "PID") + "` 후 `pkill -f chrom` 으로 남은 브라우저를 정리하면 다음 실행부터 정상으로 돌아옵니다."
    )
    ok = _send_alert(cfg, "네이버 폐지줍기 - 이전 실행이 끝나지 않아 건너뜀", desc, ALERT_RED)
    if ok:
        try:
            with open(marker, "w", encoding="utf-8") as f:
                f.write(str(int(time.time())))
        except OSError:
            pass
    return ok


def notify_crash(cfg, exc, tracker=None, source="cron 실행"):
    """수집 도중 예외로 중단됐을 때."""
    tb_lines = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__)).strip().splitlines()
    desc = f"{source}이(가) 예외로 중단되었습니다.\n`{_clean_text(type(exc).__name__ + ': ' + str(exc), cfg)[:300]}`"
    desc += "\n스택:" + _log_block(tb_lines[-6:], cfg)
    if tracker is not None:
        desc += "\n최근 로그:" + _log_block(tracker.snapshot()[2], cfg)
    return _send_alert(cfg, "네이버 폐지줍기 - 수집이 예외로 중단되었습니다", desc, ALERT_RED)


def balance_notes(result, cfg):
    """포인트 잔액을 믿기 어려운 계정 안내 문구 목록."""
    notes = []
    for ar in getattr(result, "account_results", []) or []:
        if getattr(ar, "balance_unreliable", False):
            uid = getattr(ar, "user_id", "")
            name = _mask_id(uid) if cfg.get("discord_mask_ids", True) else uid
            notes.append(f"[참고] **{name}** 포인트 잔액을 정확히 읽지 못해(읽기 실패 또는 0으로만 표시) 적립액이 실제와 다를 수 있습니다. 네이버페이 포인트 내역으로 확인하세요.")
    return notes


def notify_run(cfg, result, db_path):
    url = cfg["discord_webhook_url"]
    if not url:
        return
    is_error = result.status == "error"
    problems, recovered = account_issues(result, cfg)
    unreliable = balance_notes(result, cfg)
    if cfg["discord_notify_mode"] == "earned" and not is_error and not problems and not recovered and not unreliable and (result.estimated_points or 0) <= 0:
        logger.info("적립 0 이라 디스코드 알림은 생략합니다 (discord_notify_mode=earned)")
        return
    account_ids = [a["user_id"] for a in cfg["accounts"]]
    stats, _first, _count = points_report(db_path, account_ids)
    run_points = {uid: 0 for uid in account_ids}
    for ar in getattr(result, "account_results", []) or []:
        run_points[getattr(ar, "user_id", "")] = int(getattr(ar, "estimated_points", 0) or 0)
    notes = [f"[경고] **{n}** {t}" for n, t in problems] + [f"[알림] **{n}** {t}" for n, t in recovered] + unreliable
    title = "네이버 폐지줍기 - 계정 확인 필요" if problems else "네이버 폐지줍기 결과"
    run_used = {uid: 0 for uid in account_ids}
    for ar in getattr(result, "account_results", []) or []:
        run_used[getattr(ar, "user_id", "")] = int(getattr(ar, "used_points", 0) or 0)
    embed = build_embed(cfg, title, stats, run_points, result.message if is_error else "", notes=notes, alert=bool(problems), run_used=run_used)
    if send_discord(url, {"embeds": [embed]}):
        logger.info("디스코드 알림 전송 완료")


# ---------------------------------------------------------------- 명령
def cmd_run(np, cfg, args):
    lock = RunLock(os.path.join(cfg["data_dir"], "naverpaper.lock"))
    if not lock.acquire():
        logger.warning("이미 수집이 실행 중입니다. 이번 실행은 건너뜁니다.")
        try:
            notify_lock_held(cfg, lock)
        except Exception:
            logger.exception("디스코드 알림 실패")
        return EXIT_LOCKED
    tracker = ProgressTracker()
    watchdog = RunWatchdog(cfg, tracker, "cron 실행", state_path=run_state_path(cfg))
    try:
        try:
            check_previous_abnormal_exit(cfg)
        except Exception:
            logger.exception("이전 실행 상태 확인 실패")
        begin_run_state(cfg, tracker, "cron 실행")
        watchdog.start()
        apply_engine_settings(np, cfg)
        seed_cookies(np, cfg, cfg["accounts"])
        run_cfg = np.RunConfig(
            db_path=os.path.join(cfg["data_dir"], "naverpaper.sqlite"),
            cookie_dir=os.path.join(cfg["data_dir"], "cookies"),
            accounts=build_accounts(np, cfg["accounts"]),
            reward_proxy_url=cfg["reward_proxy_url"],
            login_proxy_url=cfg["login_proxy_url"],
            no_paper_record=cfg["no_paper_record"],
            keep_campaign_days=cfg["keep_campaign_days"],
            keep_user_days=cfg["keep_user_days"],
        )
        def engine_log(message):
            logger.info("%s", message)
            tracker.log(message)

        try:
            result = np.run_sync(run_cfg, log=engine_log)
        except KeyboardInterrupt:
            finish_run(cfg, watchdog)  # 사용자가 직접 중단한 것은 비정상 종료로 보지 않는다
            raise
        except Exception as e:
            logger.exception("수집 중 예외")
            try:
                notify_crash(cfg, e, tracker)
            except Exception:
                logger.exception("디스코드 알림 실패")
            finish_run(cfg, watchdog)  # 이미 알렸으므로 다음 실행에서 '비정상 종료'로 중복 알리지 않는다
            raise
        logger.info(
            "수집 종료 status=%s 수집URL=%s 건너뜀=%s 방문=%s 예상포인트=%s",
            result.status,
            result.collected_url_count,
            result.skipped_url_count,
            result.visited_url_count,
            result.estimated_points,
        )
        for ar in getattr(result, "account_results", []) or []:
            logger.info(
                "  계정 %s 건너뜀=%s 방문=%s 적립=+%s",
                getattr(ar, "user_id", ""),
                getattr(ar, "skipped_url_count", 0),
                getattr(ar, "visited_url_count", 0),
                getattr(ar, "estimated_points", 0),
            )
        logger.info("이번 실행 적립: +%s", _fmt_point(result.estimated_points))
        _problems, _recovered = account_issues(result, cfg)
        for n, t in _problems:
            logger.warning("%s %s", n, t)
        for n, t in _recovered:
            logger.info("%s %s", n, t)
        for note in balance_notes(result, cfg):
            logger.warning("%s", note.replace("**", ""))
        try:
            print_points_report(
                os.path.join(cfg["data_dir"], "naverpaper.sqlite"),
                [a["user_id"] for a in cfg["accounts"]],
            )
        except Exception:
            logger.exception("적립 합계 집계 실패")
        try:
            notify_run(cfg, result, os.path.join(cfg["data_dir"], "naverpaper.sqlite"))
        except Exception:
            logger.exception("디스코드 알림 실패")
        finish_run(cfg, watchdog)
        if result.status == "error":
            logger.error("수집 오류: %s", result.message)
            return EXIT_ERROR
        return EXIT_OK
    finally:
        watchdog.stop()
        lock.release()


def cmd_total(np, cfg, args):
    db_path = os.path.join(cfg["data_dir"], "naverpaper.sqlite")
    print_points_report(db_path, [a["user_id"] for a in cfg["accounts"]])
    print_recent_runs(db_path, args.runs)
    return EXIT_OK


def cmd_notify(np, cfg, args):
    url = cfg["discord_webhook_url"]
    if not url:
        logger.error("config.json 에 discord_webhook_url 이 없습니다")
        return EXIT_CONFIG
    if args.test:
        ok = send_discord(url, {"content": "naverpaper 테스트 메시지입니다."})
    else:
        db_path = os.path.join(cfg["data_dir"], "naverpaper.sqlite")
        stats, _first, _count = points_report(db_path, [a["user_id"] for a in cfg["accounts"]]) if _db_exists(db_path) else ({}, None, 0)
        if not stats:
            stats = {a["user_id"]: {"today": 0, "d7": 0, "d30": 0, "all": 0} for a in cfg["accounts"]}
        ok = send_discord(url, {"embeds": [build_embed(cfg, "네이버 폐지줍기 적립 합계", stats)]})
    if ok:
        logger.info("디스코드 전송 완료")
        return EXIT_OK
    return EXIT_ERROR


def cmd_verify(np, cfg, args):
    """쿠키 파일이 아니라 네이버에 실제로 접속해 로그인 상태를 확인한다."""
    targets = [a for a in cfg["accounts"] if not args.user_id or a["user_id"] == args.user_id]
    if not targets:
        logger.error("config.json 에 없는 계정입니다: %s", args.user_id)
        return EXIT_CONFIG
    cookie_dir = os.path.join(cfg["data_dir"], "cookies")
    labels = {"alive": "OK", "expired": "NG", "terms": "!!", "unknown": "??"}
    code = EXIT_OK
    for acc in targets:
        account = np.AccountConfig(user_id=acc["user_id"], password=acc["password"], keep_login=acc["keep_login"])
        state, detail = np.verify_session_sync(account, cookie_dir, cfg["reward_proxy_url"], log=lambda m: logger.info("%s", m))
        print(f"{acc['user_id']}: {labels.get(state, '??')}  {detail}")
        if state != "alive":
            code = EXIT_ERROR
    if code != EXIT_OK:
        print("NG 이면 `login 아이디` 로 수동 로그인하거나 다음 수집 때 자동 재로그인을 기다리세요.")
    return code


def cmd_set_points(np, cfg, args):
    """잘못 기록된 실행의 적립액을 바로잡는다 (예: 잔액을 잘못 읽어 전체 잔액이 한 번에 적립된 것으로 기록된 경우)."""
    import shutil

    db_path = os.path.join(cfg["data_dir"], "naverpaper.sqlite")
    if not _db_exists(db_path):
        logger.error("DB가 없습니다: %s", db_path)
        return EXIT_CONFIG
    if args.points < 0:
        logger.error("적립액은 0 이상이어야 합니다")
        return EXIT_CONFIG
    lock = RunLock(os.path.join(cfg["data_dir"], "naverpaper.lock"))
    if not lock.acquire():
        logger.error("수집이 실행 중입니다. 끝난 뒤 다시 실행하세요.")
        return EXIT_LOCKED
    try:
        conn = sqlite3.connect(db_path, timeout=10)
        try:
            row = conn.execute(
                "SELECT id, estimated_points FROM run_account_summary WHERE run_id=? AND user_id=?", (args.run_id, args.user_id)
            ).fetchone()
            if not row:
                logger.error("해당 실행/계정의 기록이 없습니다 (run_id=%s, 아이디=%s). `total` 의 번호와 아이디를 확인하세요.", args.run_id, args.user_id)
                return EXIT_CONFIG
            print(f"실행 {args.run_id} / {args.user_id}: 적립 {_fmt_point(row[1])} -> {_fmt_point(args.points)}")
            if not args.yes:
                if not sys.stdin.isatty():
                    logger.error("확인 입력이 필요합니다. 터미널에서 실행하거나 --yes 를 붙이세요.")
                    return EXIT_ERROR
                if input("이 값으로 바꿀까요? (y/N) ").strip().lower() not in ("y", "yes"):
                    print("취소했습니다.")
                    return EXIT_OK
            backup = f"{db_path}.bak-{datetime.now():%Y%m%d-%H%M%S}"
            shutil.copy2(db_path, backup)
            conn.execute("UPDATE run_account_summary SET estimated_points=? WHERE id=?", (args.points, row[0]))
            conn.execute(
                "UPDATE run_history SET estimated_points=(SELECT COALESCE(SUM(estimated_points),0) FROM run_account_summary WHERE run_id=?) WHERE id=?",
                (args.run_id, args.run_id),
            )
            conn.commit()
            print(f"수정했습니다. (백업: {backup})")
            return EXIT_OK
        finally:
            conn.close()
    finally:
        lock.release()


def cmd_clear_visits(np, cfg, args):
    """계정의 방문 기록을 지워 링크를 다시 시도하게 한다 (적립 없이 방문 기록만 남은 경우: 로그인 풀림, 약관 미동의 등)."""
    import shutil

    db_path = os.path.join(cfg["data_dir"], "naverpaper.sqlite")
    if not _db_exists(db_path):
        logger.error("DB가 없습니다: %s", db_path)
        return EXIT_CONFIG
    lock = RunLock(os.path.join(cfg["data_dir"], "naverpaper.lock"))
    if not lock.acquire():
        logger.error("수집이 실행 중입니다. 끝난 뒤 다시 실행하세요.")
        return EXIT_LOCKED
    try:
        conn = sqlite3.connect(db_path, timeout=10)
        try:
            where, params = "user_id=?", [args.user_id]
            scope = "전체"
            if not args.all:
                where += " AND visited_at >= date('now','localtime')"
                scope = "오늘"
            count = conn.execute(f"SELECT COUNT(*) FROM url_visits WHERE {where}", params).fetchone()[0]
            print(f"{args.user_id}: {scope} 방문 기록 {count}건")
            if count == 0:
                return EXIT_OK
            if not args.yes:
                if not sys.stdin.isatty():
                    logger.error("확인 입력이 필요합니다. 터미널에서 실행하거나 --yes 를 붙이세요.")
                    return EXIT_ERROR
                if input("삭제할까요? (y/N) ").strip().lower() not in ("y", "yes"):
                    print("취소했습니다.")
                    return EXIT_OK
            backup = f"{db_path}.bak-{datetime.now():%Y%m%d-%H%M%S}"
            shutil.copy2(db_path, backup)
            conn.execute(f"DELETE FROM url_visits WHERE {where}", params)
            conn.commit()
            print(f"{count}건을 삭제했습니다. (백업: {backup}) 다음 실행에서 해당 링크를 다시 시도합니다.")
            return EXIT_OK
        finally:
            conn.close()
    finally:
        lock.release()


def format_probe(report, hints):
    env = report.get("env_after") or {}
    video = env.get("video") or {}
    lines = [
        f"브라우저: {report.get('channel')} / 화면 {'없음(headless)' if report.get('headless') else '있음'} / {'데스크톱' if report.get('mode') == 'desktop' else '모바일'} 모드",
        f"이동한 곳: {report.get('final_host')}   준비 대기 {report.get('ready_wait')}초, 계획 {report.get('planned')}초, 실제 {report.get('actual')}초",
        f"UA: {str(env.get('ua') or '')[:110]}",
        f"자동화 흔적: webdriver={env.get('webdriver')} window.chrome={env.get('chromeObj')} plugins={env.get('plugins')}",
        f"코덱: H.264={env.get('h264')} HLS={env.get('hls')}",
        f"창 상태: {env.get('visibility')} / 포커스 {env.get('focus')}",
        (
            f"영상: {env.get('videos')}개, paused={video.get('paused')} 재생시간={video.get('t')}초 readyState={video.get('ready')} 오류={video.get('err')} 출처={video.get('srcHost')}"
            if video
            else f"영상: {env.get('videos', 0)}개 (video 요소 없음)"
        ),
        f"완료 문구: 대기 전 {'있음' if report.get('completion_text_before') else '없음'} → 확인 {'됨' if report.get('completion_confirmed') else '안 됨'}",
        "",
        "진단:",
    ]
    lines += [f"  - {h}" for h in hints]
    return lines


def cmd_pincrux_test(np, cfg, args):
    """쇼핑라이브 링크 하나를 현재 브라우저 설정으로 열어 보고, 스크립트와 실제 크롬의 차이를 진단한다 (방문 기록 없음)."""
    url = str(args.url or "").strip()
    if not url.startswith("https://nsl.pincrux.com/"):
        logger.error("쇼핑라이브 주소(https://nsl.pincrux.com/live-view.html?...)만 시험할 수 있습니다")
        return EXIT_CONFIG
    accounts = [a for a in cfg["accounts"] if not args.account or a["user_id"] == args.account]
    if not accounts:
        logger.error("config.json 에 없는 계정입니다: %s", args.account)
        return EXIT_CONFIG
    acc = accounts[0]
    lock = RunLock(os.path.join(cfg["data_dir"], "naverpaper.lock"))
    if not lock.acquire():
        logger.error("수집이 실행 중입니다. 끝난 뒤 다시 실행하세요.")
        return EXIT_LOCKED
    try:
        apply_engine_settings(np, cfg)
        account = np.AccountConfig(user_id=acc["user_id"], password=acc["password"], keep_login=acc["keep_login"])
        report = np.pincrux_probe_sync(account, os.path.join(cfg["data_dir"], "cookies"), url, cfg["reward_proxy_url"], desktop=args.desktop, log=lambda m: logger.info("%s", m))
    finally:
        lock.release()
    if report.get("error"):
        print(f"시험 실패: {report['error']}")
        return EXIT_ERROR
    for line in format_probe(report, np.interpret_probe(report.get("env_after") or {})):
        print(line)
    return EXIT_OK


def cmd_status(np, cfg, args):
    cookie_dir = os.path.join(cfg["data_dir"], "cookies")
    for acc in cfg["accounts"]:
        ok, msg = np.check_cookie_status(acc["user_id"], cookie_dir)
        print(f"{acc['user_id']}: {'OK ' if ok else 'NG '} {msg}")
    print("(쿠키 파일의 만료일만 확인합니다. 네이버 서버의 실제 로그인 상태는 `verify` 로 확인하세요.)")
    return EXIT_OK


def _shot_bytes(data_url):
    raw = data_url.split(",", 1)[1] if "," in data_url else data_url
    return base64.b64decode(raw)


def _save_screenshot(data_url, path):
    with open(path, "wb") as f:
        f.write(_shot_bytes(data_url))


def cmd_login(np, cfg, args):
    target = next((a for a in cfg["accounts"] if a["user_id"] == args.user_id), None)
    if not target:
        logger.error("config.json 에 없는 계정입니다: %s", args.user_id)
        return EXIT_CONFIG
    if not target["password"]:
        logger.error("비밀번호가 비어 있습니다. config.json 에 NID 쿠키를 직접 넣어주세요.")
        return EXIT_CONFIG

    lock = RunLock(os.path.join(cfg["data_dir"], "naverpaper.lock"))
    if not lock.acquire():
        logger.warning("수집이 실행 중입니다. 끝난 뒤 다시 로그인하세요.")
        return EXIT_LOCKED
    try:
        cookie_dir = os.path.join(cfg["data_dir"], "cookies")
        account = np.AccountConfig(user_id=target["user_id"], password=target["password"], keep_login=target["keep_login"])
        log = lambda m: logger.info("%s", m)  # noqa: E731

        result = np.open_login_screen_sync(account, cookie_dir, cfg["login_proxy_url"], log=log)
        if not result.get("cookies") and result.get("screenshot"):
            # 처음 열린 로그인 화면에는 캡차가 없다(캡차는 로그인을 시도한 뒤에 나타남).
            # 그래서 아이디/비밀번호를 자동 입력해 먼저 로그인을 시도하고, 캡차가 나오면 그 화면을 보여 준다.
            print("아이디/비밀번호를 자동 입력해 로그인을 시도합니다...")
            result = np.submit_login_captcha_sync(target["user_id"], "", cookie_dir, log=log)
        for attempt in range(3):
            if result.get("cookies"):
                logger.info("로그인 성공: 쿠키 저장 완료 (%s)", target["user_id"])
                np.close_login_session_sync(target["user_id"])
                return EXIT_OK
            if result.get("ret") != "success" and not result.get("screenshot"):
                logger.error("로그인 실패: %s", result.get("msg"))
                return EXIT_ERROR

            shot = result.get("screenshot")
            if shot:
                path = os.path.join(cfg["data_dir"], f"login_{target['user_id']}.png")
                _save_screenshot(shot, path)
                print(f"\n로그인 화면 캡처: {path}")
                if cfg["discord_webhook_url"]:
                    sent = notify_captcha(
                        cfg,
                        target["user_id"],
                        _shot_bytes(shot),
                        "캡차 글자가 보이면 이 터미널의 입력창에 입력하세요. 보이지 않으면 그냥 Enter(빈 입력)로 다시 시도합니다.",
                        status=result.get("msg") or "",
                    )
                    print("캡차 이미지를 디스코드로 전송했습니다." if sent else "디스코드 전송에 실패했습니다 (로그 확인).")
            if not sys.stdin.isatty():
                logger.error("터미널(TTY)이 아니라 캡차를 입력할 수 없습니다. 터미널에서 직접 실행하세요.")
                np.close_login_session_sync(target["user_id"])
                return EXIT_ERROR
            text = input("캡차 문자(없으면 그냥 Enter): ").strip()
            result = np.submit_login_captcha_sync(target["user_id"], text, cookie_dir, log=log)
            if result.get("ret") == "success" and result.get("cookies"):
                logger.info("로그인 성공: 쿠키 저장 완료 (%s)", target["user_id"])
                return EXIT_OK
            logger.warning("로그인 미완료: %s", result.get("msg"))

        np.close_login_session_sync(target["user_id"])
        logger.error("로그인 시도를 모두 실패했습니다.")
        return EXIT_ERROR
    finally:
        lock.release()


def cmd_link(np, cfg, args):
    lock = RunLock(os.path.join(cfg["data_dir"], "naverpaper.lock"))
    if not lock.acquire():
        logger.warning("이미 수집이 실행 중입니다.")
        return EXIT_LOCKED
    try:
        apply_engine_settings(np, cfg)
        seed_cookies(np, cfg, cfg["accounts"])
        result = np.run_manual_link_sync(
            os.path.join(cfg["data_dir"], "naverpaper.sqlite"),
            os.path.join(cfg["data_dir"], "cookies"),
            build_accounts(np, cfg["accounts"]),
            args.url,
            cfg["reward_proxy_url"],
            log=lambda m: logger.info("%s", m),
            login_proxy_url=cfg["login_proxy_url"],
        )
        logger.info(
            "수동 링크 종료 status=%s 방문=%s 예상포인트=%s",
            result.status,
            result.visited_url_count,
            result.estimated_points,
        )
        return EXIT_ERROR if result.status == "error" else EXIT_OK
    finally:
        lock.release()


# ---------------------------------------------------------------- main
def main():
    parser = argparse.ArgumentParser(description="네이버 폐지 줍기 (독립 실행)")
    parser.add_argument("-c", "--config", default=DEFAULT_CONFIG, help="설정 파일 경로 (기본: ./config.json)")
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("run", help="1회 수집 후 종료 (기본)")
    sub.add_parser("status", help="쿠키 상태 확인")
    p_total = sub.add_parser("total", help="적립 합계 / 최근 실행 내역")
    p_total.add_argument("-n", "--runs", type=int, default=10, help="최근 실행 표시 개수 (기본 10)")
    p_notify = sub.add_parser("notify", help="적립 합계를 디스코드로 전송 (수집 없음)")
    p_notify.add_argument("--test", action="store_true", help="테스트 메시지만 전송")
    p_setp = sub.add_parser("set-points", help="잘못 기록된 실행의 적립액 수정 (total 의 번호 사용)")
    p_setp.add_argument("run_id", type=int)
    p_setp.add_argument("user_id")
    p_setp.add_argument("points", type=int)
    p_setp.add_argument("--yes", action="store_true", help="확인 질문 없이 진행")
    p_clear = sub.add_parser("clear-visits", help="계정의 방문 기록 삭제 (기본: 오늘 것만)")
    p_clear.add_argument("user_id")
    p_clear.add_argument("--all", action="store_true", help="오늘뿐 아니라 전체 기록 삭제")
    p_clear.add_argument("--yes", action="store_true", help="확인 질문 없이 진행")
    p_pt = sub.add_parser("pincrux-test", help="쇼핑라이브 링크 하나를 열어 브라우저 환경/영상 재생을 진단 (방문 기록 없음)")
    p_pt.add_argument("url")
    p_pt.add_argument("--desktop", action="store_true", help="모바일 대신 데스크톱 모드로 열기")
    p_pt.add_argument("--account", default="", help="사용할 계정 아이디 (기본: 첫 번째 계정)")
    p_verify = sub.add_parser("verify", help="네이버에 실제 접속해 로그인 상태 확인")
    p_verify.add_argument("user_id", nargs="?", default="")
    p_login = sub.add_parser("login", help="수동 로그인(캡차 대응)")
    p_login.add_argument("user_id")
    p_link = sub.add_parser("link", help="링크 하나 수동 적립")
    p_link.add_argument("url")
    args = parser.parse_args()
    command = args.command or "run"

    try:
        cfg = load_config(args.config)
    except Exception as e:
        print(f"[설정 오류] {e}", file=sys.stderr)
        return EXIT_CONFIG

    setup_logging(cfg["data_dir"], args.verbose)

    try:
        import source_naverpaper as np
    except Exception as e:
        logger.error("필수 패키지가 없습니다: %s", e)
        logger.error("pip install -r requirements.txt && playwright install chromium")
        return EXIT_CONFIG

    handlers = {"run": cmd_run, "pincrux-test": cmd_pincrux_test, "clear-visits": cmd_clear_visits, "set-points": cmd_set_points, "verify": cmd_verify, "total": cmd_total, "notify": cmd_notify, "status": cmd_status, "login": cmd_login, "link": cmd_link}
    try:
        return handlers[command](np, cfg, args)
    except KeyboardInterrupt:
        logger.warning("사용자 중단")
        return EXIT_ERROR
    except Exception:
        logger.exception("예상치 못한 오류")
        return EXIT_ERROR


if __name__ == "__main__":
    sys.exit(main())
