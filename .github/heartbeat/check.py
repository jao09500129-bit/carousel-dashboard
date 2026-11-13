"""PC 밖 생존 경보 (R24 ①) — GitHub Actions 에서 돈다. 라이브 PC 가 기준 시각까지 이 저장소에 흔적을 남겼는지 본다.

흔적 = PC 계정(PC_AUTHOR)의 커밋. 장중 갱신(09:30 · 11:00 · 13:00 · 15:00 · 16:00 KST)과 일일 갱신(23:10 KST)이 있다.
판정만 출력한다(시각·커밋 제목). 데이터 내용·비밀값은 읽지도 출력하지도 않는다.
종료 코드 0 = 정상 / 휴장 · 1 = 경보(작업 실패 → GitHub 메일). 텔레그램은 워크플로가 보낸다.
"""
from __future__ import annotations

import datetime as dt
import os
import pathlib
import subprocess
import sys

KST = dt.timezone(dt.timedelta(hours=9))
PC_AUTHOR = "jjangbab"
HOLIDAYS = pathlib.Path(__file__).with_name("holidays.txt")
# 점검 이름 -> (이 시각(KST) 이후의 PC 커밋이 있어야 정상, 설명)
CHECKS = {
    "early": (dt.time(7, 55), "08:0x 생존 신호(Heartbeat)"),
    "morning": (dt.time(9, 25), "09:30 장중 갱신"),
    "close": (dt.time(15, 55), "16:00 장중 갱신"),
    "night": (dt.time(22, 30), "23:10 일일 갱신"),
}
# (R24 ⑤ #4) early 점검은 'Heartbeat' 로 시작하는 PC 커밋만 인정하고, heartbeat/alive.json 의 상태도 본다.
#   alive.json = {"ts": ISO(+09:00), "status": "ok"|"halt"|"no-signal"} — 숫자 지표·금액 없음.
EARLY_PREFIX = "Heartbeat"
# 예약 시각(KST · heartbeat.yml cron 과 일치). GitHub schedule 은 수 시간 늦게 돌 수 있어(2026-11 실측 7~9h),
# 실행 시각의 날짜로 판정하면 자정을 넘긴 순간 '다음 날 갱신 없음' 거짓 경보가 난다 → 직전 예약 시각의 날짜로 판정한다.
SLOTS = {"early": dt.time(8, 20), "morning": dt.time(9, 50), "close": dt.time(16, 20), "night": dt.time(23, 40)}


def slot_ref(check: str, now: dt.datetime) -> dt.datetime:
    """now 이전(포함) 가장 최근의 그 점검 예약 시각. ponytail: 24h 넘게 밀린 실행은 하루 뒤 슬롯으로 본다(드묾)."""
    d = now.date() if now.time() >= SLOTS[check] else now.date() - dt.timedelta(days=1)
    return dt.datetime.combine(d, SLOTS[check], KST)
ALIVE = pathlib.Path(__file__).resolve().parents[2] / "heartbeat" / "alive.json"


def holidays() -> set[dt.date]:
    out = set()
    for line in HOLIDAYS.read_text(encoding="utf-8").splitlines():
        s = line.split("#", 1)[0].strip()
        if s:
            out.add(dt.date.fromisoformat(s))
    return out


def pc_commit_times(since: dt.datetime) -> list[tuple[dt.datetime, str]]:
    raw = subprocess.run(
        ["git", "log", f"--since={since.isoformat()}", f"--author={PC_AUTHOR}", "--format=%cI|%s"],
        capture_output=True, text=True, encoding="utf-8", check=True).stdout
    rows = []
    for line in raw.splitlines():
        t, _, subj = line.partition("|")
        rows.append((dt.datetime.fromisoformat(t).astimezone(KST), subj))
    return rows


def judge(check: str, now: dt.datetime, hol: set[dt.date], commits: list[tuple[dt.datetime, str]]) -> tuple[bool, str]:
    """(정상?, 한 줄 사유)."""
    today = now.date()
    if today.weekday() >= 5 or today in hol:
        return True, f"{today} 휴장 — 점검 안 함"
    after, label = CHECKS[check]
    floor = dt.datetime.combine(today, after, KST)
    seen = [t for t, _ in commits if t >= floor]
    if seen:
        return True, f"{today} {label} 확인 ({max(seen):%H:%M} KST)"
    last = max((t for t, _ in commits), default=None)
    last_s = f"{last:%m-%d %H:%M} KST" if last else "없음"
    return False, f"{today} {label} 없음 — PC 마지막 흔적 {last_s}. PC 정지·미로그인·네트워크·작업 누락 의심"


def judge_early(now: dt.datetime, hol: set[dt.date], commits: list[tuple[dt.datetime, str]],
                alive: dict | None) -> tuple[bool, str]:
    """08:20 점검 — 오늘 07:55 이후 'Heartbeat' 커밋 + alive.json 이 오늘 것이고 status=ok 여야 정상."""
    today = now.date()
    if today.weekday() >= 5 or today in hol:
        return True, f"{today} 휴장 — 점검 안 함"
    floor = dt.datetime.combine(today, CHECKS["early"][0], KST)
    hb = [t for t, s in commits if t >= floor and s.startswith(EARLY_PREFIX)]
    if not hb:
        last = max((t for t, _ in commits), default=None)
        last_s = f"{last:%m-%d %H:%M} KST" if last else "없음"
        return False, (f"{today} 08:0x 생존 신호 없음 — PC 마지막 흔적 {last_s}. "
                       f"PC 정지·미로그인·08:00 신호 실행 실패·지연(데이터 재시도) 의심 · 09:05 주문 전 확인")
    st = (alive or {}).get("status")
    try:
        ts = dt.datetime.fromisoformat(str((alive or {}).get("ts"))).astimezone(KST)
    except ValueError:
        ts = None
    if ts is None or ts.date() != today:
        return False, f"{today} Heartbeat 커밋은 있으나 alive.json 시각이 오늘 것이 아님"
    if st == "ok":
        return True, f"{today} 08:0x 생존 신호 정상 ({max(hb):%H:%M} KST · ok)"
    if st == "halt":
        return False, f"{today} 08:0x 신호 보류(HALT) — 오늘 자동매매 액션 없음 · 텔레그램 신호문의 보류 사유 확인"
    return False, f"{today} 08:0x 신호 없음(no-signal) — 오늘 신호가 만들어지지 않았거나 낡았다 · 09:05 주문 전 확인"


def demo() -> None:
    t = lambda h, m: dt.datetime(2026, 10, 14, h, m, tzinfo=KST)
    hol = {dt.date(2026, 10, 9)}
    assert judge("morning", t(9, 50), hol, [(t(9, 30), "Intraday")])[0]
    assert not judge("morning", t(9, 50), hol, [(t(9, 20), "x")])[0]
    assert not judge("close", t(16, 20), hol, [])[0]
    assert judge("close", dt.datetime(2026, 10, 9, 16, 20, tzinfo=KST), hol, [])[0]       # 휴장
    assert judge("night", dt.datetime(2026, 10, 17, 23, 40, tzinfo=KST), hol, [])[0]      # 토요일
    # (R24 ⑤ #4) early
    ok_alive = {"ts": "2026-10-14T08:06:00+09:00", "status": "ok"}
    assert judge_early(t(8, 20), hol, [(t(8, 6), "Heartbeat 2026-10-14")], ok_alive)[0]
    assert not judge_early(t(8, 20), hol, [(t(8, 6), "Intraday update")], ok_alive)[0]          # 다른 커밋은 불인정
    assert not judge_early(t(8, 20), hol, [], None)[0]
    assert not judge_early(t(8, 20), hol, [(t(8, 6), "Heartbeat x")], dict(ok_alive, status="halt"))[0]
    assert not judge_early(t(8, 20), hol, [(t(8, 6), "Heartbeat x")], dict(ok_alive, status="no-signal"))[0]
    assert not judge_early(t(8, 20), hol, [(t(8, 6), "Heartbeat x")],
                           {"ts": "2026-10-13T08:06:00+09:00", "status": "ok"})[0]               # 어제 파일
    assert judge_early(dt.datetime(2026, 10, 9, 8, 20, tzinfo=KST), hol, [], None)[0]             # 휴장
    # (2026-11 거짓 경보) 16:20 close 가 다음 날 00:59 에, 23:40 night 가 다음 날 06:34 에 돌아도 원래 날짜로 판정
    late = lambda d, h, m: dt.datetime(2026, 11, d, h, m, tzinfo=KST)
    pc = [(late(12, 16, 0), "Intraday"), (late(12, 23, 10), "Daily")]
    assert slot_ref("close", late(13, 0, 59)) == late(12, 16, 20)
    assert judge("close", slot_ref("close", late(13, 0, 59)), hol, pc)[0]
    assert judge("night", slot_ref("night", late(13, 6, 34)), hol, pc)[0]
    assert not judge("night", slot_ref("night", late(13, 6, 34)), hol, pc[:1])[0]              # 진짜 누락은 여전히 경보
    assert slot_ref("morning", late(12, 16, 45)) == late(12, 9, 50)
    assert not judge("night", slot_ref("night", late(14, 6, 0)), hol, [])[0]                     # 금요일 밤 점검이 토요일에 돌아도 점검
    print("demo ok")


def main() -> int:
    check = sys.argv[1] if len(sys.argv) > 1 else os.environ.get("HB_CHECK", "")
    if check == "demo":
        demo()
        return 0
    if check not in CHECKS:
        print(f"알 수 없는 점검: {check!r}")
        return 1
    now = slot_ref(check, dt.datetime.now(KST))          # 판정 기준 = 직전 예약 시각(지연 실행 날짜 넘김 방지)
    commits = pc_commit_times(now - dt.timedelta(days=3))
    if check == "early":
        try:
            import json
            alive = json.loads(ALIVE.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            alive = None
        ok, msg = judge_early(now, holidays(), commits, alive)
    else:
        ok, msg = judge(check, now, holidays(), commits)
    print(("정상: " if ok else "경보: ") + msg)
    pathlib.Path(os.environ.get("HB_OUT", "hb_msg.txt")).write_text(msg, encoding="utf-8")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
