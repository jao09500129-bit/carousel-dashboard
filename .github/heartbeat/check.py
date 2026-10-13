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
    "morning": (dt.time(9, 25), "09:30 장중 갱신"),
    "close": (dt.time(15, 55), "16:00 장중 갱신"),
    "night": (dt.time(22, 30), "23:10 일일 갱신"),
}


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


def demo() -> None:
    t = lambda h, m: dt.datetime(2026, 10, 14, h, m, tzinfo=KST)
    hol = {dt.date(2026, 10, 9)}
    assert judge("morning", t(9, 50), hol, [(t(9, 30), "Intraday")])[0]
    assert not judge("morning", t(9, 50), hol, [(t(9, 20), "x")])[0]
    assert not judge("close", t(16, 20), hol, [])[0]
    assert judge("close", dt.datetime(2026, 10, 9, 16, 20, tzinfo=KST), hol, [])[0]       # 휴장
    assert judge("night", dt.datetime(2026, 10, 17, 23, 40, tzinfo=KST), hol, [])[0]      # 토요일
    print("demo ok")


def main() -> int:
    check = sys.argv[1] if len(sys.argv) > 1 else os.environ.get("HB_CHECK", "")
    if check == "demo":
        demo()
        return 0
    if check not in CHECKS:
        print(f"알 수 없는 점검: {check!r}")
        return 1
    now = dt.datetime.now(KST)
    commits = pc_commit_times(now - dt.timedelta(days=3))
    ok, msg = judge(check, now, holidays(), commits)
    print(("정상: " if ok else "경보: ") + msg)
    pathlib.Path(os.environ.get("HB_OUT", "hb_msg.txt")).write_text(msg, encoding="utf-8")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
