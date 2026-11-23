import asyncio
import base64
import html as html_lib
import json
import os
import queue
import re
import threading
import time
import traceback
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Callable, Optional
from urllib.parse import parse_qs, unquote, urljoin, urlparse

from bs4 import BeautifulSoup
from sqlalchemy import Boolean, Column, DateTime, ForeignKey, Integer, String, Text, create_engine, text
from sqlalchemy.orm import declarative_base, relationship, sessionmaker

# Keep this in sync across cookie creation and actual collection. Naver can invalidate
# sessions when the browser identity changes.
IPHONE13_UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 15_0 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/15.0 Mobile/15E148 Safari/604.1"
)
DESKTOP_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
)
REWARD_URL_PREFIXES = (
    "https://campaign2.naver.com",
    "https://nsl.pincrux.com/live-view.html",
)
EXCLUDED_REWARD_URL_PREFIXES = (
    "https://ofw.adison.co",
)
REWARD_KIND_CAMPAIGN2 = "campaign2"
REWARD_KIND_LIVE_VIEW = "live_view"
REWARD_KIND_EXCLUDED = "excluded"
REWARD_KIND_OTHER = "other"
NAVERPAY_MAIN_URL = "https://point.pay.naver.com/pc/main"
NAVERPAY_MISSION_DETAIL_URL = "https://point.pay.naver.com/pc/mission-detail?dataType=category&rank=20&pageKey=all"
PINCRUX_SOURCE_URL_PREFIX = "https://external-token.pay.naver.com"

LOGIN_SESSION_TTL = 300
_login_sessions = {}
_login_sessions_lock = threading.Lock()
_login_loop = None
_login_loop_lock = threading.Lock()


Base = declarative_base()


class ExcludedRewardUrl(Exception):
    pass


class CampaignUrl(Base):
    __tablename__ = "campaign_urls"

    url = Column(String, primary_key=True)
    date_added = Column(DateTime, default=datetime.now)
    is_available = Column(Boolean, default=True)


class ExcludedRewardSource(Base):
    __tablename__ = "excluded_reward_sources"

    source_key = Column(String, primary_key=True)
    target_url = Column(Text, default="")
    updated_at = Column(DateTime, default=datetime.now)


class RewardSourceResolution(Base):
    __tablename__ = "reward_source_resolutions"

    source_key = Column(String, primary_key=True)
    final_kind = Column(String, default="")
    final_url = Column(Text, default="")
    updated_at = Column(DateTime, default=datetime.now)


class UrlVisit(Base):
    __tablename__ = "url_visits"

    url = Column(String, ForeignKey("campaign_urls.url"), primary_key=True)
    user_id = Column(String, primary_key=True)
    visited_at = Column(DateTime)
    campaign_url = relationship("CampaignUrl")


class User(Base):
    __tablename__ = "user"

    user_id = Column(String, primary_key=True)
    storage_state = Column(Text, nullable=True)
    updated_at = Column(DateTime, default=datetime.now)


class RunHistory(Base):
    __tablename__ = "run_history"

    id = Column(Integer, primary_key=True, autoincrement=True)
    started_at = Column(DateTime, default=datetime.now)
    finished_at = Column(DateTime)
    status = Column(String, default="running")
    account_count = Column(Integer, default=0)
    collected_url_count = Column(Integer, default=0)
    skipped_url_count = Column(Integer, default=0)
    visited_url_count = Column(Integer, default=0)
    estimated_points = Column(Integer, default=0)
    message = Column(Text, default="")


class RunDetail(Base):
    __tablename__ = "run_detail"

    id = Column(Integer, primary_key=True, autoincrement=True)
    run_id = Column(Integer, ForeignKey("run_history.id"))
    user_id = Column(String, default="")
    url = Column(Text, default="")
    status = Column(String, default="")
    point = Column(Integer, default=0)
    message = Column(Text, default="")
    created_at = Column(DateTime, default=datetime.now)
    run = relationship("RunHistory")


class RunAccountSummary(Base):
    __tablename__ = "run_account_summary"

    id = Column(Integer, primary_key=True, autoincrement=True)
    run_id = Column(Integer, ForeignKey("run_history.id"))
    user_id = Column(String, default="")
    target_url_count = Column(Integer, default=0)
    skipped_url_count = Column(Integer, default=0)
    visited_url_count = Column(Integer, default=0)
    estimated_points = Column(Integer, default=0)
    detail_count = Column(Integer, default=0)
    message = Column(Text, default="")
    created_at = Column(DateTime, default=datetime.now)
    run = relationship("RunHistory")


class Database:
    def __init__(self, db_path: str):
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        self.db_path = db_path
        self.engine = create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False})
        self.Session = sessionmaker(bind=self.engine)

    def create_all(self):
        Base.metadata.create_all(bind=self.engine)
        self._ensure_columns()

    def _ensure_columns(self):
        with self.engine.begin() as conn:
            existing = {row[1] for row in conn.execute(text("PRAGMA table_info(run_history)"))}
            if "skipped_url_count" not in existing:
                conn.execute(text("ALTER TABLE run_history ADD COLUMN skipped_url_count INTEGER DEFAULT 0"))
            existing = {row[1] for row in conn.execute(text("PRAGMA table_info(run_account_summary)"))}
            if existing and "target_url_count" not in existing:
                conn.execute(text("ALTER TABLE run_account_summary ADD COLUMN target_url_count INTEGER DEFAULT 0"))

    @contextmanager
    def get_session(self):
        session = self.Session()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()


@dataclass
class AccountConfig:
    user_id: str
    password: str = ""
    keep_login: bool = True


@dataclass
class RunConfig:
    db_path: str
    cookie_dir: str
    accounts: list[AccountConfig]
    reward_proxy_url: str = ""
    login_proxy_url: str = ""
    no_paper_record: bool = False
    keep_campaign_days: int = 60
    keep_user_days: int = 7


@dataclass
class DetailResult:
    user_id: str = ""
    url: str = ""
    status: str = ""
    point: int = 0
    message: str = ""


@dataclass
class RunResult:
    started_at: datetime = field(default_factory=datetime.now)
    finished_at: Optional[datetime] = None
    status: str = "running"
    account_count: int = 0
    collected_url_count: int = 0
    skipped_url_count: int = 0
    visited_url_count: int = 0
    estimated_points: int = 0
    message: str = ""
    details: list[DetailResult] = field(default_factory=list)
    account_results: list = field(default_factory=list)


def parse_accounts(ids_text: str, passwords_text: str) -> list[AccountConfig]:
    ids = [x.strip() for x in str(ids_text or "").split("|") if x.strip()]
    passwords = [x.strip() for x in str(passwords_text or "").split("|")]
    return [AccountConfig(user_id=nid, password=passwords[idx].strip() if idx < len(passwords) else "", keep_login=True) for idx, nid in enumerate(ids)]


def run_sync(config: RunConfig, log: Optional[Callable[[str], None]] = None) -> RunResult:
    _cleanup_login_sessions_sync(log=log)
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return _run_coro_blocking(run(config, log=log))
    return _run_async_in_thread(lambda: run(config, log=log))


def _run_coro_blocking(coro):
    loop = asyncio.new_event_loop()
    try:
        asyncio.set_event_loop(loop)
        try:
            asyncio.events._set_running_loop(None)
        except Exception:
            pass
        return loop.run_until_complete(coro)
    finally:
        try:
            asyncio.set_event_loop(None)
        except Exception:
            pass
        loop.close()


def _run_async_in_thread(coro_factory):
    result_queue = queue.Queue(maxsize=1)

    def runner():
        try:
            result_queue.put((True, _run_coro_blocking(coro_factory())))
        except Exception as e:
            result_queue.put((False, e))

    thread = threading.Thread(target=runner, daemon=True)
    thread.start()
    ok, value = result_queue.get()
    thread.join()
    if ok:
        return value
    raise value


async def run(config: RunConfig, log: Optional[Callable[[str], None]] = None) -> RunResult:
    db = Database(config.db_path)
    db.create_all()
    result = RunResult(account_count=len(config.accounts))

    def emit(message: str):
        if log:
            log(message)

    reset_session_events()
    with db.get_session() as session_db:
        history = RunHistory(started_at=result.started_at, status="running", account_count=result.account_count)
        session_db.add(history)
        session_db.flush()

        try:
            emit("Campaign URL collection started")
            emit(f"Proxy mode login={mask_proxy_url(config.login_proxy_url)} reward={mask_proxy_url(config.reward_proxy_url)}")
            collected_urls, _existing_collected_urls, account_collected_urls = await save_naver_campaign_urls(
                session_db,
                emit,
                config.reward_proxy_url,
                config.accounts,
                config.cookie_dir,
                config.login_proxy_url,
            )
            result.collected_url_count = len(collected_urls)
            emit(f"Campaign URL collection finished: {result.collected_url_count}")
            excluded_reward_sources = load_excluded_reward_sources(session_db)
            if excluded_reward_sources:
                emit(f"Loaded excluded reward sources: {len(excluded_reward_sources)}")

            for account in config.accounts:
                account_candidate_urls = set(collected_urls)
                account_candidate_urls.update(account_collected_urls.get(account.user_id, set()))
                account_result = await process_account(
                    account,
                    session_db,
                    config.cookie_dir,
                    config.reward_proxy_url,
                    emit,
                    config.login_proxy_url,
                    account_candidate_urls,
                    excluded_reward_sources,
                )
                _apply_session_events(account_result)
                result.account_results.append(account_result)
                result.estimated_points += account_result.estimated_points
                result.skipped_url_count += account_result.skipped_url_count
                result.visited_url_count += account_result.visited_url_count
                result.details.extend(account_result.details)
                for detail in account_result.details:
                    session_db.add(
                        RunDetail(
                            run_id=history.id,
                            user_id=detail.user_id,
                            url=detail.url,
                            status=detail.status,
                            point=detail.point,
                            message=detail.message,
                        )
                    )
                session_db.add(
                    RunAccountSummary(
                        run_id=history.id,
                        user_id=account.user_id,
                        target_url_count=account_result.target_url_count,
                        skipped_url_count=account_result.skipped_url_count,
                        visited_url_count=account_result.visited_url_count,
                        estimated_points=account_result.estimated_points,
                        detail_count=len(account_result.details),
                        message=account_result.message if hasattr(account_result, "message") else "",
                    )
                )
                if config.no_paper_record and account_result.visited_url_count == 0 and not account_result.details:
                    session_db.add(
                        RunDetail(
                            run_id=history.id,
                            user_id=account.user_id,
                            status="no_url",
                            message="No unvisited campaign URL",
                        )
                    )

            delete_old_stuff(session_db, config.keep_campaign_days, config.keep_user_days, emit)
            result.status = "completed"
            result.message = ""
        except Exception as e:
            result.status = "error"
            result.message = str(e)
            emit(traceback.format_exc())
        finally:
            result.finished_at = datetime.now()
            history.finished_at = result.finished_at
            history.status = result.status
            history.account_count = result.account_count
            history.collected_url_count = result.collected_url_count
            history.skipped_url_count = result.skipped_url_count
            history.visited_url_count = result.visited_url_count
            history.estimated_points = result.estimated_points
            history.message = result.message

    return result


async def run_manual_link(
    db_path: str,
    cookie_dir: str,
    accounts: list[AccountConfig],
    link: str,
    reward_proxy_url: str = "",
    log: Optional[Callable[[str], None]] = None,
    login_proxy_url: str = "",
) -> RunResult:
    db = Database(db_path)
    db.create_all()
    result = RunResult(account_count=len(accounts), collected_url_count=1)
    link = str(link or "").strip()

    def emit(message: str):
        if log:
            log(message)

    if not link:
        result.status = "warning"
        result.message = "manual link is empty"
        return result

    reset_session_events()
    with db.get_session() as session_db:
        history = RunHistory(started_at=result.started_at, status="running", account_count=result.account_count, collected_url_count=1)
        session_db.add(history)
        session_db.flush()

        try:
            emit(f"Manual reward started: {link}")
            emit(f"Proxy mode login={mask_proxy_url(login_proxy_url)} reward={mask_proxy_url(reward_proxy_url)}")
            for account in accounts:
                account_result = await process_account_manual_link(
                    account,
                    session_db,
                    cookie_dir,
                    reward_proxy_url,
                    link,
                    emit,
                    login_proxy_url,
                )
                _apply_session_events(account_result)
                result.account_results.append(account_result)
                result.estimated_points += account_result.estimated_points
                result.skipped_url_count += account_result.skipped_url_count
                result.visited_url_count += account_result.visited_url_count
                result.details.extend(account_result.details)
                for detail in account_result.details:
                    session_db.add(
                        RunDetail(
                            run_id=history.id,
                            user_id=detail.user_id,
                            url=detail.url,
                            status=detail.status,
                            point=detail.point,
                            message=detail.message,
                        )
                    )
                session_db.add(
                    RunAccountSummary(
                        run_id=history.id,
                        user_id=account.user_id,
                        target_url_count=account_result.target_url_count,
                        skipped_url_count=account_result.skipped_url_count,
                        visited_url_count=account_result.visited_url_count,
                        estimated_points=account_result.estimated_points,
                        detail_count=len(account_result.details),
                        message=account_result.message if hasattr(account_result, "message") else "",
                    )
                )

            result.status = "completed"
            result.message = ""
        except Exception as e:
            result.status = "error"
            result.message = str(e)
            emit(traceback.format_exc())
        finally:
            result.finished_at = datetime.now()
            history.finished_at = result.finished_at
            history.status = result.status
            history.account_count = result.account_count
            history.collected_url_count = result.collected_url_count
            history.skipped_url_count = result.skipped_url_count
            history.visited_url_count = result.visited_url_count
            history.estimated_points = result.estimated_points
            history.message = result.message

    return result


def run_manual_link_sync(
    db_path: str,
    cookie_dir: str,
    accounts: list[AccountConfig],
    link: str,
    reward_proxy_url: str = "",
    log: Optional[Callable[[str], None]] = None,
    login_proxy_url: str = "",
):
    _cleanup_login_sessions_sync(log=log)
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return _run_coro_blocking(run_manual_link(db_path, cookie_dir, accounts, link, reward_proxy_url, log, login_proxy_url))
    return _run_async_in_thread(lambda: run_manual_link(db_path, cookie_dir, accounts, link, reward_proxy_url, log, login_proxy_url))


@dataclass
class AccountResult:
    user_id: str
    target_url_count: int = 0
    visited_url_count: int = 0
    skipped_url_count: int = 0
    estimated_points: int = 0
    details: list[DetailResult] = field(default_factory=list)
    session_expired: bool = False  # 네이버 서버에서 세션이 무효화됨 (재로그인까지 실패한 경우 포함)
    login_failed: bool = False  # 로그인 자체에 실패
    relogin: str = ""  # 자동 재로그인 결과: "", "ok", "failed", "skipped"
    balance_unreliable: bool = False  # 포인트 잔액을 읽지 못했거나 0 으로만 읽혀 적립액(estimated_points)을 믿기 어려움
    login_error: str = ""  # 로그인/재로그인 실패 사유 원문 (원인 분류 태그 포함)


def cookie_path(cookie_dir: str, nid: str) -> str:
    return os.path.join(cookie_dir, f"{nid}.json")


def browser_profile_dir(cookie_dir: str, nid: str) -> str:
    safe_id = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(nid or "").strip()) or "default"
    return os.path.join(os.path.dirname(cookie_dir), "browser_profiles", safe_id)


def get_storage_state(nid: str, session_db, cookie_dir: str):
    path = cookie_path(cookie_dir, nid)
    if os.path.exists(path):
        return path
    user = session_db.query(User).filter_by(user_id=nid).first()
    if user and user.storage_state:
        try:
            return json.loads(user.storage_state)
        except Exception:
            return None
    return None


def check_storage_data_status(data):
    import time

    now = time.time()
    key_cookies = ("NID_AUT", "NID_SES", "NID_JST")
    required_cookies = ("NID_AUT", "NID_SES")
    expired = []
    session_only = []
    valid = []
    for cookie in (data or {}).get("cookies", []):
        if cookie.get("name") not in key_cookies:
            continue
        exp = cookie.get("expires", -1)
        if exp == -1:
            session_only.append(cookie.get("name"))
        elif exp < now:
            expired.append(cookie.get("name"))
        else:
            valid.append(cookie.get("name"))
    if expired:
        return False, f"expired cookies: {', '.join(expired)}"
    # 만료일이 없는 세션 쿠키도 저장된 상태 파일로 새 브라우저에 주입하면 쓸 수 있다. 실제 유효 여부는 네이버 서버가
    # 결정하므로 여기서는 거부하지 않고(과거에는 매 실행마다 비밀번호 로그인을 유발), 세션 만료는 실행 중
    # 로그인 화면 이동 감지와 자동 재로그인으로 처리한다.
    present = valid + session_only
    missing = [name for name in required_cookies if not any(item.startswith(name) or item == name for item in present)]
    if missing:
        return False, f"required cookies missing: {', '.join(missing)}"
    if session_only:
        return True, f"valid cookies: {', '.join(present)} (session-only, no expiry date; the server decides the lifetime)"
    return True, f"valid cookies: {', '.join(valid)}"


def check_cookie_status(nid: str, cookie_dir: str):
    path = cookie_path(cookie_dir, nid)
    if not os.path.exists(path):
        return False, "cookie file missing"

    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return check_storage_data_status(data)
    except Exception as e:
        return False, f"cookie read error: {e}"


def save_storage_state(cookie_dir: str, nid: str, storage_state):
    os.makedirs(cookie_dir, exist_ok=True)
    with open(cookie_path(cookie_dir, nid), "w", encoding="utf-8") as f:
        json.dump(storage_state, f, ensure_ascii=False, indent=2)


def storage_cookie_values(storage_state):
    values = {}
    try:
        for cookie in (storage_state or {}).get("cookies", []):
            name = cookie.get("name")
            if name in ("NID_AUT", "NID_SES", "NID_JST"):
                values[name] = cookie.get("value") or ""
    except Exception:
        return {}
    return values


def storage_has_required_cookies(storage_state):
    values = storage_cookie_values(storage_state)
    return bool(values.get("NID_AUT") and values.get("NID_SES"))


def classify_login_failure(url: str = "", title: str = "", body: str = "") -> str:
    """로그인에 실패한 화면(주소, 제목, 본문 앞부분)으로 원인을 분류한다.
    captcha / wrong_credentials / verification / account_locked / dormant / unknown"""
    text = f"{title} {body}".lower()
    url = str(url or "").lower()
    if any(k in text for k in ("자동입력 방지", "자동 입력 방지", "보안문자", "captcha")) or "captcha" in url:
        return "captcha"
    if any(k in text for k in ("아이디 또는 비밀번호", "비밀번호를 잘못", "잘못 입력", "비밀번호가 일치하지")):
        return "wrong_credentials"
    if any(k in text for k in ("휴면",)):
        return "dormant"
    if any(k in text for k in ("보호조치", "이용이 제한", "이용 제한", "일시적으로 제한", "계정이 잠", "로그인이 제한")):
        return "account_locked"
    if any(k in text for k in ("2단계 인증", "인증번호", "본인 확인", "본인확인", "새로운 환경", "추가 인증", "기기 등록", "새로운 기기")) or "deviceconfirm" in url:
        return "verification"
    return "unknown"


async def password_login_on_page(page, account: AccountConfig, emit: Callable[[str], None]):
    if not account.password:
        emit(f"{account.user_id}: login diagnostic - password empty")
        return False, "password empty"
    emit(f"{account.user_id}: login diagnostic - opening Naver login page")
    ok, message = await goto_naver_login(page, emit, account.user_id)
    if not ok:
        emit(f"{account.user_id}: login diagnostic - login page open failed: {message}")
        return False, message
    emit(f"{account.user_id}: login diagnostic - login page loaded url={page.url}")
    try:
        id_count = await page.locator("#id").count()
        pw_count = await page.locator("#pw").count()
        emit(f"{account.user_id}: login diagnostic - input fields id={id_count} pw={pw_count}")
    except Exception as e:
        emit(f"{account.user_id}: login diagnostic - input field check failed: {e}")
    await page.locator("#id").click()
    await asyncio.sleep(0.5)
    await page.locator("#id").type(account.user_id, delay=80)
    await asyncio.sleep(0.3)
    await page.locator("#pw").click()
    await asyncio.sleep(0.5)
    await page.locator("#pw").type(account.password, delay=80)
    await set_keep_login(page, account.keep_login, emit, account.user_id)
    await asyncio.sleep(0.5)
    await page.locator("#pw").press("Enter")
    emit(f"{account.user_id}: login diagnostic - submitted credentials url={page.url}")
    last_cookie_text = ""
    last_url = ""
    for attempt in range(1, 6):
        await asyncio.sleep(1)
        storage = await page.context.storage_state()
        values = storage_cookie_values(storage)
        cookie_text = ",".join([name for name in ("NID_AUT", "NID_SES", "NID_JST") if values.get(name)]) or "none"
        current_url = page.url
        if cookie_text != last_cookie_text or current_url != last_url or attempt in (1, 5):
            emit(f"{account.user_id}: login diagnostic - wait {attempt}/5 url={current_url} cookies={cookie_text}")
            last_cookie_text = cookie_text
            last_url = current_url
        if storage_has_required_cookies(storage):
            emit(f"{account.user_id}: login success")
            return True, "login success"
    try:
        title = await page.title()
    except Exception:
        title = ""
    try:
        body_text = (await page.locator("body").inner_text(timeout=3000))[:500]
    except Exception as e:
        body_text = f"body read failed: {e}"
    emit(f"{account.user_id}: login diagnostic - failed final_url={page.url} title={title} body={body_text}")
    reason = classify_login_failure(page.url, title, body_text)
    emit(f"{account.user_id}: login diagnostic - failure reason={reason}")
    return False, f"login did not produce required cookies (reason={reason})"


KEEP_LOGIN_LABEL_RE = re.compile(r"로그인\s*상태\s*유지")


async def _keep_login_state_near(locator):
    """'로그인 상태 유지' 문구 주변에서 체크 상태를 읽는다. 체크박스가 하나뿐인 가장 가까운 상위 요소만 본다. 알 수 없으면 None."""
    try:
        return await locator.evaluate(
            r"""(el) => {
                let node = el;
                for (let i = 0; i < 4 && node; i++, node = node.parentElement) {
                    const aria = node.getAttribute && node.getAttribute('aria-checked');
                    if (aria === 'true' || aria === 'false') return aria === 'true';
                    const inputs = node.querySelectorAll ? node.querySelectorAll('input[type=checkbox]') : [];
                    if (inputs.length === 1) return !!inputs[0].checked;
                }
                return null;
            }"""
        )
    except Exception:
        return None


async def set_keep_login(page, enabled: bool, emit: Callable[[str], None], user_id: str = ""):
    selectors = [
        "#keep",
        "input[name='nvlong']",
        "input[id*='keep' i]",
        "label[for='keep']",
    ]
    for selector in selectors:
        try:
            locator = page.locator(selector).first
            if await locator.count() == 0:
                continue
            tag = (await locator.evaluate("(el) => el.tagName.toLowerCase()")).lower()
            if tag == "input":
                checked = await locator.is_checked()
                if bool(checked) != bool(enabled):
                    await locator.click()
            else:
                input_checked = await page.locator("#keep").is_checked()
                if bool(input_checked) != bool(enabled):
                    await locator.click()
            emit(f"{user_id}: login diagnostic - keep-login set to {enabled}")
            return True
        except Exception:
            continue

    # 개편된 로그인 화면: 아이디가 달라져도 "로그인 상태 유지" 문구로 찾는다 (옆의 "IP 보안" 토글은 건드리지 않는다)
    for build in (
        lambda: page.get_by_role("checkbox", name=KEEP_LOGIN_LABEL_RE),
        lambda: page.get_by_label(KEEP_LOGIN_LABEL_RE),
    ):
        try:
            locator = build().first
            if await locator.count() == 0:
                continue
            await locator.set_checked(bool(enabled), timeout=3000)
            emit(f"{user_id}: login diagnostic - keep-login set to {enabled} (by label)")
            return True
        except Exception:
            continue
    try:
        text_locator = page.get_by_text(KEEP_LOGIN_LABEL_RE).first
        if await text_locator.count() > 0:
            state = await _keep_login_state_near(text_locator)
            if state is None or bool(state) != bool(enabled):
                await text_locator.click(timeout=3000)
            emit(f"{user_id}: login diagnostic - keep-login set to {enabled} (by text click; previous state={state})")
            return True
    except Exception:
        pass

    # 그래도 못 찾으면 다음 수정에 쓸 수 있도록 후보 요소의 HTML 을 로그에 남긴다
    try:
        candidates = await page.evaluate(
            r"""() => Array.from(document.querySelectorAll('*'))
                .filter((el) => el.children.length < 6 && /로그인\s*상태\s*유지/.test(el.textContent || ''))
                .slice(-3)
                .map((el) => el.outerHTML.slice(0, 400))"""
        )
        emit(f"{user_id}: login diagnostic - keep-login candidates: {candidates}")
    except Exception:
        pass
    emit(f"{user_id}: login diagnostic - keep-login control not found")
    return False


async def goto_naver_login(page, emit: Optional[Callable[[str], None]] = None, user_id: str = ""):
    url = "https://nid.naver.com/nidlogin.login"
    errors = []
    for wait_until, timeout in (("domcontentloaded", 30000), ("commit", 20000)):
        try:
            if emit:
                emit(f"{user_id}: login diagnostic - goto start wait_until={wait_until} timeout={timeout}ms")
            await page.goto(url, wait_until=wait_until, timeout=timeout)
            try:
                await page.wait_for_selector("#id", timeout=30000)
            except Exception:
                pass
            if emit:
                emit(f"{user_id}: login diagnostic - goto success wait_until={wait_until} url={page.url}")
            return True, "login page loaded"
        except Exception as e:
            message = f"{wait_until}: {e}"
            errors.append(message)
            if emit:
                emit(f"{user_id}: login diagnostic - goto failed {message}")
    return False, "login page timeout - " + " / ".join(errors[-2:])


async def naver_login(page, account: AccountConfig, cookie_dir: str, emit: Callable[[str], None]):
    is_valid, cookie_msg = check_cookie_status(account.user_id, cookie_dir)
    if is_valid:
        emit(f"{account.user_id}: using saved cookie ({cookie_msg})")
        return True, cookie_msg

    emit(f"{account.user_id}: cookie unavailable ({cookie_msg}); trying id/password login")
    try:
        login_ok, login_message = await password_login_on_page(page, account, emit)
        if login_ok:
            return True, login_message
        return False, f"{cookie_msg}; {login_message}"
    except Exception as e:
        emit(f"{account.user_id}: login failed - {e}")
        return False, str(e)


def refresh_cookie_sync(account: AccountConfig, cookie_dir: str, proxy_url: str = "", log: Optional[Callable[[str], None]] = None):
    async def _run():
        screen = await open_login_screen(account, cookie_dir, proxy_url, log)
        if screen.get("ret") != "success":
            return screen
        return await submit_login_captcha(account.user_id, "", cookie_dir, log)

    return _run_login_async(_run())


async def refresh_cookie(account: AccountConfig, cookie_dir: str, proxy_url: str = "", log: Optional[Callable[[str], None]] = None):
    from playwright.async_api import async_playwright
    from playwright_stealth import Stealth

    def emit(message: str):
        if log:
            log(message)

    os.makedirs(cookie_dir, exist_ok=True)
    async with async_playwright() as playwright:
        launch_kwargs = {"headless": True}
        proxy = playwright_proxy(proxy_url)
        if proxy:
            launch_kwargs["proxy"] = proxy
        browser = await playwright.chromium.launch(**launch_kwargs)
        context = await browser.new_context(
            user_agent=IPHONE13_UA,
            viewport={"width": 390, "height": 844},
            device_scale_factor=3,
            is_mobile=True,
            has_touch=True,
        )
        page = await context.new_page()
        await Stealth().apply_stealth_async(page)
        try:
            login_ok, login_message = await password_login_on_page(page, account, emit)
            if not login_ok:
                emit(f"{account.user_id}: cookie refresh failed - {login_message}")
                return {"ret": "warning", "msg": "cookie refresh failed. Check plugin log for login diagnostics.", "cookies": {}}
            storage = await context.storage_state()
            with open(cookie_path(cookie_dir, account.user_id), "w", encoding="utf-8") as f:
                json.dump(storage, f, ensure_ascii=False, indent=2)
            return {"ret": "success", "msg": "cookie refreshed", "cookies": storage_cookie_values(storage)}
        finally:
            await context.close()
            await browser.close()


async def refresh_cookie_from_profile(
    account: AccountConfig,
    cookie_dir: str,
    proxy_url: str = "",
    emit: Optional[Callable[[str], None]] = None,
    allow_password_login: bool = True,
):
    from playwright.async_api import async_playwright
    from playwright_stealth import Stealth

    def log(message: str):
        if emit:
            emit(message)

    if not account.user_id:
        return False, "account id required"

    profile_dir = browser_profile_dir(cookie_dir, account.user_id)
    os.makedirs(profile_dir, exist_ok=True)
    os.makedirs(cookie_dir, exist_ok=True)
    log(f"{account.user_id}: cookie auto refresh profile check started proxy={mask_proxy_url(proxy_url)}")

    async with async_playwright() as playwright:
        launch_kwargs = {
            "headless": True,
            "user_agent": IPHONE13_UA,
            "viewport": {"width": 390, "height": 844},
            "device_scale_factor": 3,
            "is_mobile": True,
            "has_touch": True,
        }
        proxy = playwright_proxy(proxy_url)
        if proxy:
            launch_kwargs["proxy"] = proxy
        log(f"{account.user_id}: cookie auto refresh opening persistent browser")
        context = await playwright.chromium.launch_persistent_context(profile_dir, **launch_kwargs)
        try:
            page = context.pages[0] if context.pages else await context.new_page()
            await Stealth().apply_stealth_async(page)

            storage = await context.storage_state()
            ok, message = check_storage_data_status(storage)
            if ok:
                save_storage_state(cookie_dir, account.user_id, storage)
                log(f"{account.user_id}: cookie auto refresh from browser profile ({message})")
                return True, message

            try:
                log(f"{account.user_id}: cookie auto refresh checking pay login page")
                await page.goto("https://pay.naver.com/pointshistory/list?category=all", wait_until="domcontentloaded", timeout=30000)
                await page.wait_for_timeout(3000)
            except Exception as e:
                log(f"{account.user_id}: browser profile login check failed - {e}")

            storage = await context.storage_state()
            ok, message = check_storage_data_status(storage)
            if ok:
                save_storage_state(cookie_dir, account.user_id, storage)
                log(f"{account.user_id}: cookie auto refresh after login check ({message})")
                return True, message

            if not allow_password_login:
                return False, message
            if not account.password:
                return False, f"{message}; password empty"

            log(f"{account.user_id}: browser profile cookie unavailable ({message}); trying id/password login")
            login_ok, login_message = await password_login_on_page(page, account, log)
            if not login_ok:
                return False, login_message

            storage = await context.storage_state()
            ok, message = check_storage_data_status(storage)
            if not ok:
                return False, message
            save_storage_state(cookie_dir, account.user_id, storage)
            log(f"{account.user_id}: cookie auto refresh by login ({message})")
            return True, message
        finally:
            await context.close()


async def ensure_cookie_storage_state(
    account: AccountConfig,
    session_db,
    cookie_dir: str,
    proxy_url: str,
    emit: Callable[[str], None],
    allow_password_login: bool = True,
):
    ok, message = check_cookie_status(account.user_id, cookie_dir)
    if ok:
        return get_storage_state(account.user_id, session_db, cookie_dir)

    emit(f"{account.user_id}: cookie unavailable ({message}); trying auto refresh")
    refresh_ok, refresh_message = await refresh_cookie_from_profile(
        account,
        cookie_dir,
        proxy_url,
        emit,
        allow_password_login=allow_password_login,
    )
    if refresh_ok:
        return get_storage_state(account.user_id, session_db, cookie_dir)

    emit(f"{account.user_id}: cookie auto refresh failed - {refresh_message}")
    note_session_event(account.user_id, login_failed=True, login_error=str(refresh_message)[:300])
    return None


# ---------------------------------------------------------------------------
# 세션 만료 감지 / 자동 재로그인
#
# 쿠키 파일의 만료일이 남아 있어도 네이버가 서버에서 세션을 무효화하면 네이버페이 페이지가
# nid.naver.com 로그인 화면으로 이동한다. 이 상태에서 링크를 처리하면 적립 없이 "방문 기록"만
# 남아, 로그인을 복구해도 그 링크를 다시 시도하지 않게 된다. 그래서 만료를 감지하면 링크 처리를
# 중단(방문 기록 저장 안 함)하고 저장된 비밀번호로 재로그인을 시도한다.
# ---------------------------------------------------------------------------
LOGIN_REDIRECT_RE = re.compile(r"nid\.naver\.com/[^\s]*login", re.I)
RELOGIN_COOLDOWN_SECONDS = 1800
_SESSION_EVENTS: dict = {}


def is_login_redirect(url) -> bool:
    """주소(또는 주소가 들어 있는 문구)가 네이버 로그인 화면이면 True."""
    return bool(LOGIN_REDIRECT_RE.search(str(url or "")))


def reset_session_events():
    _SESSION_EVENTS.clear()


def session_event(user_id) -> dict:
    return dict(_SESSION_EVENTS.get(str(user_id or ""), {}))


def note_session_event(user_id, **values):
    _SESSION_EVENTS.setdefault(str(user_id or ""), {}).update(values)


def _balance_file(cookie_dir: str, user_id: str) -> str:
    return os.path.join(cookie_dir, f"{user_id}.balance.json")


def _read_balance_state(cookie_dir: str, user_id: str) -> dict:
    try:
        with open(_balance_file(cookie_dir, user_id), encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_balance_state(cookie_dir: str, user_id: str, state: dict):
    try:
        os.makedirs(cookie_dir, exist_ok=True)
        state["at"] = datetime.now().isoformat(timespec="seconds")
        with open(_balance_file(cookie_dir, user_id), "w", encoding="utf-8") as f:
            json.dump(state, f)
    except OSError:
        pass


def load_last_balance(cookie_dir: str, user_id: str) -> Optional[int]:
    """마지막으로 확인한 포인트 잔액 (없으면 None)."""
    try:
        value = _read_balance_state(cookie_dir, user_id).get("balance")
        return int(value) if value is not None else None
    except (ValueError, TypeError):
        return None


def save_last_balance(cookie_dir: str, user_id: str, balance: int):
    state = _read_balance_state(cookie_dir, user_id)
    state["balance"] = int(balance)
    _write_balance_state(cookie_dir, user_id, state)


def load_last_event(cookie_dir: str, user_id: str):
    """마지막으로 확인한 이번 달 이벤트적립 (월, 금액). 없으면 (None, None)."""
    event = _read_balance_state(cookie_dir, user_id).get("event")
    if isinstance(event, dict) and event.get("month"):
        try:
            return str(event["month"]), int(event.get("amount", 0))
        except (ValueError, TypeError):
            pass
    return None, None


def save_event_baseline(cookie_dir: str, user_id: str, month: str, amount: int):
    """0 은 로딩 값일 수 있어 같은 달에 양수 기록이 있으면 덮어쓰지 않는다 (새 달의 시작은 0 으로 기록)."""
    if not month:
        return
    last_month, last_amount = load_last_event(cookie_dir, user_id)
    if amount > 0 or last_month != month:
        state = _read_balance_state(cookie_dir, user_id)
        state["event"] = {"month": month, "amount": int(amount)}
        _write_balance_state(cookie_dir, user_id, state)


def _compute_earned_points(cookie_dir: str, user_id: str, before: dict, after: dict, emit: Callable[[str], None]):
    """이번 실행의 적립액을 계산한다. (적립액, 기준 신뢰 여부, 이벤트적립 기준 사용 여부)

    1순위: 이번 달 '이벤트적립' 증가분. 구매적립이나 멤버십적립이 섞이는 잔액 차이보다 이 도구의 클릭 적립에 가깝다.
    직전 값이 로딩 전 기본값 0 으로 읽히면 전체가 이번 실행의 적립으로 잡히므로, 마지막으로 확인한 값을 기준으로 삼는다.
    2순위: 이벤트적립을 읽지 못했거나 달이 바뀐 경우 잔액 차이 (_compute_estimated_points)."""
    b_event, a_event = before.get("event"), after.get("event")
    month = after.get("month")
    if b_event is not None and a_event is not None and month and before.get("month") == month:
        last_month, last_amount = load_last_event(cookie_dir, user_id)
        baseline, reliable, note = b_event, True, ""
        if b_event == 0 and a_event > 0:
            if last_month == month and last_amount and last_amount > 0:
                baseline = last_amount
                note = f" [baseline {last_amount} used: event earnings before the run read as 0]"
            elif last_month is None:
                baseline, reliable = a_event, False
                note = " [no baseline yet: event earnings before the run read as 0, so the earned amount is not counted]"
            # 다른 달의 기록만 있으면 새 달의 첫 적립이므로 0 에서 시작하는 것이 맞다
        estimated = max(0, a_event - baseline)
        b_bal, a_bal = before.get("balance"), after.get("balance")
        change = f"+{max(0, a_bal - b_bal)}" if b_bal is not None and a_bal is not None else "n/a"
        emit(f"{user_id}: point balance {b_bal} -> {a_bal} (+{estimated}) [event earnings {month} {b_event} -> {a_event}; balance change {change}]{note}")
        save_event_baseline(cookie_dir, user_id, month, a_event)
        if a_bal is not None and a_bal > 0:
            save_last_balance(cookie_dir, user_id, a_bal)
        return estimated, reliable, True
    estimated, reliable = _compute_estimated_points(cookie_dir, user_id, before.get("balance"), after.get("balance"), emit)
    return estimated, reliable, False


def _compute_estimated_points(cookie_dir: str, user_id: str, before, after, emit: Callable[[str], None]):
    """수집 전후 잔액으로 이번 실행의 적립액을 계산한다. (적립액, 기준 신뢰 여부)

    실행 직전 잔액이 로딩 전 기본값 0 으로 읽히면 '직후 잔액 - 0' 이 되어 지금까지 모은 전체 잔액이 이번 실행 한 번의
    적립으로 기록된다. 이를 막기 위해 마지막으로 확인한 잔액을 기준으로 삼고, 기준이 없으면 적립액을 세지 않는다."""
    if before is None or after is None:
        return 0, False  # 읽지 못함: 호출부의 _mark_balance_reliability 가 경고한다
    last = load_last_balance(cookie_dir, user_id)
    baseline, note, reliable = before, "", True
    if before == 0 and after > 0:
        if last is None:
            baseline, reliable = after, False
            note = " [no baseline yet: balance before the run read as 0, so the earned amount is not counted]"
        elif last > 0:
            baseline = last
            note = f" [baseline {last} used: balance before the run read as 0]"
    estimated = max(0, after - baseline)
    emit(f"{user_id}: point balance {before} -> {after} (+{estimated}){note}")
    if after > 0:  # 0 은 로딩 값일 수 있어 기준으로 저장하지 않는다
        save_last_balance(cookie_dir, user_id, after)
    return estimated, reliable


def _mark_balance_reliability(result, before_points, after_points, emit):
    """잔액을 못 읽었거나 앞뒤가 모두 0이면 적립액 계산을 믿을 수 없다고 표시한다 (앱 알림과 리포트가 어긋나는 경우의 단서)."""
    if before_points is None or after_points is None or (before_points == 0 and after_points == 0):
        result.balance_unreliable = True
        emit(
            f"{result.user_id}: point balance could not be read reliably (before={before_points}, after={after_points}); "
            "the reported earned amount may be inaccurate - check the Naver Pay point history"
        )


def _apply_session_events(account_result):
    ev = session_event(account_result.user_id)
    if ev.get("relogin"):
        account_result.relogin = ev["relogin"]
    if ev.get("login_failed") and ev.get("relogin") != "ok":
        account_result.login_failed = True
    if ev.get("login_error") and not account_result.login_error and ev.get("relogin") != "ok":
        account_result.login_error = ev["login_error"]
    if ev.get("expired") and ev.get("relogin") != "ok":
        account_result.session_expired = True
        if ev.get("relogin") in ("failed", "skipped") and ev.get("message") and not account_result.login_error:
            account_result.login_error = ev["message"]


def _relogin_marker(cookie_dir: str, user_id: str) -> str:
    return os.path.join(cookie_dir, f"{user_id}.relogin")


async def force_relogin(account: AccountConfig, session_db, cookie_dir: str, proxy_url: str, emit: Callable[[str], None]):
    """세션이 서버에서 무효화된 경우: 저장된 쿠키·프로필 쿠키를 믿지 않고 비밀번호로 새 세션을 만든다."""
    from playwright.async_api import async_playwright
    from playwright_stealth import Stealth

    if not account.password:
        return False, "password empty"
    os.makedirs(cookie_dir, exist_ok=True)
    profile_dir = browser_profile_dir(cookie_dir, account.user_id)
    os.makedirs(profile_dir, exist_ok=True)

    # 무효가 된 쿠키는 더 이상 쓰지 않는다 (실패해도 status 가 NG 로 보이도록 먼저 지운다)
    try:
        os.remove(cookie_path(cookie_dir, account.user_id))
    except FileNotFoundError:
        pass
    except OSError as e:
        emit(f"{account.user_id}: could not remove stale cookie file - {e}")
    try:
        session_db.query(User).filter_by(user_id=account.user_id).update({"storage_state": ""})
    except Exception as e:
        emit(f"{account.user_id}: could not clear stored session - {e}")

    async with async_playwright() as playwright:
        launch_kwargs = {
            "headless": True,
            "user_agent": IPHONE13_UA,
            "viewport": {"width": 390, "height": 844},
            "device_scale_factor": 3,
            "is_mobile": True,
            "has_touch": True,
        }
        proxy = playwright_proxy(proxy_url)
        if proxy:
            launch_kwargs["proxy"] = proxy
        context = await playwright.chromium.launch_persistent_context(profile_dir, **launch_kwargs)
        try:
            page = context.pages[0] if context.pages else await context.new_page()
            await Stealth().apply_stealth_async(page)
            await context.clear_cookies()  # 프로필에 남은 무효 쿠키 제거
            login_ok, login_message = await password_login_on_page(page, account, emit)
            if not login_ok:
                return False, login_message
            storage = await context.storage_state()
            ok, message = check_storage_data_status(storage)
            if not ok:
                return False, message
            save_storage_state(cookie_dir, account.user_id, storage)
            session_db.merge(User(user_id=account.user_id, storage_state=json.dumps(storage), updated_at=datetime.now()))
            return True, message
        finally:
            await context.close()


async def repair_expired_session(account: AccountConfig, session_db, cookie_dir: str, proxy_url: str, emit: Callable[[str], None]) -> bool:
    """세션 만료 시 재로그인을 시도한다. 한 번의 실행에서는 계정당 한 번만, 30분 이내 재시도는 건너뛴다."""
    uid = account.user_id
    ev = session_event(uid)
    if ev.get("relogin"):
        return ev["relogin"] == "ok"

    marker = _relogin_marker(cookie_dir, uid)
    try:
        if time.time() - os.path.getmtime(marker) < RELOGIN_COOLDOWN_SECONDS:
            emit(f"{uid}: session expired; re-login skipped (tried within the last {RELOGIN_COOLDOWN_SECONDS // 60} min)")
            note_session_event(uid, expired=True, relogin="skipped", message="cooldown")
            return False
    except OSError:
        pass

    emit(f"{uid}: session expired; forcing re-login with the saved password")
    try:
        os.makedirs(cookie_dir, exist_ok=True)
        with open(marker, "w", encoding="utf-8") as f:
            f.write(str(int(time.time())))
    except OSError:
        pass
    try:
        ok, message = await force_relogin(account, session_db, cookie_dir, proxy_url, emit)
    except Exception as e:
        ok, message = False, str(e)
    note_session_event(uid, expired=True, relogin="ok" if ok else "failed", message=str(message)[:200])
    emit(f"{uid}: re-login {'succeeded' if ok else 'failed'} ({message})")
    return ok


async def verify_session(account: AccountConfig, cookie_dir: str, reward_proxy_url: str = "", emit: Optional[Callable[[str], None]] = None):
    """저장된 쿠키로 네이버페이에 실제 접속해 로그인 상태를 확인한다. (state, detail) — state: alive / expired / unknown"""
    from playwright.async_api import async_playwright
    from playwright_stealth import Stealth

    ok, message = check_cookie_status(account.user_id, cookie_dir)
    if not ok:
        return "expired", f"cookie file: {message}"
    async with async_playwright() as playwright:
        launch_kwargs = {"headless": True}
        proxy = playwright_proxy(reward_proxy_url)
        if proxy:
            launch_kwargs["proxy"] = proxy
        browser = await playwright.chromium.launch(**launch_kwargs)
        try:
            context = await browser.new_context(
                user_agent=IPHONE13_UA,
                viewport={"width": 390, "height": 844},
                device_scale_factor=3,
                is_mobile=True,
                has_touch=True,
                storage_state=cookie_path(cookie_dir, account.user_id),
            )
            page = await context.new_page()
            await Stealth().apply_stealth_async(page)
            summary = await get_naverpay_point_summary(page, emit, account.user_id)
            points = summary["balance"]
            if is_login_redirect(page.url):
                return "expired", "redirected to login (server-side session is no longer valid)"
            if points is None:
                return "unknown", "point balance unreadable (page did not load as expected)"
            if points > 0:
                save_last_balance(cookie_dir, account.user_id, points)  # 이후 적립액 계산의 기준 잔액
            if summary.get("event") is not None and summary.get("month"):
                save_event_baseline(cookie_dir, account.user_id, summary["month"], summary["event"])
            if emit:
                try:
                    body = await page.locator("body").inner_text(timeout=5000)
                    sample = [x.strip()[:80] for x in body.splitlines() if x.strip()][:45]
                    emit(f"{account.user_id}: point history sample - {sample}")
                except Exception:
                    pass
            event_text = f", event earnings {summary['month']}: {summary['event']}" if summary.get("event") is not None else ""
            return "alive", f"logged in (point balance {points}{event_text})"
        finally:
            await browser.close()


def verify_session_sync(account: AccountConfig, cookie_dir: str, reward_proxy_url: str = "", log: Optional[Callable[[str], None]] = None):
    return _run_async_in_thread(lambda: verify_session(account, cookie_dir, reward_proxy_url, log))


def _run_async(coro):
    try:
        return asyncio.run(coro)
    except RuntimeError:
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(coro)
        finally:
            loop.close()


def _get_login_loop():
    global _login_loop
    with _login_loop_lock:
        if _login_loop and not _login_loop.is_closed() and _login_loop.is_running():
            return _login_loop
        _login_loop = None
        loop = asyncio.new_event_loop()

        def run_loop():
            asyncio.set_event_loop(loop)
            loop.run_forever()

        thread = threading.Thread(target=run_loop, daemon=True)
        thread.start()
        _login_loop = loop
        return loop


def _run_login_async(coro):
    global _login_loop
    try:
        future = asyncio.run_coroutine_threadsafe(coro, _get_login_loop())
        return future.result()
    except RuntimeError:
        with _login_loop_lock:
            _login_loop = None
        raise


async def _close_login_session(user_id: str):
    session = None
    with _login_sessions_lock:
        session = _login_sessions.pop(user_id, None)
    if not session:
        return
    for key in ("context", "browser", "playwright"):
        obj = session.get(key)
        if obj is None:
            continue
        try:
            if key == "playwright":
                await obj.stop()
            else:
                await obj.close()
        except Exception:
            pass


def close_login_session_sync(user_id: str):
    return _run_login_async(_close_login_session(str(user_id or "").strip()))


async def _cleanup_login_sessions():
    now = time.time()
    expired = []
    with _login_sessions_lock:
        for user_id, session in list(_login_sessions.items()):
            if now - float(session.get("created_at") or now) > LOGIN_SESSION_TTL:
                expired.append(user_id)
    for user_id in expired:
        await _close_login_session(user_id)


def _cleanup_login_sessions_sync(log: Optional[Callable[[str], None]] = None):
    try:
        _run_login_async(_cleanup_login_sessions())
    except Exception as exc:
        if log:
            log(f"login session cleanup skipped: {exc}")


async def _login_screenshot_payload(page) -> str:
    png = await page.screenshot(full_page=True, type="png")
    return "data:image/png;base64," + base64.b64encode(png).decode("ascii")


async def _dispatch_input_events(locator):
    try:
        await locator.evaluate(
            """(el) => {
                el.dispatchEvent(new Event('input', {bubbles: true}));
                el.dispatchEvent(new Event('change', {bubbles: true}));
                el.dispatchEvent(new KeyboardEvent('keyup', {bubbles: true}));
            }"""
        )
    except Exception:
        pass


async def _wait_for_login_cookies(context, timeout_seconds: int = 5):
    for _ in range(timeout_seconds):
        await asyncio.sleep(1)
        storage = await context.storage_state()
        if storage_has_required_cookies(storage):
            return storage
    return await context.storage_state()


async def open_login_screen(account: AccountConfig, cookie_dir: str, proxy_url: str = "", log: Optional[Callable[[str], None]] = None):
    from playwright.async_api import async_playwright
    from playwright_stealth import Stealth

    def emit(message: str):
        if log:
            log(message)

    if not account.user_id or not account.password:
        return {"ret": "warning", "msg": "account id/password required"}
    await _cleanup_login_sessions()
    normalized_proxy_url = str(proxy_url or "").strip()
    with _login_sessions_lock:
        existing_session = _login_sessions.get(account.user_id)
    if existing_session and existing_session.get("proxy_url", "") != normalized_proxy_url:
        emit(f"{account.user_id}: manual login session proxy changed; reopening browser")
        await _close_login_session(account.user_id)
        existing_session = None
    if existing_session and existing_session.get("page"):
        page = existing_session.get("page")
        existing_session["account"] = account
        existing_session["created_at"] = time.time()
        emit(f"{account.user_id}: manual login session reused url={page.url}")
        return {"ret": "success", "msg": "current login screen captured", "screenshot": await _login_screenshot_payload(page), "url": page.url}
    os.makedirs(cookie_dir, exist_ok=True)

    playwright = await async_playwright().start()
    launch_kwargs = {"headless": True}
    proxy = playwright_proxy(normalized_proxy_url)
    if proxy:
        launch_kwargs["proxy"] = proxy
        emit(f"{account.user_id}: manual login proxy enabled {mask_proxy_url(normalized_proxy_url)}")
    else:
        emit(f"{account.user_id}: manual login proxy disabled")
    profile_dir = browser_profile_dir(cookie_dir, account.user_id)
    os.makedirs(profile_dir, exist_ok=True)
    persistent_kwargs = {
        "user_agent": IPHONE13_UA,
        "viewport": {"width": 390, "height": 844},
        "device_scale_factor": 3,
        "is_mobile": True,
        "has_touch": True,
    }
    persistent_kwargs.update(launch_kwargs)
    context = await playwright.chromium.launch_persistent_context(profile_dir, **persistent_kwargs)
    page = context.pages[0] if context.pages else await context.new_page()
    await Stealth().apply_stealth_async(page)
    try:
        storage = await context.storage_state()
        ok, cookie_message = check_storage_data_status(storage)
        if ok:
            save_storage_state(cookie_dir, account.user_id, storage)
            await context.close()
            await playwright.stop()
            emit(f"{account.user_id}: manual login skipped; browser profile already logged in ({cookie_message})")
            return {"ret": "success", "msg": "cookie refreshed", "cookies": storage_cookie_values(storage)}
        ok, message = await goto_naver_login(page, emit, account.user_id)
        if not ok:
            await context.close()
            await playwright.stop()
            return {"ret": "warning", "msg": message}
        screenshot = await _login_screenshot_payload(page)
        with _login_sessions_lock:
            _login_sessions[account.user_id] = {
                "playwright": playwright,
                "context": context,
                "page": page,
                "account": account,
                "created_at": time.time(),
                "proxy_url": normalized_proxy_url,
            }
        emit(f"{account.user_id}: manual login session opened url={page.url}")
        return {"ret": "success", "msg": "login screen captured", "screenshot": screenshot, "url": page.url}
    except Exception as e:
        await context.close()
        await playwright.stop()
        return {"ret": "danger", "msg": f"login screen capture failed: {str(e)}"}


def open_login_screen_sync(account: AccountConfig, cookie_dir: str, proxy_url: str = "", log: Optional[Callable[[str], None]] = None):
    return _run_login_async(open_login_screen(account, cookie_dir, proxy_url, log))


async def submit_login_captcha(user_id: str, captcha_text: str, cookie_dir: str, log: Optional[Callable[[str], None]] = None):
    def emit(message: str):
        if log:
            log(message)

    user_id = str(user_id or "").strip()
    captcha_text = str(captcha_text or "").strip()
    if not user_id:
        return {"ret": "warning", "msg": "account id required"}
    with _login_sessions_lock:
        session = _login_sessions.get(user_id)
    if not session:
        return {"ret": "warning", "msg": "login screen session not found or expired"}
    page = session.get("page")
    context = session.get("context")
    account = session.get("account")
    try:
        emit(f"{user_id}: manual login diagnostic - submit requested url={page.url} captcha={'yes' if captcha_text else 'no'}")
        if account and account.user_id:
            id_field = page.locator("#id")
            if await id_field.count():
                await id_field.click()
                await id_field.fill("")
                await id_field.type(account.user_id, delay=80)
                await _dispatch_input_events(id_field)
        if account and account.password:
            pw_field = page.locator("#pw")
            if await pw_field.count():
                await pw_field.click()
                await pw_field.fill("")
                await pw_field.type(account.password, delay=80)
                await _dispatch_input_events(pw_field)
        if account:
            await set_keep_login(page, account.keep_login, emit, user_id)

        selectors = [
            "#captcha",
            "input[name='captcha']",
            "input[id*='captcha' i]",
            "input[name*='captcha' i]",
            "input[type='text']",
        ]
        if captcha_text:
            filled = False
            for selector in selectors:
                locator = page.locator(selector)
                count = await locator.count()
                for idx in range(count):
                    field = locator.nth(idx)
                    try:
                        if await field.is_visible():
                            field_id = await field.get_attribute("id") or ""
                            field_name = await field.get_attribute("name") or ""
                            if field_id == "id" or field_id == "pw" or field_name == "id" or field_name == "pw":
                                continue
                            await field.click()
                            await field.fill(captcha_text)
                            await _dispatch_input_events(field)
                            filled = True
                            break
                    except Exception:
                        continue
                if filled:
                    break
            if not filled:
                return {"ret": "warning", "msg": "captcha input field not found", "screenshot": await _login_screenshot_payload(page)}

        await asyncio.sleep(0.5)
        try:
            submit = page.locator("#log\\.login").first
            if await submit.count() == 0:
                submit = page.locator("button[type='submit'], input[type='submit']").first
            if await submit.count() == 0:
                submit = page.get_by_role("button", name="로그인", exact=True).first
            if await submit.count() == 0:
                raise RuntimeError("login button not found")  # 5초 기다리지 않고 바로 Enter 로 넘어간다
            await submit.click(timeout=5000, force=True)
        except Exception as e:
            emit(f"{user_id}: manual login diagnostic - submit click failed: {e}; pressing Enter")
            await page.keyboard.press("Enter")
        try:
            await page.wait_for_load_state("domcontentloaded", timeout=3000)
        except Exception:
            pass
        await asyncio.sleep(1)
        emit(f"{user_id}: manual login diagnostic - submitted url={page.url}")
        storage = await _wait_for_login_cookies(context, 5)
        if not storage_has_required_cookies(storage):
            emit(f"{user_id}: manual login diagnostic - required cookies missing url={page.url}")
            return {"ret": "warning", "msg": "login did not produce required cookies", "screenshot": await _login_screenshot_payload(page)}
        os.makedirs(cookie_dir, exist_ok=True)
        with open(cookie_path(cookie_dir, user_id), "w", encoding="utf-8") as f:
            json.dump(storage, f, ensure_ascii=False, indent=2)
        await _close_login_session(user_id)
        _ok, _cookie_message = check_storage_data_status(storage)
        emit(f"{user_id}: captcha login success ({_cookie_message})")
        return {"ret": "success", "msg": "cookie refreshed", "cookies": storage_cookie_values(storage)}
    except Exception as e:
        screenshot = ""
        try:
            screenshot = await _login_screenshot_payload(page)
        except Exception:
            pass
        return {"ret": "danger", "msg": f"captcha submit failed: {str(e)}", "screenshot": screenshot}


def submit_login_captcha_sync(user_id: str, captcha_text: str, cookie_dir: str, log: Optional[Callable[[str], None]] = None):
    return _run_login_async(submit_login_captcha(user_id, captcha_text, cookie_dir, log))


async def campaign_page_diagnostic(page, user_id: str, label: str, emit: Callable[[str], None]):
    async def limited(coro, timeout=2):
        return await asyncio.wait_for(coro, timeout=timeout)

    try:
        current_url = page.url
    except Exception:
        current_url = ""
    try:
        title = await limited(page.title())
    except Exception as e:
        title = f"title read failed: {e}"
    try:
        ready_state = await limited(page.evaluate("() => document.readyState"))
    except Exception as e:
        ready_state = f"readyState read failed: {e}"
    try:
        point_buttons = await limited(page.locator("a.popup_link, button, a").filter(has_text="포인트 받기").count())
    except Exception as e:
        point_buttons = f"button count failed: {e}"
    try:
        popup_links = await limited(page.locator("a.popup_link").count())
    except Exception as e:
        popup_links = f"popup link count failed: {e}"
    try:
        raw_body = await limited(page.locator("body").inner_text(timeout=2000), timeout=3)
        body_text = campaign_relevant_body(raw_body)
    except Exception as e:
        body_text = f"body read failed: {e}"
    if body_text:
        emit(
            f"{user_id}: campaign diagnostic - {label} url={current_url} title={title} "
            f"ready={ready_state} point_buttons={point_buttons} popup_links={popup_links} body={body_text}"
        )


def campaign_relevant_body(body_text: str) -> str:
    text = re.sub(r"\s+", " ", str(body_text or "")).strip()
    if not text:
        return ""

    patterns = [
        r"클릭 적립은 캠페인 당\s*1회만 적립됩니다\.?\s*확인",
        r"다음 페이지에서\s*\d+초 이상\s*머물러야\s*네이버페이 포인트\s*\d+원이 적립돼요\.?\s*포인트 받기",
        r"\d+초 후 적립\s*\d+원 적립 완료",
        r"알림신청 완료!?\s*알림을 취소했습니다\.?\s*쇼핑라이브 보고\s*포인트 받기\s*\d+원",
        r"쇼핑라이브 보고\s*포인트 받기\s*\d+원",
        r"\d+원 적립 완료",
    ]
    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            return match.group(0)
    return ""


async def click_and_dwell(page, locator, dwell_seconds: float, user_id: str, emit: Callable[[str], None], label: str, js_click: bool = False):
    context = page.context
    before_pages = list(context.pages)
    before_url = page.url
    if js_click:
        await locator.evaluate(
            """el => {
                const target = el.closest('a, button, [role="button"], [onclick]') || el;
                target.click();
            }"""
        )
    else:
        await locator.click(timeout=5000, force=True)

    await asyncio.sleep(1)
    pages = list(context.pages)
    new_pages = [candidate for candidate in pages if candidate not in before_pages]
    active_page = new_pages[-1] if new_pages else page
    try:
        await active_page.wait_for_load_state("domcontentloaded", timeout=5000)
    except Exception as e:
        emit(f"{user_id}: {label} domcontentloaded wait skipped - {e}")
    try:
        await active_page.bring_to_front()
    except Exception:
        pass
    if is_excluded_reward_url(active_page.url):
        emit(f"{user_id}: excluded reward URL - {active_page.url}")
        return active_page
    ready_wait = await wait_for_dwell_ready(active_page)
    dwell_started_at = time.monotonic()
    await asyncio.sleep(dwell_seconds)
    actual_dwell = time.monotonic() - dwell_started_at
    emit_visited(user_id, active_page.url, emit, dwell_seconds=dwell_seconds, actual_dwell=actual_dwell, ready_wait=ready_wait)
    try:
        await campaign_page_diagnostic(active_page, user_id, f"{label} after dwell", emit)
    except Exception as e:
        emit(f"{user_id}: {label} diagnostic skipped after dwell - {e}")
    return active_page


async def wait_for_dwell_ready(page) -> float:
    if "live-view.html" not in str(page.url or ""):
        return 0.0
    started_at = time.monotonic()
    try:
        await page.bring_to_front()
    except Exception:
        pass
    try:
        await page.wait_for_load_state("load", timeout=8000)
    except Exception:
        pass
    try:
        await page.wait_for_load_state("networkidle", timeout=8000)
    except Exception:
        pass
    try:
        await page.locator("body").wait_for(state="visible", timeout=5000)
    except Exception:
        pass
    try:
        await wait_for_pincrux_live_view_ready(page, timeout_ms=12000)
    except Exception:
        pass
    try:
        await page.evaluate(
            """() => new Promise(resolve => {
                requestAnimationFrame(() => requestAnimationFrame(resolve));
            })"""
        )
    except Exception:
        pass
    return time.monotonic() - started_at


def emit_visited(user_id: str, url: str, emit: Callable[[str], None], dwell_seconds: Optional[float] = None, actual_dwell: Optional[float] = None, ready_wait: Optional[float] = None):
    if is_excluded_reward_url(url):
        emit(f"{user_id}: excluded reward URL - {url}")
        return
    message = f"{user_id}: visited {url}"
    if "live-view.html" in str(url or "") and actual_dwell is not None:
        message += f" dwell={actual_dwell:.1f}s"
        if dwell_seconds is not None:
            message += f" planned={float(dwell_seconds):.1f}s"
        if ready_wait:
            message += f" ready_wait={ready_wait:.1f}s"
    emit(message)


async def process_campaign2_link(page, link: str, session_db, user_id: str, emit: Callable[[str], None]):
    try:
        await page.goto(link, wait_until="domcontentloaded", timeout=30000)
    except Exception:
        await campaign_page_diagnostic(page, user_id, "campaign2 goto timeout/error", emit)
        raise
    try:
        await page.wait_for_load_state("networkidle", timeout=10000)
    except Exception:
        pass
    await wait_for_campaign2_popup(page)
    soup = BeautifulSoup(await page.content(), "html.parser")
    block_divs = soup.find_all("div", style=re.compile(r"display\s*:\s*block", re.IGNORECASE), class_=lambda x: x != "dimmed")
    block_div = block_divs[0] if block_divs else None
    if block_div is None:
        await campaign_page_diagnostic(page, user_id, "campaign2 result popup missing", emit)
        return DetailResult(user_id=user_id, url=link, status="skipped", message="result popup not found")

    text = block_div.get_text(" ", strip=True)
    status = "visited"
    if "1회만" in text and "적립" in text:
        message = "already received once"
        return DetailResult(user_id=user_id, url=link, status="skipped", message=message)
    if "적립돼요" in text:
        dwell_match = re.search(r"(\d+)\s*초\s*이상", text or "")
        required_dwell = int(dwell_match.group(1)) if dwell_match else 3
        dwell_seconds = required_dwell + 3
        try:
            await click_campaign2_point_button_and_dwell(page, dwell_seconds, user_id, emit)
        except ExcludedRewardUrl as e:
            return DetailResult(user_id=user_id, url=link, status="skipped", message=str(e))
        except Exception:
            await campaign_page_diagnostic(page, user_id, "campaign2 point button click failed", emit)
            raise
        status = "visited"
        message = f"visited by dwell rule: {required_dwell}s+"
    else:
        message = text
        if "적립 기간이 아닙니다" in text:
            campaign_url = session_db.query(CampaignUrl).filter_by(url=link).first()
            if campaign_url:
                campaign_url.is_available = False
            status = "unavailable"
    await asyncio.sleep(3)
    return DetailResult(user_id=user_id, url=link, status=status, point=0, message=message)


async def process_live_view_link(page, link: str, user_id: str, emit: Callable[[str], None], seconds: int = 1):
    try:
        await page.goto(link, wait_until="domcontentloaded", timeout=30000)
    except Exception:
        await campaign_page_diagnostic(page, user_id, "live-view goto timeout/error", emit)
        raise
    dwell_seconds = max(1, int(seconds or 1)) + 3
    ready_wait = await wait_for_dwell_ready(page)
    dwell_started_at = time.monotonic()
    await asyncio.sleep(dwell_seconds)
    reward_completed = await wait_for_pincrux_reward_completion(page, timeout_ms=5000)
    actual_dwell = time.monotonic() - dwell_started_at
    emit_visited(user_id, page.url, emit, dwell_seconds=dwell_seconds, actual_dwell=actual_dwell, ready_wait=ready_wait)
    message = f"live-view watched: {dwell_seconds}s"
    if reward_completed:
        message += "; reward completed"
    return DetailResult(user_id=user_id, url=link, status="visited", message=message)


async def wait_for_campaign2_popup(page, timeout_ms: int = 12000):
    deadline = time.time() + (timeout_ms / 1000)
    while time.time() < deadline:
        try:
            body_text = await page.locator("body").inner_text(timeout=1000)
        except Exception:
            body_text = ""
        if "포인트 받기" in body_text or "1회만" in body_text or "적립돼요" in body_text:
            return
        await asyncio.sleep(0.5)


async def click_campaign2_point_button_and_dwell(page, dwell_seconds: float, user_id: str, emit: Callable[[str], None]):
    context = page.context
    before_pages = list(context.pages)
    before_url = page.url
    clicked = await page.evaluate(
        """() => {
            const norm = s => (s || '').replace(/\\s+/g, ' ').trim();
            const candidates = Array.from(document.querySelectorAll('a.popup_link, button, a, [role="button"], [onclick]'));
            for (const el of candidates) {
                const text = norm(el.innerText || el.textContent);
                if (!text.includes('포인트 받기')) continue;
                const rect = el.getBoundingClientRect();
                const style = window.getComputedStyle(el);
                if (rect.width <= 0 || rect.height <= 0 || style.display === 'none' || style.visibility === 'hidden') continue;
                el.click();
                return true;
            }
            return false;
        }"""
    )
    if not clicked:
        raise RuntimeError("campaign2 point button not found")
    await asyncio.sleep(1)
    pages = list(context.pages)
    new_pages = [candidate for candidate in pages if candidate not in before_pages]
    active_page = new_pages[-1] if new_pages else page
    try:
        await active_page.wait_for_load_state("domcontentloaded", timeout=5000)
    except Exception:
        pass
    try:
        await active_page.bring_to_front()
    except Exception:
        pass
    if is_excluded_reward_url(active_page.url):
        emit(f"{user_id}: excluded reward URL - {active_page.url}")
        raise ExcludedRewardUrl("excluded reward URL")
    if not is_reward_url(active_page.url):
        emit(f"{user_id}: ignored unsupported reward landing - {active_page.url}")
        if active_page is not page:
            try:
                await active_page.close()
            except Exception:
                pass
            try:
                await page.bring_to_front()
            except Exception:
                pass
        return active_page
    ready_wait = await wait_for_dwell_ready(active_page)
    dwell_started_at = time.monotonic()
    await asyncio.sleep(dwell_seconds)
    actual_dwell = time.monotonic() - dwell_started_at
    emit_visited(user_id, active_page.url, emit, dwell_seconds=dwell_seconds, actual_dwell=actual_dwell, ready_wait=ready_wait)
    return active_page


async def handle_pincrux_shopping_live(page, link: str, user_id: str, emit: Callable[[str], None]):
    await campaign_page_diagnostic(page, user_id, "pincrux shopping live loaded", emit)
    short_reward_count = await handle_pincrux_short_live_cards(page, user_id, emit)
    if short_reward_count > 0:
        return DetailResult(user_id=user_id, url=link, status="visited", message=f"pincrux live cards visited: {short_reward_count}")

    clicked = False
    watched_page = page
    live_watch_seconds = await extract_pincrux_watch_seconds(page, default_seconds=5)
    live_dwell_seconds = live_watch_seconds + 3
    candidates = [
        ("text=라이브 중이에요!", True),
        ("text=5초 보면", True),
        ("xpath=//*[contains(normalize-space(.), '라이브 중이에요!')]/ancestor::*[self::a or self::button or @role='button' or @onclick][1]", False),
        ("xpath=//*[contains(normalize-space(.), '5초 보면')]/ancestor::*[self::a or self::button or @role='button' or @onclick][1]", False),
    ]
    for selector, js_click in candidates:
        try:
            locator = page.locator(selector).first
            if await locator.count() == 0:
                continue
            watched_page = await click_and_dwell(page, locator, live_dwell_seconds, user_id, emit, f"pincrux live selector={selector}", js_click=js_click)
            clicked = True
            break
        except Exception as e:
            emit(f"{user_id}: pincrux live click failed selector={selector} error={e}")
    if not clicked:
        await campaign_page_diagnostic(page, user_id, "pincrux live click target missing", emit)
        return DetailResult(user_id=user_id, url=link, status="skipped", message="pincrux live click target not found")

    completed = False
    finish_selectors = [
        "text=끝",
        "text=포인트 받기",
        "text=받기",
        "xpath=//*[contains(normalize-space(.), '끝') or contains(normalize-space(.), '포인트 받기') or contains(normalize-space(.), '받기')]/ancestor::*[self::a or self::button or @role='button' or @onclick][1]",
    ]
    for selector in finish_selectors:
        try:
            locator = watched_page.locator(selector).first
            if await locator.count() == 0:
                continue
            await click_and_dwell(watched_page, locator, 2, user_id, emit, f"pincrux live finish selector={selector}", js_click=selector.startswith("text="))
            completed = True
            break
        except Exception as e:
            emit(f"{user_id}: pincrux live finish click failed selector={selector} error={e}")
    await campaign_page_diagnostic(watched_page, user_id, "pincrux live watched", emit)
    try:
        body_text = await watched_page.locator("body").inner_text(timeout=3000)
    except Exception:
        body_text = ""
    message = f"pincrux live clicked, watched {live_dwell_seconds}s, and finish clicked" if completed else f"pincrux live clicked and watched {live_dwell_seconds}s; finish button not found"
    if short_reward_count > 0:
        message += f"; short live cards visited: {short_reward_count}"
    return DetailResult(user_id=user_id, url=link, status="visited", message=message)


async def extract_pincrux_watch_seconds(page, default_seconds: int = 5) -> int:
    try:
        body_text = await page.locator("body").inner_text(timeout=2000)
    except Exception:
        body_text = ""
    patterns = [
        r"(\d+)\s*초\s*이상\s*시청",
        r"(\d+)\s*초\s*보면",
    ]
    for pattern in patterns:
        match = re.search(pattern, body_text or "")
        if match:
            return max(1, int(match.group(1)))
    return max(1, int(default_seconds or 5))


async def handle_pincrux_short_live_cards(page, user_id: str, emit: Callable[[str], None]) -> int:
    await expand_pincrux_live_more(page)
    try:
        rows = await collect_pincrux_short_live_rows(page)
    except Exception:
        return 0

    visited = 0
    source_url = page.url
    for row in rows[:40]:
        href = str(row.get("href") or "")
        target_id = str(row.get("id") or "")
        if not href and not target_id:
            continue
        seconds = max(1, int(row.get("seconds") or 1))
        work_page = None
        try:
            if href:
                work_page = await page.context.new_page()
                await work_page.goto(href, wait_until="domcontentloaded", timeout=30000)
                active_page = work_page
            else:
                before_pages = list(page.context.pages)
                locator = page.locator(f'[data-naverpaper-short-live-id="{target_id}"]').first
                if await locator.count() == 0:
                    continue
                await locator.click(timeout=5000, force=True)
                await asyncio.sleep(1)
                pages = list(page.context.pages)
                active_page = ([candidate for candidate in pages if candidate not in before_pages] or [page])[-1]
            try:
                await active_page.wait_for_load_state("domcontentloaded", timeout=3000)
            except Exception:
                pass
            active_page = await follow_pincrux_note_reward_button(active_page, seconds)
            if is_excluded_reward_url(active_page.url):
                emit(f"{user_id}: excluded reward URL - {active_page.url}")
                continue
            dwell_seconds = seconds + 3
            ready_wait = await wait_for_dwell_ready(active_page)
            dwell_started_at = time.monotonic()
            await asyncio.sleep(dwell_seconds)
            reward_completed = await wait_for_pincrux_reward_completion(active_page, timeout_ms=5000)
            actual_dwell = time.monotonic() - dwell_started_at
            emit_visited(user_id, active_page.url, emit, dwell_seconds=dwell_seconds, actual_dwell=actual_dwell, ready_wait=ready_wait)
            if reward_completed:
                emit(f"{user_id}: pincrux reward completed - {active_page.url}")
            visited += 1
            if work_page is None and active_page is not page:
                try:
                    await active_page.close()
                except Exception:
                    pass
                try:
                    await page.bring_to_front()
                except Exception:
                    pass
            if work_page is None and page.url != source_url:
                try:
                    await page.goto(source_url, wait_until="domcontentloaded", timeout=20000)
                    await expand_pincrux_live_more(page)
                except Exception:
                    pass
        except Exception as e:
            emit(f"{user_id}: pincrux short live card failed - {e}")
        finally:
            if work_page is not None:
                try:
                    await work_page.close()
                except Exception:
                    pass
                try:
                    await page.bring_to_front()
                except Exception:
                    pass
    return visited


async def expand_pincrux_live_more(page, max_clicks: int = 5):
    for _ in range(max_clicks):
        try:
            button = page.locator("button, a, [role='button']").filter(has_text="라이브 더 보기").first
            if await button.count() == 0:
                break
            await button.scroll_into_view_if_needed(timeout=2000)
            await button.click(timeout=3000, force=True)
            await asyncio.sleep(0.7)
        except Exception:
            break


async def collect_pincrux_short_live_rows(page):
    return await page.evaluate(
        """() => {
            const norm = s => (s || '').replace(/\\s+/g, ' ').trim();
            const toHref = el => {
                const href = el.href || '';
                if (href) return href;
                const onclick = el.getAttribute('onclick') || '';
                const match = onclick.match(/Go\\(['"]([^'"]+)['"]/);
                if (!match) return '';
                try {
                    return new URL(match[1].replace(/&amp;/g, '&'), location.href).href;
                } catch (e) {
                    return match[1].replace(/&amp;/g, '&');
                }
            };
            const candidates = [];
            const seen = new Set();
            const roots = Array.from(document.querySelectorAll('a[href], button, [role="button"], [onclick], .comming-list-item'));
            for (const el of roots) {
                const text = norm(el.innerText || el.textContent);
                const onclick = el.getAttribute('onclick') || '';
                const href = toHref(el);
                const secondsMatch = text.match(/(\\d+)\\s*초\\s*보면/);
                const pointMatch = text.match(/(\\d+)\\s*원/);
                const isShortLive =
                    secondsMatch ||
                    ((href.includes('note.html') || onclick.includes('note.html')) && pointMatch) ||
                    ((href.includes('live-view.html') || onclick.includes('live-view.html')) && pointMatch);
                if (!isShortLive || /적립\\s*완료|적립완료/.test(text)) continue;
                const rect = el.getBoundingClientRect();
                if (rect.width <= 0 || rect.height <= 0) continue;
                if (!el.dataset.naverpaperShortLiveId) {
                    el.dataset.naverpaperShortLiveId = String(candidates.length + 1);
                }
                let seconds = secondsMatch ? Number(secondsMatch[1] || 1) : 1;
                if (!secondsMatch && (href.includes('live-view.html') || onclick.includes('live-view.html'))) {
                    seconds = 5;
                }
                const key = href || el.dataset.naverpaperShortLiveId || text;
                if (seen.has(key)) continue;
                seen.add(key);
                candidates.push({
                    id: el.dataset.naverpaperShortLiveId,
                    href,
                    text,
                    seconds,
                    point: pointMatch ? Number(pointMatch[1] || 0) : 0
                });
            }
            return candidates;
        }"""
    )


async def follow_pincrux_note_reward_button(page, seconds: int):
    try:
        body_text = await page.locator("body").inner_text(timeout=2000)
    except Exception:
        body_text = ""
    if "초 보고" not in body_text or "적립" not in body_text:
        return page
    before_pages = list(page.context.pages)
    clicked = await page.evaluate(
        """(seconds) => {
            const norm = s => (s || '').replace(/\\s+/g, ' ').trim();
            const exact = new RegExp(String(seconds) + '\\\\s*초\\\\s*보고\\\\s*\\\\d+\\\\s*원\\\\s*적립');
            const loose = /\\d+\\s*초\\s*보고\\s*\\d+\\s*원\\s*적립/;
            for (const el of Array.from(document.querySelectorAll('button, a, [role="button"], [onclick]'))) {
                const text = norm(el.innerText || el.textContent);
                if (!exact.test(text) && !loose.test(text)) continue;
                const rect = el.getBoundingClientRect();
                const style = window.getComputedStyle(el);
                if (rect.width <= 0 || rect.height <= 0 || style.display === 'none' || style.visibility === 'hidden') continue;
                el.click();
                return true;
            }
            return false;
        }""",
        seconds,
    )
    if not clicked:
        return page
    await asyncio.sleep(1)
    pages = list(page.context.pages)
    new_pages = [candidate for candidate in pages if candidate not in before_pages]
    active_page = new_pages[-1] if new_pages else page
    try:
        await active_page.wait_for_load_state("domcontentloaded", timeout=5000)
    except Exception:
        pass
    return active_page


async def expand_pincrux_live_more(page, max_clicks: int = 5):
    for _ in range(max_clicks):
        try:
            button = page.locator("button, a, [role='button']").filter(has_text=re.compile(r"\ub77c\uc774\ube0c\s*\ub354\s*\ubcf4\uae30")).first
            if await button.count() == 0:
                break
            await button.scroll_into_view_if_needed(timeout=2000)
            await button.click(timeout=3000, force=True)
            await asyncio.sleep(0.7)
        except Exception:
            break


async def collect_pincrux_short_live_rows(page):
    return await page.evaluate(
        """() => {
            const norm = s => (s || '').replace(/\\s+/g, ' ').trim();
            const toHref = el => {
                const href = el.href || '';
                if (href) return href;
                const onclick = el.getAttribute('onclick') || '';
                const match = onclick.match(/Go\\(['"]([^'"]+)['"]/);
                if (!match) return '';
                try {
                    return new URL(match[1].replace(/&amp;/g, '&'), location.href).href;
                } catch (e) {
                    return match[1].replace(/&amp;/g, '&');
                }
            };
            const candidates = [];
            const seen = new Set();
            const roots = Array.from(document.querySelectorAll('a[href], button, [role="button"], [onclick], .comming-list-item'));
            for (const el of roots) {
                const text = norm(el.innerText || el.textContent);
                const onclick = el.getAttribute('onclick') || '';
                const href = toHref(el);
                const secondsMatch = text.match(/(\\d+)\\s*\\uCD08\\s*\\uBCF4\\uBA74/);
                const pointMatch = text.match(/(\\d+)\\s*\\uC6D0/);
                const isShortLive =
                    secondsMatch ||
                    ((href.includes('note.html') || onclick.includes('note.html')) && pointMatch) ||
                    ((href.includes('live-view.html') || onclick.includes('live-view.html')) && pointMatch);
                if (!isShortLive || /\\uC801\\uB9BD\\s*\\uC644\\uB8CC|\\uC801\\uB9BD\\uC644\\uB8CC/.test(text)) continue;
                const rect = el.getBoundingClientRect();
                const style = window.getComputedStyle(el);
                if (rect.width <= 0 || rect.height <= 0 || style.display === 'none' || style.visibility === 'hidden') continue;
                if (!el.dataset.naverpaperShortLiveId) {
                    el.dataset.naverpaperShortLiveId = String(candidates.length + 1);
                }
                let seconds = secondsMatch ? Number(secondsMatch[1] || 1) : 1;
                if (!secondsMatch && (href.includes('live-view.html') || onclick.includes('live-view.html'))) {
                    seconds = 5;
                }
                const key = href || el.dataset.naverpaperShortLiveId || text;
                if (seen.has(key)) continue;
                seen.add(key);
                candidates.push({
                    id: el.dataset.naverpaperShortLiveId,
                    href,
                    text,
                    seconds,
                    point: pointMatch ? Number(pointMatch[1] || 0) : 0
                });
            }
            return candidates;
        }"""
    )


async def follow_pincrux_note_reward_button(page, seconds: int):
    clicked = await page.evaluate(
        """(seconds) => {
            const norm = s => (s || '').replace(/\\s+/g, ' ').trim();
            const rewardText = new RegExp('\\\\d+\\\\s*\\uCD08\\\\s*\\uBCF4\\uACE0\\\\s*\\\\d+\\\\s*\\uC6D0\\\\s*\\uC801\\uB9BD');
            const candidates = Array.from(document.querySelectorAll('button, a, [role="button"], [onclick], .cta-btn'));
            for (const el of candidates) {
                const text = norm(el.innerText || el.textContent);
                const onclick = el.getAttribute('onclick') || '';
                const rect = el.getBoundingClientRect();
                const style = window.getComputedStyle(el);
                if (rect.width <= 0 || rect.height <= 0 || style.display === 'none' || style.visibility === 'hidden') continue;
                const ctaLike = onclick.includes('showLive') || (el.className || '').includes('cta-btn');
                if (!ctaLike && !rewardText.test(text)) continue;
                el.click();
                return {text, onclick, tag: el.tagName};
            }
            return null;
        }""",
        seconds,
    )
    if not clicked:
        return page
    deadline = time.time() + 12
    while time.time() < deadline:
        if "live-view.html" in str(page.url or ""):
            break
        try:
            await page.wait_for_load_state("domcontentloaded", timeout=1000)
        except Exception:
            pass
        await asyncio.sleep(0.3)
    try:
        await page.wait_for_load_state("domcontentloaded", timeout=5000)
    except Exception:
        pass
    return page


async def wait_for_pincrux_live_view_ready(page, timeout_ms: int = 12000) -> bool:
    if "live-view.html" not in str(page.url or ""):
        return False
    deadline = time.time() + (timeout_ms / 1000)
    while time.time() < deadline:
        try:
            if await page.locator("iframe").count() > 0:
                for frame in page.frames:
                    if "view.shoppinglive.naver.com" not in str(frame.url or ""):
                        continue
                    try:
                        await frame.wait_for_load_state("domcontentloaded", timeout=1000)
                    except Exception:
                        pass
                    try:
                        video_count = await frame.locator("video").count()
                    except Exception:
                        video_count = 0
                    if video_count > 0:
                        return True
                    try:
                        body_text = await frame.locator("body").inner_text(timeout=1000)
                    except Exception:
                        body_text = ""
                    if body_text.strip():
                        return True
        except Exception:
            pass
        await asyncio.sleep(0.4)
    return False


async def wait_for_pincrux_reward_completion(page, timeout_ms: int = 5000) -> bool:
    deadline = time.time() + (timeout_ms / 1000)
    while time.time() < deadline:
        try:
            body_text = await page.locator("body").inner_text(timeout=1000)
        except Exception:
            body_text = ""
        if "\uc801\ub9bd \uc644\ub8cc" in body_text or "\uc801\ub9bd\uc644\ub8cc" in body_text:
            return True
        await asyncio.sleep(0.4)
    return False


def ppomppu_target_url(link: str) -> str:
    try:
        target = (parse_qs(urlparse(link).query).get("target") or [""])[0]
        if not target:
            return ""
        padding = "=" * (-len(target) % 4)
        return base64.urlsafe_b64decode((target + padding).encode("ascii")).decode("utf-8", errors="ignore")
    except Exception:
        return ""


async def visit_campaign_url(page, link: str, session_db, user_id: str, emit: Callable[[str], None], excluded_reward_sources: Optional[set[str]] = None):
    excluded_reward_sources = excluded_reward_sources if excluded_reward_sources is not None else set()
    if reward_source_key(link) in excluded_reward_sources:
        emit(f"{user_id}: skipped known excluded reward source - {link}")
        return DetailResult(user_id=user_id, url=link, status="skipped", message="known excluded reward source")
    if is_excluded_reward_url(link):
        return DetailResult(user_id=user_id, url=link, status="skipped", message="excluded reward URL")
    if link.startswith("https://campaign2"):
        return await process_campaign2_link(page, link, session_db, user_id, emit)
    if link.startswith("https://nsl.pincrux.com/live-view.html"):
        return await process_live_view_link(page, link, user_id, emit)

    try:
        await page.goto(link, wait_until="domcontentloaded", timeout=20000)
        if is_pincrux_source_url(link):
            await wait_for_pincrux_source_redirect(page)
    except Exception:
        await campaign_page_diagnostic(page, user_id, "generic campaign goto timeout/error", emit)
        raise
    try:
        await page.wait_for_load_state("networkidle", timeout=5000)
    except Exception:
        pass
    if is_excluded_reward_url(page.url):
        emit(f"{user_id}: excluded reward URL - {page.url}")
        record_excluded_reward_source(session_db, excluded_reward_sources, link, page.url)
        return DetailResult(user_id=user_id, url=link, status="skipped", message="excluded reward URL")
    emit(f"{user_id}: visited {page.url}")
    if page.url.startswith("https://campaign2"):
        return await process_campaign2_link(page, page.url, session_db, user_id, emit)
    if page.url.startswith("https://nsl.pincrux.com/live-view.html"):
        return await process_live_view_link(page, page.url, user_id, emit)
    if "nsl.pincrux.com/note.html" in page.url or "nsl.pincrux.com/note.html" in link:
        seconds = await extract_pincrux_watch_seconds(page, default_seconds=1)
        active_page = await follow_pincrux_note_reward_button(page, seconds)
        if active_page.url.startswith("https://nsl.pincrux.com/live-view.html"):
            return await process_live_view_link(active_page, active_page.url, user_id, emit, seconds)
        return DetailResult(user_id=user_id, url=link, status="skipped", message=f"pincrux note reward button not available: {active_page.url}")
    if "point.pay.naver.com/mission-detail" in page.url or "point.pay.naver.com/pc/mission-detail" in page.url:
        return await handle_naverpay_mission_detail(page, link, session_db, user_id, emit, excluded_reward_sources)
    if "nsl.pincrux.com/shopping-live" in page.url or "nsl.pincrux.com/shopping-live" in link:
        return await handle_pincrux_shopping_live(page, link, user_id, emit)
    return DetailResult(user_id=user_id, url=link, status="skipped", message=f"unsupported final URL: {page.url}")


async def handle_naverpay_mission_detail(page, link: str, session_db, user_id: str, emit: Callable[[str], None], excluded_reward_sources: Optional[set[str]] = None):
    excluded_reward_sources = excluded_reward_sources if excluded_reward_sources is not None else set()
    try:
        await collect_lazy_page_content(page)
        rows = await collect_naverpay_mission_reward_rows(page)
    except Exception:
        rows = []

    visited = 0
    source_url = page.url
    for row in rows[:40]:
        href = str(row.get("href") or "")
        target_id = str(row.get("id") or "")
        source_key = reward_source_key(str(row.get("source_key") or "") or href or str(row.get("text") or ""))
        row_seconds = max(1, int(row.get("seconds") or 5))
        if not href and not target_id:
            continue
        if source_key and source_key in excluded_reward_sources:
            emit(f"{user_id}: skipped known excluded reward source - {source_key}")
            continue
        if is_excluded_reward_url(href):
            emit(f"{user_id}: excluded reward URL - {href}")
            record_excluded_reward_source(session_db, excluded_reward_sources, source_key, href)
            continue
        work_page = None
        try:
            if href:
                if href == source_url:
                    continue
                work_page = await page.context.new_page()
                await work_page.goto(href, wait_until="domcontentloaded", timeout=30000)
                active_page = work_page
            else:
                before_pages = list(page.context.pages)
                locator = page.locator(f'[data-naverpaper-mission-id="{target_id}"]').first
                if await locator.count() == 0:
                    continue
                await locator.scroll_into_view_if_needed(timeout=2000)
                await locator.click(timeout=5000, force=True)
                await asyncio.sleep(1)
                pages = list(page.context.pages)
                active_page = ([candidate for candidate in pages if candidate not in before_pages] or [page])[-1]
            try:
                await active_page.wait_for_load_state("domcontentloaded", timeout=5000)
            except Exception:
                pass
            try:
                await active_page.bring_to_front()
            except Exception:
                pass
            await wait_for_bridge_redirect(active_page)
            if is_excluded_reward_url(active_page.url):
                emit(f"{user_id}: excluded reward URL - {active_page.url}")
                record_excluded_reward_source(session_db, excluded_reward_sources, source_key, active_page.url)
                if work_page is None and active_page is not page:
                    try:
                        await active_page.close()
                    except Exception:
                        pass
                continue
            if active_page.url.startswith("https://campaign2"):
                detail = await process_campaign2_link(active_page, active_page.url, session_db, user_id, emit)
                if detail.status not in ("skipped", "unavailable", "no_url", "error"):
                    visited += 1
            else:
                dwell_seconds = row_seconds + 3 if "live-view.html" in active_page.url else 4
                ready_wait = await wait_for_dwell_ready(active_page)
                dwell_started_at = time.monotonic()
                await asyncio.sleep(dwell_seconds)
                actual_dwell = time.monotonic() - dwell_started_at
                emit_visited(user_id, active_page.url, emit, dwell_seconds=dwell_seconds, actual_dwell=actual_dwell, ready_wait=ready_wait)
                visited += 1
            if work_page is None and active_page is not page:
                try:
                    await active_page.close()
                except Exception:
                    pass
            if work_page is None and page.url != source_url:
                try:
                    await page.goto(source_url, wait_until="domcontentloaded", timeout=20000)
                    await collect_lazy_page_content(page)
                except Exception:
                    pass
        except Exception as e:
            emit(f"{user_id}: naverpay mission click failed - {e}")
        finally:
            if work_page is not None:
                try:
                    await work_page.close()
                except Exception:
                    pass
                try:
                    await page.bring_to_front()
                except Exception:
                    pass
    return DetailResult(user_id=user_id, url=link, status="visited", message=f"mission rewards visited: {visited}")


async def wait_for_bridge_redirect(page, timeout_ms: int = 8000):
    start_url = page.url
    deadline = time.time() + (timeout_ms / 1000)
    while time.time() < deadline:
        current_url = page.url
        if current_url != start_url and "point.pay.naver.com/bridge/eventbenefit" not in current_url:
            break
        try:
            await page.wait_for_load_state("domcontentloaded", timeout=1000)
        except Exception:
            pass
        await asyncio.sleep(0.5)
    try:
        await page.wait_for_load_state("networkidle", timeout=2000)
    except Exception:
        pass


async def wait_for_pincrux_source_redirect(page, timeout_ms: int = 12000):
    deadline = time.time() + (timeout_ms / 1000)
    while time.time() < deadline:
        current_url = str(page.url or "")
        if (
            current_url.startswith("https://nsl.pincrux.com/live-view.html")
            or "nsl.pincrux.com/shopping-live" in current_url
            or is_excluded_reward_url(current_url)
        ):
            break
        try:
            await page.wait_for_load_state("domcontentloaded", timeout=1000)
        except Exception:
            pass
        await asyncio.sleep(0.5)
    try:
        await page.wait_for_load_state("networkidle", timeout=2000)
    except Exception:
        pass


async def collect_lazy_page_content(page):
    try:
        last_height = 0
        stable_count = 0
        for _ in range(12):
            height = await page.evaluate("() => document.body.scrollHeight")
            if height == last_height:
                stable_count += 1
            else:
                stable_count = 0
            if stable_count >= 2:
                break
            last_height = height
            await page.evaluate("() => window.scrollTo(0, document.body.scrollHeight)")
            await asyncio.sleep(0.7)
        await page.evaluate("() => window.scrollTo(0, 0)")
        await asyncio.sleep(0.2)
    except Exception:
        pass


async def collect_naverpay_mission_reward_rows(page):
    return await page.evaluate(
        """() => {
            const norm = s => (s || '').replace(/\\s+/g, ' ').trim();
            const candidates = [];
            const seen = new Set();
            const elements = Array.from(document.querySelectorAll('a[href], button, [role="button"], [onclick]'));
            for (const el of elements) {
                const row = el.closest('li, article, section, div') || el;
                const text = norm(row.innerText || row.textContent);
                if (!/클릭\\s*\\d+원/.test(text)) continue;
                if (/적립\\s*완료|적립완료/.test(text)) continue;
                const actionText = norm(el.innerText || el.textContent);
                if (actionText && !/(혜택보기|쿠폰받기|확인하기|적립|받기|클릭)/.test(actionText) && !/클릭\\s*\\d+원/.test(actionText)) continue;
                const rect = el.getBoundingClientRect();
                if (rect.width <= 0 || rect.height <= 0) continue;
                if (!el.dataset.naverpaperMissionId) {
                    el.dataset.naverpaperMissionId = String(candidates.length + 1);
                }
                const href = el.href || '';
                const onclick = el.getAttribute('onclick') || '';
                const secondsMatch = text.match(/(\\d+)\\s*ì´ˆ/);
                const key = href || el.dataset.naverpaperMissionId || text;
                if (seen.has(key)) continue;
                seen.add(key);
                candidates.push({ id: el.dataset.naverpaperMissionId, href, text, source_key: href || onclick || text, seconds: secondsMatch ? Number(secondsMatch[1] || 5) : 5 });
            }
            return candidates;
        }"""
    )


async def process_campaign_links(page, campaign_links, session_db, user_id: str, emit: Callable[[str], None], excluded_reward_sources: Optional[set[str]] = None):
    excluded_reward_sources = excluded_reward_sources if excluded_reward_sources is not None else set()
    details = []
    for link in campaign_links:
        try:
            link_key = reward_source_key(link)
            if link_key in excluded_reward_sources:
                emit(f"{user_id}: skipped known excluded reward source - {link}")
                details.append(DetailResult(user_id=user_id, url=link, status="skipped", message="known excluded reward source"))
                continue
            if not is_reward_url(link) and not is_pincrux_reward_candidate(link):
                details.append(DetailResult(user_id=user_id, url=link, status="skipped", message="unsupported reward URL"))
                continue
            if is_excluded_reward_url(link):
                emit(f"{user_id}: excluded reward URL - {link}")
                record_excluded_reward_source(session_db, excluded_reward_sources, link, link)
                details.append(DetailResult(user_id=user_id, url=link, status="skipped", message="excluded reward URL"))
                continue
            if link.startswith("https://campaign2"):
                detail = await process_campaign2_link(page, link, session_db, user_id, emit)
            elif link.startswith("https://s.ppomppu.co.kr"):
                fallback_url = ppomppu_target_url(link)
                try:
                    await page.goto(link, wait_until="domcontentloaded", timeout=10000)
                    try:
                        await page.wait_for_load_state("networkidle", timeout=5000)
                    except Exception:
                        pass
                except Exception as e:
                    await campaign_page_diagnostic(page, user_id, "ppomppu redirect timeout/error", emit)
                    emit(f"{user_id}: ppomppu redirect timeout; fallback={fallback_url or 'none'} error={e}")
                redirected_url = page.url
                target_url = redirected_url
                if redirected_url.startswith("https://s.ppomppu.co.kr") and fallback_url:
                    target_url = fallback_url
                if target_url in excluded_reward_sources:
                    detail = DetailResult(user_id=user_id, url=link, status="skipped", message="known excluded reward source")
                    emit(f"{user_id}: skipped known excluded reward source - {target_url}")
                    details.append(detail)
                    continue
                if is_excluded_reward_url(target_url):
                    detail = DetailResult(user_id=user_id, url=link, status="skipped", message="excluded reward URL")
                    emit(f"{user_id}: excluded reward URL - {target_url}")
                    record_excluded_reward_source(session_db, excluded_reward_sources, target_url, target_url)
                    record_excluded_reward_source(session_db, excluded_reward_sources, link, target_url)
                    details.append(detail)
                    continue
                if target_url.startswith("https://campaign2"):
                    detail = await process_campaign2_link(page, target_url, session_db, user_id, emit)
                else:
                    detail = await visit_campaign_url(page, target_url, session_db, user_id, emit, excluded_reward_sources)
                detail.url = link
            else:
                detail = await visit_campaign_url(page, link, session_db, user_id, emit, excluded_reward_sources)
            if is_login_redirect(page.url) or is_login_redirect(getattr(detail, "message", "")):
                emit(f"{user_id}: session expired while processing (redirected to login); stopping and NOT recording as visited: {link}")
                details.append(DetailResult(user_id=user_id, url=link, status="login_error", message="session expired during run"))
                break
            if is_reward_url(link) and not should_revisit_url(link):
                existing_visit = session_db.query(UrlVisit).filter_by(url=link, user_id=user_id).first()
                if not existing_visit:
                    session_db.add(UrlVisit(url=link, user_id=user_id, visited_at=datetime.now()))
            details.append(detail)
        except Exception as e:
            details.append(DetailResult(user_id=user_id, url=link, status="error", message=str(e)))
            emit(f"{user_id}: campaign URL error - {link} - {e}")
    return details


def playwright_proxy(proxy_url: str):
    proxy_url = str(proxy_url or "").strip()
    if not proxy_url:
        return None
    parsed = urlparse(proxy_url)
    if parsed.scheme and parsed.hostname:
        host = parsed.hostname
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"
        server = f"{parsed.scheme}://{host}"
        if parsed.port:
            server = f"{server}:{parsed.port}"
        proxy = {"server": server}
        if parsed.username:
            proxy["username"] = unquote(parsed.username)
        if parsed.password:
            proxy["password"] = unquote(parsed.password)
        return proxy
    return {"server": proxy_url}


def mask_proxy_url(proxy_url: str) -> str:
    proxy_url = str(proxy_url or "").strip()
    if not proxy_url:
        return "disabled"
    parsed = urlparse(proxy_url)
    if parsed.scheme and parsed.hostname:
        host = parsed.hostname
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"
        server = f"{parsed.scheme}://{host}"
        if parsed.port:
            server = f"{server}:{parsed.port}"
        if parsed.username:
            return f"{server} (auth)"
        return server
    return proxy_url


async def process_account(
    account: AccountConfig,
    session_db,
    cookie_dir: str,
    reward_proxy_url: str,
    emit: Callable[[str], None],
    login_proxy_url: str = "",
    candidate_urls: Optional[set[str]] = None,
    excluded_reward_sources: Optional[set[str]] = None,
) -> AccountResult:
    from playwright.async_api import async_playwright
    from playwright_stealth import Stealth

    result = AccountResult(user_id=account.user_id)
    campaign_links, already_visited_count = await fetch_naver_campaign_urls(session_db, account.user_id, candidate_urls)
    result.target_url_count = len(campaign_links) + already_visited_count
    if already_visited_count:
        emit(f"{account.user_id}: skipped previously visited URLs={already_visited_count}")
        result.skipped_url_count += already_visited_count
    if not campaign_links:
        return result

    # 수집 단계에서 세션 만료가 확인됐고 재로그인도 실패한 경우: 링크를 처리하면 방문 기록만 남으므로 건너뛴다
    ev = session_event(account.user_id)
    if ev.get("expired") and ev.get("relogin") in ("failed", "skipped"):
        emit(f"{account.user_id}: session expired and re-login failed; leaving {len(campaign_links)} link(s) unvisited (not recorded)")
        result.session_expired = True
        result.relogin = ev.get("relogin", "")
        result.details.append(DetailResult(user_id=account.user_id, status="login_error", message="session expired; re-login failed"))
        return result

    if ev.get("login_failed") and ev.get("relogin") != "ok":
        # 수집 단계에서 이미 로그인에 실패했다면 같은 실행에서 다시 시도하지 않는다 (캡차/보안 위험)
        emit(f"{account.user_id}: login already failed in this run; leaving {len(campaign_links)} link(s) unvisited (not recorded)")
        result.login_failed = True
        result.login_error = ev.get("login_error", "")
        result.details.append(DetailResult(user_id=account.user_id, status="login_error", message="cookie auto refresh failed"))
        return result

    storage_state = await ensure_cookie_storage_state(account, session_db, cookie_dir, login_proxy_url, emit)
    if not storage_state:
        result.login_failed = True
        result.login_error = session_event(account.user_id).get("login_error", "")
        result.details.append(DetailResult(user_id=account.user_id, status="login_error", message="cookie auto refresh failed"))
        return result
    os.makedirs(cookie_dir, exist_ok=True)
    async with async_playwright() as playwright:
        for attempt in (1, 2):
            launch_kwargs = {"headless": True}
            proxy = playwright_proxy(reward_proxy_url)
            if proxy:
                launch_kwargs["proxy"] = proxy
            browser = await playwright.chromium.launch(**launch_kwargs)
            context = await browser.new_context(
                user_agent=IPHONE13_UA,
                viewport={"width": 390, "height": 844},
                device_scale_factor=3,
                is_mobile=True,
                has_touch=True,
                storage_state=storage_state,
            )
            page = await context.new_page()
            await Stealth().apply_stealth_async(page)

            expired = False
            try:
                login_ok, login_message = await naver_login(page, account, cookie_dir, emit)
                if not login_ok:
                    result.login_failed = True
                    result.login_error = login_message
                    result.details.append(DetailResult(user_id=account.user_id, status="login_error", message=login_message))
                else:
                    before_summary = await get_naverpay_point_summary(page, emit, account.user_id)
                    before_points = before_summary["balance"]
                    if is_login_redirect(page.url):
                        # 쿠키 파일은 유효해 보여도 서버에서 세션이 무효화된 상태
                        emit(f"{account.user_id}: session expired (redirected to login) url={page.url}")
                        note_session_event(account.user_id, expired=True)
                        expired = True
                    else:
                        if before_points is None:
                            emit(f"{account.user_id}: point balance unavailable before run (could not read the balance)")
                        details = await process_campaign_links(page, campaign_links, session_db, account.user_id, emit, excluded_reward_sources)
                        after_summary = await get_naverpay_point_summary(page, emit, account.user_id)
                        after_points = after_summary["balance"]
                        result.details.extend(details)
                        result.skipped_url_count += len([x for x in details if x.status in ("skipped", "unavailable", "no_url")])
                        result.visited_url_count = len([x for x in details if x.status not in ("error", "skipped", "unavailable", "no_url", "login_error")])
                        result.estimated_points, baseline_ok, used_event = _compute_earned_points(cookie_dir, account.user_id, before_summary, after_summary, emit)
                        if not used_event:
                            _mark_balance_reliability(result, before_points, after_points, emit)
                        if not baseline_ok:
                            result.balance_unreliable = True
                        if any(x.status == "login_error" for x in details):
                            result.session_expired = True
                            note_session_event(account.user_id, expired=True)
                        else:
                            new_storage = await context.storage_state()
                            session_db.merge(User(user_id=account.user_id, storage_state=json.dumps(new_storage), updated_at=datetime.now()))
                            with open(cookie_path(cookie_dir, account.user_id), "w", encoding="utf-8") as f:
                                json.dump(new_storage, f, ensure_ascii=False, indent=2)
            finally:
                await context.close()
                await browser.close()

            if expired:
                if attempt == 1 and await repair_expired_session(account, session_db, cookie_dir, login_proxy_url, emit):
                    storage_state = get_storage_state(account.user_id, session_db, cookie_dir)
                    continue  # 새 세션으로 한 번 더 시도
                result.session_expired = True
                result.details.append(DetailResult(user_id=account.user_id, status="login_error", message="session expired; re-login failed"))
            break

    result.relogin = session_event(account.user_id).get("relogin", "")
    return result


def _parse_point_balance(lines):
    """페이지의 줄 목록에서 '내 포인트' 잔액을 찾는다. (잔액 또는 None, '내 포인트' 줄 위치 또는 -1)"""
    for idx, line in enumerate(lines):
        same_line = re.fullmatch(r"내 포인트\s*([\d,]+)\s*(원|P)", line)
        if same_line:
            return int(re.sub(r"[^0-9]", "", same_line.group(1)) or "0"), idx
        if line != "내 포인트":
            continue
        window = lines[idx + 1 : idx + 8]
        for item in window:  # 금액만 있는 줄을 우선
            if re.fullmatch(r"[\d,]+\s*(원|P)", item):
                return int(re.sub(r"[^0-9]", "", item) or "0"), idx
        for item in window:  # 예전 방식: 숫자가 있고 '원'으로 끝나는 첫 줄
            if item.endswith("원") and any(ch.isdigit() for ch in item):
                return int(re.sub(r"[^0-9]", "", item) or "0"), idx
        return None, idx
    return None, -1


def _to_amount(text: str) -> int:
    return int(re.sub(r"[^0-9]", "", str(text)) or "0")


def _first_amount_after(lines, idx: int, span: int = 4) -> Optional[int]:
    for item in lines[idx + 1 : idx + 1 + span]:
        if re.fullmatch(r"[\d,]+\s*원", item):
            return _to_amount(item)
    return None


def _parse_point_benefits(lines):
    """포인트 화면의 '이번 달 적립 혜택' 요약에서 이벤트적립 금액을 읽는다.
    화면 구성: 기간 줄('2026.11.01. ~ 11.30.'), '총 적립 혜택' 다음 금액, '이벤트적립' 다음 금액('누적 적립 N%' 줄이 사이에 있을 수 있음)."""
    month = ""
    for line in lines:
        found = re.match(r"(\d{4})\.(\d{1,2})\.\d{1,2}\.?\s*~", line)
        if found:
            month = f"{found.group(1)}-{int(found.group(2)):02d}"
            break
    month_total = event = None
    for idx, line in enumerate(lines):
        if month_total is None and line == "총 적립 혜택":
            month_total = _first_amount_after(lines, idx)
        if event is None and line == "이벤트적립":
            event = _first_amount_after(lines, idx)
    return {"event": event, "month_total": month_total, "month": month}


async def get_naverpay_point_summary(page, emit: Optional[Callable[[str], None]] = None, label: str = "", want_event: bool = True) -> dict:
    """네이버페이 포인트 화면에서 잔액과 이번 달 이벤트적립 금액을 읽는다.
    반환: {"balance": int|None, "event": int|None, "month_total": int|None, "month": "YYYY-MM"|""}
    로딩 중에는 '0원' 같은 기본값이 보일 수 있어, 0보다 큰 잔액(그리고 want_event 이면 이벤트적립)이 나올 때까지 다시 읽는다."""
    summary = {"balance": None, "event": None, "month_total": None, "month": ""}
    value, idx, lines = None, -1, []
    seen_at = None
    try:
        await page.goto("https://pay.naver.com/pointshistory/list?category=all", wait_until="domcontentloaded", timeout=30000)
        deadline = time.time() + 12
        await page.wait_for_timeout(2500)
        while True:
            body = await page.locator("body").inner_text(timeout=5000)
            lines = [x.strip() for x in body.splitlines() if x.strip()]
            value, idx = _parse_point_balance(lines)
            summary.update(_parse_point_benefits(lines))
            if value is not None and value > 0:
                if seen_at is None:
                    seen_at = time.time()
                # 이벤트적립 요약은 잔액보다 늦게 나타날 수 있어 최대 5초 더 기다린다
                if not want_event or summary["event"] is not None or time.time() - seen_at >= 5:
                    summary["balance"] = value
                    return summary
            if time.time() >= deadline:
                break
            await page.wait_for_timeout(1500)
    except Exception as e:
        if emit:
            emit(f"{label}: point balance read error - {e}")
        return summary
    summary["balance"] = value
    if emit:
        sample = lines[idx : idx + 8] if idx >= 0 else lines[:12]
        emit(f"{label}: point balance diagnostic - value={value} lines near '내 포인트'={sample}")
    return summary


async def get_naverpay_point_balance(page, emit: Optional[Callable[[str], None]] = None, label: str = "") -> Optional[int]:
    """잔액만 필요할 때 (이벤트적립 요약을 기다리지 않는다)."""
    return (await get_naverpay_point_summary(page, emit, label, want_event=False))["balance"]


async def process_account_manual_link(
    account: AccountConfig,
    session_db,
    cookie_dir: str,
    reward_proxy_url: str,
    link: str,
    emit: Callable[[str], None],
    login_proxy_url: str = "",
    _retried: bool = False,
) -> AccountResult:
    from playwright.async_api import async_playwright
    from playwright_stealth import Stealth

    result = AccountResult(user_id=account.user_id)
    result.target_url_count = 1
    storage_state = await ensure_cookie_storage_state(account, session_db, cookie_dir, login_proxy_url, emit)
    if not storage_state:
        result.login_failed = True
        result.login_error = session_event(account.user_id).get("login_error", "")
        result.details.append(DetailResult(user_id=account.user_id, url=link, status="login_error", message="cookie auto refresh failed"))
        return result
    os.makedirs(cookie_dir, exist_ok=True)
    expired = False
    async with async_playwright() as playwright:
        launch_kwargs = {"headless": True}
        proxy = playwright_proxy(reward_proxy_url)
        if proxy:
            launch_kwargs["proxy"] = proxy
        browser = await playwright.chromium.launch(**launch_kwargs)
        context = await browser.new_context(
            user_agent=IPHONE13_UA,
            viewport={"width": 390, "height": 844},
            device_scale_factor=3,
            is_mobile=True,
            has_touch=True,
            storage_state=storage_state,
        )
        page = await context.new_page()
        await Stealth().apply_stealth_async(page)

        login_ok, login_message = await naver_login(page, account, cookie_dir, emit)
        if login_ok:
            before_summary = await get_naverpay_point_summary(page, emit, account.user_id)
            before_points = before_summary["balance"]
            if is_login_redirect(page.url):
                emit(f"{account.user_id}: session expired (redirected to login) url={page.url}")
                note_session_event(account.user_id, expired=True)
                expired = True
            else:
                details = await process_campaign_links(page, [link], session_db, account.user_id, emit)
                after_summary = await get_naverpay_point_summary(page, emit, account.user_id)
                after_points = after_summary["balance"]
                result.details.extend(details)
                result.skipped_url_count += len([x for x in details if x.status in ("skipped", "unavailable", "no_url")])
                result.visited_url_count = len([x for x in details if x.status not in ("error", "skipped", "unavailable", "no_url", "login_error")])
                result.estimated_points, baseline_ok, used_event = _compute_earned_points(cookie_dir, account.user_id, before_summary, after_summary, emit)
                if not used_event:
                    _mark_balance_reliability(result, before_points, after_points, emit)
                if not baseline_ok:
                    result.balance_unreliable = True
                if any(x.status == "login_error" for x in details):
                    result.session_expired = True
                    note_session_event(account.user_id, expired=True)
                else:
                    new_storage = await context.storage_state()
                    session_db.merge(User(user_id=account.user_id, storage_state=json.dumps(new_storage), updated_at=datetime.now()))
                    with open(cookie_path(cookie_dir, account.user_id), "w", encoding="utf-8") as f:
                        json.dump(new_storage, f, ensure_ascii=False, indent=2)
        else:
            result.login_failed = True
            result.login_error = login_message
            result.details.append(DetailResult(user_id=account.user_id, url=link, status="login_error", message=login_message))
        await context.close()
        await browser.close()

    if expired:
        if not _retried and await repair_expired_session(account, session_db, cookie_dir, login_proxy_url, emit):
            return await process_account_manual_link(account, session_db, cookie_dir, reward_proxy_url, link, emit, login_proxy_url, _retried=True)
        result.session_expired = True
        result.details.append(DetailResult(user_id=account.user_id, url=link, status="login_error", message="session expired; re-login failed"))
    result.relogin = session_event(account.user_id).get("relogin", "")
    return result


async def fetch(url: str, session, proxy_url: str = "", user_agent: str = IPHONE13_UA):
    headers = {"User-Agent": user_agent or IPHONE13_UA}
    proxy = str(proxy_url or "").strip() or None
    async with session.get(url, headers=headers, proxy=proxy, timeout=20) as response:
        return await response.text(errors="ignore")


def _looks_like_cloudflare(html: str) -> bool:
    if not html:
        return True
    lowered = html.lower()
    return "just a moment" in lowered or "cf_chl_" in lowered or "challenge-platform" in lowered or "verify you are human" in lowered


async def fetch_with_playwright(url: str, proxy_url: str = "") -> str:
    from playwright.async_api import async_playwright
    from playwright_stealth import Stealth

    async with async_playwright() as pw:
        launch_kwargs = {"headless": True}
        proxy = playwright_proxy(proxy_url)
        if proxy:
            launch_kwargs["proxy"] = proxy
        browser = await pw.chromium.launch(**launch_kwargs)
        context = await browser.new_context(
            user_agent=IPHONE13_UA,
            locale="ko-KR",
            viewport={"width": 390, "height": 844},
            is_mobile=True,
            has_touch=True,
        )
        page = await context.new_page()
        await Stealth().apply_stealth_async(page)
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=30000)
            try:
                await page.wait_for_load_state("networkidle", timeout=10000)
            except Exception:
                pass
            return await page.content()
        finally:
            await context.close()
            await browser.close()


async def get_html(url: str, session, emit: Callable[[str], None], proxy_url: str = "", user_agent: str = IPHONE13_UA) -> str:
    html = await fetch(url, session, proxy_url, user_agent=user_agent)
    if _looks_like_cloudflare(html):
        try:
            html = await fetch_with_playwright(url, proxy_url)
        except Exception as e:
            emit(f"{url}: Playwright fallback failed - {e}")
    return html or ""


async def get_soup(url: str, session, emit: Callable[[str], None], proxy_url: str = "", user_agent: str = IPHONE13_UA) -> BeautifulSoup:
    html = await get_html(url, session, emit, proxy_url, user_agent=user_agent)
    return BeautifulSoup(html or "", "html.parser")


async def process_url(url, session, process_func, collected_urls, emit, proxy_url: str = ""):
    soup = await get_soup(url, session, emit, proxy_url)
    await process_func(url, soup, session, collected_urls, emit, proxy_url)


async def process_clien_url(url, soup, session, collected_urls, emit, proxy_url: str = ""):
    initial_count = len(collected_urls)
    naver_links = []
    for span in soup.select('[class="list_item symph-row"]'):
        a_tag = span.select_one(':-soup-contains("네이버")')
        if a_tag and span.get("href"):
            naver_links.append(span["href"])

    for link in naver_links:
        full_link = urljoin(url, link)
        inner_soup = BeautifulSoup(await fetch(full_link, session, proxy_url), "html.parser")
        for a_tag in inner_soup.find_all("a", href=True):
            href = a_tag["href"]
            if href.startswith("https://campaign2.naver.com") and len(href) > 40:
                add_reward_url(collected_urls, href)


async def process_ppomppu_url(url, soup, session, collected_urls, emit, proxy_url: str = ""):
    initial_count = len(collected_urls)
    base_url = "https://m.ppomppu.co.kr"
    naver_links = []
    for a_tag in soup.find_all("a", href=True):
        if "네이버페이" in a_tag.get_text():
            naver_links.append(a_tag["href"])

    for link in naver_links:
        full_link = urljoin(base_url, link)
        inner_soup = BeautifulSoup(await fetch(full_link, session, proxy_url), "html.parser")
        for a_tag in inner_soup.find_all("a", class_="noeffect", href=True):
            href = a_tag["href"]
            if href.startswith("https://s.ppomppu.co.kr?idno=coupon") and len(href) > 40:
                target_url = canonical_reward_url(ppomppu_target_url(href))
                if is_reward_url(target_url):
                    collected_urls.add(target_url)


def is_reward_url(href: str) -> bool:
    if is_excluded_reward_url(href):
        return False
    if not href or not href.startswith(REWARD_URL_PREFIXES) or len(href) <= 40:
        return False
    if href.startswith("https://campaign2.naver.com"):
        event_id = (parse_qs(urlparse(href).query).get("eventId") or [""])[0]
        return len(event_id) >= 10 or "/npay/branddraw" in href or "/global10save" in href
    if href.startswith("https://nsl.pincrux.com/live-view.html"):
        return "enc_param=" in href
    return True


def is_excluded_reward_url(href: str) -> bool:
    return str(href or "").strip().startswith(EXCLUDED_REWARD_URL_PREFIXES)


def is_account_scoped_reward_url(href: str) -> bool:
    href = str(href or "").strip()
    return is_pincrux_reward_candidate(href)


def is_pincrux_source_url(href: str) -> bool:
    return str(href or "").strip().startswith(PINCRUX_SOURCE_URL_PREFIX)


def is_pincrux_page_url(href: str) -> bool:
    return str(href or "").strip().startswith("https://nsl.pincrux.com")


def is_pincrux_reward_candidate(href: str) -> bool:
    href = str(href or "").strip()
    return is_pincrux_source_url(href) or is_pincrux_page_url(href)


def reward_source_key(source_url: str) -> str:
    source_url = str(source_url or "").strip()
    parsed = urlparse(source_url)
    if parsed.netloc == "point.pay.naver.com" and parsed.path == "/bridge/eventbenefit":
        query = parse_qs(parsed.query)
        ad_id = (query.get("adId") or [""])[0]
        placement_id = (query.get("placementId") or [""])[0]
        inventory = (query.get("inventory") or [""])[0]
        if ad_id:
            return f"naverpay_bridge:{inventory}:{placement_id}:{ad_id}"
    return source_url


def classify_reward_final_url(url: str) -> str:
    url = str(url or "").strip()
    if is_excluded_reward_url(url):
        return REWARD_KIND_EXCLUDED
    if url.startswith("https://campaign2.naver.com"):
        return REWARD_KIND_CAMPAIGN2
    if url.startswith("https://nsl.pincrux.com/live-view.html"):
        return REWARD_KIND_LIVE_VIEW
    if "nsl.pincrux.com/shopping-live" in url:
        return REWARD_KIND_LIVE_VIEW
    return REWARD_KIND_OTHER


def load_excluded_reward_sources(session_db) -> set[str]:
    try:
        return {reward_source_key(row.source_key) for row in session_db.query(ExcludedRewardSource).all() if row.source_key}
    except Exception:
        return set()


def record_excluded_reward_source(session_db, excluded_reward_sources: Optional[set[str]], source_key: str, target_url: str = ""):
    source_key = reward_source_key(source_key)
    if not source_key:
        return
    if excluded_reward_sources is not None:
        excluded_reward_sources.add(source_key)
    try:
        session_db.merge(ExcludedRewardSource(source_key=source_key, target_url=str(target_url or ""), updated_at=datetime.now()))
    except Exception:
        pass


def load_reward_source_resolutions(session_db) -> dict[str, tuple[str, str]]:
    try:
        return {
            row.source_key: (row.final_kind or "", row.final_url or "")
            for row in session_db.query(RewardSourceResolution).all()
            if row.source_key
        }
    except Exception:
        return {}


def record_reward_source_resolution(session_db, cache: dict[str, tuple[str, str]], source_key: str, final_kind: str, final_url: str = ""):
    source_key = reward_source_key(source_key)
    if not source_key:
        return
    final_url = canonical_reward_url(final_url)
    cache[source_key] = (final_kind, final_url)
    try:
        session_db.merge(
            RewardSourceResolution(
                source_key=source_key,
                final_kind=final_kind,
                final_url=final_url,
                updated_at=datetime.now(),
            )
        )
    except Exception:
        pass


def canonical_reward_url(href: str) -> str:
    href = str(href or "").strip()
    if href.startswith("https://campaign2.naver.com"):
        event_id = (parse_qs(urlparse(href).query).get("eventId") or [""])[0]
        if event_id:
            return f"https://campaign2.naver.com/npay/v2/click-point/?eventId={event_id}"
    return href


def add_reward_url(collected_urls, href: str):
    href = canonical_reward_url(href)
    if is_reward_url(href):
        collected_urls.add(href)


def _decode_js_string(value: str) -> str:
    try:
        return json.loads(f'"{value}"')
    except Exception:
        return value


def extract_campaign_urls_from_html(html: str) -> list[str]:
    normalized = html_lib.unescape(
        str(html or "")
        .replace("\\/", "/")
        .replace("\\u0026", "&")
        .replace("\\u002F", "/")
        .replace("\\u003c", "<")
        .replace("\\u003C", "<")
        .replace("\\u003e", ">")
        .replace("\\u003E", ">")
        .replace("\\u003D", "=")
        .replace("\\u003d", "=")
        .replace("\\u003F", "?")
        .replace("\\u003f", "?")
        .replace("&amp;", "&")
    )
    urls = []
    seen = set()
    pattern = r"https://(?:campaign2\.naver\.com|external-token\.pay\.naver\.com|point\.pay\.naver\.com/bridge/eventbenefit)[^\"'<>\\\s]+"
    for match in re.finditer(pattern, normalized):
        href = canonical_reward_url(match.group(0).rstrip(").,"))
        if is_reward_url(href) and href not in seen:
            seen.add(href)
            urls.append(href)
    return urls


def extract_damoang_post_links(html: str, base_url: str) -> list[str]:
    match = re.search(r"posts:\[(.*?)\],notices:", str(html or ""), re.DOTALL)
    if not match:
        return []
    links = []
    seen = set()
    pattern = r"\{id:(\d+),title:\"((?:\\.|[^\"\\])*)\".*?category:\"((?:\\.|[^\"\\])*)\""
    for item in re.finditer(pattern, match.group(1), re.DOTALL):
        post_id = item.group(1)
        title = _decode_js_string(item.group(2))
        category = _decode_js_string(item.group(3))
        if "네이버페이" not in title and "네이버페이" not in category:
            continue
        link = f"{base_url.rstrip('/')}/{post_id}"
        if link not in seen:
            seen.add(link)
            links.append(link)
    return links


async def process_damoang_url(url, soup, session, collected_urls, emit, proxy_url: str = ""):
    from playwright.async_api import async_playwright
    from playwright_stealth import Stealth

    initial_count = len(collected_urls)
    launch_kwargs = {"headless": True}
    proxy = playwright_proxy(proxy_url)
    if proxy:
        launch_kwargs["proxy"] = proxy

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(**launch_kwargs)
        context = await browser.new_context(
            user_agent=DESKTOP_UA,
            locale="ko-KR",
            viewport={"width": 1365, "height": 900},
        )
        page = await context.new_page()
        await Stealth().apply_stealth_async(page)
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=30000)
            try:
                await page.wait_for_load_state("networkidle", timeout=10000)
            except Exception:
                pass
            page_html = await page.content()
            list_soup = BeautifulSoup(page_html, "html.parser")

            naver_links = []
            for a_tag in list_soup.find_all("a", href=True):
                text = (a_tag.get_text() or "").strip()
                href = a_tag.get("href")
                if href and text and "네이버페이" in text:
                    full_link = urljoin(url, href)
                    if full_link not in naver_links:
                        naver_links.append(full_link)

            for link in extract_damoang_post_links(page_html, url):
                if link not in naver_links:
                    naver_links.append(link)

            for idx, link in enumerate(naver_links, 1):
                try:
                    await page.goto(link, wait_until="domcontentloaded", timeout=30000)
                    try:
                        await page.wait_for_load_state("networkidle", timeout=7000)
                    except Exception:
                        pass
                    detail_html = await page.content()
                    inner_soup = BeautifulSoup(detail_html, "html.parser")
                    before_count = len(collected_urls)
                    for a_tag in inner_soup.find_all("a", href=True):
                        href = a_tag.get("href")
                        if is_reward_url(href):
                            add_reward_url(collected_urls, href)
                    for href in extract_campaign_urls_from_html(detail_html):
                        add_reward_url(collected_urls, href)
                except Exception as e:
                    emit(f"damoang post playwright error {idx}/{len(naver_links)}: {e}")
        finally:
            await context.close()
            await browser.close()


async def process_naverpay_point_main(
    account: AccountConfig,
    session_db,
    cookie_dir: str,
    collected_urls,
    emit,
    reward_proxy_url: str = "",
    login_proxy_url: str = "",
):
    from playwright.async_api import async_playwright
    from playwright_stealth import Stealth

    url = NAVERPAY_MAIN_URL
    initial_count = len(collected_urls)
    storage_state = await ensure_cookie_storage_state(account, session_db, cookie_dir, login_proxy_url, emit)
    if not storage_state:
        emit(f"naverpay point main skipped for {account.user_id}: cookie missing")
        return

    launch_kwargs = {"headless": True}
    proxy = playwright_proxy(reward_proxy_url)
    if proxy:
        launch_kwargs["proxy"] = proxy

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(**launch_kwargs)
        context = await browser.new_context(
            user_agent=DESKTOP_UA,
            locale="ko-KR",
            viewport={"width": 1365, "height": 1000},
            storage_state=storage_state,
        )
        page = await context.new_page()
        await Stealth().apply_stealth_async(page)
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=30000)
            await page.wait_for_timeout(8000)
            if is_login_redirect(page.url):
                emit(f"{account.user_id}: session expired (point main redirected to login)")
                note_session_event(account.user_id, expired=True)
                return
            body_text = await page.locator("body").inner_text(timeout=5000)
            if "로그인" in body_text[:500] and "포인트" not in body_text[:500]:
                return

            point_rows = await _naverpay_text_hrefs(page, "클릭하고")
            live_rows = await _naverpay_text_hrefs(page, "쇼핑라이브 보고")

            point_rows.extend(await _naverpay_text_hrefs(page, "\ud074\ub9ad\ud558\uace0"))
            live_rows.extend(await _naverpay_text_hrefs(page, "\uc1fc\ud551\ub77c\uc774\ube0c \ubcf4\uace0"))

            for href, text in live_rows:
                if is_reward_url(href):
                    add_reward_url(collected_urls, href)
                elif is_pincrux_page_url(href):
                    collected_urls.add(canonical_reward_url(href))
                elif is_pincrux_source_url(href):
                    collected_urls.add(canonical_reward_url(href))

            for href, text in point_rows:
                if is_reward_url(href):
                    add_reward_url(collected_urls, href)
                    emit(f"naverpay point main direct click link: {href}")
        finally:
            await context.close()
            await browser.close()


async def process_naverpay_mission_detail_source(
    account: AccountConfig,
    session_db,
    cookie_dir: str,
    collected_urls,
    emit,
    reward_proxy_url: str = "",
    login_proxy_url: str = "",
    source_resolutions: Optional[dict[str, tuple[str, str]]] = None,
):
    from playwright.async_api import async_playwright
    from playwright_stealth import Stealth

    emit(f"naverpay mission detail started for {account.user_id}")
    storage_state = await ensure_cookie_storage_state(account, session_db, cookie_dir, login_proxy_url, emit)
    if not storage_state:
        emit(f"naverpay mission detail skipped for {account.user_id}: cookie missing")
        return

    launch_kwargs = {"headless": True}
    proxy = playwright_proxy(reward_proxy_url)
    if proxy:
        launch_kwargs["proxy"] = proxy
    emit(f"naverpay mission detail browser opening for {account.user_id} proxy={mask_proxy_url(reward_proxy_url)}")

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(**launch_kwargs)
        context = await browser.new_context(
            user_agent=DESKTOP_UA,
            locale="ko-KR",
            viewport={"width": 1365, "height": 1000},
            storage_state=storage_state,
        )
        page = await context.new_page()
        await Stealth().apply_stealth_async(page)
        try:
            emit(f"naverpay mission detail page loading for {account.user_id}")
            await page.goto(NAVERPAY_MISSION_DETAIL_URL, wait_until="domcontentloaded", timeout=30000)
            emit(f"naverpay mission detail page loaded for {account.user_id}: {page.url}")
            if is_login_redirect(page.url):
                emit(f"{account.user_id}: session expired (mission page redirected to login)")
                note_session_event(account.user_id, expired=True)
                return
            await page.wait_for_timeout(3000)
            await collect_lazy_page_content(page)
            emit(f"naverpay mission detail lazy content collected for {account.user_id}")
            source_resolutions = source_resolutions if source_resolutions is not None else {}
            bridge_urls = await collect_naverpay_mission_source_urls(page)
            emit(f"naverpay mission detail bridge urls for {account.user_id}: {len(bridge_urls)}")
            campaign_count = 0
            live_count = 0
            resolved_count = 0
            total = len(bridge_urls)
            for idx, bridge_url in enumerate(bridge_urls, 1):
                source_key = reward_source_key(bridge_url)
                cached = source_resolutions.get(source_key)
                if cached:
                    final_kind, final_url = cached
                    if final_kind == REWARD_KIND_CAMPAIGN2 and is_reward_url(final_url):
                        add_reward_url(collected_urls, final_url)
                        campaign_count += 1
                    elif final_kind == REWARD_KIND_LIVE_VIEW:
                        final_kind, final_url, live_urls, pincrux_source_url = await resolve_naverpay_bridge_url_for_collection(page, bridge_url)
                        if pincrux_source_url:
                            collected_urls.add(pincrux_source_url)
                            live_count += 1
                        else:
                            for live_url in live_urls:
                                if is_reward_url(live_url):
                                    add_reward_url(collected_urls, live_url)
                                    live_count += 1
                                elif is_pincrux_page_url(live_url):
                                    collected_urls.add(canonical_reward_url(live_url))
                                    live_count += 1
                    continue
                if emit and (idx == 1 or idx == total or idx % 5 == 0):
                    emit(f"naverpay mission detail resolving bridge urls for {account.user_id}: {idx}/{total}")
                final_kind, final_url, live_urls, pincrux_source_url = await resolve_naverpay_bridge_url_for_collection(page, bridge_url)
                record_reward_source_resolution(session_db, source_resolutions, source_key, final_kind, final_url)
                resolved_count += 1
                if final_kind == REWARD_KIND_EXCLUDED:
                    record_excluded_reward_source(session_db, None, source_key, final_url)
                    continue
                if final_kind == REWARD_KIND_CAMPAIGN2 and is_reward_url(final_url):
                    add_reward_url(collected_urls, final_url)
                    campaign_count += 1
                elif final_kind == REWARD_KIND_LIVE_VIEW:
                    if pincrux_source_url:
                        collected_urls.add(pincrux_source_url)
                        live_count += 1
                    else:
                        for live_url in live_urls:
                            if is_reward_url(live_url):
                                add_reward_url(collected_urls, live_url)
                                live_count += 1
                            elif is_pincrux_page_url(live_url):
                                collected_urls.add(canonical_reward_url(live_url))
                                live_count += 1
            emit(
                f"naverpay mission detail reward urls for {account.user_id}: "
                f"campaign2={campaign_count} live-view={live_count} newly-resolved={resolved_count}"
            )
        finally:
            await context.close()
            await browser.close()


async def collect_naverpay_mission_source_urls(page) -> list[str]:
    try:
        raw_rows = await page.evaluate(
            """() => {
                const norm = s => (s || '').replace(/\\s+/g, ' ').trim();
                const urls = [];
                const seen = new Set();
                for (const a of Array.from(document.querySelectorAll('a[href]'))) {
                    const href = a.href || '';
                    if (!href.startsWith('https://point.pay.naver.com/bridge/eventbenefit')) continue;
                    const text = norm(a.innerText || a.textContent);
                    if (!/클릭\\s*\\d+원/.test(text)) continue;
                    if (/적립\\s*완료|적립완료/.test(text)) continue;
                    const rect = a.getBoundingClientRect();
                    if (rect.width <= 0 || rect.height <= 0) continue;
                    if (seen.has(href)) continue;
                    seen.add(href);
                    urls.push(href);
                }
                return urls;
            }"""
        )
    except Exception:
        return []
    return [
        str(url or "").strip()
        for url in raw_rows or []
        if str(url or "").strip().startswith("https://point.pay.naver.com/bridge/eventbenefit")
    ]


async def resolve_naverpay_bridge_urls_to_campaign2(
    page,
    bridge_urls: list[str],
    emit: Optional[Callable[[str], None]] = None,
    user_id: str = "",
) -> list[str]:
    resolved = []
    seen = set()
    total = len(bridge_urls)
    for idx, bridge_url in enumerate(bridge_urls, 1):
        if emit and (idx == 1 or idx == total or idx % 5 == 0):
            emit(f"naverpay mission detail resolving bridge urls for {user_id}: {idx}/{total}")
        campaign_url = await resolve_naverpay_bridge_url_to_campaign2(page, bridge_url)
        if not campaign_url:
            campaign_url = bridge_url
        campaign_url = canonical_reward_url(campaign_url)
        if is_reward_url(campaign_url) and campaign_url not in seen:
            seen.add(campaign_url)
            resolved.append(campaign_url)
    return resolved


async def collect_pincrux_live_view_urls(page) -> list[str]:
    urls = []
    seen = set()

    def add(url: str, allow_pincrux_page: bool = False):
        url = canonical_reward_url(str(url or "").strip())
        if (is_reward_url(url) or (allow_pincrux_page and is_pincrux_page_url(url))) and url not in seen:
            seen.add(url)
            urls.append(url)

    add(page.url, allow_pincrux_page=True)
    if "nsl.pincrux.com/shopping-live" not in str(page.url or ""):
        return urls

    try:
        await expand_pincrux_live_more(page)
    except Exception:
        pass
    try:
        rows = await collect_pincrux_short_live_rows(page)
        for row in rows:
            add(str(row.get("href") or ""), allow_pincrux_page=True)
    except Exception:
        pass

    candidates = [
        ("text=ë¼ì´ë¸Œ ì¤‘ì´ì—ìš”!", True),
        ("text=5ì´ˆ ë³´ë©´", True),
        ("xpath=//*[contains(normalize-space(.), 'ë¼ì´ë¸Œ ì¤‘ì´ì—ìš”!')]/ancestor::*[self::a or self::button or @role='button' or @onclick][1]", False),
        ("xpath=//*[contains(normalize-space(.), '5ì´ˆ ë³´ë©´')]/ancestor::*[self::a or self::button or @role='button' or @onclick][1]", False),
    ]
    for selector, js_click in candidates:
        try:
            before_pages = list(page.context.pages)
            locator = page.locator(selector).first
            if await locator.count() == 0:
                continue
            if js_click:
                await locator.evaluate(
                    """el => {
                        const target = el.closest('a, button, [role="button"], [onclick]') || el;
                        target.click();
                    }"""
                )
            else:
                await locator.click(timeout=5000, force=True)
            await asyncio.sleep(1)
            pages = list(page.context.pages)
            active_page = ([candidate for candidate in pages if candidate not in before_pages] or [page])[-1]
            try:
                await active_page.wait_for_load_state("domcontentloaded", timeout=5000)
            except Exception:
                pass
            add(active_page.url)
            if active_page is not page:
                try:
                    await active_page.close()
                except Exception:
                    pass
            if urls:
                break
        except Exception:
            pass
    return urls


async def resolve_naverpay_bridge_url_for_collection(page, bridge_url: str) -> tuple[str, str, list[str], str]:
    try:
        await page.goto(bridge_url, wait_until="domcontentloaded", timeout=30000)
    except Exception:
        return REWARD_KIND_OTHER, "", [], ""
    await wait_for_bridge_redirect(page, timeout_ms=10000)
    pincrux_source_url = canonical_reward_url(page.url) if is_pincrux_source_url(page.url) else ""
    if pincrux_source_url:
        await wait_for_pincrux_source_redirect(page, timeout_ms=12000)
    current_url = canonical_reward_url(page.url)
    final_kind = classify_reward_final_url(current_url)
    if final_kind == REWARD_KIND_CAMPAIGN2:
        return final_kind, current_url, [current_url] if is_reward_url(current_url) else [], ""
    if final_kind == REWARD_KIND_LIVE_VIEW:
        live_urls = await collect_pincrux_live_view_urls(page)
        if not live_urls and is_pincrux_page_url(current_url):
            live_urls = [current_url]
        return final_kind, live_urls[0] if live_urls else current_url, live_urls, pincrux_source_url
    return final_kind, current_url, [], pincrux_source_url


async def resolve_naverpay_bridge_url_to_campaign2(page, bridge_url: str) -> str:
    try:
        await page.goto(bridge_url, wait_until="domcontentloaded", timeout=30000)
    except Exception:
        return ""
    deadline = time.time() + 10
    while time.time() < deadline:
        current_url = page.url
        if current_url.startswith("https://campaign2.naver.com"):
            return current_url
        try:
            body_text = await page.locator("body").inner_text(timeout=500)
        except Exception:
            body_text = ""
        if ("1회만" in body_text or "포인트 받기" in body_text or "적립돼요" in body_text) and page.url.startswith("https://campaign2.naver.com"):
            return page.url
        await asyncio.sleep(0.5)
    return page.url if page.url.startswith("https://campaign2.naver.com") else ""



async def _naverpay_text_hrefs(page, needle: str) -> list[tuple[str, str]]:
    try:
        raw_rows = await page.evaluate(
            """needle => {
                const norm = s => (s || '').replace(/\\s+/g, ' ').trim();
                return Array.from(document.querySelectorAll('a[href]')).map(a => {
                    const rect = a.getBoundingClientRect();
                    return {
                        href: a.href,
                        text: norm(a.innerText || a.textContent),
                        visible: rect.width > 0 && rect.height > 0
                    };
                }).filter(row => row.visible && row.href && row.text.includes(needle));
            }""",
            needle,
        )
    except Exception:
        return []

    rows = []
    seen = set()
    for item in raw_rows or []:
        href = str(item.get("href") or "").strip()
        text = str(item.get("text") or "").strip()
        key = (href, text)
        if href and key not in seen:
            seen.add(key)
            rows.append((href, text))
    return rows


async def save_naver_campaign_urls(
    session_db,
    emit: Callable[[str], None],
    reward_proxy_url: str = "",
    accounts: Optional[list[AccountConfig]] = None,
    cookie_dir: str = "",
    login_proxy_url: str = "",
):
    from aiohttp import ClientSession, ClientTimeout

    collected_urls = set()
    account_collected_urls = {}
    source_resolutions = load_reward_source_resolutions(session_db)

    def add_collected_for_account(user_id: str, urls: set[str]):
        account_only = {url for url in urls if is_account_scoped_reward_url(url)}
        common = set(urls) - account_only
        collected_urls.update(common)
        if account_only:
            account_collected_urls.setdefault(user_id, set()).update(account_only)

    urls = [
        ("https://www.clien.net/service/board/jirum", process_clien_url),
        ("https://m.ppomppu.co.kr/new/bbs_list.php?id=coupon&extref=1", process_ppomppu_url),
        ("https://damoang.net/economy", process_damoang_url),
    ]
    collector_account = (accounts or [None])[0]

    async def collect_mission_source():
        account_urls = set()
        emit(f"collection source {NAVERPAY_MISSION_DETAIL_URL} ({collector_account.user_id}) started")
        await process_naverpay_mission_detail_source(
            collector_account,
            session_db,
            cookie_dir,
            account_urls,
            emit,
            reward_proxy_url,
            login_proxy_url,
            source_resolutions,
        )
        add_collected_for_account(collector_account.user_id, account_urls)
        emit(f"collection source {NAVERPAY_MISSION_DETAIL_URL} ({collector_account.user_id}): +{len(account_urls)}")

    if collector_account:
        # 쿠키를 쓸 수 없으면 링크 유무와 상관없이 여기서 로그인을 시도한다 (실패하면 사유가 기록되어 알림이 간다)
        cookie_ok, _cookie_message = check_cookie_status(collector_account.user_id, cookie_dir)
        if not cookie_ok:
            await ensure_cookie_storage_state(collector_account, session_db, cookie_dir, login_proxy_url, emit)
        try:
            await collect_mission_source()
        except Exception as e:
            emit(f"{NAVERPAY_MISSION_DETAIL_URL}: collection error for {collector_account.user_id} - {e}")
        # 세션이 만료되어 미션 페이지가 로그인 화면으로 이동했다면: 재로그인 후 이 소스만 한 번 다시 수집
        collector_event = session_event(collector_account.user_id)
        if collector_event.get("expired") and not collector_event.get("relogin"):
            if await repair_expired_session(collector_account, session_db, cookie_dir, login_proxy_url, emit):
                try:
                    await collect_mission_source()
                except Exception as e:
                    emit(f"{NAVERPAY_MISSION_DETAIL_URL}: collection error for {collector_account.user_id} - {e}")

    timeout = ClientTimeout(total=30, connect=10, sock_connect=10, sock_read=20)
    async with ClientSession(timeout=timeout) as session:
        for url, process_func in urls:
            try:
                before = len(collected_urls)
                emit(f"collection source {url} started")
                if process_func is process_damoang_url:
                    await process_func(url, BeautifulSoup("", "html.parser"), session, collected_urls, emit, reward_proxy_url)
                else:
                    await process_url(url, session, process_func, collected_urls, emit, reward_proxy_url)
                emit(f"collection source {url}: +{len(collected_urls) - before}")
            except Exception as e:
                emit(f"{url}: collection error - {e}")

    if collector_account:
        try:
            account_urls = set()
            emit(f"collection source {NAVERPAY_MAIN_URL} ({collector_account.user_id}) started")
            await process_naverpay_point_main(
                collector_account,
                session_db,
                cookie_dir,
                account_urls,
                emit,
                reward_proxy_url,
                login_proxy_url,
            )
            add_collected_for_account(collector_account.user_id, account_urls)
            emit(f"collection source {NAVERPAY_MAIN_URL} ({collector_account.user_id}): +{len(account_urls)}")
        except Exception as e:
            emit(f"{NAVERPAY_MAIN_URL}: collection error for {collector_account.user_id} - {e}")

    account_scoped_urls = set()
    for urls_for_account in account_collected_urls.values():
        account_scoped_urls.update(urls_for_account)
    if account_scoped_urls:
        for account in accounts or []:
            account_collected_urls.setdefault(account.user_id, set()).update(account_scoped_urls)

    existing_collected_urls = set()
    for stale_url in session_db.query(CampaignUrl).all():
        if not is_reward_url(stale_url.url):
            stale_url.is_available = False
    for link in collected_urls:
        if not is_reward_url(link):
            continue
        existing_url = session_db.query(CampaignUrl).filter_by(url=link).first()
        if existing_url:
            existing_collected_urls.add(link)
        else:
            session_db.add(CampaignUrl(url=link))
    return collected_urls, existing_collected_urls, account_collected_urls


async def fetch_naver_campaign_urls(session_db, nid: str, candidate_urls: Optional[set[str]] = None):
    campaign_links = set()
    already_visited_count = 0
    if candidate_urls is not None:
        if not candidate_urls:
            return campaign_links, already_visited_count
        for link in candidate_urls:
            link = str(link or "").strip()
            if not link or (not is_reward_url(link) and not is_pincrux_reward_candidate(link)):
                continue
            if should_revisit_url(link):
                campaign_links.add(link)
                continue
            url_obj = session_db.query(CampaignUrl).filter_by(url=link).first()
            if url_obj and not url_obj.is_available:
                continue
            if should_revisit_url(link):
                campaign_links.add(link)
                continue
            existing_visit = session_db.query(UrlVisit).filter_by(url=link, user_id=nid).first()
            if not existing_visit:
                campaign_links.add(link)
            else:
                already_visited_count += 1
        return campaign_links, already_visited_count

    available_urls = session_db.query(CampaignUrl).all()
    for url_obj in available_urls:
        if not is_reward_url(url_obj.url) and not is_pincrux_reward_candidate(url_obj.url):
            continue
        if should_revisit_url(url_obj.url):
            campaign_links.add(url_obj.url)
            continue
        if not url_obj.is_available:
            continue
        existing_visit = session_db.query(UrlVisit).filter_by(url=url_obj.url, user_id=nid).first()
        if not existing_visit:
            campaign_links.add(url_obj.url)
        else:
            already_visited_count += 1
    return campaign_links, already_visited_count


def should_revisit_url(url: str) -> bool:
    url = str(url or "")
    return is_pincrux_reward_candidate(url)


def delete_old_stuff(session_db, keep_campaign_days: int, keep_user_days: int, emit: Callable[[str], None]):
    current_date = datetime.now()
    campaign_cutoff = current_date - timedelta(days=max(1, int(keep_campaign_days or 60)))
    user_cutoff = current_date - timedelta(days=max(1, int(keep_user_days or 7)))
    try:
        old_urls = session_db.query(CampaignUrl).filter(CampaignUrl.date_added < campaign_cutoff)
        for old_url in old_urls:
            session_db.query(UrlVisit).filter_by(url=old_url.url).delete()
        old_urls.delete()
        session_db.query(User).filter(User.updated_at < user_cutoff).delete()
    except Exception as e:
        emit(f"cleanup error - {e}")


def recent_runs(db_path: str, limit: int = 30):
    db = Database(db_path)
    db.create_all()
    with db.get_session() as session:
        rows = session.query(RunHistory).order_by(RunHistory.id.desc()).limit(limit).all()
        return [_history_to_dict(row) for row in rows]


def recent_run_account_tabs(db_path: str, limit: int = 30, profile_order: Optional[list[str]] = None):
    db = Database(db_path)
    db.create_all()
    with db.get_session() as session:
        histories = session.query(RunHistory).order_by(RunHistory.id.desc()).limit(limit).all()
        if not histories:
            return []

        run_map = {row.id: _history_to_dict(row) for row in histories}
        tabs = {}
        order = []

        summaries = (
            session.query(RunAccountSummary)
            .filter(RunAccountSummary.run_id.in_(list(run_map.keys())))
            .order_by(RunAccountSummary.run_id.desc(), RunAccountSummary.id.asc())
            .all()
        )
        for summary in summaries:
            user_id = summary.user_id or "기타"
            if user_id not in tabs:
                tabs[user_id] = {"user_id": user_id, "runs": []}
                order.append(user_id)
            base = dict(run_map.get(summary.run_id) or {})
            if not base:
                continue
            base.update(
                {
                    "user_id": user_id,
                    "target_url_count": summary.target_url_count or ((summary.skipped_url_count or 0) + (summary.visited_url_count or 0)),
                    "skipped_url_count": summary.skipped_url_count or 0,
                    "visited_url_count": summary.visited_url_count or 0,
                    "estimated_points": summary.estimated_points or 0,
                    "detail_count": summary.detail_count or 0,
                }
            )
            tabs[user_id]["runs"].append(base)

        summarized_run_ids = {row.run_id for row in summaries}
        for history in histories:
            if history.id in summarized_run_ids:
                continue
            details = session.query(RunDetail).filter_by(run_id=history.id).all()
            if not details:
                user_id = "전체"
                if user_id not in tabs:
                    tabs[user_id] = {"user_id": user_id, "runs": []}
                    order.append(user_id)
                tabs[user_id]["runs"].append(_history_to_dict(history))
                continue
            grouped = {}
            for detail in details:
                user_id = detail.user_id or "기타"
                grouped.setdefault(user_id, {"target": 0, "skipped": 0, "visited": 0, "points": 0, "details": 0})
                grouped[user_id]["target"] += 1
                grouped[user_id]["details"] += 1
                if detail.status in ("skipped", "unavailable", "no_url"):
                    grouped[user_id]["skipped"] += 1
                elif detail.status != "error":
                    grouped[user_id]["visited"] += 1
                grouped[user_id]["points"] += int(detail.point or 0)
            for user_id, values in grouped.items():
                if user_id not in tabs:
                    tabs[user_id] = {"user_id": user_id, "runs": []}
                    order.append(user_id)
                base = _history_to_dict(history)
                base.update(
                    {
                        "user_id": user_id,
                        "target_url_count": values["target"],
                        "skipped_url_count": values["skipped"],
                        "visited_url_count": values["visited"],
                        "estimated_points": values["points"],
                        "detail_count": values["details"],
                    }
                )
                tabs[user_id]["runs"].append(base)

        profile_order = [str(user_id or "").strip() for user_id in (profile_order or []) if str(user_id or "").strip()]
        if profile_order:
            known = [user_id for user_id in profile_order if user_id in tabs]
            unknown = [user_id for user_id in order if user_id not in set(known)]
            order = known + unknown

        return [tabs[user_id] for user_id in order]


def run_details(db_path: str, run_id: int):
    db = Database(db_path)
    db.create_all()
    with db.get_session() as session:
        rows = session.query(RunDetail).filter_by(run_id=run_id).order_by(RunDetail.id.asc()).all()
        return [_detail_to_dict(row) for row in rows]


def run_account_summaries(db_path: str, run_id: int):
    db = Database(db_path)
    db.create_all()
    with db.get_session() as session:
        rows = session.query(RunAccountSummary).filter_by(run_id=run_id).order_by(RunAccountSummary.id.asc()).all()
        if rows:
            return [_account_summary_to_dict(row) for row in rows]

        details = session.query(RunDetail).filter_by(run_id=run_id).order_by(RunDetail.id.asc()).all()
        grouped = {}
        order = []
        for detail in details:
            user_id = detail.user_id or ""
            if user_id not in grouped:
                grouped[user_id] = {
                    "user_id": user_id,
                    "target_url_count": 0,
                    "skipped_url_count": 0,
                    "visited_url_count": 0,
                    "estimated_points": 0,
                    "detail_count": 0,
                    "message": "",
                }
                order.append(user_id)
            grouped[user_id]["detail_count"] += 1
            grouped[user_id]["target_url_count"] += 1
            if detail.status in ("skipped", "unavailable", "no_url"):
                grouped[user_id]["skipped_url_count"] += 1
            elif detail.status != "error":
                grouped[user_id]["visited_url_count"] += 1
            grouped[user_id]["estimated_points"] += int(detail.point or 0)
        return [grouped[user_id] for user_id in order]


def cookie_statuses(db_path: str, cookie_dir: str, accounts: list[AccountConfig]):
    db = Database(db_path)
    db.create_all()
    rows = []
    with db.get_session() as session:
        for account in accounts:
            valid, message = check_cookie_status(account.user_id, cookie_dir)
            user = session.query(User).filter_by(user_id=account.user_id).first()
            rows.append(
                {
                    "user_id": account.user_id,
                    "valid": valid,
                    "message": message,
                    "db_storage": bool(user and user.storage_state),
                    "updated_at": user.updated_at.strftime("%Y-%m-%d %H:%M:%S") if user and user.updated_at else "",
                }
            )
    return rows


def _history_to_dict(row: RunHistory):
    return {
        "id": row.id,
        "started_at": _fmt(row.started_at),
        "finished_at": _fmt(row.finished_at),
        "status": row.status,
        "account_count": row.account_count,
        "collected_url_count": row.collected_url_count,
        "skipped_url_count": row.skipped_url_count or 0,
        "visited_url_count": row.visited_url_count,
        "estimated_points": row.estimated_points,
        "message": row.message or "",
    }


def _detail_to_dict(row: RunDetail):
    return {
        "id": row.id,
        "run_id": row.run_id,
        "user_id": row.user_id,
        "url": row.url,
        "status": row.status,
        "point": row.point,
        "message": row.message or "",
        "created_at": _fmt(row.created_at),
    }


def _account_summary_to_dict(row: RunAccountSummary):
    return {
        "id": row.id,
        "run_id": row.run_id,
        "user_id": row.user_id,
        "target_url_count": row.target_url_count or ((row.skipped_url_count or 0) + (row.visited_url_count or 0)),
        "skipped_url_count": row.skipped_url_count or 0,
        "visited_url_count": row.visited_url_count or 0,
        "estimated_points": row.estimated_points or 0,
        "detail_count": row.detail_count or 0,
        "message": row.message or "",
        "created_at": _fmt(row.created_at),
    }


def _fmt(value):
    if not value:
        return ""
    return value.strftime("%Y-%m-%d %H:%M:%S")
