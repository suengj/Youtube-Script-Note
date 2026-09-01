# YT Script 자료 → 블로그 / AI·투자 활용 기획서

> **상태:** Phase R0~R1 문서·스크립트 반영 · R2~R3 deferred  
> **관련:** [UPDATE_LOG_v4_mobile_catalog.md](UPDATE_LOG_v4_mobile_catalog.md) · [NOTIFICATION_SPEC.md](NOTIFICATION_SPEC.md)

## 1. 현재 보유 자산 (원장)

| 자산 | 규모 | 블로그/투자에 쓸 필드 |
|------|------|----------------------|
| Obsidian MD (`002_YT_Script/`) | ~20,862건 | 본문, `## 한눈에 보기`, Insights callout, `[확정/정황/추정]` |
| `{P03}/index/note_catalog.jsonl` | ~24k rows | `vid`, `channel`, `tags`, `title`, `tldr`, `upload_date`, `md_path_rel` |
| `002_YT_Script/digest/` | 일별 | 당일 신규 N건 TL;DR 테이블 |
| `p02_blog/dev` | CSV→draft | `topic`, `keywords`, `context_summary`, `serp_site_list`, category |

**형식 혼재 (정상):** v4.0 frontmatter-only / v1 `_dS4f` / v4.1 풀포맷. **신규 `_dS4f`부터** `tags`·`tldr`·callout 품질이 blog/투자 입력으로 적합.

---

## 2. 활용 원칙

1. **iCloud 20k 풀스캔 금지** — `note_catalog.jsonl` + `digest/` + tags/channel 필터만 사용.
2. **출처 분리** — `한눈에 보기`/`본문` = `[확정]`/`[정황]` (원문 사실); Insights = `[외부지식]`/`[추정]` (해석); Key Takeaways = so-what / risks / watch-items (시사점). 블로그·투자 메모에서 역할별로 재배치. Callout은 한눈에 보기·본문과 중복 금지.
3. **투자 ≠ 투자 조언** — Obsidian은 **리서치 메모·thesis 추적**; 공개 블로그는 fact-check + SERP + 면책.
4. **초기: 문서 + 알림** — 자동 발행보다 **주간 리뷰 큐** → 본인 선별 → `input.csv`.

---

## 3. Use Case A — 블로그 (p02_blog)

### A1. 단일 영상 심층글 (1 MD → 1 post)

| 단계 | 입력 | p02_blog 매핑 |
|------|------|---------------|
| 선별 | catalog `tldr` + tags | `topic`, `keywords` |
| 맥락 | MD `한눈에 보기` + 핵심 H2 | `context_summary` |
| 출처 | `source_url`, channel | 본문 각주 / "참고 영상" |
| 검증 | `[확정]`/`[정황]`만 fact 본문 | `[추정]`/`Insights` → "해석" |
| 보강 | `serp_site_list` | aitimes, bloomberg 등 |

가이드: [p02_blog/docs/YT_SOURCE_GUIDE.md](../../p02_blog/docs/YT_SOURCE_GUIDE.md)

### A2. 주간 큐레이션글 (digest → 1 post)

- 입력: `digest/` 5~7일치
- RSS digest(외부 뉴스)와 **내부 시청 digest** 이원화

### A3. 테마 시리즈 (catalog tags)

| 테마 | 필터 | 글 유형 |
|------|------|---------|
| AI Agent / AX | tags + channel | 기업 AX 사례 비교 |
| Crypto / Solana | crypto, solana | 이벤트 타임라인 |
| LLM 비용 | llm, openai | 파이프라인 경험 결합 |

---

## 4. Use Case B — AI·투자 리서치 (Obsidian)

### B1. Thesis 카드

- 폴더: `003_Research/AI_Investment/`
- 템플릿: `_templates/thesis_ai_investment.md`
- 입력: v4.1 `한눈에 보기` + Key Takeaways

### B2. 이벤트 타임라인

- 축: `upload_date` + `channel` + `tags`
- MOC: `MOC/AI_Investment.md` (Phase R2)

### B3. 채널별 신뢰도

| 채널 유형 | 활용 |
|-----------|------|
| Crypto (UncleLee 등) | `[정황]` → 근거 섹션 |
| Vox / 설명형 | `[확정]` → 블로그 인용 |
| EO / 인터뷰 | Key Takeaways → 비즈니스 |

### B4. SERP 역할 분담

| 레이어 | 도구 |
|--------|------|
| 1차 | YT MD + catalog |
| 2차 | p02_blog SERP |
| 3차 | fact-check + draft |

---

## 5. 주간 워크플로 (30~45분)

1. `digest/` 최근 7일 + catalog tags 필터
2. 주간 리뷰 큐 갱신
3. 블로그 1~2건 → `input.csv`
4. 투자 1~2건 → thesis 노트
5. 알림: [NOTIFICATION_SPEC.md](NOTIFICATION_SPEC.md) (구현 deferred)

---

## 6. 구현 로드맵

| Phase | 내용 | 상태 |
|-------|------|------|
| **R0** | 문서, 템플릿, 가이드 | ✅ |
| **R1** | `export_blog_candidates.py` | ✅ |
| **R2** | MOC, weekly digest merge, thesis backlink | ⏸ |
| **R3** | 알림 spec only | 📄 spec |

**하지 않을 것:** 20k 일괄 블로그화, YT→투자 조언 자동 생성, catalog 없이 iCloud grep.

---

## 7. 비용·품질

| 경로 | LLM | 리스크 |
|------|-----|--------|
| 수동 큐 + CSV | $0 | 낮음 |
| export_candidates | $0 | 낮음 |
| p02_blog draft | blog 기존 | SERP/fact-check |
| 자동 thesis | +$0.01~0.05/건 | 환각 → 비권장 |

---

## 8. Usage (R1)

```bash
cd $PROJECT_ROOT

# 최근 7일, tags 필터 → blog 후보 CSV
python scripts/export_blog_candidates.py --days 7 --tags ai,crypto,llm

# p02_blog input.csv에 붙여넣기 전 검토
python scripts/export_blog_candidates.py --days 14 --tags ai --dry-run
```

출력: `YTT_AUDIO/index/blog_candidates_YYYYMMDD.csv` (또는 `--output`)

---

## 9. 한 줄 결론

**공통 원장 = catalog + digest + v4.1 MD.** 블로그는 `input.csv`, 투자는 Obsidian thesis. 지금은 주간 큐 운영; bridge·알림은 v4.1 MD 2~4주 쌓인 뒤 R2~R3.
