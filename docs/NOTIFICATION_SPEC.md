# 알림 Spec — digest / 주간 큐 (Phase R3, 구현 deferred)

> **목적:** 자동 **발행 없이** 리뷰 시점만 알림. 구현은 v4.1 MD 2~4주 축적 후.  
> **기획:** [CONTENT_REUSE_PLAN.md](CONTENT_REUSE_PLAN.md)

## 원칙

- Notification **only** — draft/발행 자동화 없음
- 트리거는 catalog/digest **로컬 파일** 기준 (iCloud 풀스캔 없음)
- 사용자가 [WEEKLY_CONTENT_QUEUE.md](WEEKLY_CONTENT_QUEUE.md)를 열어 최종 선별

---

## 트리거 정의

| ID | 트리거 | 조건 | 알림 문구 (예) | 채널 (후보) |
|----|--------|------|----------------|-------------|
| N1 | digest_updated | `build_daily_digest.py` 성공 후 | `오늘 digest N건 — 002_YT_Script/digest/YYYY_MM_DD.md` | macOS Notification / Obsidian daily note |
| N2 | weekly_queue | 매주 금 18:00 | `WEEKLY_CONTENT_QUEUE 갱신 (30분)` | Calendar / Reminders |
| N3 | candidate_pool | catalog v4.1 + `--tags ai,crypto` ≥10건 / 7일 | `블로그 후보 풀 충분 — export 검토` | Reminders (월 1회) |
| N4 | kickstart_done | `main.py` 배치 success ≥1 | `(선택) digest 갱신됨` | N1과 통합 가능 |

---

## 구현 후보 (미구현)

### Option A — macOS Reminders (수동 1회 설정)

- 반복: 매주 금요일 18:00
- URL: `obsidian://open?vault=...&file=.../WEEKLY_CONTENT_QUEUE.md`
- **비용:** $0 · **난이도:** 최저

### Option B — launchd + `osascript` (N1)

- Hook: [`main.py`](../main.py) `process_videos()` digest 갱신 직후
- Script: `scripts/notify_digest_updated.sh` → `display notification`
- **비용:** $0 · **난이도:** 낮음

### Option C — Shortcuts

- Shortcut: "Open weekly queue + run export dry-run"
- **비용:** $0 · **난이도:** 중

### Option D — 주간 큐 자동 초안 (notification body only)

- `scripts/build_weekly_queue_draft.py` → `WEEKLY_CONTENT_QUEUE.md` 표 자동 채움
- 알림: "큐 초안 ready — 검토 후 CSV 반영"
- **LLM:** $0 (catalog/digest만) · **구현:** R3.2

---

## 비포함 (명시)

- p02_blog 자동 draft / Blogger 업로드
- thesis 노트 LLM 자동 생성
- 20k MD 스캔 기반 알림

---

## 수용 기준 (구현 시)

1. N2 주간 Reminder 동작 확인
2. N1은 digest write 성공 시에만 1회 (실패 시 silent)
3. 알림 본문에 **파일 경로** 포함
4. [WEEKLY_CONTENT_QUEUE.md](WEEKLY_CONTENT_QUEUE.md) 링크 또는 Obsidian URI

---

## 관련 파일 (향후)

| 파일 | 역할 |
|------|------|
| `scripts/notify_digest_updated.sh` | N1 (deferred) |
| `scripts/build_weekly_queue_draft.py` | N4/D (deferred) |
| `launchd/com.user.p03-weekly-queue-reminder.plist` | N2 (deferred) |

현재는 **Option A (Reminders 수동)** 만으로도 R0 워크플로 충분.
