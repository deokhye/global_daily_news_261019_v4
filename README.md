# Global HR Insight

한국타이어 해외 사업장 39개국의 **HR·노동, 현지 정세, 경제지표, 완성차·타이어 동향**을 매일 자동 수집해
한 화면에서 보여 주는 대시보드입니다. GitHub Actions 로 수집하고 GitHub Pages 로 배포합니다.

| 화면 | 주소 | 내용 |
|---|---|---|
| 오늘의 브리핑 | `#/briefing` (첫 화면) | 조직(권역) 카드 · 현지 안전 동향(국가별 1건) · HR·노동 주요 소식(국가별 1건, 법·제도 우선) · 글로벌 완성차·타이어 동향 |
| 국가 상세 | `#/country/DE` | 권역 탭·국가 칩 · 국가 개요/거점 · 환율·물가·실업률·최저임금 · 환율 계산기 · HR·노동 · 현지 주요 뉴스(정치·경제 / 사회·문화 / 안전·재난) · 완성차·타이어 · 공식 기관 |

---

## 1. 폴더 구조

```
├── config/                      ← 사람이 직접 관리하는 설정 (코드 수정 없이 바꿀 수 있음)
│   ├── countries.csv            국가·권역·영문명·ISO3·수도(한글)·Trading Economics slug
│   ├── country_codes.csv        ISO3 → ISO2·통화·Eurostat 코드
│   ├── sites.csv                한국타이어 거점
│   ├── official_sources.csv     국가별 공식 기관·보고서 바로가기 (ALL=전체, EU=EU 회원국 공통)
│   ├── manual_profile.json      자동 수집 전/실패 시 쓰는 보조 지표(인구·GDP·물가·실업률·최저임금)
│   ├── news_filters.json        국가별 뉴스 별칭·제외어·거부 정규식·검색용 국가명
│   ├── min_wage_overrides.csv   최저임금 보정 파일 (HR 이 정부 발표 확인 후 입력)
│   └── settings.toml            국가 프로필 수집 기준값
├── src/
│   ├── collector.py             매일 뉴스·환율 수집 → data/, docs/archive/
│   ├── profile_overlay.py       config + 프로필 값 조립 (collector·build_site 공용)
│   └── build_site.py            data + template → docs/index.html
├── country_profile/             국가 기본 프로필 수집 (월간·연간) → output/
├── template/template.html       화면 (HTML·CSS·JS 한 파일)
├── tests/                       단위 테스트 (pytest)
├── data/                        수집 결과 (자동 생성·커밋)
├── docs/archive/                날짜별 아카이브 180일 (자동 생성·커밋)
├── output/                      국가 프로필 결과·엑셀 (자동 생성·커밋)
└── .github/workflows/           자동 실행 4종
```

## 2. 자동 실행

| 워크플로 | 언제 | 하는 일 |
|---|---|---|
| **Daily Global Brief Update** (`update.yml`) | 매일 KST 05:20 (백업 05:50) | `collect`: 뉴스·환율 수집 → 화면 생성 → 데이터 커밋 → 배포 |
| | main 에 `src/`·`template/`·`config/` 변경 push | `build`: 화면만 다시 생성 → 배포 (수집 없음) |
| | 수동 실행 | `collect` 또는 `build` 선택 |
| **Country Profile — Monthly** | 매월 1일 KST 09:00 | 인플레이션·실업률 수집 → `output/` 커밋 → 대시보드 `build` 실행 |
| **Country Profile — Annual** | 매년 1월 1일 KST 09:00 | 총인구·GDP·최저임금 수집 → `output/` 커밋 → 대시보드 `build` 실행 |
| **Dev Check** | main 이외 브랜치 push, main 대상 PR | 테스트 + 저장된 데이터로 화면 생성(1~2분). 수동 실행에서 "실제 수집까지 점검"을 켜면 수집도 점검 |

- 예약 실행은 **기본 브랜치(main)의 워크플로 파일** 기준으로만 돕니다.
- GitHub 예약 실행은 수십 분 늦게 시작될 수 있습니다. 백업 실행은 그날 데이터가 없을 때만 수집합니다.
- 수집 단계는 시간 예산(환율 3분·뉴스 8분·번역 4분)을 넘기면 남은 작업을 직전 값으로 대체해 20분 안에 끝납니다.

## 3. 새 저장소 설정 순서

1. 이 폴더의 내용 전체를 저장소 **루트**에 올립니다 (`.github`, `.gitignore` 숨김 파일 포함).
2. **Settings → Pages → Source: GitHub Actions**
3. **Settings → Actions → General → Workflow permissions: Read and write permissions**
4. (기존 저장소에서 옮기는 경우, 선택) 과거 자료를 이어 쓰려면 기존 저장소의
   `docs/archive/` 폴더와 `data/translation_cache.json` 을 같은 위치에 복사합니다.
5. **Actions → Daily Global Brief Update → Run workflow (mode: collect)** — 첫 데이터 생성
6. **Actions → Country Profile — Annual → Run workflow**, 이어서 **Monthly → Run workflow**
7. Settings → Pages 에 표시되는 주소(`https://<아이디>.github.io/<저장소>/`)로 접속

> 공개(Public) 저장소이면 `config/`(최저임금 보정 파일 포함)와 수집 데이터도 공개됩니다.

## 4. 설정 파일 고치기

| 하고 싶은 일 | 파일 |
|---|---|
| 수도·국가명·권역 수정 | `config/countries.csv` |
| 거점 추가·삭제 | `config/sites.csv` (국가 안에서는 파일 순서대로 표시) |
| 공식 기관 링크 추가 | `config/official_sources.csv` — **현재 목록은 초안이니 각국 담당자 확인 필요** |
| 엉뚱한 나라 기사가 섞임 | `config/news_filters.json` 의 해당 국가 `exclude`(검색 제외어)·`reject`(제목 거부 정규식) |
| 기사가 너무 적게 잡힘 | `config/news_filters.json` 의 `aliases`(제목에 있어야 하는 이름) 추가, `query_name`(검색용 영문명) |
| 최저임금 직접 입력 | `config/min_wage_overrides.csv` ([README_country_profile.md](README_country_profile.md) 5절) |
| 자동 수집 전 보조 지표 | `config/manual_profile.json` |

`config/` 를 main 에 올리면 화면이 자동으로 다시 만들어집니다(수집 없음).

## 5. 화면 표시 규칙

- **현지 안전 동향 분류**: 무력충돌·테러 / 자연재해 / 대형사고 (빨강) · 사회불안 / 보건·환경 (주황).
  제목에 해당 단어가 있어야 채택하며, 훈련·기념일·영화 등은 제외합니다. 공식 경보가 아닌 **언론 보도**입니다.
- **연예·가십 기사**는 모든 분류에서 제외합니다 (`collector.py` `GOSSIP_RE`, 화면에서도 한 번 더 거름).
- **기사 개수(최대)**: 정치·경제 4, 사회·문화 3, 안전·재난 3, HR·노동 4, 완성차·타이어 4 (`NEWS_CATEGORIES`, `INDUSTRY_TOPICS`).
- **지표 출처**: 자동 수집 값은 IMF / Trading Economics / Eurostat·ILOSTAT, 최저임금 보정 파일 값은 "정부 공식 발표",
  법정 최저임금이 없는 나라는 "각국 노동법 기준". 환율은 "MM.DD 종가 기준 · 출처: Yahoo Finance(링크)".
- **기사 날짜**: 오늘 / 어제 / N일 전, 7일 넘은 기사는 흐리게.

## 6. 로컬 실행·테스트

```bash
pip install -r requirements.txt
pytest -q                                   # 단위 테스트
OFFLINE=1 python src/collector.py           # 네트워크 없이 흐름만 점검
python src/collector.py                     # 실제 수집
python src/build_site.py                    # 화면 생성
python -m http.server --directory docs      # http://localhost:8000
python -m country_profile monthly           # 국가 프로필 (README_country_profile.md)
```

## 7. 알려진 한계

- **번역**: Google 번역 공개 엔드포인트를 사용합니다. 막히면 MyMemory(일일 한도 있음)로 넘어가고, 그것도 실패하면 영문 원문이 표시됩니다.
- **Trading Economics**: 웹 페이지 수집입니다. 이용약관 검토를 권하며, 수집기는 `country_profile/sources/monthly.py` 에서 교체할 수 있습니다.
- **패키지 버전**: `requirements.txt` 는 메이저 버전 상한만 고정합니다. 실제 설치 버전은 Actions 로그 "Install" 단계에서 확인할 수 있습니다.
