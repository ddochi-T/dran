"""
뜨란채 예약 시스템 (Streamlit + Firebase Firestore)
--------------------------------------------------
요구 조건 (업데이트 적용):
- 모든 요일 1~6교시 표시(항상 6교시). 단, **수요일 6교시 예약 불가**.
- 전주 목요일 07:00(KST) 비배정학년 오픈 규칙 유지, 모달에 안내 문구 표기.
- 비배정학년이 예약 시도 시, **언제 이후 예약 가능한지** 안내.
- 주별 배정학년은 코드에 **기본 표(이미지 기준 1~21주 시퀀스)**로 내장. (관리자에서 DB값으로 덮어쓰기 가능)
- 2025-09-01 ~ 2025-09-10 **여름방학**: 전 슬롯을 빨간 글씨로 표시하고 예약 차단.
- 한 슬롯에는 1학급만 예약(트랜잭션). PIN(숫자 4자리)으로 삭제 검증.
- 관리자: 제한 무시 예약/삭제, 학급 수 설정, 데이터 내보내기.
- 배포: 깃허브에 올린 `app.py`를 Streamlit에서 가져와 실행.

⚙️ 사전 준비(필수)
- Streamlit Cloud/서버 `st.secrets` 설정:

[[secrets.toml 예시]]
FIREBASE_SERVICE_ACCOUNT = {  # 서비스 계정 JSON 그대로
  "type": "service_account",
  "project_id": "your-project-id",
  "private_key_id": "...",
  "private_key": "-----BEGIN PRIVATE KEY-----
...
-----END PRIVATE KEY-----
",
  "client_email": "firebase-adminsdk@your-project-id.iam.gserviceaccount.com",
  "client_id": "..."
}
ADMIN_PASSWORD = "trran-admin-0000"
TIMEZONE = "Asia/Seoul"
CLASSES_PER_GRADE = {"1": 6, "2": 6, "3": 6, "4": 6, "5": 6, "6": 6}

Firestore 구조
- config/settings
  { classes_per_grade: {"1":6,...} }
- config/weekly_assignments
  { "YYYY-MM-DD": 3, ... }  # 해당 주 월요일 → 배정학년
- reservations/{slot_id}
  slot_id 예: "2025-09-15_P3"

"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Dict, List, Optional, Tuple

import pandas as pd
import streamlit as st

# Firebase
import firebase_admin
from firebase_admin import credentials, firestore

try:
    from zoneinfo import ZoneInfo  # Python 3.9+
except Exception:  # pragma: no cover
    from pytz import timezone as ZoneInfo  # fallback

# ----------------------------- 기본 설정 ----------------------------- #
st.set_page_config(page_title="뜨란채 예약 시스템", layout="wide")
st.markdown(
    """
    <style>
      .small {font-size:0.85rem;color:#666}
      .muted {color:#888}
      .badge {background:#eef;border:1px solid #ccd;border-radius:6px;padding:2px 6px;margin-left:6px}
      .reserved {background:#f9f9ff;border:1px solid #dfe3ff;border-radius:8px;padding:8px}
      .open {background:#f6fff6;border:1px solid #cfe8cf;border-radius:8px;padding:8px}
      .blocked {background:#fff6f6;border:1px solid #ffd7d7;border-radius:8px;padding:8px;opacity:0.95}
      .cell-btn {width:100%}
      .titlebar {display:flex; gap:8px; align-items:center}
    </style>
    """,
    unsafe_allow_html=True,
)

# -------------------------- Firebase 초기화 -------------------------- #
@st.cache_resource(show_spinner=False)
def init_db():
    cred = credentials.Certificate(st.secrets["FIREBASE_SERVICE_ACCOUNT"])  # type: ignore
    if not firebase_admin._apps:
        firebase_admin.initialize_app(cred)
    return firestore.client()

db = init_db()

# -------------------------- 유틸 / 도메인 로직 -------------------------- #
KST = ZoneInfo(st.secrets.get("TIMEZONE", "Asia/Seoul"))
SUMMER_BREAK_START = date(2025, 9, 1)
SUMMER_BREAK_END = date(2025, 9, 10)


def kst_now() -> datetime:
    return datetime.now(tz=KST)


def week_monday(d: date) -> date:
    return d - timedelta(days=d.weekday())  # 월요일=0


def format_hhmm(t: time) -> str:
    return f"{t.hour:02d}:{t.minute:02d}"


def open_time_for_week(monday: date) -> datetime:
    """비배정학년 예약 오픈 = 해당 주의 전주 목요일 07:00 (KST)"""
    prev_thu = monday - timedelta(days=4)  # 월요일-4=목
    return datetime.combine(prev_thu, time(7, 0), tzinfo=KST)


@st.cache_data(show_spinner=False)
def load_settings() -> Dict:
    doc = db.collection("config").document("settings").get()
    if doc.exists:
        return doc.to_dict() or {}
    defaults = {"classes_per_grade": st.secrets.get("CLASSES_PER_GRADE", {str(i): 6 for i in range(1, 7)})}
    db.collection("config").document("settings").set(defaults)
    return defaults


@st.cache_data(show_spinner=False)
def load_assignments() -> Dict[str, int]:
    doc = db.collection("config").document("weekly_assignments").get()
    if doc.exists:
        data = doc.to_dict() or {}
        return {k: int(v) for k, v in data.items()}
    return {}

# 기본 배정표 (첨부 표 기준 1~21주 시퀀스)
ASSIGN_DEFAULT_SEQUENCE = [1, 1, 1, 2, None, 2, 3, 3, 6, 5, 4, 1, 1, 2, 2, 3, 3, 4, 5, 6, 6]
ASSIGN_DEFAULT_START = date(2025, 9, 8)  # 학기 첫 월요일(표의 1주차 포함 주)
ASSIGN_DEFAULTS: Dict[str, int] = {}
for i, g in enumerate(ASSIGN_DEFAULT_SEQUENCE):
    monday_i = ASSIGN_DEFAULT_START + timedelta(weeks=i)
    if g is not None:
        ASSIGN_DEFAULTS[monday_i.isoformat()] = g

SETTINGS = load_settings()
ASSIGN = {**ASSIGN_DEFAULTS, **load_assignments()}  # DB 값이 있으면 우선


def assigned_grade_for_week(monday: date) -> Optional[int]:
    return ASSIGN.get(monday.isoformat())


def build_period_table(max_periods: int = 6) -> Dict[int, Tuple[time, time]]:
    """교시별 시작/종료 시각 계산: 시작 08:50, 수업 40분, 쉬는 10분, 6교시까지 고정"""
    start = datetime.combine(date.today(), time(8, 50))
    periods: Dict[int, Tuple[time, time]] = {}
    cur = start
    for p in range(1, max_periods + 1):
        lesson_end = cur + timedelta(minutes=40)
        periods[p] = (cur.time(), lesson_end.time())
        if p != max_periods:
            cur = lesson_end + timedelta(minutes=10)
        else:
            cur = lesson_end
    return periods


def hash_pin(pin: str) -> str:
    return hashlib.sha256(pin.encode()).hexdigest()


@dataclass
class Slot:
    day: date
    period: int
    start: time
    end: time

    @property
    def id(self) -> str:
        return f"{self.day.isoformat()}_P{self.period}"


def week_slots(monday: date, max_periods: int = 6) -> List[Slot]:
    per_table = build_period_table(max_periods)
    days = [monday + timedelta(days=i) for i in range(5)]  # 월~금
    slots: List[Slot] = []
    for d in days:
        for p, (s, e) in per_table.items():
            slots.append(Slot(d, p, s, e))
    return slots


def get_reservation(slot_id: str):
    return db.collection("reservations").document(slot_id).get()


def put_reservation(slot: Slot, grade: int, class_no: int, purpose: str, pin: str, *, force: bool = False) -> Tuple[bool, str]:
    doc_ref = db.collection("reservations").document(slot.id)

    def txn_op(txn):
        snap = doc_ref.get(transaction=txn)
        if snap.exists and not force:
            raise RuntimeError("이미 예약된 슬롯입니다.")
        payload = {
            "date": slot.day.isoformat(),
            "period": slot.period,
            "start": format_hhmm(slot.start),
            "end": format_hhmm(slot.end),
            "grade": int(grade),
            "class_no": int(class_no),
            "purpose": purpose.strip(),
            "pin_hash": hash_pin(pin),
            "created_at": firestore.SERVER_TIMESTAMP,
            "updated_at": firestore.SERVER_TIMESTAMP,
        }
        txn.set(doc_ref, payload)

    try:
        db.transaction()(txn_op)  # type: ignore
        return True, "예약이 완료되었습니다."
    except Exception as e:
        return False, str(e)


def delete_reservation(slot_id: str, pin: Optional[str] = None, *, admin: bool = False) -> Tuple[bool, str]:
    doc_ref = db.collection("reservations").document(slot_id)
    snap = doc_ref.get()
    if not snap.exists:
        return False, "해당 슬롯에 예약이 없습니다."
    data = snap.to_dict() or {}
    if admin:
        doc_ref.delete()
        return True, "관리자 권한으로 삭제했습니다."
    if not pin or hash_pin(pin) != data.get("pin_hash"):
        return False, "비밀번호가 일치하지 않습니다."
    doc_ref.delete()
    return True, "예약이 삭제되었습니다."


# ------------------------------- UI ------------------------------- #
st.title("뜨란채 예약 시스템")

# Sidebar: 주 선택 & 모드
with st.sidebar:
    today = kst_now().date()
    pick_date = st.date_input("주 선택 (해당 주의 아무 날짜)", value=today)
    monday = week_monday(pick_date)

    assigned = assigned_grade_for_week(monday)
    if assigned:
        st.markdown(f"**해당 주 배정학년:** {assigned}학년")
    else:
        st.markdown("**해당 주 배정학년:** _미등록_ (기본 표 적용)")

    open_dt = open_time_for_week(monday)
    st.caption(f"비배정학년 예약 오픈: {open_dt.strftime('%Y-%m-%d %H:%M')} (KST)")

    st.markdown("---")
    mode = st.radio("모드", ["예약하기", "내 예약 관리", "관리자"], index=0)

    st.markdown("---")
    admin_pw = st.text_input("관리자 비밀번호", type="password")
    is_admin = admin_pw and (admin_pw == st.secrets.get("ADMIN_PASSWORD"))
    if is_admin:
        st.success("관리자 모드 활성화")
    else:
        st.caption("관리자 전용 기능은 비밀번호 필요")

# 학년/반 드롭다운
classes_per_grade: Dict[str, int] = load_settings().get("classes_per_grade", {str(i): 6 for i in range(1, 7)})

# 주간 슬롯(항상 6교시)
slots = week_slots(monday, 6)

# 기존 예약 로드
reservations: Dict[str, Dict] = {}
for s in slots:
    snap = get_reservation(s.id)
    if snap.exists:
        reservations[s.id] = snap.to_dict() or {}


def can_book(user_grade: int) -> Tuple[bool, str]:
    if is_admin:
        return True, "관리자 권한으로 예약 가능"
    if assigned and user_grade == assigned:
        return True, "배정학년 우선 예약 기간"
    now = kst_now()
    if now >= open_dt:
        return True, "비배정학년 예약 오픈 기간"
    return False, f"비배정학년은 {open_dt.strftime('%m/%d %H:%M')} 이후 예약 가능"

# ---------------------------- 메인 화면 ---------------------------- #
if mode == "예약하기":
    st.subheader(f"주간 예약표 · {monday.strftime('%Y-%m-%d')} ~ {(monday + timedelta(days=4)).strftime('%Y-%m-%d')}")

    # 입력 폼
    with st.expander("내 정보/예약 입력", expanded=True):
        col1, col2, col3, col4 = st.columns([1, 1, 3, 1])
        with col1:
            sel_grade = st.selectbox("학년", options=[1, 2, 3, 4, 5, 6], index=0)
        with col2:
            max_class = int(classes_per_grade.get(str(sel_grade), 6))
            sel_class = st.selectbox("반", options=list(range(1, max_class + 1)), index=0)
        with col3:
            purpose = st.text_input("사용 목적", placeholder="예) 과학 수업, 독서 활동 등")
        with col4:
            pin = st.text_input("예약 비밀번호(4자리)", max_chars=4, type="password")
            if pin and (not pin.isdigit() or len(pin) != 4):
                st.warning("숫자 4자리로 입력하세요.")

        ok, msg = can_book(int(sel_grade))
        if ok:
            st.caption(f"✓ {msg}")
        else:
            st.error(msg)

    days = [monday + timedelta(days=i) for i in range(5)]
    header_cols = st.columns([1] + [2] * 5)
    with header_cols[0]:
        st.markdown("**교시/요일**")
    for i, d in enumerate(days, start=1):
        with header_cols[i]:
            label = d.strftime("%m/%d(%a)")
            st.markdown(f"**{label}**")

    per_table = build_period_table(6)
    for p in range(1, 7):
        row = st.columns([1] + [2] * 5)
        with row[0]:
            s, e = per_table[p]
            st.markdown(f"**{p}교시**

<span class='small'>{format_hhmm(s)}–{format_hhmm(e)}</span>", unsafe_allow_html=True)
        for i, d in enumerate(days, start=1):
            slot = Slot(d, p, *per_table[p])
            data = reservations.get(slot.id)
            with row[i]:
                # 여름방학 차단
                if SUMMER_BREAK_START <= d <= SUMMER_BREAK_END:
                    st.markdown("<div class='blocked' style='border-color:#ffb3b3'><b style='color:#d00'>여름방학</b></div>", unsafe_allow_html=True)
                    st.button("예약", key=f"vac_{d}_P{p}", disabled=True, use_container_width=True)
                    continue
                # 수요일 6교시 차단
                if d.weekday() == 2 and p == 6:
                    st.markdown("<div class='blocked'><b>수요일 6교시 예약 불가</b></div>", unsafe_allow_html=True)
                    st.button("예약", key=f"w6_{d}_P{p}", disabled=True, use_container_width=True)
                    continue

                if data:
                    st.markdown(
                        f"<div class='reserved'>
"
                        f"<b>{data.get('grade')}학년 {data.get('class_no')}반</b><span class='badge'>{data.get('purpose')}</span><br>"
                        f"<span class='small'>예약됨</span>
"
                        f"</div>",
                        unsafe_allow_html=True,
                    )
                    del_key = f"del_{slot.id}"
                    if st.button("삭제", key=del_key, use_container_width=True):
                        if is_admin:
                            ok, m = delete_reservation(slot.id, admin=True)
                            st.toast(m)
                            st.rerun()
                        else:
                            with st.popover("비밀번호 입력"):
                                pin_in = st.text_input("예약 비밀번호(4자리)", max_chars=4, type="password", key=f"pin_{slot.id}")
                                if st.button("확인", key=f"conf_{slot.id}"):
                                    ok, m = delete_reservation(slot.id, pin=pin_in, admin=False)
                                    st.toast(m)
                                    st.rerun()
                else:
                    ok, reason = can_book(int(sel_grade))
                    disabled = not ok or not purpose or not (pin and pin.isdigit() and len(pin) == 4)
                    style = "open" if ok else "blocked"
                    st.markdown(
                        f"<div class='{style}'>"
                        f"<span class='small'>{format_hhmm(slot.start)}–{format_hhmm(slot.end)}</span>"
                        f"</div>",
                        unsafe_allow_html=True,
                    )
                    if st.button("예약", key=f"book_{slot.id}", disabled=disabled, use_container_width=True):
                        # 모달 팝업에서 최종 확인 및 안내
                        with st.modal("예약 확인"):
                            st.markdown(f"**{d.strftime('%Y-%m-%d (%a)')} · {p}교시**")
                            st.caption(f"시간: {format_hhmm(slot.start)}–{format_hhmm(slot.end)}")
                            st.write(f"학년/반: **{sel_grade}학년 {sel_class}반**")
                            st.write(f"사용 목적: **{purpose}**")
                            if not ok:
                                # 비배정학년 안내 문구 강화
                                st.error(reason)
                            else:
                                st.success("예약이 가능합니다.")
                            if st.button("최종 예약 확정", key=f"confirm_{slot.id}", disabled=not ok):
                                ok2, m2 = put_reservation(slot, int(sel_grade), int(sel_class), purpose, pin, force=is_admin)
                                st.toast(m2)
                                st.rerun()

elif mode == "내 예약 관리":
    st.subheader("내 예약 확인/삭제")
    col1, col2 = st.columns(2)
    with col1:
        my_grade = st.selectbox("학년", [1, 2, 3, 4, 5, 6])
        my_class = st.selectbox("반", list(range(1, int(classes_per_grade.get(str(my_grade), 6)) + 1)))
    with col2:
        my_pin = st.text_input("비밀번호(4자리)", max_chars=4, type="password")
        date_from = st.date_input("조회 시작일", value=week_monday(kst_now().date()))
        date_to = st.date_input("조회 종료일", value=week_monday(kst_now().date()) + timedelta(days=4))

    if st.button("내 예약 조회"):
        results = []
        day = date_from
        while day <= date_to:
            if day.weekday() < 5:
                for p in range(1, 7):
                    sid = f"{day.isoformat()}_P{p}"
                    snap = db.collection("reservations").document(sid).get()
                    if snap.exists:
                        data = snap.to_dict() or {}
                        if data.get("grade") == int(my_grade) and data.get("class_no") == int(my_class):
                            results.append({"date": day.isoformat(), **data, "slot_id": sid})
            day += timedelta(days=1)
        if results:
            df = pd.DataFrame(results)[["date", "period", "start", "end", "purpose", "slot_id"]]
            st.dataframe(df, use_container_width=True, hide_index=True)
        else:
            st.info("해당 기간에 예약이 없습니다.")

    st.markdown("---")
    del_sid = st.text_input("삭제할 슬롯 ID (예: 2025-09-15_P3)")
    if st.button("내 예약 삭제"):
        if not my_pin or not my_pin.isdigit() or len(my_pin) != 4:
            st.error("비밀번호는 숫자 4자리입니다.")
        else:
            ok, m = delete_reservation(del_sid, pin=my_pin, admin=False)
            st.toast(m)

else:  # 관리자
    st.subheader("관리자 설정")
    if not (admin_pw and is_admin):
        st.error("관리자 비밀번호가 필요합니다.")
    else:
        # 고정 배정표 미리보기
        st.markdown("### 주별 배정학년(기본값 + DB 덮어쓰기)")
        if ASSIGN:
            view = pd.DataFrame(
                sorted([(k, v) for k, v in ASSIGN.items()], key=lambda x: x[0]),
                columns=["monday", "grade"],
            )
            st.dataframe(view, use_container_width=True, hide_index=True)
            st.caption("※ 코드 내 기본값을 포함해 표시합니다. DB에 저장하면 해당 주는 덮어써집니다.")
        else:
            st.info("배정표가 없습니다.")

        st.markdown("---")
        # 학년별 학급 수 설정
        st.markdown("### 학년별 학급 수 설정")
        new_cfg = {}
        cols = st.columns(6)
        for i in range(6):
            g = str(i + 1)
            with cols[i]:
                new_cfg[g] = st.number_input(f"{g}학년", min_value=1, max_value=20, value=int(classes_per_grade.get(g, 6)))
        if st.button("학급 수 저장"):
            db.collection("config").document("settings").set({"classes_per_grade": new_cfg}, merge=True)
            st.success("저장되었습니다. 새로고침(F5) 후 적용됩니다.")

        st.markdown("---")
        # 관리자 예약/삭제
        st.markdown("### 관리자 예약/삭제")
        colA, colB, colC, colD = st.columns(4)
        with colA:
            ad_grade = st.selectbox("학년", [1, 2, 3, 4, 5, 6], key="ad_g")
        with colB:
            ad_class = st.selectbox("반", list(range(1, int(classes_per_grade.get(str(ad_grade), 6)) + 1)), key="ad_c")
        with colC:
            ad_purpose = st.text_input("사용 목적", key="ad_p")
        with colD:
            ad_pin = st.text_input("비밀번호(4자리)", max_chars=4, key="ad_pin")

        ad_day = st.date_input("날짜(월~금)", value=monday)
        ad_period = st.number_input("교시(1~6)", min_value=1, max_value=6, value=1)
        per_table_all = build_period_table(6)
        s_t, e_t = per_table_all[int(ad_period)]
        ad_slot = Slot(ad_day, int(ad_period), s_t, e_t)
        st.caption(f"선택 슬롯: {ad_slot.id} | {format_hhmm(s_t)}–{format_hhmm(e_t)}")
        c1, c2 = st.columns(2)
        with c1:
            if st.button("관리자 예약 강제 등록"):
                ok, m = put_reservation(ad_slot, int(ad_grade), int(ad_class), ad_purpose, ad_pin or "0000", force=True)
                st.toast(m)
        with c2:
            if st.button("관리자 예약 삭제"):
                ok, m = delete_reservation(ad_slot.id, admin=True)
                st.toast(m)

        st.markdown("---")
        st.markdown("### 데이터 내보내기")
        export_from = st.date_input("내보내기 시작일", value=week_monday(kst_now().date()), key="ex_s")
        export_to = st.date_input("내보내기 종료일", value=week_monday(kst_now().date()) + timedelta(days=28), key="ex_e")
        if st.button("CSV로 다운로드"):
            rows = []
            day = export_from
            while day <= export_to:
                if day.weekday() < 5:
                    for p in range(1, 7):
                        sid = f"{day.isoformat()}_P{p}"
                        snap = db.collection("reservations").document(sid).get()
                        if snap.exists:
                            data = snap.to_dict() or {}
                            rows.append({"slot_id": sid, "date": day.isoformat(), **data})
                day += timedelta(days=1)
            if rows:
                df = pd.DataFrame(rows)
                st.download_button(
                    label="CSV 저장",
                    data=df.to_csv(index=False).encode("utf-8-sig"),
                    file_name="reservations.csv",
                    mime="text/csv",
                )
            else:
                st.info("해당 기간에 데이터가 없습니다.")

# --------------------------- 풋터/도움말 --------------------------- #
st.markdown("---")
st.markdown(
    """
    **도움말**  
    • 모든 날은 1~6교시까지 표시되며, **수요일 6교시**는 예약이 불가합니다.  
    • 비배정학년은 전주 **목요일 07:00(KST)**부터 예약할 수 있습니다.  
    • 2025-09-01 ~ 2025-09-10은 **여름방학**으로 전 슬롯 예약이 차단됩니다.  
    • 깃허브에 `app.py`를 올리고 Streamlit에서 GitHub 연결로 실행하세요 (secrets는 프로젝트 설정에 등록).  
    • DB에 `weekly_assignments` 문서를 저장하면 코드 내 기본 배정표를 덮어쓸 수 있습니다.
    """
)
