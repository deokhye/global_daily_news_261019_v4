# Global HR Radar

한국타이어 **해외 사업장 38개국**(한국 제외)의 **HR·노동, 현지 안전·정세, 경제지표, 완성차·타이어 동향**을 매일 자동 수집해
한 화면에서 보여 주고, 매주 월요일 주간 브리핑 메일로 요약해 보내는 서비스입니다.
GitHub Actions 로 수집하고 GitHub Pages 로 배포합니다. 사내 HR Insight 화면도 이 저장소의 결과(`latest.json`·`archive/`)를 받아서 보여 줍니다.

| 화면 | 주소 | 내용 |
|---|---|---|
| 오늘의 브리핑 | `#/briefing` (첫 화면) | **오늘 주요 브리핑(중요도 상위 5건)** · 권역 카드(중국·유럽·미주·아태·중아·중남미) · 현지 안전 동향 · HR·노동 주요 소식(각 8건 + 더보기) · 글로벌 완성차·타이어 동향 |
| 국가 상세 | `#/country/DE` | 권역 탭·국가 칩(본부·공장 국가 먼저) · 국가 개요/거점 · 환율·물가·실업률·최저임금 · 환율 계산기 · HR·노동(최근 2주) · 현지 주요 뉴스(안전·재난 / 정치·경제 / 사회·문화) · 완성차·타이어 · 공식 기관 |
| 공통 | 탭 줄 오른쪽 | 일자 이동(◀ 날짜(요일) ▶, 최대 180일) · 기사 검색(최근 30일, 날짜별로 묶어 표시) |

> ⚠️ **저장소 이름·GitHub Pages 주소를 바꾸거나 Pages 를 끄면 HR Insight 갱신이 멈춥니다.** 바꿔야 하면 HR Insight 관리자에게 먼저 알리세요.
> HR Insight 화면 파일(`view.html`)과 연동 가이드는 사내 경로가 들어 있으므로 이 저장소에 올리지 않습니다.

---

## 1. 폴더 구조

```
├── config/                      ← 사람이 직접 관리하는 설정 (코드 수정 없이 바꿀 수 있음)
│   ├── countries.csv            국가·권역·영문명·ISO3·수도(한글)·Trading Economics slug (해외 38개국)
│   ├── country_codes.csv        ISO3 → ISO2·통화·Eurostat 코드
│   ├── sites.csv                한국타이어 거점
│   ├── official_sources.csv     국가별 공식 기관·보고서 바로가기 (ALL=전체, EU=EU 회원국 공통)
│   ├── manual_profile.json      자동 수집 전/실패 시 쓰는 보조 지표(인구·GDP·물가·실업률·최저임금)
│   ├── news_filters.json        국가별 뉴스 별칭·제외어·거부 정규식·검색용 국가명
│   ├── news_rules.json          뉴스 선별 규칙 전부 (제외어·매체 목록·HR 키워드·안전 판단·타국 기사·추가 정리)
│   ├── operations.json          알림 조건, 주간 메일 설정(섹션별 건수·제목)
│   ├── min_wage_overrides.csv   최저임금 보정 파일 (HR 이 정부 발표 확인 후 입력)
│   └── settings.toml            국가 프로필 수집 기준값
├── src/
│   ├── collector.py             매일 뉴스·환율 수집·번역·정리 → data/, docs/archive/
│   ├── news_rules.py            제외 규칙·배지·중요도 점수·같은 사건 묶기·신뢰 매체 판단
│   ├── profile_overlay.py       config + 프로필 값 조립 (collector·build_site 공용, 권역 순서)
│   ├── build_site.py            data + template → docs/index.html, docs/latest.json
│   ├── weekly_mail.py           주간 브리핑 메일
│   ├── notify.py                수집 문제 알림 메일
│   └── mailer.py                메일 발송 공용
├── country_profile/             국가 기본 프로필 수집 (월간·연간) → output/
├── template/template.html       화면 (HTML·CSS·JS 한 파일)
├── tests/                       단위 테스트 (pytest)
├── data/                        수집 결과·번역 저장분 (자동 생성·커밋)
├── docs/archive/                날짜별 아카이브 180일 (자동 생성·커밋)
├── output/                      국가 프로필 결과·엑셀 (자동 생성·커밋)
└── .github/workflows/           자동 실행 5종
```

## 2. 자동 실행

| 워크플로 | 언제 | 하는 일 |
|---|---|---|
| **Daily Global Brief Update** (`update.yml`) | **매일 KST 05:20 외부 예약(cron-job.org, mode `auto`)** + GitHub 예약 02:40·03:40·04:40 (보조) | 그날 처음 시작된 실행 1회만 수집(`collect`): 뉴스·환율 수집 → 번역·정리 → 화면 생성 → 데이터 커밋 → 배포. 이미 수집했으면 바로 끝남 |
| | 월요일 첫 자동 수집 (05:00 이후) | 같은 실행 안에서 **주간 브리핑 메일** 발송 (같은 날 두 번 보내지 않음, `data/weekly_mail_sent.txt`) |
| | main 에 `src/`·`template/`·`config/` 변경 push | `build`: 화면만 다시 생성 → 배포 (수집 없음) |
| | 수동 실행 | mode `collect`·`build`·`auto`·`mail` + 주간 메일 `none`·`preview`·`send` |
| **Backfill Archive** (`backfill.yml`) | 수동 실행 | 지정한 과거 날짜를 그날 기준으로 다시 수집해 `docs/archive/날짜.json` 만 교체 (기사 30건 미만이면 기존 유지) |
| **Country Profile — Monthly** | 매월 1일 KST 09:00 | 인플레이션·실업률 수집 → `output/` 커밋 → 대시보드 `build` |
| **Country Profile — Annual** | 매년 1월 1일 KST 09:00 | 총인구·GDP·최저임금 수집 → `output/` 커밋 → 대시보드 `build` |
| **Dev Check** | main 이외 브랜치 push, main 대상 PR | 테스트 + 저장된 데이터로 화면 생성 |

- **GitHub 예약 실행은 2~4시간 늦거나 빠질 수 있어** 매일 05:20 외부 예약(cron-job.org)을 주 실행으로 씁니다.
  - cron-job.org 작업: `POST https://api.github.com/repos/<계정>/<저장소>/actions/workflows/update.yml/dispatches`
  - 헤더 `Authorization: Bearer <토큰>`, `Accept: application/vnd.github+json` / 본문 `{"ref":"main","inputs":{"mode":"auto","weekly_mail":"none"}}`
  - 토큰: GitHub Fine-grained token, 이 저장소만 · **Actions: Read and write** 만. **만료 1주 전에 새로 발급**해 cron-job.org 값만 교체.
- 예약 실행은 **기본 브랜치(main)의 워크플로 파일** 기준으로만 돕니다.
- 수집 단계는 시간 예산(환율 3분·뉴스 8분·번역 9분)을 넘기면 남은 작업을 직전 값으로 대체해 25분 안에 끝납니다.
- 사내 HR Insight 는 매일 06:10~12:10 매시간 새 데이터를 받아 갑니다(보통 06:10 반영, 최근 7일 보관).

## 3. 새 저장소 설정 순서

1. 이 폴더의 내용 전체를 저장소 **루트**에 올립니다 (`.github`, `.gitignore` 숨김 파일 포함).
2. **Settings → Pages → Source: GitHub Actions**
3. **Settings → Actions → General → Workflow permissions: Read and write permissions**
4. (기존 저장소에서 옮기는 경우, 선택) 기존 저장소의 `docs/archive/` 와 `data/translation_cache.json` 을 같은 위치에 복사합니다.
5. **Actions → Daily Global Brief Update → Run workflow (mode: collect)** — 첫 데이터 생성
6. **Actions → Country Profile — Annual → Run workflow**, 이어서 **Monthly → Run workflow**
7. cron-job.org 매일 05:20 작업 설정 (2절)
8. (선택) 메일 기능 — 아래 **"메일 설정"**

### 메일 설정 (Settings → Secrets and variables → Actions → **Repository secrets**)

| 이름 | 값 | 쓰는 곳 |
|---|---|---|
| `MAIL_USER` | 발송용 Gmail 주소 | 알림·주간 메일 |
| `MAIL_PASSWORD` | Gmail **앱 비밀번호** 16자리 (Google 계정 → 보안 → 2단계 인증 → 앱 비밀번호) | 알림·주간 메일 |
| `MAIL_TO` | 주간 메일 받는 사람들 (쉼표로 구분) | 주간 메일 |
| `ALERT_TO` | 수집 문제 알림 받는 사람 (보통 본인) | 매일 알림 |
| `MAIL_SMTP_HOST` / `MAIL_SMTP_PORT` (선택) | Gmail 이 아닌 메일 서버를 쓸 때 (회사 Outlook 은 SMTP 인증이 막혀 있을 수 있음) | 메일 |

- 받는 사람끼리 주소가 보이지 않도록 **숨은 참조**로 보냅니다. 받는 사람을 바꿀 때는 `MAIL_TO` 값만 고칩니다.
- 메일 설정이 없으면 알림·주간 메일만 건너뛰고 수집·배포는 그대로 돕니다.
- 미리보기: **Actions → Daily Global Brief Update → Run workflow** (mode `build`, 주간 메일 `preview`) → 실행 화면 아래 Artifacts 의 `weekly-mail-preview`.
- 메일 실패 원인은 실행 요약 화면 맨 위에 한글로 표시됩니다(예: 로그인 거부 535).

> 공개(Public) 저장소이면 `config/`(최저임금 보정 파일 포함)와 수집 데이터도 공개됩니다.

## 4. 설정 파일 고치기

| 하고 싶은 일 | 파일 |
|---|---|
| 수도·국가명·권역 수정, 국가 추가·삭제 | `config/countries.csv` (국가 수가 30개 미만이 되면 HR Insight 가 저장을 거부하므로 관리자와 상의) |
| 권역 표시 순서 | `src/profile_overlay.py` 의 `REGION_KEYS` (지금: 중국 · 유럽 · 미주 · 아태 · 중아 · 중남미) |
| 거점 추가·삭제 | `config/sites.csv` |
| 공식 기관 링크 추가 | `config/official_sources.csv` |
| 엉뚱한 나라 기사가 섞임 | `config/news_filters.json` 의 `exclude`·`reject`, `news_rules.json` 의 `foreign_story`(다른 나라·대기업 이름) |
| 기사가 너무 적게 잡힘 | `config/news_filters.json` 의 `aliases`·`query_name` |
| 최저임금 직접 입력 | `config/min_wage_overrides.csv` ([README_country_profile.md](README_country_profile.md) 5절) |
| 사설·칼럼·과거 회고·스포츠 제외어 | `config/news_rules.json` 의 `opinion`·`past_event`·`noise`·`non_news` |
| 광고성 매체 차단 | `config/news_rules.json` 의 `sources.blocked` |
| **신뢰 매체(먼저 노출)** | `config/news_rules.json` 의 `sources.trusted`(글로벌)·`sources.trusted_by_country`(나라별 주요 매체) |
| 주가·광고·개인 범죄 제외, 여파 기사 이동 | `config/news_rules.json` 의 `refine` |
| HR·노동 검색어·제외 문맥(군사 strike 등) | `config/news_rules.json` 의 `hr` |
| 안전 '실제 발생' 판단 | `config/news_rules.json` 의 `safety` |
| 경쟁사 목록 | `config/news_rules.json` 의 `company.competitors` |
| 주요 브리핑 건수 | `config/news_rules.json` 의 `top_briefing` |
| 주간 메일 섹션별 건수·정치/경제/사회 비율·제목 | `config/operations.json` 의 `weekly_mail.sections`(`safe`·`hr`·`pol`·`pol_mix`)·`subject` |

`config/` 를 main 에 올리면 화면이 자동으로 다시 만들어집니다(수집 없음). 수집 규칙(`news_rules.json`)은 **다음 수집부터** 적용됩니다.
`news_rules.json` 의 정규식이 잘못되면 그 줄만 무시하고 로그에 경고를 남깁니다.

## 5. 수집·선별 규칙

- **수집**: 국가 × 분류별 Google 뉴스 검색(RSS)을 하루 1회. 영어(제목에 국가명 필수)·한국어·현지어(HR, 16개국).
  기간: 안전 3일 · 정치·경제·사회 7일 · HR·업계 14일.
- **제외**: 사설·칼럼, 매거진·커뮤니티, 연예·스포츠·베팅, 주가·투자, 보도자료·광고성, 개인 범죄·단건 사고, 오래된 기사·지난 연도 이야기.
- **안전·재난**: 실제 발생한 사건만. 유형 무력충돌·테러 / 자연재해 / 대형사고(빨강) · 사회불안 / 보건·환경(주황).
  발언·전망 기사 제외, 다른 나라 사건의 여파 기사는 정치·경제로 이동.
- **HR·노동**: 노동 관련 단어 필수, 군사 문맥의 strike(공습) 제외.
- **나라 정확도**: 현지 언론이 옮겨 쓴 다른 나라 기사 제외, 다른 나라에서 일어난 일은 그 나라로 이동, 한국 제외.
- **신뢰 매체 우선**: 글로벌 통신사·나라별 주요 매체 기사를 먼저 고르고, 같은 사건이면 그 매체 기사를 대표로 씀.
- **번역**: 해외 기사 제목을 Google 번역(무료) + 보조 번역기로 번역, 품질 검사 후 반영·저장. 나라별로 번갈아 번역해 오래된 기사도 차례가 옴.

## 6. 화면·메일 표시 규칙

- **오늘 주요 브리핑**: 오늘·어제 기사 중 중요도 점수 상위 5건 (같은 분류·같은 나라 최대 2건, 번역된 기사만).
  점수 = 분류 기본점(안전 40 · HR 32 · 업계 26 · 정치·경제 20 · 완성차 18 · 사회·문화 10) + 심각한 재난 20 + 실제 피해 10 + 법·제도 15
  + 당사 언급 30 + 경쟁사 12 + 거점 인근 15 + 제조업 10 + **신뢰 매체 20** + 여러 매체·여러 나라 보도 + 본부·주요 공장 국가 6 + 최신성.
  나라 표시는 제목에 나오는 나라만.
- **같은 사건 묶기**: 제목이 비슷한 보도는 1건으로 묶고 "외 N개 매체"로 표시.
- **배지**: 분류(안전 유형·HR·정치·경제 등) 1개 · 경쟁사 · 법·제도.
- **번역 안 된 기사**: 로마자(영어·유럽어 등)는 "원문" 표시로 보이고, 비로마자(태국어·일본어·중국어·러시아어·아랍어 등)는 숨김.
- **기사 날짜**: MM.DD. 하단에 실제 수집 시각 표시.
- **주간 브리핑 메일** (매주 월요일 아침): 지난주 월~일, 안전·재난 5 · HR·노동 5(최저임금 → 법·제도 우선) · 정치·경제·사회 5(정치 2·경제 2·사회 1).
  번역 안 된 기사는 발송 직전에 다시 번역, 실패하면 다음 기사로 채움. 업계·경쟁사와 한국 기사는 넣지 않음.

## 7. 로컬 실행·테스트

```bash
pip install -r requirements.txt
pytest -q                                   # 단위 테스트
OFFLINE=1 python src/collector.py           # 네트워크 없이 흐름만 점검
python src/collector.py                     # 실제 수집
python src/build_site.py                    # 화면 생성
python -m http.server --directory docs      # http://localhost:8000
python -m country_profile monthly           # 국가 프로필 (README_country_profile.md)
python src/weekly_mail.py --dry-run         # 주간 메일 미리보기 → weekly_mail_preview.html (발송 안 함)
```

## 8. 알려진 한계

- **번역**: 무료 Google 번역 공개 엔드포인트라 직역 말투이고 일부 제목은 실패합니다. 회사 Vertex AI(Gemini) 연결을 검토 중입니다.
- **예약 실행**: GitHub 예약은 지연·누락이 잦아 외부 예약(cron-job.org)에 의존합니다. 토큰 만료일을 관리해야 합니다.
- **Trading Economics**: 웹 페이지 수집입니다. 이용약관 검토를 권하며, 수집기는 `country_profile/sources/monthly.py` 에서 교체할 수 있습니다.
- **현지어 HR 검색**: 16개국(`news_rules.json` → `hr.local`). 다른 나라를 추가하려면 같은 형식으로 넣습니다.
- **패키지 버전**: `requirements.txt` 는 메이저 버전 상한만 고정합니다. 실제 설치 버전은 Actions 로그 "Install" 단계에서 확인할 수 있습니다.
