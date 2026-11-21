#!/usr/bin/env python3
"""네이버 폐지 줍기 - 독립 실행(cron)용 실행기

원본: https://gitlab.com/sukajds/ff_naverpaper (flaskfarm 플러그인)
수집 엔진(source_naverpaper.py)은 그대로 쓰고, flaskfarm 연동부만 CLI로 대체했습니다.

사용법
  python3 naverpaper.py                 # 1회 수집 후 종료 (cron용, 기본)
  python3 naverpaper.py run
  python3 naverpaper.py total           # 적립 합계(오늘/7일/30일/전체) + 최근 실행 내역
  python3 naverpaper.py notify          # 적립 합계를 디스코드로 전송 (--test: 테스트 메시지)
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
import sys
import sqlite3
import time
import uuid
from datetime import datetime, timedelta, timezone
from logging.handlers import RotatingFileHandler

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)

DEFAULT_CONFIG = os.path.join(BASE_DIR, "config.json")
DEFAULT_DATA_DIR = os.path.join(BASE_DIR, "data")

EXIT_OK, EXIT_ERROR, EXIT_LOCKED, EXIT_CONFIG = 0, 1, 2, 3

logger = logging.getLogger("naverpaper")


# ---------------------------------------------------------------- 설정
def _to_bool(value, default=False):
    if isinstance(value, bool):
        return value
    if value is None or value == "":
        return default
    return str(value).strip().lower() in ("true", "1", "on", "yes")


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
    if webhook and not webhook.startswith(("https://discord.com/api/webhooks/", "https://discordapp.com/api/webhooks/")):
        raise ValueError("discord_webhook_url 형식이 올바르지 않습니다 (https://discord.com/api/webhooks/... 로 시작해야 함)")
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


class RunLock:
    """cron 중복 실행 방지용 파일 락 (리눅스/맥). 락을 못 잡으면 acquire() 가 False."""

    def __init__(self, path):
        self.path = path
        self.fp = None

    def acquire(self):
        try:
            import fcntl
        except ImportError:  # Windows 등: 락 없이 진행
            return True
        self.fp = open(self.path, "w")
        try:
            fcntl.flock(self.fp, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.fp.write(str(os.getpid()))
            self.fp.flush()
            return True
        except OSError:
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


def points_report(db_path, accounts=None):
    """DB(run_account_summary)에서 계정별 오늘/7일/30일/전체 적립 합계를 구한다.

    estimated_points 는 수집 전후 네이버페이 포인트 잔액 차이(after - before)입니다.
    """
    now = datetime.now()
    cutoffs = {
        "today": now.replace(hour=0, minute=0, second=0, microsecond=0),
        "d7": now - timedelta(days=7),
        "d30": now - timedelta(days=30),
    }
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(
            "SELECT user_id, created_at, COALESCE(estimated_points, 0) FROM run_account_summary"
        ).fetchall()
        first = conn.execute("SELECT MIN(started_at) FROM run_history").fetchone()[0]
        run_count = conn.execute("SELECT COUNT(*) FROM run_history").fetchone()[0]
    finally:
        conn.close()

    stats = {}
    for user_id, created_at, points in rows:
        entry = stats.setdefault(user_id, {"today": 0, "d7": 0, "d30": 0, "all": 0})
        try:
            ts = datetime.fromisoformat(str(created_at))
        except Exception:
            ts = None
        entry["all"] += points
        if ts:
            for key, cutoff in cutoffs.items():
                if ts >= cutoff:
                    entry[key] += points
    if accounts:
        for user_id in accounts:
            stats.setdefault(user_id, {"today": 0, "d7": 0, "d30": 0, "all": 0})
    return stats, first, run_count


def print_points_report(db_path, accounts=None, title="적립 합계"):
    if not _db_exists(db_path):
        print("아직 수집 기록이 없습니다.")
        return
    stats, first, run_count = points_report(db_path, accounts)
    total = {"today": 0, "d7": 0, "d30": 0, "all": 0}
    lines = [f"== {title} (기록 시작: {str(first)[:10] if first else '-'}, 총 {run_count}회 실행) =="]
    lines.append(f"{'계정':<20}{'오늘':>10}{'7일':>10}{'30일':>10}{'전체':>12}")
    for user_id in sorted(stats):
        e = stats[user_id]
        for k in total:
            total[k] += e[k]
        lines.append(
            f"{user_id:<20}{_fmt_point(e['today']):>10}{_fmt_point(e['d7']):>10}{_fmt_point(e['d30']):>10}{_fmt_point(e['all']):>12}"
        )
    if len(stats) > 1:
        lines.append(
            f"{'합계':<20}{_fmt_point(total['today']):>10}{_fmt_point(total['d7']):>10}{_fmt_point(total['d30']):>10}{_fmt_point(total['all']):>12}"
        )
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
        print(f"{'시작 시각':<20}{'상태':<12}{'방문':>6}{'적립':>10}   계정별")
        for run_id, started, status, visited, points in runs:
            per = conn.execute(
                "SELECT user_id, COALESCE(estimated_points,0) FROM run_account_summary WHERE run_id=? ORDER BY id",
                (run_id,),
            ).fetchall()
            detail = ", ".join(f"{u} +{_fmt_point(p)}" for u, p in per)
            print(f"{str(started)[:19]:<20}{str(status):<12}{visited or 0:>6}{_fmt_point(points):>10}   {detail}")
    finally:
        conn.close()


# ---------------------------------------------------------------- 디스코드 알림
def _mask_id(user_id):
    user_id = str(user_id)
    return (user_id[:3] if len(user_id) > 3 else user_id[:1]) + "•••"


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
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                return 200 <= resp.status < 300
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
    """이미지 첨부 전송 (multipart). payload 는 payload_json 으로 함께 보낸다."""
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


def build_embed(cfg, title, stats, run_points=None, error_message="", notes=None, alert=False):
    """run_points: {계정: 이번 실행 적립}. None 이면 합계 요약(notify)용."""
    lines = []
    totals = {"today": 0, "d7": 0, "d30": 0, "all": 0}
    for user_id in sorted(stats):
        e = stats[user_id]
        for k in totals:
            totals[k] += e[k]
        name = _mask_id(user_id) if cfg["discord_mask_ids"] else user_id
        if run_points is not None:
            lines.append(
                f"**{name}**  이번 +{_fmt_point(run_points.get(user_id, 0))} · 오늘 {_fmt_point(e['today'])} · 전체 {_fmt_point(e['all'])}"
            )
        else:
            lines.append(
                f"**{name}**  오늘 {_fmt_point(e['today'])} · 7일 {_fmt_point(e['d7'])} · 전체 {_fmt_point(e['all'])}"
            )

    run_total = sum(run_points.values()) if run_points is not None else None
    if error_message:
        color = 0xE74C3C
        head = f"수집 중 오류: {error_message[:300]}"
    elif alert:
        color = 0xE74C3C
        head = "**로그인 문제가 있어 확인이 필요합니다.**"
    elif run_total is not None and run_total > 0:
        color = 0x2ECC71
        head = f"이번 실행 적립 **+{_fmt_point(run_total)}**"
    else:
        color = 0x95A5A6
        head = f"이번 실행 적립 **+{_fmt_point(run_total)}**" if run_total is not None else "적립 합계"

    description = head + "\n\n" + "\n".join(lines)
    if notes:
        description += "\n\n" + "\n".join(notes)
    description = description[:4000]
    return {
        "title": title,
        "description": description,
        "color": color,
        "fields": [
            {"name": "오늘", "value": _fmt_point(totals["today"]), "inline": True},
            {"name": "30일", "value": _fmt_point(totals["d30"]), "inline": True},
            {"name": "전체", "value": _fmt_point(totals["all"]), "inline": True},
        ],
        "footer": {"text": "naverpaper"},
        "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z"),
    }


def account_issues(result, cfg):
    """계정별 로그인/세션 문제를 (problems, recovered) 로 돌려준다. 각 항목은 (계정 표시명, 안내 문구)."""
    problems, recovered = [], []
    for ar in getattr(result, "account_results", []) or []:
        uid = getattr(ar, "user_id", "")
        name = _mask_id(uid) if cfg.get("discord_mask_ids", True) else uid
        relogin = getattr(ar, "relogin", "") or ""
        if getattr(ar, "session_expired", False):
            if relogin == "skipped":
                text = "네이버 세션이 만료되었지만 최근 30분 안에 재로그인을 이미 시도해 이번에는 건너뛰었습니다. 링크는 처리하지 않았습니다(방문 기록 없음)."
            elif relogin == "failed":
                text = "네이버 세션이 만료되었고 자동 재로그인에 실패했습니다. 링크는 처리하지 않았습니다(방문 기록 없음). 터미널에서 `login 아이디`로 수동 로그인이 필요합니다."
            else:
                text = "네이버 세션이 만료되어 링크 처리를 중단했습니다(방문 기록 없음). `login 아이디`로 수동 로그인이 필요합니다."
            problems.append((name, text))
        elif getattr(ar, "login_failed", False):
            problems.append((name, "로그인에 실패했습니다(쿠키 만료 또는 캡차 가능성). `login 아이디`로 수동 로그인이 필요합니다."))
        elif relogin == "ok":
            recovered.append((name, "세션이 만료되어 자동으로 재로그인했습니다. 정상 처리되었습니다."))
    return problems, recovered


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
    title = "네이버 폐지줍기 - 로그인 확인 필요" if problems else "네이버 폐지줍기 결과"
    embed = build_embed(cfg, title, stats, run_points, result.message if is_error else "", notes=notes, alert=bool(problems))
    if send_discord(url, {"embeds": [embed]}):
        logger.info("디스코드 알림 전송 완료")


# ---------------------------------------------------------------- 명령
def cmd_run(np, cfg, args):
    lock = RunLock(os.path.join(cfg["data_dir"], "naverpaper.lock"))
    if not lock.acquire():
        logger.warning("이미 수집이 실행 중입니다. 이번 실행은 건너뜁니다.")
        return EXIT_LOCKED
    try:
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
        result = np.run_sync(run_cfg, log=lambda m: logger.info("%s", m))
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
        if result.status == "error":
            logger.error("수집 오류: %s", result.message)
            return EXIT_ERROR
        return EXIT_OK
    finally:
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
    labels = {"alive": "OK", "expired": "NG", "unknown": "??"}
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

    handlers = {"run": cmd_run, "verify": cmd_verify, "total": cmd_total, "notify": cmd_notify, "status": cmd_status, "login": cmd_login, "link": cmd_link}
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
