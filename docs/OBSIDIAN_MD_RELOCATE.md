# Obsidian .md 저장 (날짜 폴더)

STT는 `.md` 파일을 저장할 때 **해당 날짜 폴더** (`YYYY_MM_DD/`) 내에 직접 저장합니다. 폴더가 없으면 생성합니다.

## 저장 형식

- **경로:** `OUTPUT_MD_PATH/YYYY_MM_DD/파일명.md`
- **예:** `002_YT_Script/2026_01_28/Title_VID_5-mini.md`
- **폴더 생성:** `os.makedirs(date_dir, exist_ok=True)` — 날짜 폴더가 없으면 자동 생성

## md_relocate.py (레거시)

기존에 flat 형식(`YYYY-MM-DD_파일명.md`)으로 저장된 파일을 날짜 폴더로 **일회성 이동**할 때만 사용합니다.  
**md_relocate plist는 해제됨** — 주간 배치 불필요.

```bash
# 기존 flat 파일이 있을 때만 일회성 실행
python md_relocate.py --dry-run
python md_relocate.py
```

## plist 해제 (이미 적용됨)

com.user.p03-md-relocate.plist는 LaunchAgents에서 제거됨.

```bash
# 이미 해제된 경우 무시. 재등록 방지용:
launchctl bootout gui/$(id -u)/com.user.p03-md-relocate
rm ~/Library/LaunchAgents/com.user.p03-md-relocate.plist  # 심볼릭 링크 삭제
```
