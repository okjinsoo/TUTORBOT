# bot.py — TutorBot (All Features + Final Patch)
# 2025-10-01
# - Atomic save/recovery for JSON (overrides/attendance)
# - SheetCache with executor (non-blocking gspread)
# - ID-first override storage
# - Schedulers with double-run guard
# - Situation room send via fetch_channel (no missing messages)
# - No duplicate sheet fetch (build_timetable_message -> passes parsed to effective_sessions_for)

# ===== Standard Library =====
import os, json, re, asyncio, random
from typing import Dict, List, Tuple, Optional, Any, Set
from datetime import datetime, date, timedelta, time as dtime, timezone

# ===== .env =====
from dotenv import load_dotenv
load_dotenv()
ENV = os.environ.get

# ===== Env / Config =====
BOT_TOKEN = (ENV("BOT_TOKEN") or "").strip()
GUILD_ID = int(ENV("GUILD_ID") or "0") or None
TEACHER_MAIN_ID = int(ENV("TEACHER_MAIN_ID") or "0") or None
SITUATION_ROOM_CHANNEL_ID = int(ENV("SITUATION_ROOM_CHANNEL_ID") or "0") or None
SHEET_ID = (ENV("SHEET_ID") or "").strip()
SHEET_NAME = (ENV("SHEET_NAME") or "시간표").strip()
SERVICE_ACCOUNT_JSON = (ENV("SERVICE_ACCOUNT_JSON") or "service_account.json").strip()

# ===== KST =====
try:
    from zoneinfo import ZoneInfo
    def get_kst(): return ZoneInfo("Asia/Seoul")
except Exception:
    # 일부 환경(구버전/윈도)에서 zoneinfo가 없을 때의 안전한 폴백
    def get_kst(): return timezone(timedelta(hours=9))
KST = get_kst()

# ===== Discord =====
import discord
from discord.ext import commands
from discord import app_commands

# ===== Google Sheets =====
import gspread
from google.oauth2.service_account import Credentials

# ===== Files / persistence =====
OVERRIDE_FILE = "overrides.json"    # { "YYYY-MM-DD": { "<id str>|<legacy name>": {cancel, change, changes, makeup}, ... } }
ATTENDANCE_FILE = "attendance.json" # { "YYYY-MM-DD": [discord_id, ...] }
HOMEWORK_FILE = "homework.json"      # { "YYYY-MM-DD": [discord_id, ...] }

_overrides_lock = asyncio.Lock()
_attendance_lock = asyncio.Lock()
_homework_lock = asyncio.Lock()

def _safe_json_dumps(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2)

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
    def _load(p: str):
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

async def save_overrides():
    async with _overrides_lock:
        save_json_atomic(OVERRIDE_FILE, overrides)

async def save_attendance():
    async with _attendance_lock:
        save_json_atomic(ATTENDANCE_FILE, attendance)

async def save_homework():
    async with _homework_lock:
        save_json_atomic(HOMEWORK_FILE, homework)

def prune_old_homework(days: int = 60):
    """오래된 숙제 기록 정리(기본 60일)"""
    try:
        cutoff = datetime.now(KST).date() - timedelta(days=days)
        old_keys = []
        for k in list(homework.keys()):
            try:
                d = date.fromisoformat(k)
            except Exception:
                continue
            if d < cutoff:
                old_keys.append(k)
        for k in old_keys:
            try: del homework[k]
            except: pass
    except Exception as e:
        print(f"[숙제 보관 정리 오류] {type(e).__name__}: {e}")

# ===== Bot / Intents =====
intents = discord.Intents.default()
intents.guilds = True
intents.members = True
intents.voice_states = True
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

# ===== Runtime =====
midcheck_tasks: Dict[int, asyncio.Task] = {}
last_question_at: Dict[int, float] = {}
STUDENT_ID_MAP: Dict[str, int] = {}  # {real_name: id}
rel_tasks: Dict[Tuple[Optional[int], int, str, int], asyncio.Task] = {}  # (sid|None, HHMM, day_iso, offset)
oneoff_homework_tasks: Dict[Tuple[int, str], asyncio.Task] = {}

CATEGORY_SUFFIX = " 채널"
TEXT_NAME = "채팅채널"
VOICE_NAME = "음성채널"

# ===== Time utils =====
TIME_RE = re.compile(r"^\s*(\d{1,2})\s*[:시]\s*(\d{0,2})\s*(분)?\s*$")
WEEKDAY_MAP = {"월":0, "화":1, "수":2, "목":3, "금":4, "토":5, "일":6}

def parse_time_str(s: str) -> Optional[dtime]:
    if not isinstance(s, str): return None
    m = TIME_RE.match(s.strip())
    if not m: return None
    hh = int(m.group(1)); mm = int(m.group(2) or 0)
    if 0 <= hh <= 23 and 0 <= mm <= 59:
        return dtime(hh, mm)
    return None

def parse_date_yyyy_mm_dd(s: str) -> Optional[date]:
    if not isinstance(s, str) or not s.strip(): return None
    try:
        return datetime.fromisoformat(s.strip()).date()
    except Exception:
        return None

def normalize_base_name(name: str) -> str:
    if not name: return name
    return re.sub(r'(-\d{4})+$', '', name).strip()

# ===== Google Sheets =====
def gs_client():
    scopes = ["https://www.googleapis.com/auth/spreadsheets.readonly"]
    creds = Credentials.from_service_account_file(SERVICE_ACCOUNT_JSON, scopes=scopes)
    return gspread.authorize(creds)

class SheetCache:
    def __init__(self, ttl_seconds: int = 90):
        self.ttl = ttl_seconds
        self._rows: Optional[List[List[str]]] = None
        self._parsed: Optional[Dict[str, Any]] = None
        self._ts: float = 0.0
        self._lock = asyncio.Lock()
        self._min_interval = 2.0
        self._last_fetch: float = 0.0

    async def get_rows(self) -> List[List[str]]:
        now = asyncio.get_event_loop().time()
        if self._rows is not None and (now - self._ts) <= self.ttl:
            return self._rows
        async with self._lock:
            now2 = asyncio.get_event_loop().time()
            if self._rows is not None and (now2 - self._ts) <= self.ttl:
                return self._rows
            wait = self._min_interval - (now2 - self._last_fetch)
            if wait > 0:
                await asyncio.sleep(wait)
            self._last_fetch = asyncio.get_event_loop().time()
            loop = asyncio.get_running_loop()

            def _fetch():
                gc = gs_client()
                ws = gc.open_by_key(SHEET_ID).worksheet(SHEET_NAME)
                return ws.get_all_values()

            rows = await loop.run_in_executor(None, _fetch)
            self._rows = rows
            self._parsed = None
            self._ts = asyncio.get_event_loop().time()
            return rows

    async def get_parsed(self) -> Dict[str, Any]:
        now = asyncio.get_event_loop().time()
        if self._parsed is not None and (now - self._ts) <= self.ttl:
            return self._parsed
        rows = await self.get_rows()
        self._parsed = parse_schedule_single_sheet(rows)
        return self._parsed

SHEET_CACHE = SheetCache(ttl_seconds=90)

def parse_schedule_single_sheet(rows):
    """
    Header expected:
      학생 이름 | discord_id | (요일|시간)* | ... | (서비스 시작일) | (서비스 종료일)

    returns: { key: {"name":str,"id":int|None,"pairs":[(요일,dtime)], "start_raw":str, "end_raw":str} }
    """
    if not rows: return {}
    header = [h.strip() for h in rows[0]]
    if "학생 이름" in header:
        name_idx = header.index("학생 이름")
    elif "이름" in header:
        name_idx = header.index("이름")
    else:
        return {}
    id_idx = header.index("discord_id") if "discord_id" in header else None
    start_idx = header.index("서비스 시작일") if "서비스 시작일" in header else None
    end_idx   = header.index("서비스 종료일") if "서비스 종료일" in header else None

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
            if i + 1 >= len(r): break
            day = (r[i] or "").strip()
            time_raw = (r[i+1] or "").strip()
            if not day or not time_raw: continue
            if day not in WEEKDAY_MAP: break
            t = parse_time_str(time_raw)
            if t: pairs.append((day, t))

        start_raw = (r[start_idx].strip() if (start_idx is not None and len(r) > start_idx) else "") if start_idx is not None else ""
        end_raw   = (r[end_idx].strip()   if (end_idx   is not None and len(r) > end_idx)   else "") if end_idx   is not None else ""

        key = str(did) if isinstance(did, int) else f"{name}#row{ridx}"
        data[key] = {"name": name, "id": did, "pairs": pairs, "start_raw": start_raw, "end_raw": end_raw}
    return data

async def refresh_student_id_map():
    global STUDENT_ID_MAP
    try:
        base = await SHEET_CACHE.get_parsed()
        m = {}
        for info in base.values():
            did = info.get("id")
            real_name = info.get("name")
            if isinstance(did, int) and real_name:
                m[real_name] = did
        STUDENT_ID_MAP = m
        print(f"[학생ID맵] 로드 OK: {len(STUDENT_ID_MAP)}명")
    except Exception as e:
        print(f"[학생ID맵 로드 오류] {e}")

# ---- overrides helpers (ID-first) ----
def _ov_get(ovs_day: dict, student_name: Optional[str], student_id: Optional[int]) -> Optional[dict]:
    if isinstance(student_id, int):
        got = ovs_day.get(str(student_id))
        if isinstance(got, dict):
            return got
    if student_name:
        got = ovs_day.get(student_name)
        if isinstance(got, dict):
            return got
    return None

def _ov_set(ovs_day: dict, student_name: str, student_id: int, entry: dict) -> None:
    ovs_day[str(student_id)] = entry
    if student_name in ovs_day and str(student_id) != student_name:
        try: del ovs_day[student_name]
        except Exception: pass

# ===== Scheduling core (service period aware) =====
async def effective_sessions_for(day: date, parsed: Optional[Dict[str, Any]] = None):
    """
    Returns list of (student_name, start_time:dtime, student_id)
    Service period:
      - if start_raw missing -> exclude basic sessions (not started)
      - if end_raw missing   -> end = start + 28 days
    Overrides:
      - cancel removes all
      - change/changes modifies base times
      - makeup added regardless of period
    """
    base = parsed or await SHEET_CACHE.get_parsed()
    wd = day.weekday()
    day_iso = day.isoformat()
    ovs_day = overrides.get(day_iso, {})

    result = []
    for _key, info in base.items():
        student = info.get("name")
        sid = info.get("id")
        pairs: List[Tuple[str, dtime]] = info.get("pairs", [])
        times: List[dtime] = [t for (d, t) in pairs if WEEKDAY_MAP.get(d) == wd]

        # service period
        start_raw = (info.get("start_raw") or "").strip()
        end_raw   = (info.get("end_raw")   or "").strip()
        start_date = parse_date_yyyy_mm_dd(start_raw)
        if start_date is None:
            times = []  # not started
        else:
            end_date = parse_date_yyyy_mm_dd(end_raw) or (start_date + timedelta(days=28))
            if not (start_date <= day <= end_date):
                times = []

        # overrides
        entry = _ov_get(ovs_day, student, sid) if student is not None else None
        if entry:
            chg = entry.get("changes")
            if isinstance(chg, list) and chg:
                for item in chg:
                    src = item.get("from"); dst = item.get("to")
                    t_from = parse_time_str(str(src)) if src is not None else None
                    t_to   = parse_time_str(str(dst)) if dst is not None else None
                    if t_from and t_to and t_from in times:
                        try: times.remove(t_from)
                        except ValueError: pass
                        if t_to not in times:
                            times.append(t_to)
                times = sorted(set(times))
            ch = entry.get("change")
            if ch is not None:
                t_ch = parse_time_str(str(ch))
                if t_ch:
                    times = [t_ch]
            adds = entry.get("makeup") or []
            for a in adds:
                t_add = parse_time_str(str(a))
                if t_add and t_add not in times:
                    times.append(t_add)
            if entry.get("cancel"):
                times = []

        for t in sorted(times):
            result.append((student, t, sid))
    return result

async def next_student_session_date(student_id: Optional[int] = None, student_name: Optional[str] = None, days_ahead: int = 30) -> Optional[date]:
    """
    오늘 이후 가장 가까운 '수업 날짜'(일자 기준)를 반환.
    - 오늘이라면 '지금 이후' 세션이 있는 경우만 인정.
    - 내일 이후는 세션이 하나라도 있으면 그 날짜를 반환.
    """
    base_day = datetime.now(KST).date()
    now_time = datetime.now(KST).time()

    for i in range(days_ahead + 1):
        d = base_day + timedelta(days=i)
        sessions = await effective_sessions_for(d)  # [(name, dtime, sid)]
        found = False
        for n, t, sid in sessions:
            if (isinstance(student_id, int) and sid == student_id) or (student_name and n == student_name):
                if i == 0 and (t.hour, t.minute) <= (now_time.hour, now_time.minute):
                    continue
                found = True
                break
        if found:
            return d
    return None

# ===== Mention/Channel utils (ID-first) =====
def _mention_student_by_id(student_id: Optional[int], fallback_name: str) -> str:
    return f"<@{student_id}>" if isinstance(student_id, int) else fallback_name

def _label_from_guild_or_default(name: str, sid: Optional[int]) -> str:
    if isinstance(sid, int):
        for guild in bot.guilds:
            m = guild.get_member(sid)
            if m:
                return (m.display_name or m.nick or name)
    if isinstance(sid, int):
        return f"{name}-{str(sid)[-4:]}"
    return name

def _find_student_text_channel_by_id(student_id: Optional[int], fallback_name: str) -> Optional[discord.TextChannel]:
    # 학생 객체 조회
    member = None
    if isinstance(student_id, int):
        for guild in bot.guilds:
            m = guild.get_member(student_id)
            if m:
                member = m
                break

    # 1) 표시명 기반 카테고리명 매칭
    if member:
        display = (member.display_name or member.nick or fallback_name)
        cat_name = f"{display}{CATEGORY_SUFFIX}"
        for guild in bot.guilds:
            category = discord.utils.get(guild.categories, name=cat_name)
            if category:
                text = discord.utils.get(category.text_channels, name=TEXT_NAME) or (category.text_channels[0] if category.text_channels else None)
                if text:
                    return text

    # 2) 권한 기반 매칭: 카테고리 오버라이드에 학생이 볼 수 있는 곳
    if member:
        for guild in bot.guilds:
            for category in guild.categories:
                try:
                    if _category_belongs_to_member(category, member):
                        text = (discord.utils.get(category.text_channels, name=TEXT_NAME)
                                or (category.text_channels[0] if category.text_channels else None))
                        if text:
                            return text
                except Exception:
                    continue

    # 3) 토픽 기반 매칭: 텍스트 채널 topic에 SID:<id>
    if isinstance(student_id, int):
        sid_tag = f"SID:{student_id}"
        for guild in bot.guilds:
            for category in guild.categories:
                for text in category.text_channels:
                    try:
                        if (text.topic or "").find(sid_tag) != -1:
                            return text
                    except Exception:
                        continue

    # 4) 실시간 매칭(최후): 학생이 현재 들어있는 음성 채널의 카테고리
    if member and member.voice and member.voice.channel and member.voice.channel.category:
        category = member.voice.channel.category
        text = discord.utils.get(category.text_channels, name=TEXT_NAME) or (category.text_channels[0] if category.text_channels else None)
        if text:
            return text

    # 모든 경로 실패 → None
    return None

def _is_teacher_in_category(category: Optional[discord.CategoryChannel]) -> bool:
    if not category: return False
    for vc in category.voice_channels:
        for m in vc.members:
            if TEACHER_MAIN_ID and m.id == TEACHER_MAIN_ID:
                return True
    return False

def _has_human_student_in_voice(category: Optional[discord.CategoryChannel]) -> bool:
    if not category: return False
    for vc in category.voice_channels:
        for m in vc.members:
            if (not m.bot) and (not TEACHER_MAIN_ID or m.id != TEACHER_MAIN_ID):
                return True
    return False

def _category_belongs_to_member(category: discord.CategoryChannel, member: discord.Member) -> bool:
    if not category or not isinstance(category, discord.CategoryChannel):
        return False
    # 최종 계산된 권한(역할/상속 포함)으로 판단
    perms = category.permissions_for(member)
    return bool(getattr(perms, "view_channel", False))

def _make_unique_nickname(guild: discord.Guild, base: str) -> str:
    existing = {(m.nick or m.display_name or "").strip() for m in guild.members if (m.nick or m.display_name)}
    if base not in existing: return base
    i = 1
    while True:
        cand = f"{base}-{i}"
        if cand not in existing: return cand
        i += 1

def _make_unique_category_name(guild: discord.Guild, base_name: str) -> str:
    if not discord.utils.get(guild.categories, name=base_name): return base_name
    i = 1
    while True:
        cand = f"{base_name}-{i}"
        if not discord.utils.get(guild.categories, name=cand): return cand
        i += 1

def _student_ids_in_channel(vc: discord.VoiceChannel) -> List[int]:
    """해당 보이스 채널에 있는 '학생'(봇/선생 제외)들의 Discord ID 리스트를 반환."""
    return [m.id for m in vc.members if (not m.bot) and (not TEACHER_MAIN_ID or m.id != TEACHER_MAIN_ID)]

# ===== Timetable message (attendance included) =====
async def build_timetable_message(day: date) -> str:
    day_iso = day.isoformat()
    wd = day.weekday()

    # 1) 시트 파싱 결과 1회만 사용 (중복 fetch 방지)
    parsed = await SHEET_CACHE.get_parsed()

    # 2) 서비스 기간까지 반영한 '기본 수업' 집계
    def _in_service_period(info: Dict[str, Any], d: date) -> bool:
        start_raw = (info.get("start_raw") or "").strip()
        end_raw   = (info.get("end_raw") or "").strip()
        start_date = parse_date_yyyy_mm_dd(start_raw)
        if start_date is None:
            return False
        end_date = parse_date_yyyy_mm_dd(end_raw) or (start_date + timedelta(days=28))
        return (start_date <= d <= end_date)

    base_on_day: Dict[Tuple[str, Optional[int]], List[dtime]] = {}
    for info in parsed.values():
        name = info.get("name")
        sid = info.get("id")
        if not name:
            continue
        if not _in_service_period(info, day):
            continue
        pairs: List[Tuple[str, dtime]] = info.get("pairs", [])
        times = sorted([t for (d_lbl, t) in pairs if WEEKDAY_MAP.get(d_lbl) == wd])
        if times:
            base_on_day[(name, sid)] = times

    # 3) overrides와 병합하여 표시 대상 키 생성
    ovs_day: dict = overrides.get(day_iso, {})
    display_keys: set = set(base_on_day.keys())

    def _member_display_name_by_id(sid: int) -> Optional[str]:
        for guild in bot.guilds:
            m = guild.get_member(sid)
            if m:
                return (m.display_name or m.nick or m.name)
        return None

    for k in list(ovs_day.keys()):
        if isinstance(k, str) and k.isdigit():
            sid = int(k)
            name_from_base = None
            for info in parsed.values():
                if info.get("id") == sid:
                    name_from_base = info.get("name"); break
            if not name_from_base:
                # 표시명 폴백 (표시명-카테고리 불일치 보완)
                name_from_base = _member_display_name_by_id(sid) or "학생"
            display_keys.add((name_from_base, sid))
        else:
            display_keys.add((k, None))

    def _tl(t: dtime) -> str:
        return t.strftime("%H:%M")

    # 4) 라벨 캐시로 길드 조회 최소화
    label_cache: Dict[Tuple[str, Optional[int]], str] = {}
    def get_label(name: str, sid: Optional[int]) -> str:
        key = (name, sid)
        if key not in label_cache:
            label_cache[key] = _label_from_guild_or_default(name, sid)
        return label_cache[key]

    canceled_lines: List[str] = []
    changed_lines: List[str] = []
    makeup_lines: List[str] = []

    # 5) 변경/보강/휴강 섹션 구축 (학생별 내부 정렬까지 안정화)
    for (name, sid) in sorted(display_keys, key=lambda x: get_label(x[0], x[1])):
        if not name:
            continue
        label = get_label(name, sid)
        entry = _ov_get(ovs_day, name, sid)
        if not entry:
            continue

        # 휴강
        if entry.get("cancel"):
            old_times = base_on_day.get((name, sid), [])
            old_str = ", ".join(_tl(t) for t in old_times) if old_times else "(기본 없음)"
            canceled_lines.append(f"- {label}: {old_str} (휴강)")
            continue

        # 변경 (복수)
        chg = entry.get("changes")
        if isinstance(chg, list) and chg:
            pairs_fmt: List[Tuple[dtime, str]] = []
            for item in chg:
                t_from = parse_time_str(str(item.get("from")))
                t_to   = parse_time_str(str(item.get("to")))
                if t_from and t_to:
                    pairs_fmt.append((t_from, f"{_tl(t_from)}→{_tl(t_to)}"))
            # 학생 내부에서도 from 시간 기준 정렬
            pairs_fmt.sort(key=lambda p: (p[0].hour, p[0].minute))
            if pairs_fmt:
                changed_lines.append(f"- {label}: " + ", ".join(p for _, p in pairs_fmt))
        else:
            # 변경 (단일, 레거시)
            ch = entry.get("change")
            if ch is not None:
                t_ch = parse_time_str(str(ch))
                if t_ch:
                    old = base_on_day.get((name, sid), [])
                    old_str = ", ".join(_tl(t) for t in old) if old else "(기본 없음)"
                    changed_lines.append(f"- {label}: {old_str} → {_tl(t_ch)}")

        # 보강
        adds = entry.get("makeup") or []
        adds_times: List[dtime] = []
        for a in adds:
            t = parse_time_str(str(a))
            if t:
                adds_times.append(t)
        adds_times = sorted(set(adds_times), key=lambda t: (t.hour, t.minute))
        if adds_times:
            makeup_lines.append(f"- {label}: " + ", ".join(_tl(t) for t in adds_times))

    # 6) 최종 세션(서비스 기간 + overrides 적용) 및 출석 표시
    effective = await effective_sessions_for(day, parsed)  # parsed 재사용으로 중복 fetch 방지
    attended_ids = set(attendance.get(day_iso, []))

    effective_labeled = []
    for (n, t, sid) in effective:
        label = get_label(n, sid)
        effective_labeled.append((label, t, sid))

    effective_sorted = sorted(effective_labeled, key=lambda x: (x[0], x[1]))

    # 7) 출력 빌드 (섹션별 정렬 안정화)
    lines = [f"**[수업 집계] ({day_iso})**", ""]

    if makeup_lines:
        lines.append("**📌 보강**")
        lines.extend(sorted(makeup_lines))
    else:
        lines.append("**📌 보강**: 없음")
    lines.append("")

    if changed_lines:
        lines.append("**🔄 변경**")
        lines.extend(sorted(changed_lines))
    else:
        lines.append("**🔄 변경**: 없음")
    lines.append("")

    if canceled_lines:
        lines.append("**⛔ 휴강**")
        lines.extend(sorted(canceled_lines))
    else:
        lines.append("**⛔ 휴강**: 없음")
    lines.append("")

    if effective_sorted:
        lines.append("**🗓️ 수업 (최종)**")
        for label, t, sid in effective_sorted:
            mark = "✅ 출석" if (isinstance(sid, int) and sid in attended_ids) else "❌ 미출석"
            lines.append(f"- {label}: {t.strftime('%H:%M')} [{mark}]")
    else:
        lines.append("**🗓️ 수업 (최종)**: 없음")

    out = "\n".join(lines)
    out = "\n".join(["> " + line for line in out.splitlines()])
    return out

# ===== Situation room & DM =====
async def send_long(dest, text: str, max_len: int = 1990):
    """Discord 2000자 제한 고려해 줄 단위로 안전 분할 전송."""
    buf = ""
    for line in (text or "").splitlines():
        add = line + "\n"
        if len(buf) + len(add) > max_len:
            await dest.send(buf); buf = ""
        buf += add
    if buf.strip():
        await dest.send(buf)

async def post_today_summary():
    """상황실에 오늘 [수업 집계] 게시 (캐시 우선 채널 조회)"""
    ch = await _get_text_channel_cached(SITUATION_ROOM_CHANNEL_ID)
    if not ch:
        print(f"[경고] 상황실 채널(ID={SITUATION_ROOM_CHANNEL_ID}) 조회 실패/비텍스트")
        return
    today = datetime.now(KST).date()
    out = (await build_timetable_message(today) or "").strip() or "> **[수업 집계]**\n> (내용 없음)"
    # ✅ 길이 체크/분할 전송 수작업 제거 → 유틸로 통일
    await send_long(ch, out)

async def post_day_summary_to_teacher(day: date):
    """선생님 DM 전송(자정 요약 등) — 캐시 우선 유저 조회"""
    try:
        if not TEACHER_MAIN_ID:
            print("[경고] TEACHER_MAIN_ID 미설정"); return
        teacher = await _get_user_cached(TEACHER_MAIN_ID)
        if not teacher:
            print("[경고] 선생님 유저 조회 실패"); return
        out = (await build_timetable_message(day) or "").strip() or "> **[수업 집계]**\n> (내용 없음)"
        await send_long(teacher, out)
    except Exception as e:
        print(f"[자정 로그 DM 실패] {type(e).__name__}: {e}")

# ===== Voice mid-check (3→2 타이머, 학생만 멘션) =====
# 🔹 파일 상단 Runtime 섹션에 아래 전역변수 1줄이 있어야 합니다.
# midcheck_channel_tasks: Dict[int, asyncio.Task] = {}
midcheck_channel_tasks: Dict[int, asyncio.Task] = {}

def _parse_screen_share_ids_from_env() -> Set[int]:
    """ .env 의 SCREEN_SHARE_IDS=111,222 형식을 파싱 (없으면 빈 집합) """
    raw = (ENV("SCREEN_SHARE_IDS") or "").strip()
    if not raw:
        return set()
    ids: Set[int] = set()
    for tok in re.split(r"[,\s]+", raw):
        if tok.isdigit():
            try:
                ids.add(int(tok))
            except Exception:
                pass
    return ids

_SCREEN_IDS: Set[int] = _parse_screen_share_ids_from_env()

def _is_teacher(member: discord.Member) -> bool:
    return bool(TEACHER_MAIN_ID and member.id == TEACHER_MAIN_ID)

def _is_student(member: discord.Member) -> bool:
    if member.bot:
        return False
    if _is_teacher(member):
        return False
    if member.id in _SCREEN_IDS:  # 화면공유 계정 제외
        return False
    return True

def _voice_humans(vc: Optional[discord.VoiceChannel]) -> List[discord.Member]:
    """봇 제외 사람 멤버 목록"""
    if not vc:
        return []
    return [m for m in vc.members if not m.bot]

def _category_label(cat: Optional[discord.CategoryChannel]) -> str:
    """상황실 로그용 라벨 (카테고리명 그대로 사용, 없으면 '학생')"""
    if not cat or not isinstance(cat, discord.CategoryChannel):
        return "학생"
    return cat.name[:-len(CATEGORY_SUFFIX)] if cat.name.endswith(CATEGORY_SUFFIX) else cat.name

async def _text_channel_in_category(cat: Optional[discord.CategoryChannel]) -> Optional[discord.TextChannel]:
    if not cat or not isinstance(cat, discord.CategoryChannel):
        return None
    text = discord.utils.get(cat.text_channels, name=TEXT_NAME)
    if text:
        return text
    return cat.text_channels[0] if cat.text_channels else None

async def _midcheck_channel_timer(vc: discord.VoiceChannel):
    """20분 대기 후 여전히 2명(학생 포함)일 때 학생만 멘션 + 상황실 로그"""
    MIDCHECK_DELAY_MIN = 20
    try:
        await asyncio.sleep(MIDCHECK_DELAY_MIN * 60)

        # 현재 상태 재확인
        if not vc or not vc.category:
            return
        humans_now = _voice_humans(vc)

        # 3명 이상이거나 2명 미만이면 취소
        if len(humans_now) != 2:
            return

        # 학생(멘션 대상)만 우선, 없으면 사람 2명 모두
        students_now = [m for m in humans_now if _is_student(m)]
        targets = students_now if students_now else humans_now

        text_ch = await _text_channel_in_category(vc.category)
        if not text_ch:
            return

        mentions = " ".join(m.mention for m in targets)
        await text_ch.send(f"{mentions}\n선생님이 곧 입장합니다. 질문이 있다면 준비해주세요.")

        # 상황실 로그 — 캐시 우선 채널 조회 사용
        try:
            room = await _get_text_channel_cached(SITUATION_ROOM_CHANNEL_ID)
            if room:
                await room.send(f"[중간점검 안내] {_category_label(vc.category)} 채널(음성)에 20분 유지 → 안내 메시지 발송")
        except Exception:
            pass

    except asyncio.CancelledError:
        return
    except Exception as e:
        print(f"[중간점검 타이머 오류] {e}")
    finally:
        # 완료/취소 후 정리 (채널 ID 키)
        task = midcheck_channel_tasks.pop(getattr(vc, "id", None), None)
        if task and not task.done():
            task.cancel()

def _maybe_start_or_cancel_midcheck(vc: Optional[discord.VoiceChannel]):
    """
    규칙:
      - 음성 채널 인원(봇 제외)이 3→2가 되면 타이머 시작
      - 2→3(이상) 되면 타이머 취소
      - 2→1/0 도 취소
    """
    if not vc or vc.name != VOICE_NAME:
        return

    humans = _voice_humans(vc)
    ch_id = vc.id

    if len(humans) == 2:
        if ch_id not in midcheck_channel_tasks or midcheck_channel_tasks[ch_id].done():
            midcheck_channel_tasks[ch_id] = asyncio.create_task(_midcheck_channel_timer(vc))
            print(f"[중간점검] 채널 {ch_id} 2명 유지 → 타이머 시작")
        return

    # 3명 이상 또는 1명 이하: 타이머 있으면 취소
    task = midcheck_channel_tasks.pop(ch_id, None)
    if task and not task.done():
        task.cancel()
        print(f"[중간점검] 채널 {ch_id} 인원 {len(humans)}명 → 타이머 취소")

@bot.event
async def on_voice_state_update(member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
    """입장/퇴장/이동 시, 우리 수업 음성채널 인원 변화 감지해서 타이머 제어"""
    try:
        if before and before.channel:
            _maybe_start_or_cancel_midcheck(before.channel)
        if after and after.channel:
            _maybe_start_or_cancel_midcheck(after.channel)
    except Exception as e:
        print(f"[on_voice_state_update 오류] {e}")

# ===== Situation room & DM =====
async def _get_text_channel_cached(channel_id: Optional[int]) -> Optional[discord.TextChannel]:
    if not channel_id:
        return None
    # 1) 캐시/메모리 우선
    ch = bot.get_channel(channel_id)
    if isinstance(ch, discord.TextChannel):
        return ch
    # 2) 없으면 원격 조회 (HTTP)
    try:
        ch = await bot.fetch_channel(channel_id)
        return ch if isinstance(ch, discord.TextChannel) else None
    except Exception as e:
        print(f"[채널 조회 실패] {type(e).__name__}: {e}")
        return None

async def _get_user_cached(user_id: Optional[int]) -> Optional[discord.User]:
    if not user_id:
        return None
    # 1) 캐시 우선
    u = bot.get_user(user_id)
    if u:
        return u
    # 2) 없으면 원격 조회
    try:
        return await bot.fetch_user(user_id)
    except Exception as e:
        print(f"[유저 조회 실패] {type(e).__name__}: {e}")
        return None

# ===== Service deadline reminders (DM to teacher) =====
DEADLINE_OFFSETS_ONLY_START = (21, 25, 27, 28)  # 시작일만 있을 때: start + n일 == 오늘
DEADLINE_OFFSETS_END_GIVEN  = (-7, -3, -1, 0)   # 종료일 있을 때: end + n일 == 오늘 (0은 D-DAY)

async def check_service_deadlines():
    """매일 00:00에 호출: 규칙에 맞는 학생들을 한 메시지로 선생님 DM."""
    if not TEACHER_MAIN_ID:
        return
    try:
        base = await SHEET_CACHE.get_parsed()
    except Exception as e:
        print(f"[서비스 기한 체크 실패] {type(e).__name__}: {e}")
        return

    today = datetime.now(KST).date()
    matched: List[str] = []

    # 라벨 캐시
    _label_cache: Dict[Tuple[str, Optional[int]], str] = {}
    def _get_label(name: str, sid: Optional[int]) -> str:
        key = (name, sid)
        if key not in _label_cache:
            _label_cache[key] = _label_from_guild_or_default(name, sid)
        return _label_cache[key]

    for info in base.values():
        name = (info.get("name") or "학생")
        sid  = info.get("id")
        start_raw = (info.get("start_raw") or "").strip()
        end_raw   = (info.get("end_raw") or "").strip()
        start_date = parse_date_yyyy_mm_dd(start_raw)
        end_date   = parse_date_yyyy_mm_dd(end_raw)

        # case 1) 시작일만 있는 경우
        if start_date and not end_date:
            delta = (today - start_date).days
            if delta >= 0 and delta in DEADLINE_OFFSETS_ONLY_START:
                when = {21:"D-7", 25:"D-3", 27:"D-1", 28:"D-DAY"}[delta]
                label = _get_label(name, sid)
                matched.append(f"• {label} — 서비스 시작일 {start_date.isoformat()} 기준 {when}")

        # case 2) 시작/종료일 모두 있는 경우
        if start_date and end_date:
            delta2 = (today - end_date).days  # -7, -3, -1, 0 등
            if delta2 in DEADLINE_OFFSETS_END_GIVEN:
                when = {-7:"D-7", -3:"D-3", -1:"D-1", 0:"D-DAY"}[delta2]
                label = _get_label(name, sid)
                matched.append(f"• {label} — 서비스 종료일 {end_date.isoformat()} {when}")

    # 정렬 + 중복 제거
    if not matched:
        return
    matched_sorted = sorted(dict.fromkeys(matched))  # 유지 순서 중복제거

    try:
        teacher = await _get_user_cached(TEACHER_MAIN_ID)
        if not teacher:
            print("[서비스 종료일 DM 실패] 선생님 유저 조회 실패")
            return

        header = ["**[서비스 종료일 알림]**", f"기준일: {today.isoformat()}", ""]
        body = "\n".join(matched_sorted)
        full_text = "\n".join(header) + "\n" + body

        # 긴 메시지 분할 전송 (유틸이 있으면 재사용)
        async def _send_long(u, text: str, max_len: int = 1990):
            buf = ""
            for line in text.splitlines():
                add = line + "\n"
                if len(buf) + len(add) > max_len:
                    await u.send(buf); buf = ""
                buf += add
            if buf.strip():
                await u.send(buf)

        await _send_long(teacher, full_text)

    except Exception as e:
        print(f"[서비스 종료일 DM 실패] {type(e).__name__}: {e}")

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

# ===== Alerts =====
ALERT_OFFSETS = (-10, 75, 85)  # 10분 전, 75분 후, 85분 후

# 라벨 계산 캐시 (동시 다발 알림 시 미세 최적화)
_label_cache: Dict[Tuple[str, Optional[int]], str] = {}
def _get_label(name: str, sid: Optional[int]) -> str:
    key = (name, sid)
    if key not in _label_cache:
        _label_cache[key] = _label_from_guild_or_default(name, sid)
    return _label_cache[key]

def _cancel_rel_tasks_for(day_iso: str, offset_min: Optional[int] = None):
    """
    오늘(day_iso) 예약들 중 특정 offset만 또는 전체를 취소하고 정리.
    """
    to_cancel = []
    for key, task in list(rel_tasks.items()):
        _sid, _hhmm, _day, _off = key
        if _day != day_iso:
            continue
        if offset_min is not None and _off != offset_min:
            continue
        to_cancel.append(key)
        if task and not task.done():
            task.cancel()
    for k in to_cancel:
        rel_tasks.pop(k, None)
        # 필요 시 주석 해제해서 취소 로그 확인
        # print(f"[알림취소] {k}")

async def _fire_relative(
    student_name: str,
    student_id: Optional[int],
    start_time: dtime,
    fire_at: datetime,
    offset_min: int,
):
    """알림: 학생 텍스트 채널 + 상황실 로그(선생님 DM 없음)"""
    try:
        # 예약 시각까지 대기
        await asyncio.sleep(max(0, (fire_at - datetime.now(KST)).total_seconds()))

        # 🔒 슬립/지연 등으로 이미 오래 지나버렸다면 발송 생략(허용 오차 2분)
        if datetime.now(KST) - fire_at > timedelta(minutes=2):
            return

        mention = f"<@{student_id}>" if isinstance(student_id, int) else student_name
        label = _get_label(student_name, student_id)
        start_label = start_time.strftime('%H:%M')

        if offset_min < 0:
            msg_student = (
                f"{mention} 수업 {abs(offset_min)}분 전입니다.\n"
                f"- 시작 시각: {start_label}\n"
                f"- 준비물: 태블릿/필기도구/문제지\n"
                f"- 10분 내 디스코드 입장 부탁드립니다."
            )
            log = f"[상황실] {label} 수업 {abs(offset_min)}분 전 알림 전송"
        else:
            msg_student = (
                f"{mention} 수업이 {offset_min}분이 지났습니다. "
                f"수업을 마칠 준비를 해주세요. (시작 {start_label})"
            )
            log = f"[상황실] {label} 수업 {offset_min}분 경과 알림 전송"

        # 학생 텍스트 채널 알림
        ch = _find_student_text_channel_by_id(student_id, student_name)
        if ch:
            try:
                await ch.send(msg_student)
            except Exception:
                pass
        else:
            # 🔁 보강: 학생 채널을 못 찾으면 상황실에 실패 로그라도 남김
            room_fb = await _get_text_channel_cached(SITUATION_ROOM_CHANNEL_ID)
            if room_fb:
                try:
                    await room_fb.send(f"[알림 실패] {label} 학생 채널을 찾지 못해 학생 알림 생략")
                except Exception:
                    pass

        # 상황실 로그(캐시 우선 조회)
        room = await _get_text_channel_cached(SITUATION_ROOM_CHANNEL_ID) if SITUATION_ROOM_CHANNEL_ID else None
        if room:
            try:
                await room.send(log)
            except Exception:
                pass

    except asyncio.CancelledError:
        return
    except Exception as e:
        print(f"[REL{offset_min}] 알림 오류: {e}", flush=True)

async def schedule_relative_alerts_for_today(offset_min: int):
    """
    오늘의 유효 수업 세션들을 조회하여, 각 세션 시작시각 기준 offset_min 분 전/후에 알림 예약.
    """
    today = datetime.now(KST).date()
    today_iso = today.isoformat()
    sessions = await effective_sessions_for(today)  # [(name, dtime, sid)]

    # 기존 같은 offset 예약들 정리
    _cancel_rel_tasks_for(today_iso, offset_min)

    now = datetime.now(KST)
    for name, t, sid in sessions:
        start_dt = datetime.combine(today, t, KST)
        fire_at = start_dt + timedelta(minutes=offset_min)
        if (fire_at - now).total_seconds() <= 0:
            # 이미 지나간 예약은 스킵
            continue

        hhmm = t.hour * 100 + t.minute
        key = (sid if isinstance(sid, int) else None, hhmm, today_iso, offset_min)

        # ⚠️ 같은 키가 이미 존재하면 먼저 취소 후 교체(명시적)
        old = rel_tasks.get(key)
        if old and not old.done():
            old.cancel()

        rel_tasks[key] = asyncio.create_task(
            _fire_relative(name, sid, t, fire_at, offset_min)
        )

# ===== Events =====
@bot.event
async def on_ready():
    print(f"Logged in as {bot.user} (KST {datetime.now(KST)})")

    # 1) 가벼운 초기화만 await (빠르게)
    await refresh_student_id_map()
    try:
        await schedule_all_offsets_for_today()
        print("[부팅] 오늘 알림 예약 완료", (-10, 75, 85))
    except Exception as e:
        print(f"[부팅 예약 오류] {e}")

    # 2) 스케줄러 중복 가드
    if getattr(bot, "_schedulers_started", False):
        print("[가드] 스케줄러는 이미 시작됨. 재시작 안 함.")
    else:
        bot._schedulers_started = True
        asyncio.create_task(daily_scheduler())
        asyncio.create_task(midnight_scheduler())
        print("[스케줄러] daily + midnight 시작")

    # 👉 숙제 리마인더 스케줄러 시작 (중복 가드)
    if not getattr(bot, "_hw_sched_started", False):
        bot._hw_sched_started = True
        asyncio.create_task(homework_reminder_scheduler())
        print("[스케줄러] 숙제 리마인더(18/22시) 시작")

    # 3) 백그라운드 작업(슬래시 동기화 + 시트 워밍업)도 중복 가드
    if not getattr(bot, "_bg_tasks_started", False):
        bot._bg_tasks_started = True
        asyncio.create_task(_background_after_ready())
    else:
        print("[가드] 백그라운드 after_ready 작업은 이미 시작됨.")

async def _background_after_ready():
    # a) 슬래시 동기화: 길드 전용 1회만
    try:
        if GUILD_ID:
            guild_obj = discord.Object(id=GUILD_ID)
            # 글로벌 정의를 길드로 복사 후 길드만 sync (중복/지연 줄임)
            bot.tree.copy_global_to(guild=guild_obj)
            synced = await bot.tree.sync(guild=guild_obj)
            names = ", ".join(cmd.name for cmd in synced)
            print(f"✅ 길드({GUILD_ID}) 전용 슬래시 재등록: {len(synced)}개 [{names}]")
        else:
            synced = await bot.tree.sync()
            print(f"⚠️ GUILD_ID 미설정 → 글로벌 동기화: {len(synced)}개")
    except Exception as e:
        print(f"[슬래시 정리/등록 오류] {e}")

    # b) 시트 캐시 워밍업: 1회만 (첫 호출 지연 방지)
    try:
        await SHEET_CACHE.get_parsed()  # rows까지 내부에서 채워짐
        print("[워밍업] 시트 캐시 준비 완료")
    except Exception as e:
        print(f"[워밍업 실패] {type(e).__name__}: {e}")

async def schedule_all_offsets_for_today():
    for off in ALERT_OFFSETS:
        await schedule_relative_alerts_for_today(off)

# ===== Homework Reminder Core =====
HOMEWORK_FILE = "homework.json"  # 없으면 기존 정의 재사용
_homework_lock = asyncio.Lock()
homework: Dict[str, List[int]] = load_json_with_recovery(HOMEWORK_FILE, {})  # { "YYYY-MM-DD": [discord_id, ...] }

async def _students_with_session_on(day: date) -> Set[int]:
    """해당 날짜에 수업 있는 학생 sid 집합 (휴강/변경/보강 반영된 최종 세션 기준)."""
    sessions = await effective_sessions_for(day)
    return {sid for _, _, sid in sessions if isinstance(sid, int)}

async def _students_needing_homework_reminder_for(day: date) -> Set[int]:
    """해당 날짜 숙제 미제출자 집합(수업 있는 학생 - homework 제출자)."""
    sids = await _students_with_session_on(day)
    submitted = set(homework.get(day.isoformat(), []))
    # homework에는 리마인더 로직상 수업 sid도 함께 저장될 수 있으므로 교집합 고려
    return {sid for sid in sids if sid not in submitted}

async def _send_homework_reminders_for_tomorrow(hour: int):
    """hour ∈ {18, 22} 기준으로 내일 수업 대상의 '미제출자'에게만 안내 전송."""
    assert hour in (18, 22)
    today = datetime.now(KST).date()
    target = today + timedelta(days=1)

    try:
        targets = await _students_needing_homework_reminder_for(target)
        if not targets:
            return

        for sid in sorted(targets):
            # 학생 텍스트 채널 찾기
            ch = _find_student_text_channel_by_id(sid, "학생")
            if not ch:
                # 상황실에 실패 로그 남김
                room = await _get_text_channel_cached(SITUATION_ROOM_CHANNEL_ID)
                if room:
                    await room.send(f"[숙제 리마인더 {hour}] 채널 없음 → SID:{sid}")
                continue

            mention = f"<@{sid}>"
            msg = _pick_homework_msg(hour)
            try:
                await ch.send(f"{mention}\n{msg}")
            except Exception:
                # 개별 전송 오류는 넘어가되 상황실에 기록
                room = await _get_text_channel_cached(SITUATION_ROOM_CHANNEL_ID)
                if room:
                    await room.send(f"[숙제 리마인더 {hour}] 전송 실패 → SID:{sid}")

        # 상황실 요약 로그
        room = await _get_text_channel_cached(SITUATION_ROOM_CHANNEL_ID)
        if room:
            await room.send(f"[숙제 리마인더 {hour}] 내일({target.isoformat()}) 미제출자 {len(targets)}명 안내 완료")

    except Exception as e:
        print(f"[숙제 리마인더 {hour}] 오류: {type(e).__name__}: {e}")

# ===== Schedulers =====
async def daily_scheduler():
    """매일 13:00 오늘 집계 상황실 게시 (유예창구 포함)"""
    await bot.wait_until_ready()
    GRACE_SEC = 5 * 60   # 5분 유예

    while not bot.is_closed():
        now = datetime.now(KST)
        target = datetime.combine(now.date(), dtime(13, 0), KST)

        # 유예 내 즉시 실행
        if 0 <= (now - target).total_seconds() <= GRACE_SEC:
            try:
                await refresh_student_id_map()
                await post_today_summary()
                print("[13:00(유예 내 즉시)] 오늘 [수업 집계] 전송 완료")
            except Exception as e:
                print(f"[13시 집계 오류(유예)] {type(e).__name__}: {e}")
            target = target + timedelta(days=1)

        # 이미 충분히 지났으면 내일로
        if now > target:
            target = target + timedelta(days=1)

        # ⬇️ 음수 방지 (클럭 점프 대비)
        sleep_sec = max(0.0, (target - now).total_seconds())
        try:
            await asyncio.sleep(sleep_sec)
        except asyncio.CancelledError:
            return

        # 깨어난 뒤 실제 실행
        try:
            await refresh_student_id_map()
            await post_today_summary()
            print("[13:00] 오늘 [수업 집계] 전송 완료")
        except Exception as e:
            print(f"[13시 집계 오류] {type(e).__name__}: {e}")

async def midnight_scheduler():
    """매일 00:00: 전일 DM → 오늘 집계 게시 → 알림 예약 → 서비스 종료일 알림"""
    await bot.wait_until_ready()
    while not bot.is_closed():
        now = datetime.now(KST)
        target = datetime.combine(now.date(), dtime(0, 0), KST)
        if now >= target:
            target += timedelta(days=1)
        await asyncio.sleep((target - now).total_seconds())

        # ⬇️ 자정 실행 시점의 기준일을 한 번만 잡고 재사용
        run_base = datetime.now(KST).date()
        yesterday = run_base - timedelta(days=1)

        try:
            await post_day_summary_to_teacher(yesterday)
        except Exception as e:
            print(f"[자정 DM 오류] {type(e).__name__}: {e}")

        try:
            await refresh_student_id_map()
            await post_today_summary()
            await schedule_all_offsets_for_today()  # (-10, 75, 85)
            await check_service_deadlines()         # 서비스 종료일/시작일 기반 DM
            print("[00:00] 새로고침 완료: 집계 게시 + 알림 예약 + 기한 DM")
        except Exception as e:
            print(f"[자정 새로고침/예약 오류] {type(e).__name__}: {e}")

async def _send_homework_reminders_for(day: date):
    """주어진 날짜의 수업 대상 중 '숙제 미제출' 학생에게 리마인드."""
    sessions = await effective_sessions_for(day)  # [(name, time, sid)]
    target_ids: Set[int] = set()
    for n, t, sid in sessions:
        if isinstance(sid, int):
            target_ids.add(sid)

    day_iso = day.isoformat()
    submitted = set(homework.get(day_iso, []))
    remind_ids = [sid for sid in target_ids if sid not in submitted]

    if not remind_ids:
        return

    for sid in remind_ids:
        ch = _find_student_text_channel_by_id(sid, "학생")
        if ch:
            try:
                await ch.send(f"<@{sid}>\n숙제 제출, 잊지 않으셨죠? 😊\n숙제를 제출하셨다면 `!숙제` 를 입력해주세요!")
            except Exception:
                pass
        else:
            try:
                room = await _get_text_channel_cached(SITUATION_ROOM_CHANNEL_ID)
                if room:
                    await room.send(f"[숙제 리마인더 실패] SID:{sid} 학생 채널을 찾지 못해 발송 생략")
            except Exception:
                pass

async def homework_reminder_scheduler():
    """매일 18:00 / 22:00에 '내일 수업' 미제출자에게 리마인더 전송."""
    await bot.wait_until_ready()
    while not bot.is_closed():
        now = datetime.now(KST)

        # 다음 목표 시각 계산(오늘 18:00, 22:00 중 다음 또는 내일 18:00)
        candidates = [
            datetime.combine(now.date(), dtime(18, 0), KST),
            datetime.combine(now.date(), dtime(22, 0), KST),
        ]
        next_run = min((t for t in candidates if t > now), default=None)
        if next_run is None:
            # 오늘 두 타임이 모두 지났으면 내일 18:00
            next_run = datetime.combine(now.date() + timedelta(days=1), dtime(18, 0), KST)

        # 대기
        try:
            await asyncio.sleep(max(0.0, (next_run - now).total_seconds()))
        except asyncio.CancelledError:
            return

        # 실행
        try:
            hour = next_run.hour  # 18 또는 22
            await _send_homework_reminders_for_tomorrow(hour)
        except Exception as e:
            print(f"[숙제 리마인더 실행 오류] {type(e).__name__}: {e}")

async def _send_homework_checklist_dm_for(day: date):
    """해당 날짜 수업 대상 학생들의 숙제 제출 현황을 선생님 DM으로 전송."""
    if not TEACHER_MAIN_ID:
        return
    teacher = await _get_user_cached(TEACHER_MAIN_ID)
    if not teacher:
        print("[숙제 체크 DM 실패] 선생님 유저 조회 실패")
        return

    sessions = await effective_sessions_for(day)  # [(name, time, sid)]
    by_sid: Dict[int, str] = {}
    for n, t, sid in sessions:
        if isinstance(sid, int) and sid not in by_sid:
            by_sid[sid] = _label_from_guild_or_default(n, sid)

    day_iso = day.isoformat()
    submitted = set(homework.get(day_iso, []))

    lines = [f"**[숙제 체크] ({day_iso})**"]
    if not by_sid:
        lines.append("오늘 수업 대상이 없습니다.")
    else:
        for sid, label in sorted(by_sid.items(), key=lambda x: x[1]):
            mark = "📝 제출" if sid in submitted else "⛔ 미제출"
            lines.append(f"- {label}: {mark}")

    await send_long(teacher, "\n".join(lines))

async def homework_checklist_2am_scheduler():
    """매일 02:00에 '오늘자 숙제 체크표'를 선생님 DM으로 전송."""
    await bot.wait_until_ready()
    if getattr(bot, "_hw_2am_started", False):
        print("[가드] 02:00 숙제 체크 스케줄러는 이미 시작됨.")
        return
    bot._hw_2am_started = True

    while not bot.is_closed():
        now = datetime.now(KST)
        target = datetime.combine(now.date(), dtime(2, 0), KST)
        if now >= target:
            target += timedelta(days=1)
        try:
            await asyncio.sleep((target - now).total_seconds())
        except asyncio.CancelledError:
            return

        try:
            today = datetime.now(KST).date()
            await _send_homework_checklist_dm_for(today)
            prune_old_homework(60)
            await save_homework()
            print("[02:00] 숙제 체크 DM 전송 + 숙제 기록 정리 완료")
        except Exception as e:
            print(f"[02:00 숙제 체크 오류] {type(e).__name__}: {e}")

async def _schedule_oneoff_homework_reminder(sid: int, day: date, fire_at: datetime, reason: str):
    """
    (sid, day) 단위 일회성 숙제 리마인더 예약 or 즉시 발송.
    - 예약 전에/직전에 모두 '이미 제출' 여부를 체크해 중복 발송 방지.
    - 동일 (sid, day) 기존 예약이 있으면 취소 후 교체.
    """
    day_iso = day.isoformat()

    # 1차: 이미 제출했으면 예약 불필요
    if sid in set(homework.get(day_iso, [])):
        return

    # 기존 예약 취소
    key = (sid, day_iso)
    old = oneoff_homework_tasks.get(key)
    if old and not old.done():
        old.cancel()

    async def _fire():
        try:
            # 2차: 발송 직전 재확인
            if sid in set(homework.get(day_iso, [])):
                return
            ch = _find_student_text_channel_by_id(sid, "학생")
            if ch:
                await ch.send(
                    f"<@{sid}>\n숙제 제출, 잊지 않으셨죠? 😊\n숙제를 제출하셨다면 `!숙제` 를 입력해주세요! ({reason})"
                )
            room = await _get_text_channel_cached(SITUATION_ROOM_CHANNEL_ID)
            if room:
                await room.send(f"[숙제 리마인더(일회성)] SID:{sid} {day_iso} @ {fire_at.strftime('%H:%M')} ({reason})")
        except asyncio.CancelledError:
            return

    now = datetime.now(KST)
    if fire_at <= now:
        await _fire()
        oneoff_homework_tasks.pop(key, None)
    else:
        sleep_task = asyncio.create_task(asyncio.sleep((fire_at - now).total_seconds()))
        async def _runner():
            try:
                await sleep_task
                await _fire()
            finally:
                oneoff_homework_tasks.pop(key, None)
        oneoff_homework_tasks[key] = asyncio.create_task(_runner())

# ===== Slash 공통 후처리(집계 게시 + 오늘이면 알림 재예약) =====
async def _after_override_commit(dt: date):
    # 오늘이면 알림(-10, 75, 85) 재예약
    if dt == datetime.now(KST).date():
        try:
            await refresh_student_id_map()
            await schedule_all_offsets_for_today()
        except Exception as e:
            print(f"[후처리] 알림 재예약 실패: {type(e).__name__}: {e}")
    # 상황실에 해당 날짜 최신 집계 게시
    try:
        out = (await build_timetable_message(dt) or "").strip() or "> **[수업 집계]**\n> (내용 없음)"
        ch = await _get_text_channel_cached(SITUATION_ROOM_CHANNEL_ID)
        if ch:
            await ch.send(out)
    except Exception as e:
        print(f"[후처리] 집계 게시 실패: {type(e).__name__}: {e}")

# ===== Slash Commands =====
# ⚠️ 이미 위에서 normalize_base_name / _category_belongs_to_member /
#     _make_unique_nickname / _make_unique_category_name 가 정의되어 있다면
#     여기서는 다시 정의하지 마세요.

async def _ensure_student_space_with_name(guild: discord.Guild, student: discord.Member, final_name: str):
    # 이름 정규화(중복 숫자 꼬리 제거 등) + 비어있음 대비
    final_name = (normalize_base_name(final_name) or "학생").strip()
    base_category_name = f"{final_name}{CATEGORY_SUFFIX}"

    # 같은 이름의 카테고리가 이미 있고, 그 카테고리가 학생에게 실제로 보이는 채널이 아니라면 → 유니크 이름 부여
    existing = discord.utils.get(guild.categories, name=base_category_name)
    if existing and not _category_belongs_to_member(existing, student):
        category_name = _make_unique_category_name(guild, base_category_name)
    else:
        category_name = base_category_name

    # 카테고리 생성/획득
    category = discord.utils.get(guild.categories, name=category_name)
    if category is None:
        overwrites = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            student: discord.PermissionOverwrite(view_channel=True, send_messages=True, connect=True, speak=True),
        }
        teacher = guild.get_member(TEACHER_MAIN_ID) if TEACHER_MAIN_ID else None
        if teacher:
            overwrites[teacher] = discord.PermissionOverwrite(view_channel=True, send_messages=True, connect=True, speak=True)

        category = await guild.create_category(category_name, overwrites=overwrites)

    # 텍스트/보이스 채널 보장
    text = discord.utils.get(category.text_channels, name=TEXT_NAME)
    if text is None:
        text = await guild.create_text_channel(TEXT_NAME, category=category)

    voice = discord.utils.get(category.voice_channels, name=VOICE_NAME)
    if voice is None:
        voice = await guild.create_voice_channel(VOICE_NAME, category=category)

    # 🔐 텍스트 채널 topic에 SID:<id> 메타 기록(중복 방지)
    try:
        topic = (text.topic or "").strip()
        sid_tag = f"SID:{student.id}"
        if sid_tag not in topic:
            new_topic = f"{topic}  |  {sid_tag}" if topic else sid_tag
            await text.edit(topic=new_topic)
    except Exception:
        # 토픽 편집 권한 없으면 조용히 무시
        pass

    return category, text, voice

# 슬래시 권한: 관리자가 UI에서 바로 요구사항을 볼 수 있게
@bot.tree.command(name="출석", description="오늘자 출석을 기록합니다.")
@app_commands.guild_only()
async def slash_attend(inter: discord.Interaction):
    # 공개로 남기는 응답
    await inter.response.defer(ephemeral=False, thinking=True)

    # 1) 학생 카테고리 채널에서만 허용
    category = getattr(inter.channel, "category", None)
    if not category or not isinstance(category, discord.CategoryChannel) or not category.name.endswith(CATEGORY_SUFFIX):
        await inter.followup.send(f"이 명령은 `{CATEGORY_SUFFIX}`가 붙은 학생 채널에서만 사용할 수 있어요.", ephemeral=False)
        return

    uid = inter.user.id
    today_iso = datetime.now(KST).date().isoformat()

    try:
        async with _attendance_lock:
            arr = attendance.get(today_iso, [])
            if uid in arr:
                await inter.followup.send(f"{inter.user.mention} 이미 출석으로 기록되어 있습니다. ✅", ephemeral=False)
                return
            arr.append(uid)
            attendance[today_iso] = arr
            # 빠른 동기 저장(기존 방식과 동일)
            save_json_atomic(ATTENDANCE_FILE, attendance)

        await inter.followup.send(f"{inter.user.mention} ✅ 출석 완료! (기록됨)", ephemeral=False)

        # (선택) 상황실 로그 남기고 싶다면 주석 해제
        try:
            room = await _get_text_channel_cached(SITUATION_ROOM_CHANNEL_ID)
            if room:
                await room.send(f"[출석] {inter.user.mention} — {today_iso}")
        except Exception:
            pass

    except Exception as e:
        print(f"[/출석 오류] {type(e).__name__}: {e}")
        await inter.followup.send("출석 기록 중 문제가 발생했어요. 잠시 후 다시 시도해주세요.", ephemeral=False)

@bot.tree.command(name="선생님", description="선생님을 호출합니다. (상황실 로그 전송)")
@app_commands.describe(message="선생님께 전달할 간단한 내용 (선택)")
@app_commands.guild_only()
async def slash_call_teacher(inter: discord.Interaction, message: Optional[str] = None):
    await inter.response.defer(ephemeral=False, thinking=True)

    # 1) 학생 카테고리 채널에서만 허용
    category = getattr(inter.channel, "category", None)
    if not category or not isinstance(category, discord.CategoryChannel) or not category.name.endswith(CATEGORY_SUFFIX):
        await inter.followup.send(f"이 명령은 `{CATEGORY_SUFFIX}`가 붙은 학생 채널에서만 사용할 수 있어요.", ephemeral=False)
        return

    # 2) 사용자 쿨다운 60초 (기존 last_question_at 재사용)
    uid = inter.user.id
    now_monotonic = asyncio.get_event_loop().time()
    last_ts = last_question_at.get(uid, 0.0)
    if now_monotonic - last_ts < 60:
        await inter.followup.send("조금 전에도 호출이 있었어요. 1분 후에 다시 시도해주세요 🙏", ephemeral=False)
        return
    last_question_at[uid] = now_monotonic

    # 3) 상황실 로그
    room = await _get_text_channel_cached(SITUATION_ROOM_CHANNEL_ID)
    teacher_mention = f"<@{TEACHER_MAIN_ID}>" if TEACHER_MAIN_ID else "(선생님)"
    try:
        if room:
            student_label = category.name[:-len(CATEGORY_SUFFIX)] if category.name.endswith(CATEGORY_SUFFIX) else "학생"
            msg = f"{teacher_mention} {inter.user.mention} — **{student_label}** 채널에서 선생님 호출"
            if (message or "").strip():
                msg += f" : {(message or '').strip()}"
            await room.send(msg)
        else:
            await inter.followup.send("상황실 채널을 찾지 못했어요. 관리자에게 알려주세요.", ephemeral=False)
            return
    except Exception:
        await inter.followup.send("호출 접수가 실패했어요. 잠시 후 다시 시도하거나 관리자에게 알려주세요.", ephemeral=False)
        return

    # 4) 채널 피드백(공개)
    await inter.followup.send("호출 접수 완료! 곧 선생님이 도와드릴게요. 🙌", ephemeral=False)

@bot.tree.command(name="숙제", description="숙제 제출을 기록합니다. (예: /숙제, /숙제 오늘, /숙제 내일, /숙제 11-04, /숙제 2025-11-04)")
@app_commands.describe(when="미입력: 가장 가까운 수업 / '오늘' / '내일' / YYYY-MM-DD / MM-DD(연도 생략 가능)")
@app_commands.guild_only()
async def slash_hw_submit(inter: discord.Interaction, when: Optional[str] = None):
    await inter.response.defer(ephemeral=False, thinking=True)

    # 1) 학생 카테고리 채널에서만 허용
    category = getattr(inter.channel, "category", None)
    if not category or not isinstance(category, discord.CategoryChannel) or not category.name.endswith(CATEGORY_SUFFIX):
        await inter.followup.send(f"이 명령은 `{CATEGORY_SUFFIX}`가 붙은 학생 채널에서만 사용할 수 있어요.", ephemeral=False)
        return

    uid = inter.user.id
    now = datetime.now(KST)
    today = now.date()
    target = (when or "").strip()
    desired_day: Optional[date] = None

    try:
        # 2) 목표 날짜 해석
        if not target:
            # 가장 가까운 수업(오늘 남은 수업 있으면 오늘, 아니면 이후 첫 수업)
            desired_day = await next_student_session_date(student_id=uid)

        elif target in ("오늘", "today"):
            desired_day = today

        elif target in ("내일", "다음", "tomorrow"):
            # 오늘은 건너뛰고 이후 첫 수업
            for i in range(1, 31 + 1):
                d = today + timedelta(days=i)
                sessions = await effective_sessions_for(d)
                if any(isinstance(sid, int) and sid == uid for _, _, sid in sessions):
                    desired_day = d
                    break

        else:
            # 공통 파서 있으면 사용 권장: desired_day = _parse_day_input(target)
            # 여기서는 직접 연도 생략 보정 포함
            if re.fullmatch(r"\d{1,2}-\d{1,2}", target):
                y = datetime.now(KST).year
                mm, dd = target.split("-")
                target = f"{y}-{mm.zfill(2)}-{dd.zfill(2)}"

            try:
                cand = date.fromisoformat(target)
            except Exception:
                await inter.followup.send("날짜 형식이 올바르지 않아요. YYYY-MM-DD / MM-DD(연도 생략) / '내일'을 사용해 주세요.", ephemeral=False)
                return

            sessions = await effective_sessions_for(cand)
            if any(isinstance(sid, int) and sid == uid for _, _, sid in sessions):
                desired_day = cand
            else:
                await inter.followup.send(f"{cand.isoformat()}에는 수업이 없는 것 같아요 🧐\n혹시 일정이 변경되었다면 선생님에게 문의해주세요!", ephemeral=False)
                return

        if not desired_day:
            await inter.followup.send("앞으로 예정된 수업 날짜를 찾지 못했어요. 🧐\n혹시 일정이 변경되었다면 선생님에게 문의해주세요!", ephemeral=False)
            return

        day_iso = desired_day.isoformat()

        # 3) 해당 날짜 세션의 실제 sid도 함께 저장 → 리마인더와 정확히 연동
        sessions = await effective_sessions_for(desired_day)
        candidate_sids = {sid for _, _, sid in sessions if isinstance(sid, int)}

        async with _homework_lock:
            arr = set(homework.get(day_iso, []))
            arr.add(uid)
            arr |= candidate_sids
            homework[day_iso] = sorted(arr)
            save_json_atomic(HOMEWORK_FILE, homework)

        # 4) 채널 피드백(공개)
        await inter.followup.send(
            f"{inter.user.mention}\n**{day_iso}까지 제출할 숙제**가 제출되었습니다. 🎉\n"
            f"숙제 제출일이 다르다면 `/숙제 MM-DD`을 사용하거나 선생님에게 알려주세요 😊",
            ephemeral=False
        )

        # 5) 상황실 로그
        try:
            room = await _get_text_channel_cached(SITUATION_ROOM_CHANNEL_ID)
            if room:
                await room.send(f"[숙제 제출] {inter.user.mention} — 대상일: {day_iso}")
        except Exception:
            pass

    except Exception as e:
        print(f"[/숙제 오류] {type(e).__name__}: {e}")
        await inter.followup.send("숙제 제출 처리 중 문제가 발생했어요. 잠시 후 다시 시도해주세요.", ephemeral=False)

@app_commands.default_permissions(manage_channels=True)
@bot.tree.command(
    name="신규",
    description="학생 닉네임을 '이름-끝4자리'로 맞추고, 학생 전용 카테고리/채팅/음성 채널을 만듭니다."
)
@app_commands.describe(student="학생 유저(멘션/선택)", realname="학생의 본명(선택)")
@app_commands.checks.has_permissions(manage_channels=True)
async def slash_new(
    inter: discord.Interaction,
    student: discord.Member,
    realname: Optional[str] = None
):
    await inter.response.defer(ephemeral=True)

    guild = inter.guild
    if guild is None:
        await inter.followup.send("❌ 서버 내에서만 사용할 수 있어요.", ephemeral=True)
        return

    # 봇 권한(선제 체크)
    me = guild.me
    if not me:
        await inter.followup.send("❌ 봇 정보를 확인할 수 없어요.", ephemeral=True)
        return

    bot_perms = me.guild_permissions
    if not (bot_perms.manage_channels and bot_perms.view_channel):
        await inter.followup.send("❌ 채널 생성/편집 권한이 부족합니다. (Manage Channels, View Channel)", ephemeral=True)
        return

    base_raw = (realname or student.name or "").strip()
    if not base_raw:
        await inter.followup.send("❌ 본명을 확인할 수 없습니다. `realname`를 입력해주세요.", ephemeral=True)
        return

    base = normalize_base_name(base_raw)
    preferred = f"{base}-{str(student.id)[-4:]}"
    final_nick = _make_unique_nickname(guild, preferred)

    # 닉네임 변경(권한/역할 높이 문제는 개별 안내 후 계속 진행)
    nick_ok = True
    if (student.nick or "") != final_nick:
        if not bot_perms.manage_nicknames:
            nick_ok = False
        else:
            try:
                # 역할 높이 이슈 방지: 봇 역할이 학생보다 위인지 간단 체크
                if me.top_role <= student.top_role:
                    nick_ok = False
                else:
                    await student.edit(nick=final_nick, reason="/신규: 본명/끝4 접미 적용")
            except discord.Forbidden:
                nick_ok = False
            except discord.HTTPException:
                nick_ok = False

    # 카테고리/채널 보장
    try:
        category, text, voice = await _ensure_student_space_with_name(guild, student, final_nick)
    except Exception as e:
        await inter.followup.send(f"❌ 채널 생성 실패: {type(e).__name__}: {e}", ephemeral=True)
        return

    # (선택) 학생 ID 맵 갱신
    try:
        await refresh_student_id_map()
    except Exception:
        pass

    # 결과 요약
    parts = [f"✅ `{category.name}` 구성 완료 (텍스트:`{text.name}`, 음성:`{voice.name}`)"]
    if nick_ok:
        parts.append(f"닉네임: `{final_nick}`")
    else:
        parts.append("⚠️ 닉네임 변경 실패(권한/역할 순서 문제). 봇 역할을 학생보다 위로 올려주세요.")

    await inter.followup.send("\n".join(parts), ephemeral=True)

# ===== Overrides helpers • extended =====

def _ensure_day_bucket(day_iso: str) -> dict:
    """overrides[day_iso] 딕셔너리를 보장하여 반환."""
    bucket = overrides.get(day_iso)
    if not isinstance(bucket, dict):
        bucket = {}
        overrides[day_iso] = bucket
    return bucket

def _migrate_legacy_key_if_any(ovs_day: dict, student_name: Optional[str], student_id: Optional[int]) -> None:
    """
    같은 학생이 name 키로 저장된 엔트리가 있고, 이번에 id를 알게 되면
    ID 키로 옮기고 name 키는 정리.
    """
    if not (isinstance(student_id, int) and student_name):
        return
    legacy = ovs_day.get(student_name)
    if isinstance(legacy, dict):
        ovs_day[str(student_id)] = legacy
        try:
            del ovs_day[student_name]
        except Exception:
            pass

def _ov_delete(ovs_day: dict, student_name: Optional[str], student_id: Optional[int]) -> bool:
    """학생의 override 엔트리 전체를 삭제. 삭제되면 True."""
    removed = False
    if isinstance(student_id, int) and str(student_id) in ovs_day:
        try:
            del ovs_day[str(student_id)]
            removed = True
        except Exception:
            pass
    if student_name and student_name in ovs_day:
        try:
            del ovs_day[student_name]
            removed = True or removed
        except Exception:
            pass
    return removed

def _ov_get_or_create(ovs_day: dict, student_name: Optional[str], student_id: Optional[int]) -> dict:
    """
    학생의 override 엔트리를 반환. 없으면 새 dict을 생성해서 연결.
    ID가 있으면 ID 키를 우선 사용하고, 레거시 name 엔트리가 있으면 migrate.
    """
    entry = _ov_get(ovs_day, student_name, student_id)
    if isinstance(entry, dict):
        # ID를 알게 됐고 name 키가 있었다면 옮김
        _migrate_legacy_key_if_any(ovs_day, student_name, student_id)
        return entry

    entry = {"cancel": False, "change": None, "changes": [], "makeup": []}
    if isinstance(student_id, int):
        ovs_day[str(student_id)] = entry
        if student_name and student_name in ovs_day:
            try: del ovs_day[student_name]
            except Exception: pass
    elif student_name:
        ovs_day[student_name] = entry
    else:
        # 안전장치: 식별자가 전혀 없는 경우(거의 없음)
        raise ValueError("student_name 또는 student_id 중 하나는 필요합니다.")
    return entry

def _normalize_time_token(tok: Any) -> Optional[dtime]:
    """시간 문자열/토큰을 dtime으로 변환. 실패 시 None."""
    if tok is None:
        return None
    s = str(tok).strip()
    t = parse_time_str(s)
    return t

def _append_unique_time_list(lst: list, t: dtime) -> None:
    """중복 없이 시간 추가."""
    if t not in lst:
        lst.append(t)

def _format_time(t: dtime) -> str:
    return t.strftime("%H:%M")

# ==== 명령 편의 API (슬래시 커맨드에서 바로 사용) ====

def ov_set_cancel(ovs_day: dict, student_name: Optional[str], student_id: Optional[int], flag: bool) -> dict:
    entry = _ov_get_or_create(ovs_day, student_name, student_id)
    entry["cancel"] = bool(flag)
    return entry

def ov_set_change_single(ovs_day: dict, student_name: Optional[str], student_id: Optional[int], new_time: Any) -> dict:
    """
    단일 변경: change 필드에 최종 1개 시간만 남김.
    기존 changes(복수)와 충돌 시 '단일 변경 우선' 정책으로 changes는 비움.
    """
    t = _normalize_time_token(new_time)
    if not t:
        raise ValueError("유효한 시간 형식이 아닙니다.")
    entry = _ov_get_or_create(ovs_day, student_name, student_id)
    entry["change"] = _format_time(t)
    entry["changes"] = []
    entry["cancel"] = False
    return entry

def ov_add_change_pair(ovs_day: dict, student_name: Optional[str], student_id: Optional[int], src_time: Any, dst_time: Any) -> dict:
    """
    복수 변경 용: changes 리스트에 {from,to} 추가.
    단일 change가 이미 있으면 우선순위를 명확히 하기 위해 change는 해제.
    """
    t_from = _normalize_time_token(src_time)
    t_to   = _normalize_time_token(dst_time)
    if not (t_from and t_to):
        raise ValueError("변경 시간 형식이 올바르지 않습니다.")
    entry = _ov_get_or_create(ovs_day, student_name, student_id)
    entry["change"] = None
    changes = entry.get("changes") or []
    # 중복 방지
    key = (_format_time(t_from), _format_time(t_to))
    if not any((c.get("from"), c.get("to")) == key for c in changes):
        changes.append({"from": key[0], "to": key[1]})
    entry["changes"] = changes
    entry["cancel"] = False
    return entry

def ov_clear_changes(ovs_day: dict, student_name: Optional[str], student_id: Optional[int]) -> dict:
    entry = _ov_get_or_create(ovs_day, student_name, student_id)
    entry["change"] = None
    entry["changes"] = []
    return entry

def ov_add_makeup(ovs_day: dict, student_name: Optional[str], student_id: Optional[int], extra_time: Any) -> dict:
    t = _normalize_time_token(extra_time)
    if not t:
        raise ValueError("보강 시간 형식이 올바르지 않습니다.")
    entry = _ov_get_or_create(ovs_day, student_name, student_id)
    makeup = entry.get("makeup") or []
    hhmm = _format_time(t)
    if hhmm not in makeup:
        makeup.append(hhmm)
    entry["makeup"] = makeup
    # 보강은 서비스 기간과 무관히 추가되며 cancel과 독립.
    return entry

def ov_remove_makeup(ovs_day: dict, student_name: Optional[str], student_id: Optional[int], extra_time: Any) -> dict:
    t = _normalize_time_token(extra_time)
    if not t:
        raise ValueError("보강 시간 형식이 올바르지 않습니다.")
    entry = _ov_get_or_create(ovs_day, student_name, student_id)
    hhmm = _format_time(t)
    makeup = [m for m in (entry.get("makeup") or []) if m != hhmm]
    entry["makeup"] = makeup
    return entry

def _ensure_entry_defaults(entry: Optional[dict]) -> dict:
    """overrides 엔트리 기본 필드 보장."""
    e = entry or {}
    if "cancel" not in e: e["cancel"] = False
    if "change" not in e: e["change"] = None
    if "changes" not in e or not isinstance(e.get("changes"), list): e["changes"] = []
    if "makeup" not in e or not isinstance(e.get("makeup"), list): e["makeup"] = []
    return e

def _cleanup_entry_if_empty(ovs: dict, sid: int, entry: dict):
    """엔트리가 완전히 비면 overrides에서 제거."""
    if (not entry.get("cancel")) and (entry.get("change") is None) and (not entry.get("changes")) and (not entry.get("makeup")):
        try:
            del ovs[str(sid)]
        except Exception:
            pass

# ---- Slash 공통: 날짜 파서 ----
def _parse_day_input(when: str) -> Optional[date]:
    """
    지원 형식:
      - '오늘', 'today'  -> 오늘
      - '내일', 'tomorrow' -> 내일
      - 'YYYY-MM-DD'     -> 해당 날짜
      - 'MM-DD'          -> (올해)-MM-DD  로 자동 보정
    유효하지 않으면 None 반환
    """
    if when is None:
        return None
    s = when.strip()

    # 오늘/내일 별칭
    if s in ("오늘", "today"):
        return datetime.now(KST).date()
    if s in ("내일", "tomorrow"):
        return datetime.now(KST).date() + timedelta(days=1)

    # YYYY-MM-DD
    if re.fullmatch(r"\d{4}-\d{1,2}-\d{1,2}", s):
        try:
            return date.fromisoformat(s)
        except Exception:
            return None

    # MM-DD  (연도 생략 시 올해로 자동 보정)
    if re.fullmatch(r"\d{1,2}-\d{1,2}", s):
        y = datetime.now(KST).year
        mm, dd = s.split("-")
        mm = mm.zfill(2)
        dd = dd.zfill(2)
        try:
            return date.fromisoformat(f"{y}-{mm}-{dd}")
        except Exception:
            return None

    return None


# ===== Slash: 변경/보강/휴강 (리팩터링 버전) =====

# 공통 전제:
# - _parse_day_input(when: str) -> Optional[date]
# - _ensure_day_bucket(day_iso: str) -> dict
# - _after_override_commit(dt: date) -> posts summary + reschedules if today
# - ov_* 헬퍼 (ov_set_cancel, ov_add_change_pair, ov_clear_changes, ov_add_makeup, ov_remove_makeup)

# ---------- 변경 ----------
@bot.tree.command(name="변경", description="해당 날짜의 기본 수업 시각 A를 B로 변경합니다. (예: 17:30 -> 20:30)")
@app_commands.describe(
    student="학생",
    when="YYYY-MM-DD 또는 '오늘'",
    from_time="기존 시각 HH:MM",
    to_time="변경 시각 HH:MM",
)
@app_commands.default_permissions(manage_channels=True)
async def slash_change(inter: discord.Interaction, student: discord.Member, when: str, from_time: str, to_time: str):
    await inter.response.defer(ephemeral=True, thinking=True)

    dt = _parse_day_input(when)
    if not dt:
        await inter.followup.send("❌ 날짜 형식은 YYYY-MM-DD 또는 '오늘' 입니다.", ephemeral=True); return
    if not parse_time_str(from_time) or not parse_time_str(to_time):
        await inter.followup.send("❌ 시각은 HH:MM 형식이어야 합니다.", ephemeral=True); return

    day_iso = dt.isoformat()
    try:
        async with _overrides_lock:
            ovs_day = _ensure_day_bucket(day_iso)
            # 단일 change는 쓰지 않고, 복수 changes에 (A->B)만 기록
            ov_clear_changes(ovs_day, student.display_name, student.id)
            ov_add_change_pair(ovs_day, student.display_name, student.id, from_time, to_time)
            ov_set_cancel(ovs_day, student.display_name, student.id, False)  # 변경 들어오면 휴강 해제
            overrides[day_iso] = ovs_day
        await save_overrides()
    except Exception as e:
        await inter.followup.send(f"❌ 변경 저장 실패: {type(e).__name__}: {e}", ephemeral=True); return

    await _after_override_commit(dt)
    await inter.followup.send("✅ 변경 반영, 최신 집계를 상황실에 게시했습니다.", ephemeral=True)

# ---------- 변경삭제 (모든 변경 제거) ----------
@bot.tree.command(name="변경삭제", description="해당 날짜의 모든 '변경'(A->B) 기록을 제거합니다.")
@app_commands.describe(
    student="학생",
    when="YYYY-MM-DD 또는 '오늘'",
)
@app_commands.default_permissions(manage_channels=True)
async def slash_change_clear(inter: discord.Interaction, student: discord.Member, when: str):
    await inter.response.defer(ephemeral=True, thinking=True)

    dt = _parse_day_input(when)
    if not dt:
        await inter.followup.send("❌ 날짜 형식은 YYYY-MM-DD 또는 '오늘' 입니다.", ephemeral=True); return

    day_iso = dt.isoformat()
    try:
        async with _overrides_lock:
            ovs_day = _ensure_day_bucket(day_iso)
            # 변경(단일/복수) 전부 초기화
            entry = ov_clear_changes(ovs_day, student.display_name, student.id)
            # 엔트리가 비면 정리(선택)
            if not entry.get("cancel") and not entry.get("makeup"):
                _ov_delete(ovs_day, student.display_name, student.id)
            overrides[day_iso] = ovs_day
        await save_overrides()
    except Exception as e:
        await inter.followup.send(f"❌ 변경 삭제 실패: {type(e).__name__}: {e}", ephemeral=True); return

    await _after_override_commit(dt)
    await inter.followup.send("✅ 변경 기록을 모두 삭제했습니다. 최신 집계를 상황실에 게시했습니다.", ephemeral=True)

# ---------- 보강 (추가) ----------
@bot.tree.command(name="보강", description="해당 날짜에 보강 시각을 추가합니다. (예: 18:15)")
@app_commands.describe(
    student="학생",
    when="YYYY-MM-DD 또는 '오늘'",
    time="보강 시각 HH:MM",
)
@app_commands.default_permissions(manage_channels=True)
async def slash_makeup(inter: discord.Interaction, student: discord.Member, when: str, time: str):
    await inter.response.defer(ephemeral=True, thinking=True)

    dt = _parse_day_input(when)
    if not dt:
        await inter.followup.send("❌ 날짜 형식은 YYYY-MM-DD 또는 '오늘' 입니다.", ephemeral=True); return
    if not parse_time_str(time):
        await inter.followup.send("❌ 시각은 HH:MM 형식이어야 합니다.", ephemeral=True); return

    day_iso = dt.isoformat()
    try:
        async with _overrides_lock:
            ovs_day = _ensure_day_bucket(day_iso)
            ov_add_makeup(ovs_day, student.display_name, student.id, time)
            overrides[day_iso] = ovs_day
        await save_overrides()
    except Exception as e:
        await inter.followup.send(f"❌ 보강 추가 실패: {type(e).__name__}: {e}", ephemeral=True); return

    # === 여기부터: 일회성 리마인더 분기 (a/b/c) ===
    try:
        now = datetime.now(KST)
        today = now.date()
        tomorrow = today + timedelta(days=1)
        sid = student.id

        # 이미 그 날짜 숙제를 제출했다면 리마인더 불필요
        if sid not in set(homework.get(day_iso, [])):
            if dt == tomorrow and now.hour >= 22:
                # a) 보강이 내일이고 현재 시간이 22시 이후 → 내일 09:00에 1회 리마인더 예약
                fire_at = datetime.combine(dt, dtime(9, 0), KST)
                await _schedule_oneoff_homework_reminder(sid, dt, fire_at, reason="내일·22시 이후")
            elif dt == today and now.hour < 9:
                # b) 보강일이 오늘이고 현재 시간이 09시 이전 → 오늘 09:00에 1회 리마인더 예약
                fire_at = datetime.combine(dt, dtime(9, 0), KST)
                await _schedule_oneoff_homework_reminder(sid, dt, fire_at, reason="오늘·09시 이전")
            elif dt == today and now.hour >= 9:
                # c) 보강일이 오늘이고 현재 시간이 09시 이후 → 즉시 1회 리마인더
                await _schedule_oneoff_homework_reminder(sid, dt, now, reason="오늘·즉시")
            # 이틀 뒤 이상은 정규 18/22 스케줄러가 처리

    except Exception as e:
        print(f"[보강 즉시 리마인더 분기 오류] {type(e).__name__}: {e}")

    await _after_override_commit(dt)
    await inter.followup.send("✅ 보강 추가, 최신 집계를 상황실에 게시했습니다.", ephemeral=True)


# ---------- 보강변경 (A를 B로 바꾸기) ----------
@bot.tree.command(name="보강변경", description="해당 날짜의 보강 시각 A를 B로 변경합니다. (예: 17:30 -> 18:00)")
@app_commands.describe(
    student="학생",
    when="YYYY-MM-DD 또는 '오늘'",
    from_time="기존 보강 HH:MM",
    to_time="변경 보강 HH:MM",
)
@app_commands.default_permissions(manage_channels=True)
async def slash_makeup_change(inter: discord.Interaction, student: discord.Member, when: str, from_time: str, to_time: str):
    await inter.response.defer(ephemeral=True, thinking=True)

    dt = _parse_day_input(when)
    if not dt:
        await inter.followup.send("❌ 날짜 형식은 YYYY-MM-DD 또는 '오늘' 입니다.", ephemeral=True); return
    if not parse_time_str(from_time) or not parse_time_str(to_time):
        await inter.followup.send("❌ 시각은 HH:MM 형식이어야 합니다.", ephemeral=True); return

    day_iso = dt.isoformat()
    try:
        async with _overrides_lock:
            ovs_day = _ensure_day_bucket(day_iso)
            # A 제거 → B 추가
            ov_remove_makeup(ovs_day, student.display_name, student.id, from_time)
            ov_add_makeup(ovs_day, student.display_name, student.id, to_time)
            overrides[day_iso] = ovs_day
        await save_overrides()
    except Exception as e:
        await inter.followup.send(f"❌ 보강 변경 실패: {type(e).__name__}: {e}", ephemeral=True); return

    await _after_override_commit(dt)
    await inter.followup.send("✅ 보강 변경, 최신 집계를 상황실에 게시했습니다.", ephemeral=True)

# ---------- 보강삭제 (모든 보강 제거) ----------
@bot.tree.command(name="보강삭제", description="해당 날짜의 모든 보강 시각을 삭제합니다.")
@app_commands.describe(
    student="학생",
    when="YYYY-MM-DD 또는 '오늘'",
)
@app_commands.default_permissions(manage_channels=True)
async def slash_makeup_remove_all(inter: discord.Interaction, student: discord.Member, when: str):
    await inter.response.defer(ephemeral=True, thinking=True)

    dt = _parse_day_input(when)
    if not dt:
        await inter.followup.send("❌ 날짜 형식은 YYYY-MM-DD 또는 '오늘' 입니다.", ephemeral=True); return

    day_iso = dt.isoformat()
    try:
        async with _overrides_lock:
            ovs_day = _ensure_day_bucket(day_iso)
            entry = _ov_get_or_create(ovs_day, student.display_name, student.id)
            if not entry.get("makeup"):
                await inter.followup.send("ℹ️ 해당 날짜에 등록된 보강이 없습니다.", ephemeral=True); return
            entry["makeup"] = []
            # 엔트리가 비면 정리
            if not entry.get("cancel") and entry.get("change") is None and not entry.get("changes"):
                _ov_delete(ovs_day, student.display_name, student.id)
            overrides[day_iso] = ovs_day
        await save_overrides()
    except Exception as e:
        await inter.followup.send(f"❌ 보강 삭제 실패: {type(e).__name__}: {e}", ephemeral=True); return

    await _after_override_commit(dt)
    await inter.followup.send("✅ 보강 삭제, 최신 집계를 상황실에 게시했습니다.", ephemeral=True)

# ---------- 휴강 ----------
@bot.tree.command(name="휴강", description="해당 날짜를 휴강으로 처리합니다.")
@app_commands.describe(
    student="학생",
    when="YYYY-MM-DD 또는 '오늘'",
)
@app_commands.default_permissions(manage_channels=True)
async def slash_cancel(inter: discord.Interaction, student: discord.Member, when: str):
    await inter.response.defer(ephemeral=True, thinking=True)

    dt = _parse_day_input(when)
    if not dt:
        await inter.followup.send("❌ 날짜 형식은 YYYY-MM-DD 또는 '오늘' 입니다.", ephemeral=True); return

    day_iso = dt.isoformat()
    try:
        async with _overrides_lock:
            ovs_day = _ensure_day_bucket(day_iso)
            ov_set_cancel(ovs_day, student.display_name, student.id, True)
            # 휴강 시 변경/보강은 의미 없으므로 지우진 않아도 되지만, 깔끔하게 유지하려면 다음 두 줄 주석 해제 가능:
            # ov_clear_changes(ovs_day, student.display_name, student.id)
            # entry = _ov_get_or_create(ovs_day, student.display_name, student.id); entry["makeup"] = []
            overrides[day_iso] = ovs_day
        await save_overrides()
    except Exception as e:
        await inter.followup.send(f"❌ 휴강 처리 실패: {type(e).__name__}: {e}", ephemeral=True); return

    await _after_override_commit(dt)
    await inter.followup.send("✅ 휴강 처리, 최신 집계를 상황실에 게시했습니다.", ephemeral=True)

# ---------- 휴강삭제 ----------
@bot.tree.command(name="휴강삭제", description="해당 날짜의 휴강 상태를 해제합니다.")
@app_commands.describe(
    student="학생",
    when="YYYY-MM-DD 또는 '오늘'",
)
@app_commands.default_permissions(manage_channels=True)
async def slash_cancel_remove(inter: discord.Interaction, student: discord.Member, when: str):
    await inter.response.defer(ephemeral=True, thinking=True)

    dt = _parse_day_input(when)
    if not dt:
        await inter.followup.send("❌ 날짜 형식은 YYYY-MM-DD 또는 '오늘' 입니다.", ephemeral=True); return

    day_iso = dt.isoformat()
    try:
        async with _overrides_lock:
            ovs_day = _ensure_day_bucket(day_iso)
            entry = ov_set_cancel(ovs_day, student.display_name, student.id, False)
            # 엔트리가 비면 정리
            if entry.get("change") is None and not entry.get("changes") and not entry.get("makeup"):
                _ov_delete(ovs_day, student.display_name, student.id)
            overrides[day_iso] = ovs_day
        await save_overrides()
    except Exception as e:
        await inter.followup.send(f"❌ 휴강 해제 실패: {type(e).__name__}: {e}", ephemeral=True); return

    await _after_override_commit(dt)
    await inter.followup.send("✅ 휴강 해제, 최신 집계를 상황실에 게시했습니다.", ephemeral=True)

@bot.tree.command(name="예정", description="앞으로 N일 동안의 휴강/변경/보강 예약을 요약합니다(기본 30일).")
@app_commands.describe(student="학생", days="조회 일수 (기본 30)")
async def slash_upcoming(inter: discord.Interaction, student: discord.Member, days: int = 30):
    await inter.response.defer(ephemeral=True, thinking=True)

    if days <= 0:
        await inter.followup.send("❌ 조회 일수(days)는 1 이상의 정수여야 합니다.", ephemeral=True)
        return

    today = datetime.now(KST).date()
    end   = today + timedelta(days=days)
    sid   = student.id
    name  = student.display_name

    items = []
    # overrides: { "YYYY-MM-DD": { "<sid str>" | "<legacy name>": {cancel, change, changes, makeup}, ... } }
    for day_str, per_student in overrides.items():
        try:
            d = date.fromisoformat(day_str)
        except Exception:
            continue
        if d < today or d > end:
            continue

        # ✅ ID-우선으로 안전하게 가져오기 (표시명 불일치에도 견고)
        entry = _ov_get(per_student, name, sid)
        if not entry:
            continue

        day_lines = []

        # ⛔ 휴강
        if entry.get("cancel"):
            day_lines.append("⛔ 휴강")

        # 🔄 변경(단일, 레거시)
        ch = entry.get("change")
        if ch is not None:
            t_ch = parse_time_str(str(ch))
            if t_ch:
                day_lines.append(f"🔄 변경 → {t_ch.strftime('%H:%M')}")

        # 🔄 변경(복수)
        chg = entry.get("changes")
        if isinstance(chg, list) and chg:
            parts = []
            for item in chg:
                t_from = parse_time_str(str(item.get("from")))
                t_to   = parse_time_str(str(item.get("to")))
                if t_from and t_to:
                    parts.append(f"{t_from.strftime('%H:%M')}→{t_to.strftime('%H:%M')}")
            if parts:
                day_lines.append("🔄 변경(복수) " + ", ".join(parts))

        # 📌 보강
        adds = entry.get("makeup") or []
        adds_times = []
        for a in adds:
            t = parse_time_str(str(a))
            if t:
                adds_times.append(t.strftime('%H:%M'))
        if adds_times:
            # 중복 제거 + 정렬
            day_lines.append("📌 보강: " + ", ".join(sorted(set(adds_times))))

        if day_lines:
            items.append((d, day_lines))

    # 날짜 순 정렬
    items.sort(key=lambda x: x[0])

    header = f"**[예정] {name} — {today.isoformat()} ~ {end.isoformat()}**"
    if not items:
        out = "\n".join([header, "", "예정된 휴강/변경/보강이 없습니다."])
        out = "\n".join(["> " + line for line in out.splitlines()])
        await inter.followup.send(out, ephemeral=True)
        return

    lines = [header, ""]
    for d, day_lines in items:
        lines.append(f"**• {d.isoformat()}**")
        for L in day_lines:
            lines.append(f"- {L}")
        lines.append("")

    out = "\n".join(lines)
    out = "\n".join(["> " + line for line in out.splitlines()])
    await inter.followup.send(out, ephemeral=True)

@bot.tree.command(name="시간표", description="[수업 집계] 출력")
@app_commands.describe(when="오늘/내일 또는 YYYY-MM-DD / MM-DD (미입력시 오늘)")
async def slash_timetable(inter: discord.Interaction, when: str = "오늘"):
    await inter.response.defer(ephemeral=True, thinking=True)

    day = _parse_day_input(when or "오늘")
    if not day:
        await inter.followup.send("날짜 형식은 오늘/내일 또는 YYYY-MM-DD, MM-DD 입니다.", ephemeral=True)
        return

    try:
        out = await build_timetable_message(day)
    except Exception as e:
        await inter.followup.send(f"❌ 시간표 로드 실패: {type(e).__name__}: {e}", ephemeral=True)
        return

    # 상황실에도 게시(가능하면)
    try:
        room = await _get_text_channel_cached(SITUATION_ROOM_CHANNEL_ID)
        if isinstance(room, discord.TextChannel):
            await room.send(out)
    except Exception:
        pass

    await inter.followup.send(f"✅ {day.isoformat()} 시간표 집계를 상황실에 게시했어요.", ephemeral=True)

@bot.tree.command(name="새로고침", description="시트 새로고침 + 오늘 집계 재게시 + 알림 타이머(-10, 75, 85) 재설정")
@app_commands.default_permissions(manage_channels=True)
async def slash_reload(inter: discord.Interaction):
    await inter.response.defer(ephemeral=True, thinking=True)
    msgs = []
    try:
        SHEET_CACHE._ts = 0.0  # 캐시 무효화
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

    await inter.followup.send("✅ 새로고침 수행 결과\n" + "\n".join(msgs), ephemeral=True)

# ===== Slash: 체크시트 =====
@bot.tree.command(name="체크시트", description="구글 시트 연결 점검(첫 2행 표본)")
async def slash_check_sheet(inter: discord.Interaction):
    await inter.response.defer(ephemeral=True, thinking=True)
    try:
        rows = await SHEET_CACHE.get_rows()
        total_rows = len(rows or [])
        if not rows:
            await inter.followup.send("⚠️ 시트에 데이터가 없습니다. (0행)", ephemeral=True)
            return

        # 첫 2행만 샘플로
        sample = rows[:2]

        # 보기 좋게 코드블록으로 출력 (길이 제한 방지)
        def _format_rows(rws):
            lines = []
            for r in rws:
                # 너무 긴 셀은 잘라서 표시
                cells = [ (c if len(c) <= 80 else (c[:77] + "…")) for c in r ]
                lines.append(" | ".join(cells))
            return "\n".join(lines)

        body = _format_rows(sample)
        msg = f"✅ 시트 연결 OK\n행 수: {total_rows}\n샘플(최대 2행):\n```\n{body}\n```"

        # 여전히 길 수 있으니 최종 안전 절단
        if len(msg) > 1900:
            msg = msg[:1850] + "\n…(생략)\n```"

        await inter.followup.send(msg, ephemeral=True)

    except Exception as e:
        await inter.followup.send(f"❌ 시트 연결 실패: {type(e).__name__}: {e}", ephemeral=True)

# ===== Slash: 로그 =====
@bot.tree.command(name="로그", description="해당 날짜 집계를 선생님 DM으로 전송")
@app_commands.describe(when="오늘/내일 또는 YYYY-MM-DD / MM-DD (미입력시 오늘)")
@app_commands.default_permissions(manage_channels=True)
async def slash_log(inter: discord.Interaction, when: str = "오늘"):
    await inter.response.defer(ephemeral=True, thinking=True)

    day = _parse_day_input(when or "오늘")
    if not day:
        await inter.followup.send("❌ 날짜 형식은 오늘/내일 또는 YYYY-MM-DD, MM-DD 입니다.", ephemeral=True)
        return

    if not TEACHER_MAIN_ID:
        await inter.followup.send("❌ TEACHER_MAIN_ID 가 설정되어 있지 않습니다.", ephemeral=True)
        return

    try:
        teacher = await _get_user_cached(TEACHER_MAIN_ID)
        if not teacher:
            await inter.followup.send("❌ 선생님 계정을 조회하지 못했습니다.", ephemeral=True)
            return

        out = (await build_timetable_message(day) or "").strip() or "> **[수업 집계]**\n> (내용 없음)"
        await send_long(teacher, out)

        await inter.followup.send(
            f"✅ `{day.isoformat()}` 집계를 선생님 DM으로 보냈습니다.",
            ephemeral=True
        )

    except discord.Forbidden:
        await inter.followup.send("❌ 선생님에게 DM을 보낼 권한이 없습니다. (DM 차단/서버 설정 확인)", ephemeral=True)
    except Exception as e:
        await inter.followup.send(f"❌ 전송 실패: {type(e).__name__}: {e}", ephemeral=True)

import traceback
from discord.ext import commands
from discord import app_commands

# ===== Error Hooks =====
@bot.event
async def on_command_error(ctx: commands.Context, error: commands.CommandError):
    # 흔한 케이스는 조용히/친절히 처리
    if isinstance(error, commands.CommandNotFound):
        return  # 접두어 명령 오타는 무시
    if isinstance(error, commands.MissingRequiredArgument):
        await ctx.send("❌ 필요한 인자가 빠졌어요. 사용법을 다시 확인해 주세요.")
        return
    if isinstance(error, commands.BadArgument):
        await ctx.send("❌ 인자 형식이 올바르지 않아요.")
        return
    if isinstance(error, commands.CommandOnCooldown):
        await ctx.send(f"⏳ 잠시 후에 다시 시도해 주세요. 남은 대기: {error.retry_after:.1f}s")
        return
    if isinstance(error, commands.MissingPermissions):
        await ctx.send("❌ 이 명령을 실행할 권한이 없어요.")
        return
    if isinstance(error, commands.BotMissingPermissions):
        await ctx.send("❌ 제가 수행할 권한이 부족해요. (봇 권한 확인)")
        return

    # 그 외는 간단 안내 + 콘솔에 상세 스택
    try:
        await ctx.send("❌ 명령 실행 중 알 수 없는 오류가 발생했어요. 콘솔 로그를 확인해 주세요.")
    except Exception:
        pass
    traceback.print_exception(type(error), error, error.__traceback__)

# 슬래시 전용 에러 훅
@bot.tree.error
async def on_app_command_error(inter: discord.Interaction, error: app_commands.AppCommandError):
    # 원인 까보기 (원본 CommandError 래핑되는 경우가 많음)
    original = getattr(error, "original", error)

    if isinstance(original, app_commands.MissingPermissions):
        msg = "❌ 이 명령을 실행할 권한이 없어요."
    elif isinstance(original, app_commands.CommandOnCooldown):
        msg = f"⏳ 잠시 후에 다시 시도해 주세요. 남은 대기: {original.retry_after:.1f}s"
    elif isinstance(original, app_commands.CheckFailure):
        msg = "❌ 이 명령을 사용할 수 없는 조건입니다."
    elif isinstance(original, app_commands.BadArgument):
        msg = "❌ 인자 형식이 올바르지 않아요."
    else:
        msg = "❌ 명령 실행 중 오류가 발생했어요. 콘솔 로그를 확인해 주세요."
        traceback.print_exception(type(original), original, original.__traceback__)

    # 이미 응답했는지 여부에 따라 분기
    try:
        if inter.response.is_done():
            await inter.followup.send(msg, ephemeral=True)
        else:
            await inter.response.send_message(msg, ephemeral=True)
    except Exception:
        pass

# ===== Main =====
async def _start_health_server():
    # Render가 할당하는 포트
    port = int(os.environ.get("PORT", "10000"))
    from aiohttp import web
    async def handle(_): return web.Response(text="ok")
    app = web.Application()
    app.router.add_get("/", handle)
    app.router.add_get("/healthz", handle)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    print(f"[health] listening on :{port}")

async def _main():
    # 헬스 서버 먼저 띄우고
    asyncio.create_task(_start_health_server())

    if not BOT_TOKEN:
        raise SystemExit("❌ BOT_TOKEN이 비어있습니다. .env 파일/환경변수를 설정하세요.")
    # discord.py는 bot.run 대신 bot.start를 써야 같은 이벤트루프에서 동작 가능
    await bot.start(BOT_TOKEN)

if __name__ == "__main__":
    try:
        asyncio.run(_main())
    except KeyboardInterrupt:
        pass
