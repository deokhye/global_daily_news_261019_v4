# 국가 기본 프로필 자동 수집 (country_profile)

한국타이어 Global HR팀 39개국 **국가 기본 프로필**(수도·총인구·GDP·인플레이션·실업률·최저임금·주요 거점)을
자동으로 수집·누적하고, 최신 프로필을 엑셀/JSON으로 내보내는 파이프라인입니다.

| 항목 | 갱신 | 출처 (우선순위) | 표기 예 |
|---|---|---|---|
| 수도 | 고정 | `config/countries.csv` | Seoul |
| 총인구 | 연 1회 (1/1) | IMF WEO `LP` → (없을 때만) World Bank `SP.POP.TOTL` | 51.6백만 명 |
| 연간 GDP 규모(명목) | 연 1회 (1/1) | IMF WEO `NGDPD` → World Bank `NY.GDP.MKTP.CD` | 1,872.3십억 USD |
| 인플레이션 (CPI YoY) | 월 1회 (매월 1일) | Trading Economics | 2.9% (2026.09) |
| 실업률 | 월 1회 (매월 1일) | Trading Economics | 9.5% (2026 Q2) |
| 최저임금 | 연 1회 (1/1) | 보정 파일 → Eurostat `earn_mw_cur` → ILOSTAT | KRW 10,320 / 시급 (전국, 2026-01-01 시행) |
| 한국타이어 주요 거점 | 고정 | `config/sites.csv` | Daejeon Plant (Daejeon) |

> 항목 순서·표기는 대시보드의 '국가 기본 프로필' 카드 순서와 요청서의 기본 형식을 따랐습니다.
> 요청서에서 말한 양식 이미지는 첨부되지 않아 확인하지 못했습니다. 이미지에 다른 항목이 있으면 알려 주세요.

---

## 1. 폴더 구조

```
(저장소 루트 — 국가 프로필 관련 부분만, 전체 구조는 README.md)
├── config/
│   ├── countries.csv           # [A] 국가·수도·TE slug (| 구분)
│   ├── sites.csv               # [B] 국가별 거점 (| 구분, 파일 순서대로 표시)
│   ├── country_codes.csv       # ISO2·통화·Eurostat 지역코드 (| 구분)
│   ├── min_wage_overrides.csv  # 최저임금 보정 파일 (쉼표 구분, HR 직접 입력)
│   └── settings.toml           # 기준연도·경고 임계치·대기시간 등 모든 기준값
├── country_profile/
│   ├── cli.py                  # 진입점 (annual / monthly / check-urls / save-samples / build)
│   ├── config.py
│   ├── fetchers/               # 수집기 인터페이스(base.py) + 예의 바른 HTTP 구현(http.py)
│   ├── parsers/tradingeconomics.py   # HTML 선택자는 전부 여기 한 곳
│   ├── sources/annual.py       # IMF / World Bank / Eurostat / ILOSTAT / 보정 파일
│   ├── sources/monthly.py      # 월간 출처 인터페이스 + Trading Economics 웹 구현
│   ├── pipeline_annual.py
│   ├── pipeline_monthly.py
│   ├── storage.py              # 이력 CSV 누적(같은 스냅샷만 덮어쓰기)
│   └── profile.py              # output/ 엑셀·JSON 생성
├── data/annual_history.csv     # 연간 이력 (long format, 누적)
├── data/monthly_history.csv    # 월간 이력 (long format, 누적)
├── output/country_profile.xlsx # 시트: 국가 기본 프로필 / 검토 필요 / 정보
├── output/country_profile.json
├── tests/                      # 파서·파이프라인·수집기 단위 테스트 + HTML 샘플
└── .github/workflows/profile-monthly.yml, profile-annual.yml
```

## 2. 설치

```bash
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
pytest -q
```

Python 3.11 이상이 필요합니다.

## 3. 수동 실행

```bash
python -m country_profile check-urls        # 최초 1회: TE 78개 URL이 열리는지 점검 (약 5분)
python -m country_profile monthly           # 인플레이션·실업률 → 스냅샷은 그달 1일로 기록
python -m country_profile annual            # 총인구·GDP·최저임금 (기준연도: settings 또는 실행 연도)
python -m country_profile annual --year 2025
python -m country_profile build             # 이력 CSV로 output/만 다시 생성
python -m country_profile save-samples --slug south-korea   # 파서 테스트용 실제 HTML 저장
```

실행이 끝나면 화면(그리고 GitHub Actions 실행 요약)에 **성공 국가, 실패 항목과 사유, 경고/검토 필요 목록**이 나옵니다.

GitHub Actions에서는 Actions 탭 → 워크플로 선택 → **Run workflow** 로 수동 실행합니다(기준연도/실행일 입력 가능).
스케줄: 월간 `0 0 1 * *`, 연간 `0 0 1 1 *` (UTC 00:00 = KST 09:00). 결과(`data/`, `output/`)는 저장소에 커밋됩니다.

## 4. 설정 파일 수정

- **국가 추가/수정**: `config/countries.csv`에 행 추가 + `config/country_codes.csv`에 ISO2·통화(·EU 대상이면 Eurostat 코드) 추가.
  Trading Economics slug는 `https://ko.tradingeconomics.com/{slug}/inflation-cpi` 주소의 `{slug}` 부분입니다.
- **거점 수정**: `config/sites.csv` — 같은 국가 안에서는 파일에 적힌 순서대로 표시됩니다.
- **기준값**: `config/settings.toml`
  - `base_year` 0 = 실행 연도 / `imf_weo_vintage` 출처 표기용 발표판 (예: "2026년 10월")
  - `override_stale_days` 보정 파일 확인일 유효기간 / `change_alert_pp` 급변 경고 기준(%p)
  - `user_agent` — **연락 가능한 실제 담당자 메일로 바꿔 주세요.**

## 5. 최저임금 보정 파일 작성법 (`config/min_wage_overrides.csv`)

엑셀에서 열어 편집 후 **CSV UTF-8(쉼표로 분리)** 으로 저장합니다.

| 열 | 의미 | 예 |
|---|---|---|
| iso3 | 국가 코드 | KOR |
| region | 기준 지역 (지역별로 다른 국가는 필수) | 전국 / 상하이시 / 도쿄도 / 연방 |
| amount | 금액 — 출처가 발표한 숫자 그대로. **비우면 '법정 최저임금 없음'** | 10320 |
| currency | 통화 | KRW |
| unit | 시급 / 일급 / 월급 (출처 단위 그대로, 환산 금지) | 시급 |
| effective_date | 시행일 YYYY-MM-DD | 2026-01-01 |
| checked_date | HR이 공식 발표를 확인한 날 | 2026-10-19 |
| source_url | 정부 공식 발표 주소 | https://… |
| note | 비고 | 법정 최저임금 없음 — 단체협약으로 결정 |

- 같은 국가에 여러 행을 두면 **시행일이 오늘 이전인 것 중 가장 최근 행**을 씁니다. 다음 해 인상분을 미리 넣어 둘 수 있습니다.
- 지역마다 다른 국가(중국·인도네시아·베트남·일본·미국·캐나다·멕시코·태국)는 대표 지역 한 곳을 정해 `region`에 적어 주세요.
- 스웨덴·오스트리아·이탈리아·싱가포르·UAE·사우디 6개국은 '법정 최저임금 없음' 행을 미리 넣어 두었습니다.
  **확인일이 비어 있어 '검토 필요'로 표시되니, 확인 후 checked_date·source_url을 채워 주세요.**
- **'검토 필요'로 분류되는 경우**: 확인일이 비었거나 1년 넘게 지남 / 자동 출처 값이 실행 연도보다 이전 기준 / 다른 출처에 더 최근 시행일 값이 있음 / '없음'으로 입력했는데 자동 출처에 값이 있음.

## 6. 상태 표기

| 상태 | 의미 |
|---|---|
| 정상 | 이번 실행에서 수집 |
| 갱신 없음 | 기준 기간이 지난달 수집분과 같음 (분기·반기 발표 국가에서 흔함) |
| 대체 출처(World Bank) | IMF 값이 없어 World Bank 값을 사용 |
| 검토 필요: … | 최저임금 검토 규칙에 해당 |
| 미갱신 (사유; 직전 값 유지) | 수집 실패. 직전 값을 그대로 두고 **추정하지 않음** |
| 법정 최저임금 없음 | 보정 파일 기준 |
| 경고: 급변 ±x.x%p | 직전 값보다 `change_alert_pp` 넘게 변동 |

엑셀의 **'검토 필요' 시트**에 위 상태 중 확인이 필요한 항목이 모입니다.

## 7. 수집 매너

- robots.txt 확인(금지 경로·확인 불가 시 요청하지 않음), 요청 간 3~5초 무작위 대기, User-Agent 명시.
- 네트워크 오류·429·5xx는 최대 3회 지수 백오프 재시도.
- **403·캡차·봇 차단 페이지는 우회하지 않고** 실패로 기록하고 실행 요약에 경고를 남깁니다.
- Trading Economics 웹 페이지 대신 공식 API 등으로 바꾸려면 `sources/monthly.py`의 `MonthlySource` 인터페이스를
  구현한 클래스를 만들어 `cli.py`의 `cmd_monthly`에서 교체하면 됩니다.
- 참고: Trading Economics 이용약관이 자동 수집을 어떻게 다루는지는 사내에서 한 번 확인해 두시길 권합니다.

## 8. 확인된 사실과 검증이 필요한 가정

**확인함 (2026-10)**
- Trading Economics 한국어 페이지 구조 — 요약 표 헤더 `실제 / 이전 / 최고 / 최저 / 날짜 / 단위 / 업데이트 주기`,
  캘린더 행 `발표일 | 시각 | 지표명 | 기준기간(Sep, Q2…) | 실제 | 이전 | …`, 본문 첫 문장 "2026년 9월 …" / "2026년 2분기에 …".
  robots.txt는 일반 크롤러에 대해 지표 페이지를 막지 않음.
- IMF DataMapper 응답 `{"values": {"LP": {"KOR": {"2026": …}}}}`, World Bank·Eurostat(`earn_mw_cur`, JSON-stat, `2026-S1`) 형식.

**가정 (첫 실행에서 확인 필요)**
- 테스트용 HTML 샘플(`tests/fixtures/te_*.html`)은 위 구조를 옮겨 만든 **재구성본**입니다.
  첫 실행 뒤 `save-samples --slug south-korea`, `--slug morocco`로 실제 HTML을 저장해 교체하고 `pytest`를 다시 돌려 주세요.
- 캘린더 기준기간의 연도는 발표일로 추정합니다 (예: 2027-01 발표 "Dec" → 2026-12).
- ILOSTAT 주소 `rplumber.ilo.org/data/indicator/?id=EAR_4MMN_CUR_NB_A&ref_area={ISO3}&format=.csv`와
  열 이름(`classif1`=CUR_TYPE_LCU, `time`, `obs_value`)은 이 환경에서 접속이 안 돼 확인하지 못했습니다.
  첫 연간 실행에서 ILOSTAT 실패가 많으면 이 부분부터 확인하세요 (`sources/annual.py` 상단 주석).
- 봇 차단 판별 문구(`captcha`, `Just a moment...` 등)는 일반적인 패턴입니다.
- 기준연도 기본값이 실행 연도이므로, 1월 1일 실행 시 IMF 값은 해당 연도 **추정치(WEO 전망)** 입니다.
  확정치가 필요하면 `settings.toml`의 `base_year`를 전년도로 지정하세요.
- Eurostat/ILOSTAT 최저임금은 월 단위·자국통화(`country_codes.csv`의 통화)로 저장합니다.

## 9. 첫 실행 결과 (개발 환경, 2026-10-19)

개발 환경은 외부 사이트 접속이 막혀 있어 **모든 항목이 '미갱신(네트워크 오류)'으로 기록**되었습니다.
실패한 항목에 값을 지어내지 않고 사유와 함께 남기는지 확인하는 용도입니다. 실제 값은 GitHub Actions 첫 실행에서 채워집니다.
`sample_run/` 폴더에 그 결과(실행 요약, 엑셀, JSON)를 넣어 두었습니다.

- 월간: 78건 모두 실패 — 사유 `robots.txt 확인 불가`(robots.txt를 확인하지 못하면 요청하지 않음)
- 연간: 총인구·GDP 78건 실패(IMF·World Bank 네트워크 오류), 최저임금 33개국 실패(보정 파일 없음 + 출처 접속 불가),
  6개국 '법정 최저임금 없음'(확인일 미입력 → 검토 필요)
- 실제 환경 첫 실행에서 예상되는 빈칸: 대만 총인구·GDP는 World Bank에 없어 IMF가 실패하면 미갱신,
  최저임금은 보정 파일을 채우기 전까지 Eurostat 대상국(EU·영국·튀르키예·세르비아 등) 외에는 ILOSTAT에만 의존합니다.
