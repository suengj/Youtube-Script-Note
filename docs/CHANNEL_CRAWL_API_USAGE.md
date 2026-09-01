# Channel crawl: API 사용량, Block 가능성, 해결된 이슈

## 해결된 이슈 요약

| 이슈 | 원인 | 조치 |
|------|------|------|
| **CID mismatch** | @handle URL 해석 시 HTML에서 **맨 처음** 나오는 `channelId`/`externalId`를 사용 → 추천·관련 채널 ID가 선택됨 | **canonical** 링크 → **canonicalBaseUrl** → fallback 순으로 사용. 현재 페이지의 채널 ID만 사용 |
| **Redundant API** | 매 run마다 `channels.list`(uploads_id 조회), @handle 시 HTML 요청, playlistItems 전체 페이지네이션 | `channel_df`에 **channel_id**, **uploads_playlist_id** 캐시 저장·사용. **cursor_dt** 기준 playlistItems **조기 종료** |
| **last_processed 미갱신** | queue에 잘못된 CID로 저장된 done 행은 channel_df의 (올바른) CID와 매칭되지 않음 | CID 수정 후 신규 추가되는 큐 행은 올바른 CID로 기록됨. 기존 잘못된 CID 행은 해당 채널의 last_processed 갱신에 관여하지 않음 |
| **uploads_playlist_id 개수** | 채널당 여러 개일 수 있다고 오해 | 채널당 **1개** (시스템 생성 uploads 플레이리스트). `channel_df`에 1개만 저장하는 설계가 맞음 |

---

## 1. CID mismatch (@handle → 잘못된 채널 ID)

### 현상
- `https://www.youtube.com/@HooverInstitution` 같은 URL에서 해석한 CID로 API를 호출하면 **전혀 다른 채널**이 나오는 경우
- 채널 페이지 HTML에는 추천 채널, 관련 채널 등 **다른 채널의 channelId**가 먼저 등장할 수 있음

### 원인
- `_resolve_handle_to_channel_id()`에서 HTML 전체에 대해 **첫 번째** `"channelId"`/`"externalId"` 패턴만 사용

### 조치 (channel_crawl.py)
- **1순위**: `<link rel="canonical" href="https://www.youtube.com/channel/UC...">` — 현재 페이지의 채널을 가리킴
- **2순위**: JSON 내 `"canonicalBaseUrl":"https://www.youtube.com/channel/UC..."`
- **3순위**: 기존 첫 channelId/externalId (호환용)

### 보완
- **channel_id**를 `channel_df.csv`에 저장해 두면, 다음 run부터는 URL/HTML 재해석 없이 캐시 사용 (중복 해석·잘못된 ID 재발 방지).

---

## 2. API 사용량 및 Block 가능성

### last_discovered와 API 호출 수
- **last_discovered / last_processed**는 “이 채널에 영상이 몇 개 있는지”를 **1회 API로** 세는 용도가 아님.
- 채널별로 **업로드 플레이리스트**를 `playlistItems.list`로 페이지네이션하며 가져오고, 메모리에서 날짜·done/queue 필터 적용.
- **적용 완료**: `cursor_dt`를 넘겨 **한 페이지 전체가 `published_at <= cursor_dt`이면** 페이지네이션 **조기 종료** → fetched 50 / 100 등 50 단위로만 추가 호출.

### 채널당 API 호출 (최적화 적용 후)

| 단계 | API | 조건 |
|------|-----|------|
| 1 | `channels.list` | **캐시 미스 시에만** (uploads_playlist_id가 channel_df에 없을 때) |
| 2 | `playlistItems.list` | **cursor_dt 기준 조기 종료** (최신순이므로, “이미 본 구간” 진입 시 중단) |
| 3 | `videos.list` | 신규로 큐에 넣을 영상이 있을 때만 (Shorts 필터용) |

### Block(Quota 초과) 가능성
- **IP 차단이 아니라 Quota(단위) 초과**로 제한. 기본 **일 10,000 units**, 호출당 1 unit 수준.
- 115채널·캐시·조기 종료 적용 시 1 run은 여유. **수백 채널 + 대형 채널**이 늘면 Quota 초과 가능성 있음.

---

## 3. 불필요/중복 API — 적용된 최적화

### 3.1 channels.list
- **이전**: 매 run마다 채널당 1회 (uploads playlist ID 조회).
- **적용**: `channel_df`에 **uploads_playlist_id** 저장. 값이 있고 UU 24자 형식이면 `channels.list` **생략**.

### 3.2 playlistItems.list
- **이전**: last_discovered 이후만 필요해도 **전체 업로드**를 끝까지 페이지네이션.
- **적용**: **cursor_dt**(last_discovered 또는 last_processed) 전달. 한 페이지의 **모든** 항목이 `published_at <= cursor_dt`이면 **조기 종료**. (backfill 모드에서는 조기 종료 없음.)

### 3.3 videos.list (duration)
- **filtered_entries**가 있을 때만, 해당 영상들에 대해서만 호출. 중복 없음.

### 3.4 @handle → channel_id
- **이전**: 매 run마다 @handle 채널에 대해 HTML 요청 (Data API 아님, Quota 무관).
- **적용**: `channel_df`에 **channel_id** 저장. 값이 있고 UC 24자 형식이면 `extract_channel_id_from_url()`(HTML 요청) **생략**.

---

## 4. last_processed / last_discovered 동작

- **출처**: 둘 다 **channel_df.csv**에서만 읽음. 로그의 `last_processed=…`, `last_discovered=…`는 현재 CSV에 저장된 값.
- **last_discovered 갱신**: `build_queue_and_get_candidates()` 안에서, API로 가져온 **모든** 영상(필터 전) 중 **가장 최신 published_at**으로 갱신 후 `save_channel_df()` 호출.
- **last_processed 갱신**: run **종료 후** `update_channel_last_processed_from_queue()`에서만. **crawl_yt_list.csv**의 `status=done` 행만 보고, **channel_id**별로 최대 `published_at`을 구해 channel_df에 반영.
- **accepted 0**: API 496개 등 가져온 뒤, 날짜·done_v_ids·queue_video_ids 필터를 거쳐 **새로 큐에 넣을 영상이 0개**인 경우. 필터는 설계대로 동작.

---

## 5. 요약

| 항목 | 내용 |
|------|------|
| CID mismatch | @handle 해석 시 canonical → canonicalBaseUrl → fallback 순 적용. channel_id 캐시로 재해석 최소화. |
| Redundant API | channel_id, uploads_playlist_id 캐시 + playlistItems cursor 조기 종료로 호출 감소. |
| Block | Quota(units) 기반. 수백 채널·대형 채널 증가 시 일일 한도 초과 가능성 있음. |
| uploads_playlist_id | 채널당 1개. channel_df에 1개만 저장. |

최적화 상세 To-Do는 계획 문서(Channel Crawl API Optimization)를 참고.
