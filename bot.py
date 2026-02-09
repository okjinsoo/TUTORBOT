# bot.py — TutorBot (ID-only overrides + /신규 시트검증 사양 반영)
# KST: Asia/Seoul

import os, json, re, asyncio, random, traceback
from typing import Dict, List, Tuple, Optional, Any, Set
from datetime import datetime, date, timedelta, time as dtime, timezone
def _retry_after_seconds(e) -> float | None:
    # discord.py 버전에 따라 구조가 다를 수 있어 최대한 안전하게 시도
    try:
        resp = getattr(e, "response", None)
        hdrs = getattr(resp, "headers", None) or {}
        ra = hdrs.get("Retry-After") or hdrs.get("retry-after")
        if ra:
            return float(ra)
        ra2 = hdrs.get("X-RateLimit-Reset-After") or hdrs.get("x-ratelimit-reset-after")
        if ra2:
            return float(ra2)
    except Exception:
        pass

    # 일부는 e.text에 dict 형태로 들어올 수 있음
    try:
        txt = getattr(e, "text", None)
        if isinstance(txt, dict) and "retry_after" in txt:
            return float(txt["retry_after"])
    except Exception:
        pass

    return None


# ====== KST ======
try:
    from zoneinfo import ZoneInfo
    KST = ZoneInfo("Asia/Seoul")
except Exception:
    KST = timezone(timedelta(hours=9))

# ====== Discord ======
import discord
from discord.ext import commands
from discord import app_commands

discord.utils.setup_logging()

intents = discord.Intents.default()
intents.guilds = True
intents.members = True
intents.voice_states = True
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

# ====== Env ======
from dotenv import load_dotenv
load_dotenv()
ENV = os.environ.get

BOT_TOKEN = (ENV("BOT_TOKEN") or "").strip()
GUILD_ID = int(ENV("GUILD_ID") or "0") or None
TEACHER_MAIN_ID = int(ENV("TEACHER_MAIN_ID") or "0") or None
SITUATION_ROOM_CHANNEL_ID = int(ENV("SITUATION_ROOM_CHANNEL_ID") or "0") or None
SHEET_ID = (ENV("SHEET_ID") or "").strip()
SHEET_NAME = (ENV("SHEET_NAME") or "시간표").strip()
SERVICE_ACCOUNT_JSON = (ENV("SERVICE_ACCOUNT_JSON") or "service_account.json").strip()

def _env_flag(name: str, default: bool = False) -> bool:
    raw = (ENV(name) or "").strip().lower()
    if raw == "":
        return default
    return raw in {"1", "true", "yes", "y", "on"}

def _env_int(name: str, default: int) -> int:
    raw = (ENV(name) or "").strip()
    if raw == "":
        return default
    try:
        return int(raw)
    except Exception:
        return default

# 429 안전모드:
# - SAFE_MODE_429=1(기본)에서는 슬래시 sync를 자동으로 하지 않아 과호출 위험을 줄입니다.
# - 정말 필요할 때만 ENABLE_SLASH_SYNC=1로 켜서 1회 sync 하세요.
SAFE_MODE_429 = _env_flag("SAFE_MODE_429", True)
ENABLE_SLASH_SYNC = _env_flag("ENABLE_SLASH_SYNC", not SAFE_MODE_429)
RATE_LIMIT_WAIT_MIN = _env_int("RATE_LIMIT_WAIT_MIN", 20 if SAFE_MODE_429 else 30)
RATE_LIMIT_WAIT_MAX = _env_int("RATE_LIMIT_WAIT_MAX", 45 if SAFE_MODE_429 else 60)
HEARTBEAT_INTERVAL_SEC = _env_int("HEARTBEAT_INTERVAL_SEC", 300)

# ===== Firestore integration =====
# 필요 패키지: pip install google-cloud-firestore google-auth
from google.oauth2 import service_account
from google.cloud import firestore

_firestore_client = None

def init_firestore_client(service_account_json_path: str):
    """서비스 계정 JSON 파일 경로로 Firestore 클라이언트를 초기화합니다.
       실패하면 _firestore_client는 None으로 남고 로그를 출력합니다."""
    global _firestore_client
    if not service_account_json_path:
        print("[Firestore] SERVICE_ACCOUNT_JSON 경로 미설정")
        return None
    try:
        # 서비스 계정 JSON 불러오기
        with open(service_account_json_path, "r", encoding="utf-8") as f:
            service_account_info = json.load(f)

        # Credentials 생성
        creds = service_account.Credentials.from_service_account_info(service_account_info)

        # Firestore 클라이언트 생성
        _firestore_client = firestore.Client(
            credentials=creds,
            project=creds.project_id
        )

        # 🔥 여기 “정확한 디버그 정보” 추가
        print(
            f"[Firestore] 연결 성공: "
            f"project={creds.project_id}, "
            f"sa_email={service_account_info.get('client_email')}"
        )

        return _firestore_client
    except Exception as e:
        print(f"[Firestore 연결 실패] {type(e).__name__}: {e}")
        _firestore_client = None
        return None

def firestore_set_doc(collection: str, doc_id: str, data: dict):
    """문서 전체를 덮어쓰기(set). _firestore_client 미설정 시 RuntimeError 발생."""
    if not _firestore_client:
        raise RuntimeError("Firestore client not initialized")
    ref = _firestore_client.collection(collection).document(doc_id)
    ref.set(data)

def firestore_get_doc(collection: str, doc_id: str, default=None):
    """문서 읽기(없으면 default). 오류 발생 시 default 반환."""
    if not _firestore_client:
        return default
    ref = _firestore_client.collection(collection).document(doc_id)
    try:
        doc = ref.get()
        if doc.exists:
            return doc.to_dict()
    except Exception as e:
        print(f"[Firestore 읽기 오류] {collection}/{doc_id}: {e}")
    return default


CATEGORY_SUFFIX = " 채널"
TEXT_NAME = "채팅채널"
VOICE_NAME = "음성채널"
_student_text_channel_cache: Dict[int, int] = {}

# ====== Files ======
OVERRIDE_FILE   = "overrides.json"   # { "YYYY-MM-DD": { "<sid str>": {cancel,change,changes,makeup}, ... } }
ATTENDANCE_FILE = "attendance.json"  # { "YYYY-MM-DD": [sid,...] }
HOMEWORK_FILE   = "homework.json"    # { "YYYY-MM-DD": [sid,...] }

_overrides_lock = asyncio.Lock()
_attendance_lock = asyncio.Lock()
_homework_lock = asyncio.Lock()
_ready_boot_lock = asyncio.Lock()
_post_ready_lock = asyncio.Lock()

def _safe_json_dumps(x): return json.dumps(x, ensure_ascii=False, indent=2)

def save_json_atomic(path: str, data: Any):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(_safe_json_dumps(data))
        try:
            f.flush(); os.fsync(f.fileno())
        except Exception:
            pass
    os.replace(tmp, path)

def load_json_with_recovery(path: str, default: Any):
    def _load(p):
        with open(p, "r", encoding="utf-8") as f: return json.load(f)
    if not os.path.exists(path): return default
    try:
        return _load(path)
    except Exception:
        tmp = path + ".tmp"
        if os.path.exists(tmp):
            try:
                data = _load(tmp)
                save_json_atomic(path, data)
                return data
            except Exception:
                pass
        return default

overrides: Dict[str, dict] = load_json_with_recovery(OVERRIDE_FILE, {})
attendance: Dict[str, List[int]] = load_json_with_recovery(ATTENDANCE_FILE, {})
homework: Dict[str, List[int]] = load_json_with_recovery(HOMEWORK_FILE, {})

def load_local_json(path: str, default):
    """로컬 JSON 파일을 안전하게 읽습니다. 실패하면 default를 돌려줍니다."""
    try:
        return load_json_with_recovery(path, default)
    except Exception as e:
        print(f"[로컬 로드 실패] {path}: {e}")
        return default

def load_from_firestore_or_local():
    """
    앱 시작할 때 Firestore에서 먼저 데이터를 읽어오고,
    실패하면 로컬 파일(overrides.json 등)에서 읽어옵니다.
    """
    global overrides, attendance, homework

    # 1) Firestore가 준비돼 있으면 Firestore에서 먼저 시도
    if _firestore_client:
        try:
            o = firestore_get_doc("persist", "overrides", None)
            a = firestore_get_doc("persist", "attendance", None)
            h = firestore_get_doc("persist", "homework", None)

            if isinstance(o, dict):
                overrides = o
            else:
                overrides = load_local_json(OVERRIDE_FILE, {})

            if isinstance(a, dict):
                attendance = a
            else:
                attendance = load_local_json(ATTENDANCE_FILE, {})

            if isinstance(h, dict):
                homework = h
            else:
                homework = load_local_json(HOMEWORK_FILE, {})

            print("[Load] Firestore 우선 로드 완료 (없으면 로컬 사용)")
            return
        except Exception as e:
            print(f"[Load 실패] Firestore 로드 오류: {e}")

    # 2) Firestore를 못 쓰는 경우 → 로컬 파일에서 읽기
    overrides = load_local_json(OVERRIDE_FILE, {})
    attendance = load_local_json(ATTENDANCE_FILE, {})
    homework = load_local_json(HOMEWORK_FILE, {})
    print("[Load] 로컬 파일 로드 완료")


async def save_overrides():
    async with _overrides_lock:
        _persist_json_snapshot("overrides", OVERRIDE_FILE, overrides, "save_overrides")

async def save_attendance():
    _persist_json_snapshot("attendance", ATTENDANCE_FILE, attendance, "save_attendance")


async def save_homework():
    _persist_json_snapshot("homework", HOMEWORK_FILE, homework, "save_homework")

def _persist_json_snapshot(doc_id: str, path: str, data: Any, tag: str):
    try:
        if _firestore_client:
            firestore_set_doc("persist", doc_id, data)
        save_json_atomic(path, data)
    except Exception as e:
        print(f"[{tag} 오류] {type(e).__name__}: {e}")
        try:
            save_json_atomic(path, data)
        except Exception as e2:
            print(f"[{tag} 로컬백업 실패] {type(e2).__name__}: {e2}")


# ====== Time / Parse ======
WEEKDAY_MAP = {"월":0,"화":1,"수":2,"목":3,"금":4,"토":5,"일":6}
_TIME_RE = re.compile(r"^\s*(\d{1,2})\s*[:시]\s*(\d{0,2})\s*(분)?\s*$")

def parse_time_str(s: str) -> Optional[dtime]:
    if not isinstance(s, str): return None
    m = _TIME_RE.match(s.strip())
    if not m: return None
    hh = int(m.group(1)); mm = int(m.group(2) or 0)
    if 0 <= hh <= 23 and 0 <= mm <= 59:
        return dtime(hh, mm)
    return None

def parse_date_yyyy_mm_dd(s: str) -> Optional[date]:
    try:
        return datetime.fromisoformat(s.strip()).date()
    except Exception:
        return None

def normalize_base_name(name: str) -> str:
    if not name: return name
    return re.sub(r'(-\d{4})+$', '', name).strip()

def _parse_day_input(when: str) -> Optional[date]:
    if when is None: return None
    s = when.strip()
    if s in ("오늘","today"): return datetime.now(KST).date()
    if s in ("내일","tomorrow"): return datetime.now(KST).date() + timedelta(days=1)
    if re.fullmatch(r"\d{4}-\d{1,2}-\d{1,2}", s):
        try: return date.fromisoformat(s)
        except: return None
    if re.fullmatch(r"\d{1,2}-\d{1,2}", s):
        y = datetime.now(KST).year
        mm, dd = s.split("-"); mm=mm.zfill(2); dd=dd.zfill(2)
        try: return date.fromisoformat(f"{y}-{mm}-{dd}")
        except: return None
    return None

def _to_int_set(items: Any) -> Set[int]:
    out: Set[int] = set()
    if not isinstance(items, (list, tuple, set)):
        return out
    for x in items:
        if isinstance(x, int):
            out.add(x)
        elif isinstance(x, str) and x.isdigit():
            out.add(int(x))
    return out

def _extract_submitted_sids(raw: Any, *, allow_legacy_list: bool) -> Set[int]:
    if isinstance(raw, dict):
        return _to_int_set(raw.get("submitted", []))
    if allow_legacy_list and isinstance(raw, list):
        return _to_int_set(raw)
    return set()

# ====== Google Sheets ======
import gspread
from google.oauth2.service_account import Credentials

def gs_client():
    scopes = ["https://www.googleapis.com/auth/spreadsheets.readonly"]
    creds = Credentials.from_service_account_file(SERVICE_ACCOUNT_JSON, scopes=scopes)
    return gspread.authorize(creds)

class SheetCache:
    def __init__(self, ttl_seconds=90):
        self.ttl = ttl_seconds
        self._rows = None
        self._parsed = None
        self._ts = 0.0
        self._lock = asyncio.Lock()

    async def get_rows(self):
        loop = asyncio.get_running_loop()
        now = loop.time()
        if self._rows is not None and (now - self._ts) <= self.ttl:
            return self._rows
        async with self._lock:
            if self._rows is not None and (loop.time() - self._ts) <= self.ttl:
                return self._rows
            def _fetch():
                gc = gs_client()
                ws = gc.open_by_key(SHEET_ID).worksheet(SHEET_NAME)
                return ws.get_all_values()
            rows = await loop.run_in_executor(None, _fetch)
            self._rows = rows
            self._parsed = None
            self._ts = loop.time()
            return rows

    async def get_parsed(self):
        now = asyncio.get_running_loop().time()
        if self._parsed is not None and (now - self._ts) <= self.ttl:
            return self._parsed
        rows = await self.get_rows()
        self._parsed = parse_schedule_single_sheet(rows)
        return self._parsed

SHEET_CACHE = SheetCache(90)

def parse_schedule_single_sheet(rows):
    """
    Header 예:
      학생 이름 | discord_id | (요일|시간)* | ... | 서비스 시작일 | 서비스 종료일
    반환:
      { key: {"name":str,"id":int|None,"pairs":[(요일,dtime)],"start_raw":str,"end_raw":str} }
    """
    if not rows: return {}
    header = [h.strip() for h in rows[0]]
    if "학생 이름" in header: name_idx = header.index("학생 이름")
    elif "이름" in header:    name_idx = header.index("이름")
    else: return {}
    id_idx    = header.index("discord_id")     if "discord_id" in header else None
    start_idx = header.index("서비스 시작일")    if "서비스 시작일" in header else None
    end_idx   = header.index("서비스 종료일")    if "서비스 종료일" in header else None

    data = {}
    for ridx, r in enumerate(rows[1:], start=1):
        if not r or len(r) <= name_idx: continue
        name = (r[name_idx] or "").strip()
        if not name: continue

        did = None
        if id_idx is not None and len(r) > id_idx:
            raw = (r[id_idx] or "").strip()
            if raw.isdigit():
                try: did = int(raw)
                except: did = None

        start_col = max(name_idx, id_idx if id_idx is not None else -1) + 1
        pairs: List[Tuple[str, dtime]] = []
        for i in range(start_col, len(r), 2):
            if i+1 >= len(r): break
            day_lbl = (r[i] or "").strip()
            t_raw   = (r[i+1] or "").strip()
            if not day_lbl or not t_raw: continue
            if day_lbl not in WEEKDAY_MAP: break
            t = parse_time_str(t_raw)
            if t: pairs.append((day_lbl, t))

        start_raw = (r[start_idx].strip() if (start_idx is not None and len(r) > start_idx) else "") if start_idx is not None else ""
        end_raw   = (r[end_idx].strip()   if (end_idx   is not None and len(r) > end_idx)   else "") if end_idx   is not None else ""

        key = str(did) if isinstance(did, int) else f"{name}#row{ridx}"
        data[key] = {"name": name, "id": did, "pairs": pairs, "start_raw": start_raw, "end_raw": end_raw}
    return data

# 이름↔ID 빠른 조회(검증용)
STUDENT_ID_MAP: Dict[str, int] = {}
def _rebuild_name_id_maps(parsed: Dict[str, Any]):
    name_to_id = {}
    id_to_name = {}
    for v in parsed.values():
        nm = v.get("name")
        sid = v.get("id")
        if nm and isinstance(sid, int):
            name_to_id[nm] = sid
            id_to_name[sid] = nm
    return name_to_id, id_to_name

async def refresh_student_id_map():
    global STUDENT_ID_MAP
    try:
        parsed = await SHEET_CACHE.get_parsed()
        STUDENT_ID_MAP, _ = _rebuild_name_id_maps(parsed)
        print(f"[학생ID맵] 로드 OK: {len(STUDENT_ID_MAP)}명")
    except Exception as e:
        print("[학생ID맵 로드 오류]", repr(e))


# ====== Label / Guild utils ======
def _label_from_guild_or_default(name: str, sid: Optional[int]) -> str:
    if isinstance(sid, int):
        for g in bot.guilds:
            m = g.get_member(sid)
            if m: return (m.display_name or m.nick or name)
        return f"{name}-{str(sid)[-4:]}"
    return name

async def _get_text_channel_cached(cid: Optional[int]) -> Optional[discord.TextChannel]:
    if not cid: return None
    ch = bot.get_channel(cid)
    if isinstance(ch, discord.TextChannel): return ch
    try:
        ch = await bot.fetch_channel(cid)
        return ch if isinstance(ch, discord.TextChannel) else None
    except Exception:
        return None

async def _get_user_cached(uid: Optional[int]) -> Optional[discord.User]:
    if not uid: return None
    u = bot.get_user(uid)
    if u: return u
    try: return await bot.fetch_user(uid)
    except Exception:
        return None

def _find_student_text_channel_by_id(student_id: Optional[int], fallback_name: str) -> Optional[discord.TextChannel]:
    if not isinstance(student_id, int): return None

    cached_cid = _student_text_channel_cache.get(student_id)
    if cached_cid:
        cached = bot.get_channel(cached_cid)
        if isinstance(cached, discord.TextChannel):
            return cached
        _student_text_channel_cache.pop(student_id, None)

    for g in bot.guilds:
        m = g.get_member(student_id)
        if not m: continue
        # 1) 카테고리명: 표시명 + " 채널"
        disp = (m.display_name or m.nick or fallback_name)
        cat_name = f"{disp}{CATEGORY_SUFFIX}"
        cat = discord.utils.get(g.categories, name=cat_name)
        if cat:
            text = discord.utils.get(cat.text_channels, name=TEXT_NAME) or (cat.text_channels[0] if cat.text_channels else None)
            if text:
                _student_text_channel_cache[student_id] = text.id
                return text
        # 2) 토픽에 SID:<id> 표시된 텍스트 채널
        sid_tag = f"SID:{student_id}"
        for cat in g.categories:
            for tx in cat.text_channels:
                try:
                    if (tx.topic or "").find(sid_tag) != -1:
                        _student_text_channel_cache[student_id] = tx.id
                        return tx
                except Exception:
                    continue
    return None

# ====== OVERRIDES (ID-only) ======
def _ensure_day_bucket(day_iso: str) -> dict:
    b = overrides.get(day_iso)
    if not isinstance(b, dict):
        b = {}; overrides[day_iso] = b
    return b

def _ov_get_id(ovs_day: dict, sid: Optional[int]) -> Optional[dict]:
    if not isinstance(sid, int): return None
    e = ovs_day.get(str(sid))
    return e if isinstance(e, dict) else None

def _ov_get_or_create_id(ovs_day: dict, sid: Optional[int]) -> dict:
    if not isinstance(sid, int):
        raise ValueError("SID가 필요합니다(ID-only 정책).")
    e = ovs_day.get(str(sid))
    if isinstance(e, dict): return e
    e = {"cancel": False, "change": None, "changes": [], "makeup": []}
    ovs_day[str(sid)] = e
    return e

def ov_set_cancel_id(ovs_day: dict, sid: int, flag: bool) -> dict:
    e = _ov_get_or_create_id(ovs_day, sid)
    e["cancel"] = bool(flag); return e

def ov_clear_changes_id(ovs_day: dict, sid: int) -> dict:
    e = _ov_get_or_create_id(ovs_day, sid)
    e["change"] = None; e["changes"] = []; return e

def ov_add_change_pair_id(ovs_day: dict, sid: int, src: Any, dst: Any) -> dict:
    tf = parse_time_str(str(src)); tt = parse_time_str(str(dst))
    if not (tf and tt): raise ValueError("변경 시간 형식 오류")
    e = _ov_get_or_create_id(ovs_day, sid)
    e["change"] = None
    key = (tf.strftime("%H:%M"), tt.strftime("%H:%M"))
    ch = e.get("changes") or []
    if not any((c.get("from"), c.get("to")) == key for c in ch):
        ch.append({"from": key[0], "to": key[1]})
    e["changes"] = ch
    e["cancel"] = False
    return e

def ov_add_makeup_id(ovs_day: dict, sid: int, t: Any) -> dict:
    tt = parse_time_str(str(t))
    if not tt: raise ValueError("보강 시간 형식 오류")
    e = _ov_get_or_create_id(ovs_day, sid)
    mm = e.get("makeup") or []
    hhmm = tt.strftime("%H:%M")
    if hhmm not in mm: mm.append(hhmm)
    e["makeup"] = mm
    return e

def _cleanup_entry_if_empty_id(ovs_day: dict, sid: int):
    e = ovs_day.get(str(sid))
    if not isinstance(e, dict): return
    if (not e.get("cancel")) and (e.get("change") is None) and (not e.get("changes")) and (not e.get("makeup")):
        try: del ovs_day[str(sid)]
        except Exception: pass

# ---- Migration: 이름키 제거/이관 ----
async def migrate_overrides_to_id_only(*, refresh_map: bool = True):
    """
    이전 파일에 '이름키'가 남아있는 경우:
      1) 같은 내용의 ID키가 이미 있으면 이름키 삭제
      2) 시트 매핑(STUDENT_ID_MAP)에서 이름→ID가 확인되면 그 ID로 이관 후 이름키 삭제
      3) 둘 다 안되면 '표시에만 쓰이던 이름키'로 간주하고 **삭제** (ID-only 정책상 무시)
    """
    try:
        if refresh_map:
            await refresh_student_id_map()
        changed = False
        for day_iso, bucket in list(overrides.items()):
            if not isinstance(bucket, dict): continue
            # 수집
            name_keys = [k for k in list(bucket.keys()) if not (isinstance(k, str) and k.isdigit())]
            for nk in name_keys:
                entry = bucket.get(nk)
                if not isinstance(entry, dict):
                    try: del bucket[nk]; changed = True
                    except: pass
                    continue
                # 1) 이미 동일 엔트리가 같은 날 ID키에 있으면 이름키 삭제
                deleted = False
                # 2) 시트 매핑으로 ID 찾기
                sid = STUDENT_ID_MAP.get(nk)
                if isinstance(sid, int):
                    eid = bucket.get(str(sid))
                    if not isinstance(eid, dict):
                        bucket[str(sid)] = entry
                    # 이름키 제거
                    try: del bucket[nk]; changed = True; deleted = True
                    except: pass
                if not deleted:
                    # ID를 모르니 이름키는 무시(삭제) — 중복/표시만 방지
                    try: del bucket[nk]; changed = True
                    except: pass
        if changed:
            await save_overrides()
            print("[마이그레이션] overrides: 이름키→ID키 정리/삭제 완료")
    except Exception as e:
        print(f"[마이그레이션 오류] {type(e).__name__}: {e}")

# ====== Core: 세션 계산 (ID-only overrides 적용) ======
async def effective_sessions_for(day: date, parsed: Optional[Dict[str, Any]] = None):
    """
    최종 세션 목록: [(name, time, sid)]
    - 서비스기간 적용(시작일 없으면 기본 수업 배제, 종료일 없으면 28일 규칙)
    - overrides: **ID키만** 반영 (이름키는 무시)
    """
    base = parsed or await SHEET_CACHE.get_parsed()
    wd = day.weekday()
    day_iso = day.isoformat()
    ovs_day = overrides.get(day_iso, {}) if isinstance(overrides.get(day_iso, {}), dict) else {}

    result = []
    for info in base.values():
        name = info.get("name") or "학생"
        sid  = info.get("id")   # 중요: None이면 override 반영 불가
        pairs: List[Tuple[str, dtime]] = info.get("pairs", [])
        times = [t for (d_lbl, t) in pairs if WEEKDAY_MAP.get(d_lbl) == wd]

        # 서비스 기간
        sd = parse_date_yyyy_mm_dd(info.get("start_raw") or "")
        ed = parse_date_yyyy_mm_dd(info.get("end_raw") or "")
        if sd is None:
            times = []
        else:
            ed2 = ed or (sd + timedelta(days=28))
            if not (sd <= day <= ed2):
                times = []

        # overrides(ID만)
        e = _ov_get_id(ovs_day, sid)
        if e:
            # 복수 변경
            chg = e.get("changes")
            if isinstance(chg, list) and chg:
                new_times = set(times)
                for it in chg:
                    tf = parse_time_str(str(it.get("from")))
                    tt = parse_time_str(str(it.get("to")))
                    if tf and tt and tf in new_times:
                        new_times.discard(tf); new_times.add(tt)
                times = sorted(new_times, key=lambda t:(t.hour,t.minute))
            # 단일 변경
            ch = e.get("change")
            if ch is not None:
                tch = parse_time_str(str(ch))
                if tch: times = [tch]
            # 보강
            adds = e.get("makeup") or []
            for a in adds:
                ta = parse_time_str(str(a))
                if ta and ta not in times:
                    times.append(ta)
            # 휴강
            if e.get("cancel"):
                times = []

        for t in sorted(times, key=lambda x:(x.hour,x.minute)):
            result.append((name, t, sid))
    return result

# ====== Summary / Posting ======
async def send_long(dest, text: str, max_len: int = 1990):
    buf = ""
    for line in (text or "").splitlines():
        add = line + "\n"
        if len(buf) + len(add) > max_len:
            await dest.send(buf); buf = ""
        buf += add
    if buf.strip():
        await dest.send(buf)

async def send_long_message(inter: discord.Interaction, text: str, *, ephemeral: bool = False):
    """디스코드 2000자 제한을 피하기 위해 메시지를 여러 개로 나눠 보내는 헬퍼 함수."""
    limit = 2000

    # 1) 전체 길이가 2000자 이하면 한 번에 전송
    if len(text) <= limit:
        if inter.response.is_done():
            await inter.followup.send(text, ephemeral=ephemeral)
        else:
            await inter.response.send_message(text, ephemeral=ephemeral)
        return

    # 2) 첫 번째 조각
    first_chunk = text[:limit]
    rest = text[limit:]

    if inter.response.is_done():
        await inter.followup.send(first_chunk, ephemeral=ephemeral)
    else:
        await inter.response.send_message(first_chunk, ephemeral=ephemeral)

    # 3) 나머지 조각들
    while rest:
        chunk = rest[:limit]
        rest = rest[limit:]
        await inter.followup.send(chunk, ephemeral=ephemeral)

async def build_timetable_message(day: date) -> str:
    day_iso = day.isoformat()
    parsed = await SHEET_CACHE.get_parsed()

    # ✅ D-day용 맵: 서비스 종료일이 있는 모든 학생
    dday_map: Dict[int, int] = {}      # sid -> 남은 일수 (0이면 D-DAY)

    # 기본 수업(서비스기간 반영)
    wd = day.weekday()
    base_on_day: Dict[int, Tuple[str, List[dtime]]] = {}  # sid -> (name, times)
    for info in parsed.values():
        name = info.get("name") or "학생"
        sid  = info.get("id")
        if not isinstance(sid, int):  # ID 없는 행은 운영 기준에서 제외
            continue
        sd = parse_date_yyyy_mm_dd(info.get("start_raw") or "")
        ed = parse_date_yyyy_mm_dd(info.get("end_raw") or "")
        if sd is None:
            continue
        ed2 = ed or (sd + timedelta(days=28))
        if not (sd <= day <= ed2):
            continue

        # ⏰ D-day 계산 (서비스 종료일이 있는 학생 전체)
        if ed is not None:
            remain = (ed - day).days
            if remain >= 0:  # 종료일 이후면 D-day 표기 안 함 (설계 선택, 추측입니다)
                dday_map[sid] = remain

        pairs = info.get("pairs", [])
        times = sorted(
            [t for (d_lbl, t) in pairs if WEEKDAY_MAP.get(d_lbl) == wd],
            key=lambda x: (x.hour, x.minute),
        )
        if times:
            base_on_day[sid] = (name, times)

    # overrides — **ID 키만** 집계
    ovs_day = overrides.get(day_iso, {}) if isinstance(overrides.get(day_iso, {}), dict) else {}
    sid_keys = [int(k) for k in ovs_day.keys() if isinstance(k, str) and k.isdigit()]
    display_sids = set(base_on_day.keys()) | set(sid_keys)

    def _tl(t: dtime) -> str:
        return t.strftime("%H:%M")

    changed_lines, makeup_lines, canceled_lines = [], [], []

    for sid in sorted(display_sids):
        e = _ov_get_id(ovs_day, sid)
        if not e:
            continue
        # 라벨
        base_name = (base_on_day.get(sid, ("학생", []))[0])
        label = _label_from_guild_or_default(base_name, sid)

        # 휴강
        if e.get("cancel"):
            old_times = base_on_day.get(sid, ("", []))[1]
            old_str = ", ".join(_tl(t) for t in old_times) if old_times else "(기본 없음)"
            canceled_lines.append(f"- {label}: {old_str} (휴강)")
            continue

        # 변경(복수)
        chg = e.get("changes")
        if isinstance(chg, list) and chg:
            pairs_fmt = []
            for it in chg:
                tf = parse_time_str(str(it.get("from")))
                tt = parse_time_str(str(it.get("to")))
                if tf and tt:
                    pairs_fmt.append((tf, f"{_tl(tf)}→{_tl(tt)}"))
            pairs_fmt.sort(key=lambda p: (p[0].hour, p[0].minute))
            if pairs_fmt:
                changed_lines.append(f"- {label}: " + ", ".join(p for _, p in pairs_fmt))
        else:
            # 단일(레거시)
            ch = e.get("change")
            if ch is not None:
                tch = parse_time_str(str(ch))
                if tch:
                    old = base_on_day.get(sid, ("", []))[1]
                    old_str = ", ".join(_tl(t) for t in old) if old else "(기본 없음)"
                    changed_lines.append(f"- {label}: {old_str} → {_tl(tch)}")

        # 보강
        adds = e.get("makeup") or []
        add_times = []
        for a in adds:
            ta = parse_time_str(str(a))
            if ta:
                add_times.append(ta)
        add_times = sorted(set(add_times), key=lambda t: (t.hour, t.minute))
        if add_times:
            makeup_lines.append(f"- {label}: " + ", ".join(_tl(t) for t in add_times))

    # ===== 여기서부터 출석 + 숙제 제출 정보 합치기 =====

    # 최종 세션
    effective = await effective_sessions_for(day, parsed)
    attended_ids = set(attendance.get(day_iso, []))

    # 숙제 제출 정보 (새 형식: {"submitted":[sid,...]} 기준)
    raw_hw = homework.get(day_iso)
    submitted_ids = _extract_submitted_sids(raw_hw, allow_legacy_list=True)

    eff_lines = []
    for n, t, sid in sorted(
        ((n, t, sid) for (n, t, sid) in effective if isinstance(sid, int)),
        key=lambda x: (_label_from_guild_or_default(x[0], x[2]), x[1]),
    ):
        label = _label_from_guild_or_default(n, sid)

        # ⏰ D-day 태그 (모든 학생 대상)
        dday_tag = ""
        if isinstance(sid, int) and sid in dday_map:
            remain = dday_map[sid]
            if remain == 0:
                dday_tag = " (D-DAY)"
            else:
                dday_tag = f" (D-{remain})"

        # 출석 여부
        att_mark = "✅ 출석" if sid in attended_ids else "❌ 미출석"
        # 숙제 여부
        hw_mark = "📘 숙제제출" if sid in submitted_ids else "🕒 미제출"
        eff_lines.append(
            f"- {label}{dday_tag}: {t.strftime('%H:%M')} [{att_mark} / {hw_mark}]"
        )
        
    # (요약용 통계 — 필요없으면 이 블록 통째로 지워도 됨)
    uniq_sids = {sid for (_, _, sid) in effective if isinstance(sid, int)}
    total = len(uniq_sids)
    att_cnt = sum(1 for sid in uniq_sids if sid in attended_ids)
    hw_cnt = sum(1 for sid in uniq_sids if sid in submitted_ids)
    att_rate = int(round(att_cnt * 100 / total)) if total > 0 else 0
    hw_rate = int(round(hw_cnt * 100 / total)) if total > 0 else 0

    lines = [f"**[수업 집계] ({day_iso})**", ""]

    # 보강
    lines.append("**📌 보강**" if makeup_lines else "**📌 보강**: 없음")
    lines += (sorted(makeup_lines) if makeup_lines else [])
    lines.append("")

    # 변경
    lines.append("**🔄 변경**" if changed_lines else "**🔄 변경**: 없음")
    lines += (sorted(changed_lines) if changed_lines else [])
    lines.append("")

    # 휴강
    lines.append("**⛔ 휴강**" if canceled_lines else "**⛔ 휴강**: 없음")
    lines += (sorted(canceled_lines) if canceled_lines else [])
    lines.append("")

    # 출석/숙제 요약
    lines.append("**📊 출석·숙제 요약**")
    lines.append(f"- 출석: {att_cnt}/{total}명 ({att_rate}%)")
    lines.append(f"- 숙제: {hw_cnt}/{total}명 ({hw_rate}%)")
    lines.append("")

    # 최종 수업
    lines.append("**🗓️ 수업 (최종)**" if eff_lines else "**🗓️ 수업 (최종)**: 없음")
    lines += eff_lines

    out = "\n".join("> " + L for L in lines)
    return out

async def post_today_summary():
    ch = await _get_text_channel_cached(SITUATION_ROOM_CHANNEL_ID)
    if not ch: return
    out = (await build_timetable_message(datetime.now(KST).date()) or "").strip() or "> **[수업 집계]**\n> (내용 없음)"
    await send_long(ch, out)

async def post_day_summary_to_teacher(day: date):
    if not TEACHER_MAIN_ID: return
    u = await _get_user_cached(TEACHER_MAIN_ID)
    if not u: return
    out = (await build_timetable_message(day) or "").strip() or "> **[수업 집계]**\n> (내용 없음)"
    await send_long(u, out)

# ====== Alerts / Homework (원형 유지, 핵심 로직은 ID 기반) ======
ALERT_OFFSETS = (-10, 75)
rel_tasks: Dict[Tuple[Optional[int], int, str, int], asyncio.Task] = {}
last_question_at: Dict[int, float] = {}

def _cancel_rel_tasks_for(day_iso: str, offset_min: Optional[int] = None):
    to_cancel = []
    for key, task in list(rel_tasks.items()):
        _sid, _hhmm, _day, _off = key
        if _day != day_iso: continue
        if offset_min is not None and _off != offset_min: continue
        to_cancel.append(key)
        if task and not task.done(): task.cancel()
    for k in to_cancel: rel_tasks.pop(k, None)

async def _fire_relative(name: str, sid: Optional[int], start_time: dtime, fire_at: datetime, offset_min: int):
    try:
        await asyncio.sleep(max(0,(fire_at - datetime.now(KST)).total_seconds()))
        if datetime.now(KST) - fire_at > timedelta(minutes=2): return
        mention = f"<@{sid}>" if isinstance(sid,int) else name
        label = _label_from_guild_or_default(name, sid)
        start_label = start_time.strftime("%H:%M")
        if offset_min < 0:
            msg_student = f"{mention} 수업 {abs(offset_min)}분 전입니다.\n⏰ 시작 시각 : {start_label}\n📝 수업 전 구글 드라이브에서 오늘 학습 자료를 다운로드!\n✅ 수업 준비되면 `/출석` 하고 화면 공유 해주세요!"
            log = f"[상황실] {label} 수업 {abs(offset_min)}분 전 알림 전송"
        else:
            msg_student = f"{mention} 수업이 {offset_min}분 경과했습니다. (시작 {start_label})"
            log = f"[상황실] {label} 수업 {offset_min}분 경과 알림 전송"

        ch = _find_student_text_channel_by_id(sid, name)
        if ch:
            try: await ch.send(msg_student)
            except Exception: pass
        room = await _get_text_channel_cached(SITUATION_ROOM_CHANNEL_ID)
        if room:
            try: await room.send(log)
            except Exception: pass
    except asyncio.CancelledError:
        return
    except Exception as e:
        print(f"[REL{offset_min}] 오류: {e}")

async def schedule_relative_alerts_for_today(offset_min: int):
    today = datetime.now(KST).date()
    today_iso = today.isoformat()
    sessions = await effective_sessions_for(today)
    _cancel_rel_tasks_for(today_iso, offset_min)
    now = datetime.now(KST)
    for name, t, sid in sessions:
        start_dt = datetime.combine(today, t, KST)
        fire_at  = start_dt + timedelta(minutes=offset_min)
        if (fire_at - now).total_seconds() <= 0: continue
        hhmm = t.hour*100 + t.minute
        key = (sid if isinstance(sid,int) else None, hhmm, today_iso, offset_min)
        old = rel_tasks.get(key)
        if old and not old.done(): old.cancel()
        rel_tasks[key] = asyncio.create_task(_fire_relative(name, sid, t, fire_at, offset_min))

async def schedule_all_offsets_for_today():
    for off in ALERT_OFFSETS:
        await schedule_relative_alerts_for_today(off)

# ====== Schedulers ======
async def daily_scheduler():
    await bot.wait_until_ready()
    while not bot.is_closed():
        now = datetime.now(KST)
        target = datetime.combine(now.date(), dtime(13,0), KST)
        if now > target: target += timedelta(days=1)
        await asyncio.sleep(max(0,(target - now).total_seconds()))
        try:
            await refresh_student_id_map()
            await post_today_summary()
            print("[13:00] 집계 전송 완료")
        except Exception as e:
            print(f"[13시 집계 오류] {type(e).__name__}: {e}")

async def midnight_scheduler():
    await bot.wait_until_ready()
    while not bot.is_closed():
        now = datetime.now(KST)
        target = datetime.combine(now.date(), dtime(0,0), KST)
        if now >= target: target += timedelta(days=1)
        await asyncio.sleep((target - now).total_seconds())

        base = datetime.now(KST).date()
        y = base - timedelta(days=1)
        try:
            await post_day_summary_to_teacher(y)
        except Exception as e:
            print(f"[자정 DM 오류] {type(e).__name__}: {e}")

        try:
            await refresh_student_id_map()
            await post_today_summary()
            await schedule_all_offsets_for_today()
            print("[00:00] 새로고침 완료")
        except Exception as e:
            print(f"[자정 새로고침/예약 오류] {type(e).__name__}: {e}")

async def homework_scheduler():
    """
    매일 18:00, 22:00 KST에 _send_homework_reminders() 실행
    """
    await bot.wait_until_ready()
    targets = (dtime(18, 0), dtime(22, 0))

    while not bot.is_closed():
        now = datetime.now(KST)

        # 오늘 남은 트리거 계산
        today_triggers = []
        for tt in targets:
            cand = datetime.combine(now.date(), tt, KST)
            if cand > now:
                today_triggers.append(cand)
        if not today_triggers:
            # 오늘 다 지났으면 내일 18:00
            nxt = datetime.combine(now.date() + timedelta(days=1), targets[0], KST)
        else:
            nxt = min(today_triggers)

        await asyncio.sleep(max(0, (nxt - now).total_seconds()))
        try:
            await _send_homework_reminders(nxt.hour)  # 18 또는 22
        except Exception as e:
            print(f"[숙제 리마인더 오류] {type(e).__name__}: {e}")
        # 다음 루프에서 다시 계산

# ====== Slash: 출석/선생님/숙제 ======
@bot.tree.command(name="출석", description="오늘자 출석을 기록합니다.")
@app_commands.guild_only()
async def slash_attend(inter: discord.Interaction):
    await inter.response.defer(ephemeral=False, thinking=True)
    uid = inter.user.id
    today_iso = datetime.now(KST).date().isoformat()
    try:
        async with _attendance_lock:
            arr = attendance.get(today_iso, [])
            if uid in arr:
                await inter.followup.send(
                    f"{inter.user.mention} 이미 출석으로 기록되어 있습니다. ✅",
                    ephemeral=False
                )
                return

            arr.append(uid)
            attendance[today_iso] = arr

            # 🔹 출석 저장 전담 함수 사용
            await save_attendance()

        await inter.followup.send(
            f"{inter.user.mention} ✅ 출석 완료! (기록됨)",
            ephemeral=False
        )

    except Exception as e:
        print(f"[/출석 오류] {type(e).__name__}: {e}")
        await inter.followup.send("출석 기록 중 문제가 발생했어요.", ephemeral=False)

@bot.tree.command(name="선생님", description="선생님을 호출합니다. (상황실 로그)")
@app_commands.describe(message="전달 내용(선택)")
@app_commands.guild_only()
async def slash_call_teacher(inter: discord.Interaction, message: Optional[str] = None):
    await inter.response.defer(ephemeral=False, thinking=True)
    uid = inter.user.id
    now_m = asyncio.get_running_loop().time()
    last = last_question_at.get(uid, 0.0)
    if now_m - last < 60:
        await inter.followup.send("조금 전에도 호출이 있었어요. 1분 후에 다시 시도해주세요 🙏", ephemeral=False); return
    last_question_at[uid] = now_m

    room = await _get_text_channel_cached(SITUATION_ROOM_CHANNEL_ID)
    teacher_mention = f"<@{TEACHER_MAIN_ID}>" if TEACHER_MAIN_ID else "(선생님)"
    if room:
        msg = f"{teacher_mention} {inter.user.mention} — 선생님 호출"
        if (message or "").strip(): msg += f" : {(message or '').strip()}"
        await room.send(msg)
    await inter.followup.send("호출 접수 완료! 곧 선생님이 도와드릴게요. 🙌", ephemeral=False)

@bot.tree.command(name="숙제", description="숙제 제출을 기록합니다.")
@app_commands.describe(when="미입력: 가장 가까운 수업 / '오늘' / '내일' / YYYY-MM-DD / MM-DD")
@app_commands.guild_only()
async def slash_hw_submit(inter: discord.Interaction, when: Optional[str] = None):
    await inter.response.defer(ephemeral=False, thinking=True)
    uid = inter.user.id
    now = datetime.now(KST)
    today = now.date()
    desired_day: Optional[date] = None

    # 1) 날짜 결정 로직 (기존 그대로 사용)
    if not when:
        # 오늘 남은 수업 있으면 오늘, 아니면 이후 첫 수업
        for i in range(0, 31):
            d = today + timedelta(days=i)
            sessions = await effective_sessions_for(d)
            times = [t for n, t, sid in sessions if isinstance(sid, int) and sid == uid]
            if not times:
                continue
            if i == 0:
                # 오늘: 아직 남은 수업이 있으면 오늘
                if any((t.hour, t.minute) > (now.hour, now.minute) for t in times):
                    desired_day = d
                    break
            else:
                desired_day = d
                break
    else:
        s = when.strip()
        if s in ("오늘", "today"):
            desired_day = today
        elif s in ("내일", "tomorrow"):
            for i in range(1, 31 + 1):
                d = today + timedelta(days=i)
                if any(isinstance(sid, int) and sid == uid for _, _, sid in await effective_sessions_for(d)):
                    desired_day = d
                    break
        else:
            # YYYY-MM-DD / MM-DD 처리
            if re.fullmatch(r"\d{1,2}-\d{1,2}", s):
                y = datetime.now(KST).year
                mm, dd = s.split("-")
                s = f"{y}-{mm.zfill(2)}-{dd.zfill(2)}"
            try:
                cand = date.fromisoformat(s)
            except Exception:
                await inter.followup.send(
                    "날짜 형식이 올바르지 않아요. YYYY-MM-DD / MM-DD / '내일'을 사용해 주세요.",
                    ephemeral=False,
                )
                return
            if any(isinstance(sid, int) and sid == uid for _, _, sid in await effective_sessions_for(cand)):
                desired_day = cand
            else:
                await inter.followup.send(
                    f"{cand.isoformat()}에는 수업이 없는 것 같아요 🧐",
                    ephemeral=False,
                )
                return

    if not desired_day:
        await inter.followup.send(
            "앞으로 예정된 수업 날짜를 찾지 못했어요. 🧐",
            ephemeral=False,
        )
        return

    day_iso = desired_day.isoformat()

    # 2) 숙제 제출 정보 저장 방식 변경
    #    homework[day_iso] = {"submitted": [sid, ...]} 형식으로 관리
    try:
        async with _homework_lock:
            raw = homework.get(day_iso)
            # 예전 형식(list 등)은 무시하고 새 형식으로 갈아탑니다.
            submitted = _extract_submitted_sids(raw, allow_legacy_list=False)

            submitted.add(uid)
            homework[day_iso] = {
                "submitted": sorted(submitted),
            }

            # 🔹 숙제 저장 전담 함수 사용
            await save_homework()

    except Exception as e:
        print(f"[/숙제 저장 오류] {type(e).__name__}: {e}")
        await inter.followup.send("숙제 제출 기록 중 문제가 발생했어요. 잠시 후 다시 시도해주세요.", ephemeral=False)
        return

    await inter.followup.send(
        f"{inter.user.mention}\n**{day_iso}까지 제출할 숙제**가 제출되었습니다. 🎉",
        ephemeral=False,
    )

@bot.tree.command(name="숙제제출", description="특정 날짜의 숙제 제출 현황을 확인합니다.")
@app_commands.describe(when="오늘/내일 또는 YYYY-MM-DD / MM-DD")
@app_commands.default_permissions(manage_channels=True)
@app_commands.guild_only()
async def slash_homework_status(inter: discord.Interaction, when: str = "오늘"):
    await inter.response.defer(ephemeral=True, thinking=True)

    # 1) 날짜 파싱
    day = _parse_day_input(when or "오늘")
    if not day:
        await inter.followup.send("날짜 형식 오류입니다. 오늘/내일 또는 YYYY-MM-DD / MM-DD 를 사용해주세요.", ephemeral=True)
        return

    day_iso = day.isoformat()

    # 2) 그 날짜에 수업 있는 학생들 계산
    try:
        sessions = await effective_sessions_for(day)
    except Exception as e:
        await inter.followup.send(f"❌ 시간표 계산 실패: {type(e).__name__}: {e}", ephemeral=True)
        return

    # sid 기준으로 한 번씩만 정리 (가장 이른 수업 시각 기준)
    per_sid: Dict[int, Tuple[str, Optional[dtime]]] = {}
    for name, t, sid in sessions:
        if not isinstance(sid, int):
            continue
        label = _label_from_guild_or_default(name, sid)
        if sid not in per_sid or (per_sid[sid][1] is not None and t < per_sid[sid][1]):
            per_sid[sid] = (label, t)

    if not per_sid:
        await inter.followup.send(f"`{day_iso}`에는 수업이 없는 것 같아요.", ephemeral=True)
        return

    # 3) homework.json 에서 제출 정보 읽기
    legacy_format = False
    submitted_sids: Set[int] = set()
    try:
        async with _homework_lock:
            raw = homework.get(day_iso)
            if isinstance(raw, list):
                # ⚠️ 예전 형식: 이 경우에는 정확한 제출자 정보를 알 수 없음
                legacy_format = True
                submitted_sids = set()
            else:
                submitted_sids = _extract_submitted_sids(raw, allow_legacy_list=False)
    except Exception as e:
        await inter.followup.send(f"❌ 숙제 데이터 읽기 실패: {type(e).__name__}: {e}", ephemeral=True)
        return

    lines: List[str] = []
    lines.append(f"**[숙제 제출 현황] {day_iso}**")

    if legacy_format:
        # 4-A) 예전 형식 → 제출 여부를 신뢰할 수 없음
        lines.append("")
        lines.append("⚠️ 이 날짜의 숙제 데이터는 **구버전 형식**이라,")
        lines.append("   누가 실제로 `/숙제`를 눌렀는지 **구분할 수 없습니다.**")
        lines.append("")
        lines.append("🗓️ 수업 대상자 목록 (제출 여부: 알 수 없음)")
        for sid, (label, t) in sorted(per_sid.items(), key=lambda x: (x[1][1] or dtime(0, 0), x[1][0])):
            time_str = t.strftime("%H:%M") if t else "--:--"
            mark = "✅ 제출" if sid in submitted_sids else "❌ 미제출"
            lines.append(f"- {label}: {time_str} [{mark}]")
        await inter.followup.send("\n".join(lines), ephemeral=True)
        return

    # 4-B) 새 형식 → 명확하게 제출/미제출 표시
    total = len(per_sid)
    submitted_cnt = sum(1 for sid in per_sid.keys() if sid in submitted_sids)
    rate = int(round(submitted_cnt * 100 / total)) if total > 0 else 0

    lines.append("")
    lines.append(f"요약: 총 {total}명 중 {submitted_cnt}명 제출 ({rate}%)")
    lines.append("")
    lines.append("📋 학생별 현황")

    for sid, (label, t) in sorted(per_sid.items(), key=lambda x: (x[1][1] or dtime(0, 0), x[1][0])):
        time_str = t.strftime("%H:%M") if t else "--:--"
        if sid in submitted_sids:
            mark = "✅ 제출"
        else:
            mark = "❌ 미제출"
        lines.append(f"- {label}: {time_str} [{mark}]")

    await inter.followup.send("\n".join(lines), ephemeral=True)

# ====== Slash: 신규 (/신규 — 시트 검증만, 쓰기 없음) ======
@bot.tree.command(name="신규", description="학생 닉네임/개인 카테고리 생성 (시트 검증만, 쓰기 없음)")
@app_commands.describe(student="학생 유저(멘션)", realname="시트의 학생 이름과 동일하게(필수)")
@app_commands.default_permissions(manage_channels=True)
@app_commands.guild_only()
async def slash_new(inter: discord.Interaction, student: discord.Member, realname: str):
    await inter.response.defer(ephemeral=True, thinking=True)
    guild = inter.guild
    if guild is None:
        await inter.followup.send("❌ 서버 내에서만 사용할 수 있어요.", ephemeral=True); return
    me = guild.me
    if not me or not (me.guild_permissions.manage_channels and me.guild_permissions.view_channel):
        await inter.followup.send("❌ 채널 생성/편집 권한이 부족합니다.", ephemeral=True); return

    base_raw = (realname or "").strip()
    if not base_raw:
        await inter.followup.send("❌ 본명(realname)은 필수입니다. 시트의 학생 이름과 동일하게 입력해주세요.", ephemeral=True); return

    base = normalize_base_name(base_raw)
    sid  = int(student.id)
    last4 = str(sid)[-4:]
    final_label = f"{base}-{last4}"

    # 시트 검증(읽기만)
    try:
        parsed = await SHEET_CACHE.get_parsed()
    except Exception as e:
        await inter.followup.send(f"❌ 시트 조회 실패: {type(e).__name__}: {e}", ephemeral=True); return
    name_to_id, id_to_name = _rebuild_name_id_maps(parsed)
    mapped_sid  = name_to_id.get(base)        # 이름→ID
    mapped_name = id_to_name.get(sid)         # ID→이름

    both_missing = (mapped_sid is None) and (mapped_name is None)
    both_match   = (mapped_sid == sid) and (mapped_name == base)
    partial_or_mismatch = (not both_missing) and (not both_match)

    # A) 둘 다 없음 → 중단
    if both_missing:
        room = await _get_text_channel_cached(SITUATION_ROOM_CHANNEL_ID)
        if room:
            await room.send("\n".join([
                "⛔ **신규 중단** — 시트에서 학생 정보를 찾지 못했습니다.",
                f"- 입력 이름: `{base}`",
                f"- discord_id: `{sid}`",
                "시트에 **이름**과 **discord_id**를 모두 기입한 뒤 다시 `/신규`를 실행해주세요.",
            ]))
        await inter.followup.send("⛔ 시트에 **이름과 discord_id가 모두 공란**입니다. 시트를 먼저 채워주세요.", ephemeral=True)
        return

    # B) 일부 불일치 → 진행 + 경고
    if partial_or_mismatch:
        details = []
        if mapped_sid is None:
            details.append("• 이름은 있으나 **discord_id가 비어 있습니다.**")
        elif mapped_sid != sid:
            details.append(f"• 이름은 확인되나 **discord_id 불일치** (시트:{mapped_sid} ≠ 입력:{sid})")
        if mapped_name is None:
            details.append("• discord_id는 있으나 **이름이 비어 있습니다.**")
        elif mapped_name != base:
            details.append(f"• discord_id는 확인되나 **이름 불일치** (시트:{mapped_name} ≠ 입력:{base})")
        room = await _get_text_channel_cached(SITUATION_ROOM_CHANNEL_ID)
        if room:
            await room.send("\n".join([
                "⚠️ **신규 진행(경고)** — 시트와 부분 불일치가 있습니다. 점검 부탁드립니다.",
                f"- 입력 이름: `{base}`",
                f"- discord_id: `{sid}`",
                *details
            ]))
        await inter.followup.send("⚠️ 시트와 일부 불일치가 있어요. (생성은 계속합니다)\n" + "\n".join(details), ephemeral=True)
    else:
        await inter.followup.send("✅ 시트 검증 통과(이름·ID 일치). 생성 계속합니다.", ephemeral=True)

    # 채널/닉네임 생성
    preferred = final_label
    final_nick = preferred
    try:
        if (student.nick or "") != final_nick and me.guild_permissions.manage_nicknames and me.top_role > student.top_role:
            await student.edit(nick=final_nick, reason="/신규: 본명/끝4")
    except Exception:
        pass

    # 카테고리/채널
    category_name = f"{final_label}{CATEGORY_SUFFIX}"
    try:
        category = discord.utils.get(guild.categories, name=category_name)
        if category is None:
            overwrites = {
                guild.default_role: discord.PermissionOverwrite(view_channel=False),
                student: discord.PermissionOverwrite(view_channel=True, send_messages=True, connect=True, speak=True),
            }
            if TEACHER_MAIN_ID:
                t = guild.get_member(TEACHER_MAIN_ID)
                if t: overwrites[t] = discord.PermissionOverwrite(view_channel=True, send_messages=True, connect=True, speak=True)
            category = await guild.create_category(category_name, overwrites=overwrites, reason="/신규: 학생 전용 카테고리")
        text = discord.utils.get(category.text_channels, name=TEXT_NAME) or await guild.create_text_channel(TEXT_NAME, category=category, reason="/신규: 채팅채널")
        discord.utils.get(category.voice_channels, name=VOICE_NAME) or await guild.create_voice_channel(VOICE_NAME, category=category, reason="/신규: 음성채널")
        # 텍스트 topic에 SID 태깅
        try:
            topic = text.topic or ""
            if f"SID:{sid}" not in topic:
                new_topic = (topic + (" | " if topic else "") + f"SID:{sid}")[:1024]
                await text.edit(topic=new_topic, reason="/신규: SID 태깅")
        except Exception:
            pass
    except Exception as e:
        await inter.followup.send(f"❌ 채널 생성 실패: {type(e).__name__}: {e}", ephemeral=True); return

    await inter.followup.send(f"✅ `{category.name}` 구성이 완료되었습니다.", ephemeral=True)

# ===== Homework Reminder Messages =====
REMINDER_18H = [
    "📘 내일은 수업하는 날!\n저번 시간에 배운 내용 복습하고 숙제도 해보도록 합시다 😊\n완료하셨다면 `/숙제` 로 알려주세요!",
    "🌞 내일은 수업하는 날!\n숙제는 복습의 시작 ✏️\n완료하셨다면 `/숙제` 로 알려주세요!",
    "📚 내일은 수업하는 날!\n숙제 한 번 확인해볼까요? ✨\n완료하셨다면 `/숙제` 로 알려주세요!",
    "🕕 내일은 수업하는 날!\n내일 수업을 위해 오늘 숙제 시작해볼까요? 🙌\n완료하셨다면 `/숙제` 로 알려주세요!",
    "📖 내일은 수업하는 날!\n오늘의 숙제는 내일의 발판이 되어 줄 거에요 🌟\n완료하셨다면 `/숙제` 로 알려주세요!",
]

REMINDER_22H = [
    "🌙 아직 늦지 않았어요!\n지금도 충분히 가능 💪\n🌱 완료하셨다면 `/숙제` 로 알려주세요!",
    "😌 오늘 하루도 고생 많았어요.\n이제 숙제만 마무리하면 정말 완벽한 하루 💫\n🌱 완료하셨다면 `/숙제` 로 알려주세요!",
    "✨ 오늘이 가기 전에 숙제까지 끝내볼까요?\n지금도 충분히 가능 💪\n🌱 완료하셨다면 `/숙제` 로 알려주세요!",
    "🌜 하루의 마지막 한 걸음!\n숙제까지 마치면 오늘 완벽한 마무리 ☺️\n🌱 완료하셨다면 `/숙제` 로 알려주세요!",
    "⭐ 오늘 수고 많았어요!\n잠깐, 숙제 한 번만 확인해봅시다 🌟\n🌱 완료하셨다면 `/숙제` 로 알려주세요!",
]

def _pick_homework_msg(hour: int) -> str:
    if hour == 18:
        return random.choice(REMINDER_18H)
    return random.choice(REMINDER_22H)

async def _send_homework_reminders(trigger_hour: int):
    today = datetime.now(KST).date()
    target_day = today + timedelta(days=1)
    day_iso = target_day.isoformat()

    sessions = await effective_sessions_for(target_day)
    candidate_sids = {sid for _, _, sid in sessions if isinstance(sid, int)}


    # 🔹 새 형식: {"submitted": [sid,...]} 기준으로 읽기
    submitted: Set[int] = set()
    try:
        async with _homework_lock:
            raw = homework.get(day_iso)
            submitted = _extract_submitted_sids(raw, allow_legacy_list=True)
    except Exception as e:
        print(f"[숙제 리마인더] homework 읽기 오류: {type(e).__name__}: {e}")
        submitted = set()

    # 이미 제출한 학생은 리마인드 대상에서 제외
    targets = sorted(sid for sid in candidate_sids if sid not in submitted)

    msg_body = _pick_homework_msg(trigger_hour)

    sent = 0
    for sid in targets:
        ch = _find_student_text_channel_by_id(sid, "학생")
        if not ch:
            continue
        try:
            await ch.send(f"<@{sid}>\n{msg_body}")
            sent += 1
        except Exception:
            pass

    room = await _get_text_channel_cached(SITUATION_ROOM_CHANNEL_ID)
    if room:
        await room.send(f"[숙제 리마인더] {trigger_hour}:00 전송 완료 — 대상 {len(targets)}명 / 실제 {sent}건 ({day_iso})")

# ====== Slash: 변경/보강/휴강 — ID-only 저장 ======
async def _after_override_commit(dt: date):
    if dt == datetime.now(KST).date():
        try:
            await refresh_student_id_map()
            await schedule_all_offsets_for_today()
        except Exception as e:
            print(f"[후처리 예약 오류] {type(e).__name__}: {e}")
    try:
        ch = await _get_text_channel_cached(SITUATION_ROOM_CHANNEL_ID)
        if ch:
            await ch.send(await build_timetable_message(dt))
    except Exception as e:
        print(f"[후처리 집계 오류] {type(e).__name__}: {e}")

@bot.tree.command(name="변경", description="해당 날짜의 기본 시각 A를 B로 변경 (A→B)")
@app_commands.describe(student="학생", when="YYYY-MM-DD 또는 '오늘'", from_time="HH:MM", to_time="HH:MM")
@app_commands.default_permissions(manage_channels=True)
async def slash_change(inter: discord.Interaction, student: discord.Member, when: str, from_time: str, to_time: str):
    await inter.response.defer(ephemeral=True, thinking=True)
    dt = _parse_day_input(when)
    if not dt: await inter.followup.send("❌ 날짜 형식 오류", ephemeral=True); return
    if not parse_time_str(from_time) or not parse_time_str(to_time):
        await inter.followup.send("❌ 시각 형식은 HH:MM 입니다.", ephemeral=True); return
    day_iso = dt.isoformat()
    try:
        async with _overrides_lock:
            ovs_day = _ensure_day_bucket(day_iso)
            ov_clear_changes_id(ovs_day, student.id)
            ov_add_change_pair_id(ovs_day, student.id, from_time, to_time)
            ov_set_cancel_id(ovs_day, student.id, False)
            overrides[day_iso] = ovs_day
        await save_overrides()
    except Exception as e:
        await inter.followup.send(f"❌ 변경 저장 실패: {type(e).__name__}: {e}", ephemeral=True); return
    await _after_override_commit(dt)
    await inter.followup.send("✅ 변경 반영 완료.", ephemeral=True)

@bot.tree.command(name="변경삭제", description="해당 날짜의 모든 변경(A→B)을 제거")
@app_commands.describe(student="학생", when="YYYY-MM-DD 또는 '오늘'")
@app_commands.default_permissions(manage_channels=True)
async def slash_change_clear(inter: discord.Interaction, student: discord.Member, when: str):
    await inter.response.defer(ephemeral=True, thinking=True)
    dt = _parse_day_input(when)
    if not dt: await inter.followup.send("❌ 날짜 형식 오류", ephemeral=True); return
    day_iso = dt.isoformat()
    try:
        async with _overrides_lock:
            ovs_day = _ensure_day_bucket(day_iso)
            ov_clear_changes_id(ovs_day, student.id)
            _cleanup_entry_if_empty_id(ovs_day, student.id)
            overrides[day_iso] = ovs_day
        await save_overrides()
    except Exception as e:
        await inter.followup.send(f"❌ 변경 삭제 실패: {type(e).__name__}: {e}", ephemeral=True); return
    await _after_override_commit(dt)
    await inter.followup.send("✅ 변경 기록을 모두 삭제했습니다.", ephemeral=True)

@bot.tree.command(name="보강", description="해당 날짜에 보강 시각을 추가합니다. (예: 18:15)")
@app_commands.describe(
    student="학생",
    when="YYYY-MM-DD 또는 '오늘'",
    time="보강 시각 HH:MM",
)
@app_commands.default_permissions(manage_channels=True)
async def slash_makeup(inter: discord.Interaction, student: discord.Member, when: str, time: str):
    await inter.response.defer(ephemeral=True, thinking=True)

    # (1) 날짜/시각 파싱
    dt = _parse_day_input(when)  # 기존 프로젝트에 있는 날짜 파서 사용
    if not dt:
        await inter.followup.send("❌ 날짜 형식은 YYYY-MM-DD 또는 '오늘' 입니다.", ephemeral=True); return
    if not parse_time_str(time):  # 기존의 HH:MM 유효성 검사 함수 사용
        await inter.followup.send("❌ 시각은 HH:MM 형식이어야 합니다.", ephemeral=True); return

    day_iso = dt.isoformat()

    # (2) 현재 휴강 상태 여부 확인 (헬퍼 없이 직접 조회)
    ovs_day = overrides.get(day_iso) or {}
    entry = _ov_get_id(ovs_day, student.id)
    is_canceled = bool(entry and entry.get("cancel"))

    # (3) 보강 추가 (ID 기반 API 사용)
    try:
        async with _overrides_lock:
            # 버킷 보장
            if day_iso not in overrides or not isinstance(overrides.get(day_iso), dict):
                overrides[day_iso] = {}
            # 중복 없이 추가
            ov_add_makeup_id(overrides[day_iso], student.id, time)  # <- 프로젝트의 ID 기반 함수
        await save_overrides()
    except Exception as e:
        await inter.followup.send(f"❌ 보강 추가 실패: {type(e).__name__}: {e}", ephemeral=True); return

    # (4) 후처리(집계 재게시 + 오늘이면 재예약까지는 _after_override_commit에서 처리)
    try:
        await _after_override_commit(dt)
    except Exception as e:
        # 후처리에 실패해도, 보강 자체는 저장되었으므로 안내만 남김
        await inter.followup.send(
            f"✅ 보강을 추가했습니다. (후처리 중 경고: {type(e).__name__})", ephemeral=True
        )
        return

    # (5) 휴강일 경고 안내
    warn = (
        "\n\n⚠️ **이 날짜는 현재 ‘휴강’ 상태**입니다.\n"
        "보강을 등록해도 **그 날의 수업/알림에는 반영되지 않습니다.**\n"
        "수업을 진행하려면 먼저 `/휴강삭제`로 휴강을 해제한 뒤 보강을 사용하세요."
        if is_canceled else ""
    )

    await inter.followup.send(f"✅ 보강을 추가했습니다. 최신 집계를 상황실에 게시했습니다.{warn}", ephemeral=True)

@bot.tree.command(name="보강삭제", description="해당 날짜의 모든 보강 삭제")
@app_commands.describe(student="학생", when="YYYY-MM-DD 또는 '오늘'")
@app_commands.default_permissions(manage_channels=True)
async def slash_makeup_remove_all(inter: discord.Interaction, student: discord.Member, when: str):
    await inter.response.defer(ephemeral=True, thinking=True)
    dt = _parse_day_input(when)
    if not dt: await inter.followup.send("❌ 날짜 형식 오류", ephemeral=True); return
    day_iso = dt.isoformat()
    try:
        async with _overrides_lock:
            ovs_day = _ensure_day_bucket(day_iso)
            e = _ov_get_or_create_id(ovs_day, student.id)
            if not e.get("makeup"):
                await inter.followup.send("ℹ️ 해당 날짜에 등록된 보강이 없습니다.", ephemeral=True); return
            e["makeup"] = []
            _cleanup_entry_if_empty_id(ovs_day, student.id)
            overrides[day_iso] = ovs_day
        await save_overrides()
    except Exception as e:
        await inter.followup.send(f"❌ 보강 삭제 실패: {type(e).__name__}: {e}", ephemeral=True); return
    await _after_override_commit(dt)
    await inter.followup.send("✅ 보강 삭제 완료.", ephemeral=True)

@bot.tree.command(name="휴강", description="해당 날짜 휴강 처리")
@app_commands.describe(student="학생", when="YYYY-MM-DD 또는 '오늘'")
@app_commands.default_permissions(manage_channels=True)
async def slash_cancel(inter: discord.Interaction, student: discord.Member, when: str):
    await inter.response.defer(ephemeral=True, thinking=True)
    dt = _parse_day_input(when)
    if not dt: await inter.followup.send("❌ 날짜 형식 오류", ephemeral=True); return
    day_iso = dt.isoformat()
    try:
        async with _overrides_lock:
            ovs_day = _ensure_day_bucket(day_iso)
            ov_set_cancel_id(ovs_day, student.id, True)
            overrides[day_iso] = ovs_day
        await save_overrides()
    except Exception as e:
        await inter.followup.send(f"❌ 휴강 처리 실패: {type(e).__name__}: {e}", ephemeral=True); return
    await _after_override_commit(dt)
    await inter.followup.send("✅ 휴강 처리 완료.", ephemeral=True)

@bot.tree.command(name="휴강삭제", description="해당 날짜의 휴강 상태 해제")
@app_commands.describe(student="학생", when="YYYY-MM-DD 또는 '오늘'")
@app_commands.default_permissions(manage_channels=True)
async def slash_cancel_remove(inter: discord.Interaction, student: discord.Member, when: str):
    await inter.response.defer(ephemeral=True, thinking=True)
    dt = _parse_day_input(when)
    if not dt: await inter.followup.send("❌ 날짜 형식 오류", ephemeral=True); return
    day_iso = dt.isoformat()
    try:
        async with _overrides_lock:
            ovs_day = _ensure_day_bucket(day_iso)
            ov_set_cancel_id(ovs_day, student.id, False)
            _cleanup_entry_if_empty_id(ovs_day, student.id)
            overrides[day_iso] = ovs_day
        await save_overrides()
    except Exception as e:
        await inter.followup.send(f"❌ 휴강 해제 실패: {type(e).__name__}: {e}", ephemeral=True); return
    await _after_override_commit(dt)
    await inter.followup.send("✅ 휴강 해제 완료.", ephemeral=True)

# ====== 관리: 시간표/새로고침/로그 ======
@bot.tree.command(name="시간표", description="특정 날짜의 수업 시간표를 보여줍니다.")
@app_commands.describe(when="오늘/내일 또는 YYYY-MM-DD / MM-DD")
@app_commands.guild_only()
async def slash_timetable(inter: discord.Interaction, when: str = "오늘"):
    # 답변 지연(로딩 표시)
    await inter.response.defer(ephemeral=True, thinking=True)

    # 1) 날짜 파싱
    day = _parse_day_input(when or "오늘")
    if not day:
        # 날짜 형식이 잘못된 경우
        if inter.response.is_done():
            await inter.followup.send(
                "날짜 형식 오류입니다. 오늘/내일 또는 YYYY-MM-DD / MM-DD 를 사용해주세요.",
                ephemeral=True,
            )
        else:
            await inter.response.send_message(
                "날짜 형식 오류입니다. 오늘/내일 또는 YYYY-MM-DD / MM-DD 를 사용해주세요.",
                ephemeral=True,
            )
        return

    # 2) 시간표 메시지 생성
    try:
        msg = await build_timetable_message(day)
    except Exception as e:
        print(f"[/시간표 오류] {type(e).__name__}: {e}")
        if inter.response.is_done():
            await inter.followup.send("시간표를 불러오는 중 문제가 발생했습니다.", ephemeral=True)
        else:
            await inter.response.send_message("시간표를 불러오는 중 문제가 발생했습니다.", ephemeral=True)
        return

    # 3) 2000자 제한을 고려해 나눠 보내기
    #    (현재는 개인에게만 보이도록 ephemeral=True 로 설정)
    await send_long_message(inter, msg, ephemeral=True)

@bot.tree.command(name="새로고침", description="시트 새로고침 + 오늘 집계 재게시 + 알림(-10,75) 재설정")
@app_commands.default_permissions(manage_channels=True)
async def slash_reload(inter: discord.Interaction):
    await inter.response.defer(ephemeral=True, thinking=True)
    msgs = []
    try:
        SHEET_CACHE._ts = 0.0
        await refresh_student_id_map()
        msgs.append("• 학생 ID맵 새로고침 완료")
    except Exception as e:
        msgs.append(f"• 학생 ID맵 새로고침 실패: {type(e).__name__}: {e}")
    try:
        await post_today_summary()
        msgs.append("• 오늘 [수업 집계] 재게시 완료")
    except Exception as e:
        msgs.append(f"• 집계 재게시 실패: {type(e).__name__}: {e}")
    try:
        await schedule_all_offsets_for_today()
        msgs.append(f"• 알림 타이머 재설정 {ALERT_OFFSETS} 완료")
    except Exception as e:
        msgs.append(f"• 알림 타이머 재설정 실패: {type(e).__name__}: {e}")
    await inter.followup.send("✅ 새로고침 결과\n" + "\n".join(msgs), ephemeral=True)

@bot.tree.command(name="로그", description="해당 날짜 집계를 선생님 DM으로 전송")
@app_commands.describe(when="오늘/내일 또는 YYYY-MM-DD / MM-DD")
@app_commands.default_permissions(manage_channels=True)
async def slash_log(inter: discord.Interaction, when: str = "오늘"):
    await inter.response.defer(ephemeral=True, thinking=True)
    day = _parse_day_input(when or "오늘")
    if not day: await inter.followup.send("날짜 형식 오류", ephemeral=True); return
    if not TEACHER_MAIN_ID: await inter.followup.send("❌ TEACHER_MAIN_ID 미설정", ephemeral=True); return
    try:
        u = await _get_user_cached(TEACHER_MAIN_ID)
        if not u: await inter.followup.send("❌ 선생님 계정 조회 실패", ephemeral=True); return
        await send_long(u, await build_timetable_message(day))
        await inter.followup.send(f"✅ `{day.isoformat()}` 집계를 선생님 DM으로 보냈습니다.", ephemeral=True)
    except Exception as e:
        await inter.followup.send(f"❌ 전송 실패: {type(e).__name__}: {e}", ephemeral=True)

# ====== Errors ======
@bot.event
async def on_command_error(ctx: commands.Context, error: commands.CommandError):
    if isinstance(error, commands.CommandNotFound): return
    try: await ctx.send("❌ 명령 실행 중 오류가 발생했어요. 콘솔 로그를 확인해 주세요.")
    except Exception: pass
    traceback.print_exception(type(error), error, error.__traceback__)

@bot.tree.error
async def on_app_command_error(inter: discord.Interaction, error: app_commands.AppCommandError):
    original = getattr(error, "original", error)
    try:
        msg = "⚠️ 명령 처리 중 오류가 발생했습니다. 로그를 확인할게요."
        if inter.response.is_done(): await inter.followup.send(msg, ephemeral=True)
        else: await inter.response.send_message(msg, ephemeral=True)
    finally:
        print(f"[AppCommandError] {type(original).__name__}: {original}")

# ====== Ready & Main ======
async def _background_after_ready():
    if getattr(bot, "_post_ready_once_done", False):
        return

    async with _post_ready_lock:
        if getattr(bot, "_post_ready_once_done", False):
            return

        # 슬래시 동기화 (429 안전모드에서는 기본 비활성)
        if ENABLE_SLASH_SYNC:
            try:
                if GUILD_ID:
                    gobj = discord.Object(id=GUILD_ID)
                    bot.tree.copy_global_to(guild=gobj)
                    synced = await bot.tree.sync(guild=gobj)
                    print(f"✅ 길드({GUILD_ID}) 슬래시 등록: {len(synced)}개")
                else:
                    synced = await bot.tree.sync()
                    print(f"⚠️ GUILD_ID 미설정 → 글로벌 sync: {len(synced)}개")
            except discord.HTTPException as e:
                if getattr(e, "status", None) == 429:
                    print("[429-safe] 슬래시 sync에서 429 감지: 자동 재시도하지 않고 건너뜁니다.")
                else:
                    print(f"[슬래시 등록 오류] {type(e).__name__}: {e}")
            except Exception as e:
                print(f"[슬래시 등록 오류] {type(e).__name__}: {e}")
        else:
            print("[429-safe] ENABLE_SLASH_SYNC=0 → 슬래시 sync를 건너뜁니다.")

        # 시트 워밍업
        try:
            await SHEET_CACHE.get_parsed()
            print("[워밍업] 시트 캐시 준비 완료")
        except Exception as e:
            print("[워밍업 실패] PermissionError repr:", repr(e))

        bot._post_ready_once_done = True


@bot.event
async def on_ready():
    print(f"Logged in as {bot.user} (KST {datetime.now(KST)})")

    # Discord 재연결 시 on_ready가 여러 번 호출될 수 있으므로
    # 무거운 초기화는 1회만 수행합니다.
    if getattr(bot, "_boot_once_done", False):
        print("[429-safe] 재연결 감지: 부팅 초기화는 건너뜁니다.")
        return

    async with _ready_boot_lock:
        if getattr(bot, "_boot_once_done", False):
            return

        # 부팅시 맵/마이그레이션
        try:
            await refresh_student_id_map()
        except Exception as e:
            print(f"[부팅 학생맵 오류] {type(e).__name__}: {e}")

        try:
            await migrate_overrides_to_id_only(refresh_map=False)  # 이름키→ID-only
        except Exception as e:
            print(f"[부팅 마이그레이션 오류] {type(e).__name__}: {e}")

        # 오늘 상대 알림(-10,75) 예약

        try:
            await schedule_all_offsets_for_today()
            print("[부팅] 오늘 알림 예약 완료", ALERT_OFFSETS)
        except Exception as e:
            print("[부팅 예약 오류] PermissionError repr:", repr(e))

        # 스케줄러 일괄 기동 (중복 방지)
        if not getattr(bot, "_sched_start", False):
            bot._sched_start = True
            asyncio.create_task(daily_scheduler())      # 13:00 집계
            asyncio.create_task(midnight_scheduler())   # 자정 집계/예약
            asyncio.create_task(homework_scheduler())   # 18:00 / 22:00 숙제 리마인더
            print("[스케줄러] daily + midnight + homework(18/22시) 시작")

        # 슬래시 sync + 시트 워밍업은 1회 비동기 실행
        if not getattr(bot, "_post_ready_task_started", False):
            bot._post_ready_task_started = True
            asyncio.create_task(_background_after_ready())

        bot._boot_once_done = True

# Health server (Render 등)
async def _start_health_server():
    port = int(os.environ.get("PORT", "10000"))
    from aiohttp import web
    async def handle(_): return web.Response(text="ok")
    app = web.Application(); app.router.add_get("/", handle); app.router.add_get("/healthz", handle)
    runner = web.AppRunner(app); await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port); await site.start()
    print(f"[health] listening on :{port}")

async def _heartbeat():
    # 주기적으로 살아있음을 출력해 로그가 비어보이는 문제를 줄입니다.
    while True:
        try:
            print(f"[heartbeat] alive {datetime.now(KST).isoformat()}")
        except Exception:
            pass
        await asyncio.sleep(max(5, HEARTBEAT_INTERVAL_SEC))

async def _main():
    asyncio.create_task(_start_health_server())
    asyncio.create_task(_heartbeat())

    # Firestore 초기화 + 데이터 로드
    init_firestore_client(SERVICE_ACCOUNT_JSON)
    load_from_firestore_or_local()

    print(
        f"[429-safe] SAFE_MODE_429={int(SAFE_MODE_429)} "
        f"ENABLE_SLASH_SYNC={int(ENABLE_SLASH_SYNC)} "
        f"BACKOFF={RATE_LIMIT_WAIT_MIN}-{RATE_LIMIT_WAIT_MAX}min"
    )

    if not BOT_TOKEN:
        raise SystemExit("❌ BOT_TOKEN이 비어있습니다.")

    attempt = 0

    # ✅ 429 자동복구: 매우 느린 재시도(백오프)
    while True:
        try:
            print("[Discord] 로그인 시도 시작")
            await bot.start(BOT_TOKEN)
            return  # bot이 종료되면 여기로 돌아올 수 있음
        except RuntimeError as e:
            # aiohttp 세션이 닫힌 상태에서 재시도되는 경우가 있어 안전하게 대기 후 재시도
            if "Session is closed" in str(e):
                lo = max(1, min(RATE_LIMIT_WAIT_MIN, RATE_LIMIT_WAIT_MAX))
                hi = max(lo, max(RATE_LIMIT_WAIT_MIN, RATE_LIMIT_WAIT_MAX))
                wait = random.randint(lo * 60, hi * 60)
                print("[치명] aiohttp Session is closed — 안전 대기 후 재시도")
                print(f"       {wait:.0f}초 대기 후 재시도")
                try:
                    bot.http.clear()
                except Exception:
                    pass
                await asyncio.sleep(wait + random.uniform(0, 3))
                continue
            raise
        except discord.HTTPException as e:
            if getattr(e, "status", None) == 429:
                attempt += 1
                ra = _retry_after_seconds(e)

                # Retry-After가 있으면 그걸 따르고,
                # 없으면 30~60분 사이 랜덤 대기(재차단 방지용 '느린' 전략)
                if isinstance(ra, (int, float)) and ra > 0:
                    wait = ra
                else:
                    lo = max(1, min(RATE_LIMIT_WAIT_MIN, RATE_LIMIT_WAIT_MAX))
                    hi = max(lo, max(RATE_LIMIT_WAIT_MIN, RATE_LIMIT_WAIT_MAX))
                    wait = random.randint(lo * 60, hi * 60)

                print("[치명] Discord 글로벌 레이트 리밋(429) — 자동 복구 모드")
                print(f"       {wait:.0f}초 대기 후 재시도 (시도 #{attempt})")

                # 지터 약간
                await asyncio.sleep(wait + random.uniform(0, 3))
                continue

            raise  # 429가 아니면 그대로 터뜨려서 원인 확인

if __name__ == "__main__":
    try:
        asyncio.run(_main())
    except KeyboardInterrupt:
        pass
