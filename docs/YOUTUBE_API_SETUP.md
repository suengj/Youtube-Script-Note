# YouTube Data API 설정 가이드

## 개요

채널 크롤 모드(`CHANNEL_CRAWL=true`)에서 **channel_df.csv**에 등록된 채널의 최신 영상 목록을 가져오려면 **YouTube Data API v3** 키가 필요합니다.  
이 프로젝트는 다음 API를 사용합니다.

- **channels.list** (part=contentDetails,snippet): 채널 정보 및 업로드 재생목록 ID 조회  
- **playlistItems.list** (part=snippet): 업로드 재생목록의 영상 목록 조회 (페이지네이션 지원)

RSS(feeds/videos.xml)는 사용하지 않으며, API를 사용해 채널 전체 업로드 목록을 페이지네이션으로 수집할 수 있습니다.

---

## 1. API 키 발급 절차

### 1단계: Google Cloud 콘솔 접속

1. [Google Cloud Console](https://console.cloud.google.com/) 접속  
2. Google 계정으로 로그인  
3. 새 프로젝트 생성 또는 기존 프로젝트 선택  
   - 상단 프로젝트 선택 → **새 프로젝트** (이름 예: `p03-speech2text`)

### 2단계: YouTube Data API v3 사용 설정

1. 왼쪽 메뉴 **API 및 서비스** → **라이브러리**  
2. 검색창에 **YouTube Data API v3** 입력  
3. **YouTube Data API v3** 선택 후 **사용** 클릭  

### 3단계: API 키 생성

1. **API 및 서비스** → **사용자 인증 정보**  
2. **+ 사용자 인증 정보 만들기** → **API 키** 선택  
3. 생성된 API 키 복사 (나중에 `.env`에 넣음)  
4. (권장) **키 제한** 설정  
   - **API 제한**: “키 제한” 선택 후 **YouTube Data API v3**만 허용  
   - **애플리케이션 제한**: “없음” 또는 IP/HTTP 리퍼러 등 필요 시 설정  

---

## 2. .env 설정

프로젝트 루트의 `.env`에 다음을 추가합니다.

```env
# YouTube Data API (채널 크롤 시 필수)
YOUTUBE_API_KEY="여기에_발급받은_API_키_붙여넣기"
```

- `CHANNEL_CRAWL=true`인데 `YOUTUBE_API_KEY`가 비어 있으면 `main.py` 실행 시 에러가 나며,  
  `CHANNEL_CRAWL requires YOUTUBE_API_KEY in .env` 메시지가 표시됩니다.  
- API 키는 **비공개로 유지**하고, Git 등에 커밋하지 마세요. (`.env`는 보통 `.gitignore`에 포함)

---

## 3. 사용량(할당량) 안내

YouTube Data API v3는 **일일 할당량(quota)** 제한이 있습니다.

- **channels.list** 1회: 1 unit  
- **playlistItems.list** 1회: 1 unit  
- **videos.list** 1회: 1 unit  (쇼츠 필터 시 duration 조회용)
- 기본 일일 할당량: **10,000 units** (프로젝트별)

채널 1개당 대략  
**1 (channels.list) + ceil(영상 수 / 50) (playlistItems.list) + ceil(후보영상 수 / 50) (videos.list)** 만큼 사용합니다.  
예: 채널당 영상 200개, 후보 120개 -> `1 + 4 + 3 = 8 units`.  
예: 채널당 영상 500개, 후보 500개 -> `1 + 10 + 10 = 21 units`.  
자세한 단위는 [YouTube API Quota Calculator](https://developers.google.com/youtube/v3/determine_quota_cost)를 참고하세요.

**즉, 10,000 units면 하루에 채널 하나가 아니라 수십~수백 개 채널 최신화가 가능합니다.**  
(예: 채널당 평균 5 units → 약 2,000개 채널, 채널당 50 units → 약 200개 채널.)

할당량 초과 시 API가 `403` 또는 할당량 초과 메시지를 반환하며, 채널 크롤이 실패할 수 있습니다.  
참고로 이는 **quota 이슈**이며, API 호출 자체가 다운로드 IP block을 유발하는 형태는 아닙니다.
필요하면 Google Cloud 콘솔에서 **할당량 증가 요청**을 할 수 있습니다 (승인 여부는 Google 정책에 따름).

---

## 4. 채널 크롤에서 API가 쓰이는 방식

1. **channel_df.csv**의 각 행에서 `channel_url`로 **channel_id**를 구합니다.  
   (`/channel/UCxxx` 또는 `@handle` → 채널 페이지/API로 channel_id 해석)
2. **channels.list**로 해당 채널의 **uploads 재생목록 ID**와 **채널명**을 가져옵니다.  
3. **playlistItems.list**로 업로드 재생목록의 영상을 **50개씩** 페이지네이션으로 가져옵니다.  
4. `last_processed_published_at` / backfill 구간(`CHANNEL_START_DATE`, `CHANNEL_END_DATE`)으로 필터하고,  
   `output_df_new.csv`에 이미 있는 `v_id`는 제외한 뒤 **url_list**와 **meta_list**를 만듭니다.  
5. 처리 완료 후 성공/스킵/이미존재 영상 기준으로 **channel_df.csv**의 `last_processed_published_at`을 갱신합니다.

RSS는 사용하지 않으므로 **최근 15개 제한 없이** 채널 전체 업로드 목록을 대상으로 증분/백필이 가능합니다.

---

## 5. 문제 해결

| 증상 | 확인 사항 |
|------|-----------|
| `CHANNEL_CRAWL requires YOUTUBE_API_KEY` | `.env`에 `YOUTUBE_API_KEY="..."` 가 설정되어 있는지 확인 |
| `403 Forbidden` / 할당량 초과 | Cloud Console → API 및 서비스 → 할당량에서 사용량 확인, 필요 시 할당량 증가 요청 |
| `API key not valid` | API 키가 올바른지, YouTube Data API v3가 사용 설정되었는지 확인 |
| 채널당 영상이 0개로 나옴 | 해당 채널이 비공개/삭제되었는지, channel_id가 올바른지 확인 |

추가 설정은 **PROJECT.md**의 “채널 크롤” 및 “config.py / .env” 섹션을 참고하세요.
