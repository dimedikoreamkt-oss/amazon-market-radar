# 마켓 레이더 — 매일 자동 업데이트 설정 가이드

매일 **한국시간 오전 6시**에 아래 과정이 자동으로 돌아갑니다. PC를 켜 둘 필요는 없습니다.

```
GitHub Actions(무료 서버) → 아마존 API에서 데이터 수집 → 비밀번호로 암호화 → 팀 전용 웹사이트 갱신
```

- 사이트 주소는 `https://<깃허브아이디>.github.io/<저장소이름>/` 형태로 고정됩니다.
- 매출 데이터는 암호화되어 올라갑니다. 팀 비밀번호를 아는 사람만 브라우저에서 풀어 볼 수 있습니다.

---

## 0. 준비물 체크리스트

| # | 준비물 | 담당 | 걸리는 시간 |
|---|---|---|---|
| 1 | GitHub 계정 (무료) | 누구나 | 5분 |
| 2 | Amazon Ads API 승인 | 광고 계정 관리자 | 1~5 영업일 |
| 3 | Selling Partner API 앱 등록 (private, 자체 승인) | 셀러센트럴 주 계정 | 1~2주 |
| 4 | 상품 원가표, 순위표, 평점표 (구글 시트 3개) | 마케팅/경영관리 | 1시간 |

> 자세한 클릭 순서는 별도 「API 승인 신청 가이드」 문서를 보세요.
> 2번과 3번은 아마존 심사가 있어서 **오늘 바로 신청**하는 것이 가장 중요합니다.

---

## 1. 아마존 API 신청

### 1-1. Selling Partner API (판매, 세션, 재고, 검색어 점유율)
1. 셀러센트럴에서 **앱 및 서비스 → 앱 개발(Develop Apps)** 으로 이동합니다.
2. **개발자 프로필(Developer Profile)** 을 작성합니다. 용도 항목에서는 **"Private developer: 내 셀러 계정 데이터 분석용"** 을 고릅니다.
3. 역할(Role)은 아래를 체크합니다.
   - Selling Partner Insights
   - Brand Analytics
   - Inventory and Order Tracking
   - Product Listing
4. 승인이 나면 **앱 추가(Add new app client)** 를 누릅니다. 유형은 SP API로 정합니다.
5. 만든 앱에서 **Authorize(자체 승인)** 를 누르면 **Refresh Token** 이 나옵니다. → `SPAPI_REFRESH_TOKEN`
6. 같은 화면의 **LWA credentials** 에서 Client ID와 Client Secret을 복사합니다. → `LWA_CLIENT_ID`, `LWA_CLIENT_SECRET`

### 1-2. Amazon Ads API (광고비, 광고 매출, 검색어)
1. advertising.amazon.com 의 **Amazon Ads API 접근 신청** 양식을 제출합니다.
2. 광고 API는 SP-API와 **별도의 LWA 보안 프로필**(developer.amazon.com)을 씁니다. → `ADS_CLIENT_ID`, `ADS_CLIENT_SECRET`
3. 동의(consent) 과정을 거쳐 **Ads Refresh Token** 을 받습니다. → `ADS_REFRESH_TOKEN` (365일 후 만료 → 매년 재발급)
4. 광고 프로필 목록에서 **미국 마켓의 Profile ID** 를 확인합니다. → `ADS_PROFILE_ID`

> 공식 문서: Ads API 시작하기 https://advertising.amazon.com/API/docs/en-us/guides/get-started/overview
> SP-API 등록 https://developer-docs.amazon.com/sp-api/docs/registering-as-a-developer

---

## 2. 구글 시트 3개 만들기 (사람이 관리하는 값)

API로 받을 수 없는 값은 구글 시트에 적어 둡니다. 수집기가 매일 이 시트를 읽어 갑니다.

| 시트 | 열 (첫 줄 그대로) | 업데이트 주기 |
|---|---|---|
| **원가표** (필수) | `asin,short,key,name,cogs,fbaFee,leadtime,safety,inbound,refPct,launch` | 원가가 바뀔 때 |
| **순위표** | `keyword,date,rank,key,volume,target,spRank` | 매일 또는 매주 (Helium 10 / Jungle Scout 내보내기를 붙여넣기) |
| **평점표** | `asin,date,rating,reviews,theme1,share1,theme2,share2` | 매주 월요일 |

각 시트의 링크를 만드는 방법은 다음과 같습니다.
1. **파일 → 공유 → 웹에 게시**를 누릅니다.
2. 해당 시트를 고르고 **쉼표로 구분된 값(.csv)** 으로 게시합니다.
3. 나온 링크를 복사해 두면 4단계에서 씁니다.

⚠️ "웹에 게시"한 링크는 아는 사람 누구나 열 수 있습니다. 원가표는 민감할 수 있으니 **링크를 외부에 공유하지 마세요.** 링크는 GitHub Secret에만 넣습니다.

---

## 3. GitHub 저장소 만들기 (10분)

1. github.com 에서 **New repository** 를 누릅니다. 이름은 예를 들어 `market-radar` 로 하고, **Private** 으로 만듭니다.
   - Private 저장소에서 Pages를 쓰려면 유료(Pro/Team) 요금제가 필요합니다. 무료로 쓰려면 Public으로 만들어도 됩니다. 저장소에는 코드만 있고 매출 데이터는 암호화되어 사이트에만 올라가므로 Public이어도 괜찮습니다.
2. 이 폴더의 파일을 모두 올립니다. **Add file → Upload files** 로 올리면 되고, `.github/workflows/daily.yml` 도 포함해야 합니다.
3. **Settings → Pages → Source** 를 **GitHub Actions** 로 바꿉니다.

---

## 4. 비밀값(Secrets) 넣기

**Settings → Secrets and variables → Actions → New repository secret** 에서 하나씩 추가합니다.

| 이름 | 값 |
|---|---|
| `LWA_CLIENT_ID` | 1-1의 Client ID |
| `LWA_CLIENT_SECRET` | 1-1의 Client Secret |
| `SPAPI_REFRESH_TOKEN` | 1-1의 Refresh Token |
| `ADS_CLIENT_ID` | 광고용 LWA 보안 프로필 Client ID |
| `ADS_CLIENT_SECRET` | 광고용 LWA 보안 프로필 Client Secret |
| `ADS_REFRESH_TOKEN` | 1-2의 Ads Refresh Token |
| `ADS_PROFILE_ID` | 1-2의 Profile ID |
| `BRAND_NAME` | 브랜드명 (영문, 브랜드 검색어 구분에 씀) |
| `RADAR_PASSWORD` | 팀 열람 비밀번호 (10자 이상, 새로 정하기) |
| `COSTS_CSV` | 원가표 CSV 링크 |
| `RANKS_CSV` | 순위표 CSV 링크 (없으면 생략) |
| `REVIEWS_CSV` | 평점표 CSV 링크 (없으면 생략) |

> 비밀값은 GitHub이 암호화해서 보관합니다. 채팅, 메일, 엑셀에는 절대 붙여넣지 마세요.

---

## 5. 첫 실행과 확인

1. **Actions 탭 → daily-radar → Run workflow** 를 누릅니다.
2. 첫 실행은 **1~3시간** 걸릴 수 있습니다. 아마존 광고 리포트 생성이 느리기 때문입니다. 다음 날부터는 이미 받은 기간을 다시 받지 않아서 더 빨라집니다.
3. 초록 체크가 뜨면 `https://<아이디>.github.io/market-radar/` 에 들어가서 팀 비밀번호를 입력합니다.
4. 오른쪽 위 표시가 **"우리 브랜드 · 자동 업데이트 n시간 전"** 이면 성공입니다.
5. 빨간 X가 뜨면 실패한 단계의 로그를 복사해서 알려주세요. 대부분 권한(Role) 누락이나 오타입니다.

이후로는 **매일 오전 6시**에 자동으로 실행되고, 팀원은 같은 주소에서 새로고침만 하면 됩니다.

---

## 자주 묻는 것

- **"매일" 업데이트인데 왜 주간 단위로 보여요?**
  아마존 광고 매출은 클릭 후 7일 동안 귀속되고, 검색어 점유율(SQP)은 주간 리포트입니다. 매일 새 데이터를 받지만 판단은 완료된 주 기준으로 합니다. 하루 단위로 판단하면 잘못된 신호가 많아집니다.
- **비용은요?** GitHub Actions 무료 한도 안에서 돌아갑니다. 아마존 API도 무료입니다.
- **비밀번호를 바꾸려면?** `RADAR_PASSWORD` Secret을 바꾸고 Run workflow를 누르면 됩니다.
