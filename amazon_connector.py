#!/usr/bin/env python3
"""
마켓 레이더 v2 — 아마존 데이터 수집기
====================================
Amazon Ads API(v3)와 Selling Partner API에서 주간 데이터를 받고,
순위·리뷰·원가 CSV를 합쳐 대시보드가 읽는 radar_data.json 한 개로 만듭니다.

실행 예
  python amazon_connector.py --weeks 13 --costs costs.csv --ranks ranks.csv --reviews reviews.csv
  python amazon_connector.py --templates         # 입력 CSV 양식 3종을 만들어 줌
  python amazon_connector.py --selftest          # API 없이 변환 로직만 점검
  python amazon_connector.py ... --encrypt --out radar_data.enc.json   # 매일 자동 실행(GitHub Actions)용

권장: 매주 월요일 오전(한국시간) 실행. 검색어 리포트는 65일만 보관되므로 원본(raw/)을 쌓아 두세요.

환경변수 (서버 비밀 저장소에만 — 코드·HTML에 넣지 마세요)
  LWA_CLIENT_ID, LWA_CLIENT_SECRET     SP-API 앱의 LWA 자격증명 (Solution Provider Portal / Develop Apps)
  ADS_CLIENT_ID, ADS_CLIENT_SECRET     광고 API용 LWA 보안 프로필 (developer.amazon.com — SP-API와 별개)
  ADS_REFRESH_TOKEN, ADS_PROFILE_ID    Amazon Ads API (2026-07-30 이후 발급 토큰은 365일 후 만료 → 매년 재발급)
  ADS_REGION   NA | EU | FE (기본 NA)
  SPAPI_REFRESH_TOKEN                  Selling Partner API (셀러 자체 승인 private app)
  SPAPI_REGION NA | EU | FE (기본 NA)
  MARKETPLACE_ID  미국 ATVPDKIKX0DER (기본)
  BRAND_NAME      표시용 브랜드명 (브랜드 검색어 판별에도 사용)
  RADAR_PASSWORD  --encrypt 사용 시 팀 공용 열람 비밀번호 (10자 이상)

입력 CSV (UTF-8, 첫 줄 머리글)
  costs.csv   asin,short,key,cogs,fbaFee,leadtime,safety,inbound[,refPct][,launch]
              key = 상품군 코드. 캠페인 이름에 이 코드나 short가 들어가면 상품별로 연결됩니다.
  ranks.csv   keyword,date,rank[,key][,volume][,target]   ← Helium 10 / Jungle Scout 순위 내보내기
              date는 YYYY-MM-DD. 같은 주의 값은 평균. 자연(Organic) 순위만.
  reviews.csv asin,date,rating,reviews[,theme1,share1,theme2,share2,...]
              주 1회 Seller Central(또는 도구)에서 기록한 평점·평가 수.

참고 문서
  Ads 리포트 v3  https://advertising.amazon.com/API/docs/en-us/guides/reporting/v3/get-started
  SP-API 리포트  https://developer-docs.amazon.com/sp-api/docs/report-type-values-analytics
  SP-API는 LWA 액세스 토큰만으로 호출합니다(2023-10부터 AWS 서명 불필요).

주의
  - 자연 검색 순위는 아마존 공식 API에 없습니다 → ranks.csv
  - 평점 추이는 공식 API로 안정적으로 받기 어렵습니다 → reviews.csv
  - SQP(검색 쿼리 성과) 응답 필드명은 아마존 스키마 버전에 따라 다를 수 있어 여러 이름을 시도합니다.
    첫 실행 후 raw/sqp_*.json을 열어 필드명을 확인하세요.

의존성: pip install requests cryptography
"""
import argparse, csv, datetime as dt, gzip, json, os, sys, time
from collections import defaultdict

LWA_URL = "https://api.amazon.com/auth/o2/token"
ADS_HOST = {"NA": "https://advertising-api.amazon.com", "EU": "https://advertising-api-eu.amazon.com", "FE": "https://advertising-api-fe.amazon.com"}
SP_HOST = {"NA": "https://sellingpartnerapi-na.amazon.com", "EU": "https://sellingpartnerapi-eu.amazon.com", "FE": "https://sellingpartnerapi-fe.amazon.com"}
RAW = "raw"

# ---------------------------------------------------------------- 공통
def env(k, default=None, required=True):
    v = os.getenv(k, default)
    if required and not v:
        sys.exit(f"[설정 오류] 환경변수 {k} 가 비어 있습니다.")
    return v

def f(x):
    try: return float(str(x).replace(",", "").replace("$", "").replace("%", ""))
    except (TypeError, ValueError): return 0.0

def save_raw(name, obj):
    os.makedirs(RAW, exist_ok=True)
    with open(os.path.join(RAW, name), "w", encoding="utf-8") as fp:
        json.dump(obj, fp, ensure_ascii=False)

def load_cache(name, end):
    """끝난 지 15일이 지난 기간의 리포트는 다시 받지 않음 (광고 귀속 확정 이후). 매일 실행해도 API 호출이 늘지 않음."""
    pth = os.path.join(RAW, name)
    try:
        if end and dt.date.fromisoformat(str(end)[:10]) <= dt.date.today() - dt.timedelta(days=15) and os.path.exists(pth):
            with open(pth, encoding="utf-8") as fp: d = json.load(fp)
            print(f"  · 캐시 사용 {name}")
            return d["tsv"] if isinstance(d, dict) and set(d) == {"tsv"} else d
    except (ValueError, OSError, json.JSONDecodeError): pass
    return None

def encrypt_json(text, password, iters=310000):
    """AES-256-GCM + PBKDF2-SHA256. 대시보드가 브라우저 WebCrypto로 같은 방식으로 풉니다."""
    import base64, secrets
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
    if len(password) < 10: sys.exit("[설정 오류] RADAR_PASSWORD는 10자 이상으로 정하세요.")
    salt, iv = secrets.token_bytes(16), secrets.token_bytes(12)
    key = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=salt, iterations=iters).derive(password.encode())
    ct = AESGCM(key).encrypt(iv, text.encode("utf-8"), None)
    b = lambda x: base64.b64encode(x).decode()
    return {"v": 1, "kdf": "PBKDF2-SHA256", "iter": iters, "salt": b(salt), "iv": b(iv), "ct": b(ct),
            "generatedAt": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")}

def write_out(out, path, encrypt):
    text = json.dumps(out, ensure_ascii=False, indent=None if encrypt else 1)
    if encrypt: text = json.dumps(encrypt_json(text, env("RADAR_PASSWORD")))
    with open(path, "w", encoding="utf-8") as fp: fp.write(text)

def read_csv(path):
    """로컬 CSV 경로 또는 URL(구글 시트 '웹에 게시 → CSV' 링크 등)"""
    if not path: return []
    if str(path).startswith(("http://", "https://")):
        import io, requests
        r = requests.get(path, timeout=60); r.raise_for_status()
        fp = io.StringIO(r.content.decode("utf-8-sig"))
    else:
        fp = open(path, encoding="utf-8-sig")
    with fp:
        return [{k.strip(): (v or "").strip() for k, v in r.items() if k} for r in csv.DictReader(fp)]

def week_start(d):
    """일요일 시작 주 (아마존 브랜드 분석 주간 기준과 동일)"""
    if isinstance(d, str): d = dt.date.fromisoformat(d[:10])
    return d - dt.timedelta(days=(d.weekday() + 1) % 7)

def week_list(n, today=None):
    last = week_start((today or dt.date.today()) - dt.timedelta(days=7))   # 마지막 '완료된' 주
    return [last - dt.timedelta(weeks=n - 1 - i) for i in range(n)]

def backoff(fn, tries=8):
    delay = 2
    for _ in range(tries):
        r = fn()
        if r.status_code in (429, 500, 502, 503, 504):
            time.sleep(delay); delay = min(delay * 2, 120); continue
        return r
    r.raise_for_status()

def lwa_token(refresh_token, cid=None, secret=None):
    import requests
    r = requests.post(LWA_URL, data={"grant_type": "refresh_token", "refresh_token": refresh_token,
                                      "client_id": cid or env("LWA_CLIENT_ID"), "client_secret": secret or env("LWA_CLIENT_SECRET")}, timeout=30)
    r.raise_for_status()
    return r.json()["access_token"]

# ---------------------------------------------------------------- Amazon Ads API
class Ads:
    def __init__(self):
        self.host = ADS_HOST[env("ADS_REGION", "NA", False)]
        # 광고 API는 SP-API와 별도의 LWA 보안 프로필을 씁니다 (ADS_CLIENT_ID/SECRET). 없으면 LWA_* 사용
        cid = os.getenv("ADS_CLIENT_ID") or env("LWA_CLIENT_ID"); sec = os.getenv("ADS_CLIENT_SECRET") or env("LWA_CLIENT_SECRET")
        self.h = {"Amazon-Advertising-API-ClientId": cid, "Authorization": f"Bearer {lwa_token(env('ADS_REFRESH_TOKEN'), cid, sec)}",
                  "Amazon-Advertising-API-Scope": env("ADS_PROFILE_ID"), "Content-Type": "application/vnd.createasyncreportrequest.v3+json"}

    def report(self, name, ad_product, report_type, group_by, columns, start, end, time_unit="SUMMARY"):
        import requests
        hit = load_cache(f"ads_{report_type}_{start}_{end}.json", end)
        if hit is not None: return hit
        body = {"name": name, "startDate": start, "endDate": end,
                "configuration": {"adProduct": ad_product, "groupBy": group_by, "columns": columns,
                                  "reportTypeId": report_type, "timeUnit": time_unit, "format": "GZIP_JSON"}}
        r = backoff(lambda: requests.post(f"{self.host}/reporting/reports", headers=self.h, json=body, timeout=60))
        if r.status_code == 425:   # 동일 요청 진행 중 → 기존 reportId 재사용
            rid = r.json().get("detail", "").split(":")[-1].strip()
        else:
            r.raise_for_status(); rid = r.json()["reportId"]
        print(f"  · {name} 요청 ({rid}) — 생성에 최대 3시간")
        while True:
            s = backoff(lambda: requests.get(f"{self.host}/reporting/reports/{rid}", headers=self.h, timeout=60)).json()
            if s["status"] == "COMPLETED":
                data = json.loads(gzip.decompress(requests.get(s["url"], timeout=120).content))
                save_raw(f"ads_{report_type}_{start}_{end}.json", data); return data
            if s["status"] == "FAILED": raise RuntimeError(f"{name} 실패: {s.get('failureReason')}")
            time.sleep(30)

# ---------------------------------------------------------------- Selling Partner API
class SP:
    def __init__(self):
        self.host = SP_HOST[env("SPAPI_REGION", "NA", False)]
        self.h = {"x-amz-access-token": lwa_token(env("SPAPI_REFRESH_TOKEN")), "Content-Type": "application/json"}
        self.mp = env("MARKETPLACE_ID", "ATVPDKIKX0DER", False)

    def report(self, report_type, start=None, end=None, options=None, tag=""):
        import requests
        if tag and tag != "now" and end:
            hit = load_cache(f"sp_{report_type}_{tag}.json", end)
            if hit is not None: return hit
        body = {"reportType": report_type, "marketplaceIds": [self.mp]}
        if start: body["dataStartTime"] = start
        if end: body["dataEndTime"] = end
        if options: body["reportOptions"] = options
        r = backoff(lambda: requests.post(f"{self.host}/reports/2021-06-30/reports", headers=self.h, json=body, timeout=60))
        r.raise_for_status(); rid = r.json()["reportId"]
        print(f"  · {report_type} {tag} 요청 ({rid})")
        while True:
            s = backoff(lambda: requests.get(f"{self.host}/reports/2021-06-30/reports/{rid}", headers=self.h, timeout=60)).json()
            if s["processingStatus"] == "DONE": break
            if s["processingStatus"] in ("CANCELLED", "FATAL"):
                raise RuntimeError(f"{report_type} 실패({s['processingStatus']}) — 권한(Role)·기간·브랜드 등록 여부 확인")
            time.sleep(20)
        d = backoff(lambda: requests.get(f"{self.host}/reports/2021-06-30/documents/{s['reportDocumentId']}", headers=self.h, timeout=60)).json()
        raw = requests.get(d["url"], timeout=120).content
        if d.get("compressionAlgorithm") == "GZIP": raw = gzip.decompress(raw)
        txt = raw.decode("utf-8", errors="replace")
        try: out = json.loads(txt)
        except json.JSONDecodeError: out = txt
        save_raw(f"sp_{report_type}_{tag or 'x'}.json", out if not isinstance(out, str) else {"tsv": out})
        return out

def tsv(txt):
    lines = [l for l in txt.splitlines() if l.strip()]
    if not lines: return []
    head = lines[0].split("\t")
    return [dict(zip(head, l.split("\t"))) for l in lines[1:]]

# ---------------------------------------------------------------- 변환 (API와 분리 — selftest 가능)
def match_key(name, costs):
    nm = name.lower()
    for c in costs:
        for cand in (c.get("key"), c.get("short")):
            if cand and cand.lower() in nm: return c["key"]
    return "brand"

def build_asins(weeks, sales_by_week, ads_daily, costs, reviews, inventory, price_fallback):
    """sales_by_week: {weekISO: {asin: {sessions, units, sales, buyBox}}}
       ads_daily: [{date, asin, cost, sales}]  (spAdvertisedProduct DAILY)"""
    W = [w.isoformat() for w in weeks]
    ad_w = defaultdict(lambda: defaultdict(lambda: [0.0, 0.0]))
    for r in ads_daily:
        wk = week_start(r["date"]).isoformat()
        ad_w[r["asin"]][wk][0] += f(r.get("cost")); ad_w[r["asin"]][wk][1] += f(r.get("sales"))
    rev = defaultdict(dict); themes = {}
    for r in reviews:
        rev[r["asin"]][week_start(r["date"]).isoformat()] = (f(r["rating"]), f(r["reviews"]))
        th = [(r[f"theme{i}"], f(r.get(f"share{i}"))) for i in range(1, 6) if r.get(f"theme{i}")]
        if th: themes[r["asin"]] = [[t, round(s)] for t, s in th]
    out = []
    for c in costs:
        a = c["asin"]; inv = inventory.get(a, {})
        def series(k, default=0.0):
            return [f(sales_by_week.get(w, {}).get(a, {}).get(k, default)) for w in W]
        sessions, units, sales, bb = series("sessions"), series("units"), series("sales"), series("buyBox", 0)
        # 평점·리뷰 수: 비어 있는 주는 직전 값으로 채움
        rt, rc, last = [], [], (None, None)
        for w in W:
            last = rev[a].get(w, last); rt.append(last[0]); rc.append(last[1])
        first = next(((x, y) for x, y in zip(rt, rc) if x is not None), (0, 0))
        rt = [x if x is not None else first[0] for x in rt]; rc = [int(y) if y is not None else int(first[1]) for y in rc]
        tot_u, tot_s = sum(units[-4:]), sum(sales[-4:])
        price = round(tot_s / tot_u, 2) if tot_u else price_fallback.get(a, 0)
        item = {"asin": a, "name": c.get("name") or inv.get("product-name", a)[:40], "short": c.get("short") or a, "key": c["key"],
                "price": price, "cogs": f(c.get("cogs")), "fbaFee": f(c.get("fbaFee")) or f(inv.get("estimated-fee-total")) or 0,
                "leadtime": int(f(c.get("leadtime")) or 45), "safety": int(f(c.get("safety")) or 14),
                "fbaStock": int(f(inv.get("available"))), "inbound": int(f(c.get("inbound")) or f(inv.get("inbound-quantity"))),
                "weeks": {"sessions": [int(x) for x in sessions], "units": [int(x) for x in units], "sales": [round(x) for x in sales],
                          "rating": rt, "reviews": rc, "buyBox": [round(x, 1) for x in bb], "price": [price] * len(W),
                          "adSpend": [round(ad_w[a][w][0]) for w in W], "adSales": [round(ad_w[a][w][1]) for w in W]}}
        if c.get("refPct"): item["refPct"] = f(c["refPct"])
        if str(c.get("launch", "")).lower() in ("1", "true", "y", "yes"): item["launch"] = True
        if a in themes: item["reviewThemes"] = themes[a]
        out.append(item)
    return out

def build_keywords(weeks, ranks, sqp, brand):
    """ranks: [{keyword,date,rank,key,volume,target}],  sqp: {keyword: {volume, impr, click, purchase}} (최근 주)"""
    W = [w.isoformat() for w in weeks]
    by = defaultdict(lambda: defaultdict(list)); meta = {}
    for r in ranks:
        k = r["keyword"].strip().lower(); by[k][week_start(r["date"]).isoformat()].append(f(r["rank"]) or 101)
        m = meta.setdefault(k, {}); m.update({x: r[x] for x in ("key", "volume", "target", "spRank") if r.get(x)})
    out = []
    for k, wk in by.items():
        ser, last = [], None
        for w in W:
            if wk.get(w): last = round(sum(wk[w]) / len(wk[w]))
            ser.append(last)
        fv = next((x for x in ser if x is not None), 101); ser = [x if x is not None else fv for x in ser]
        q = sqp.get(k, {}); m = meta.get(k, {})
        is_brand = brand and brand.lower() in k
        item = {"keyword": k, "key": "brand" if is_brand else m.get("key", ""), "volume": int(f(m.get("volume")) or q.get("volume", 0)),
                "rank": ser, "spRank": int(f(m.get("spRank"))) or None, "target": int(f(m.get("target"))) or None}
        if q: item["sqp"] = {"impr": round(q["impr"], 2), "click": round(q["click"], 2), "purchase": round(q["purchase"], 2)}
        out.append(item)
    return out

def pick(d, *paths):
    for p in paths:
        n = d
        for k in p.split("."):
            n = n.get(k) if isinstance(n, dict) else None
        if n is not None: return n
    return None

def parse_sqp(doc):
    """GET_BRAND_ANALYTICS_SEARCH_QUERY_PERFORMANCE_REPORT → {query: {volume, impr, click, purchase}} (점유율 %)"""
    rows = doc.get("dataByAsin") or doc.get("dataByQuery") or doc.get("data") or [] if isinstance(doc, dict) else []
    agg = defaultdict(lambda: {"volume": 0, "impr": 0.0, "click": 0.0, "purchase": 0.0, "_n": 0})
    for r in rows:
        q = pick(r, "searchQueryData.searchQuery", "searchQuery")
        if not q: continue
        a = agg[q.lower()]; a["_n"] += 1
        a["volume"] = max(a["volume"], int(f(pick(r, "searchQueryData.searchQueryVolume", "searchQueryVolume"))))
        for k, paths in (("impr", ("impressionData.asinImpressionShare", "impressionData.brandImpressionShare")),
                         ("click", ("clickData.asinClickShare", "clickData.brandClickShare")),
                         ("purchase", ("purchaseData.asinPurchaseShare", "purchaseData.brandPurchaseShare"))):
            v = f(pick(r, *paths)); a[k] += v * 100 if v <= 1 else v   # 0~1 또는 % 모두 대응
    for a in agg.values(): a.pop("_n")
    return dict(agg)

def build_campaigns(sp_c, sb_c, sd_c, costs):
    def camp(rows, typ, sales_k, ord_k):
        out = []
        for r in rows:
            nm = r.get("campaignName", "")
            tgt = "AUTO" if ("auto" in nm.lower() or "자동" in nm) else ("AUDIENCE" if typ == "SD" else "PRODUCT" if ("asin" in nm.lower() or "경쟁" in nm) else "MANUAL")
            c = {"id": str(r["campaignId"]), "name": nm, "type": typ, "targeting": tgt, "key": match_key(nm, costs) if typ == "SP" else "brand",
                 "budget": f(r.get("campaignBudgetAmount")), "spend": f(r.get("cost")), "impr": int(f(r.get("impressions"))),
                 "clicks": int(f(r.get("clicks"))), "orders": int(f(r.get(ord_k))), "sales": f(r.get(sales_k)),
                 "budgetCappedDays": int(f(r.get("budgetCappedDays")))}   # 예산 소진은 Marketing Stream/예산 추천 API로 보강
            if typ != "SP" and f(r.get(sales_k)): c["ntbPct"] = round(f(r.get("newToBrandSales")) / f(r.get(sales_k)) * 100)
            if "런칭" in nm or "launch" in nm.lower(): c["launch"] = True
            out.append(c)
        return out
    return camp(sp_c, "SP", "sales7d", "purchases7d") + camp(sb_c, "SB", "sales", "purchases") + camp(sd_c, "SD", "sales", "purchases")

def build_terms(terms, brand):
    out = []
    for t in terms:
        term = t.get("searchTerm", "")
        mt = t.get("matchType") or ("AUTO" if str(t.get("keywordType", "")).startswith("TARGETING_EXPRESSION_PREDEFINED") else "PRODUCT")
        out.append({"term": term, "campaignId": str(t.get("campaignId")), "matchType": mt, "impr": int(f(t.get("impressions"))),
                    "clicks": int(f(t.get("clicks"))), "spend": f(t.get("cost")), "orders": int(f(t.get("purchases7d"))),
                    "sales": f(t.get("sales7d")), "brand": bool(brand and brand.lower() in term.lower())})
    return out

def build_health(perf, ipi=None):
    pm = (perf.get("performanceMetrics") or [{}])[0] if isinstance(perf, dict) else {}
    def rate(*path):
        n = pm
        for k in path: n = (n or {}).get(k)
        return round(f(n.get("rate")) * 100, 2) if isinstance(n, dict) and n.get("rate") is not None else None
    return {"orderDefectRate": rate("orderDefectRate", "afn"), "cancellationRate": rate("preFulfillmentCancellationRate"),
            "lateShipmentRate": rate("lateShipmentRate"), "validTrackingRate": rate("validTrackingRate"), "ipi": ipi,
            "accountHealthRating": ((pm.get("accountHealthRating") or {}).get("ahrStatus"))}

def validate(out):
    W = len(out["weeks"]); probs = []
    for a in out["asins"]:
        if sum(a["weeks"]["sessions"]) == 0: probs.append(f"{a['asin']}: 세션 데이터 없음 (ASIN 오타 또는 판매 없음)")
        if not a["cogs"]: probs.append(f"{a['asin']}: 원가(cogs) 없음 → 손익 부정확")
        if any(x == 0 for x in a["weeks"]["rating"]): probs.append(f"{a['asin']}: 평점 없음 → reviews.csv 확인")
    if not out["keywords"]: probs.append("키워드 순위 없음 → ranks.csv 필요 (순위 계획이 만들어지지 않음)")
    for p in probs: print("  [점검]", p)
    return probs

# ---------------------------------------------------------------- 수집
def collect(args):
    costs = read_csv(args.costs)
    brand = env("BRAND_NAME", "", False)
    weeks = week_list(args.weeks)
    S, E = weeks[0].isoformat(), (weeks[-1] + dt.timedelta(days=6)).isoformat()
    E28S = (weeks[-1] - dt.timedelta(days=21)).isoformat()
    use_ads = bool(os.getenv("ADS_REFRESH_TOKEN") and os.getenv("ADS_PROFILE_ID"))

    sp_c = sb_c = sd_c = terms = []
    ads_daily = []
    if use_ads:
        print(f"[1/3] Amazon Ads API  ({S} ~ {E})")
        ads = Ads()
        base = ["campaignId", "campaignName", "campaignBudgetAmount", "impressions", "clicks", "cost"]
        sp_c = ads.report("SP campaigns 28d", "SPONSORED_PRODUCTS", "spCampaigns", ["campaign"], base + ["purchases7d", "sales7d"], E28S, E)
        sb_c = ads.report("SB campaigns 28d", "SPONSORED_BRANDS", "sbCampaigns", ["campaign"], base + ["purchases", "sales", "newToBrandSales"], E28S, E)
        sd_c = ads.report("SD campaigns 28d", "SPONSORED_DISPLAY", "sdCampaigns", ["campaign"], base + ["purchases", "sales", "newToBrandSales"], E28S, E)
        terms = ads.report("SP search terms 28d", "SPONSORED_PRODUCTS", "spSearchTerm", ["searchTerm"],
                           ["searchTerm", "campaignId", "matchType", "keywordType", "impressions", "clicks", "cost", "purchases7d", "sales7d"], E28S, E)
        cur = weeks[0]
        while cur <= weeks[-1]:
            end = min(cur + dt.timedelta(days=27), weeks[-1] + dt.timedelta(days=6))
            rows = ads.report(f"SP advertised product {cur}", "SPONSORED_PRODUCTS", "spAdvertisedProduct", ["advertiser"],
                              ["date", "advertisedAsin", "cost", "sales7d"], cur.isoformat(), end.isoformat(), time_unit="DAILY")
            ads_daily += [{"date": r["date"], "asin": r["advertisedAsin"], "cost": r.get("cost"), "sales": r.get("sales7d")} for r in rows]
            cur = end + dt.timedelta(days=1)
    else:
        print("[1/3] Amazon Ads API — 건너뜀 (ADS_REFRESH_TOKEN/ADS_PROFILE_ID 없음 → 광고 화면은 비어 있음)")

    print("[2/3] Selling Partner API")
    sp = SP()
    sales_by_week, price_fb = {}, {}
    for w in weeks:   # 상품별 값은 기간 합계로만 나오므로 주마다 요청
        st = sp.report("GET_SALES_AND_TRAFFIC_REPORT", f"{w}T00:00:00Z", f"{w + dt.timedelta(days=6)}T23:59:59Z",
                       {"dateGranularity": "WEEK", "asinGranularity": "CHILD"}, tag=w.isoformat())
        m = {}
        for r in (st.get("salesAndTrafficByAsin", []) if isinstance(st, dict) else []):
            a = r.get("childAsin") or r.get("parentAsin"); sa, tr = r.get("salesByAsin", {}), r.get("trafficByAsin", {})
            m[a] = {"units": f(sa.get("unitsOrdered")), "sales": f((sa.get("orderedProductSales") or {}).get("amount")),
                    "sessions": f(tr.get("sessions")), "buyBox": f(tr.get("buyBoxPercentage"))}
        sales_by_week[w.isoformat()] = m
    if not costs:   # 원가표가 아직 없으면 판매 리포트에서 ASIN을 찾아 자동 구성 (원가 0 → 손익은 부정확)
        tot = defaultdict(float)
        for m in sales_by_week.values():
            for a, v in m.items(): tot[a] += v["sales"]
        top = [a for a, v in sorted(tot.items(), key=lambda x: -x[1]) if v > 0][:15]
        costs = [{"asin": a, "short": a[-5:], "key": a.lower(), "cogs": "", "fbaFee": "", "leadtime": "", "safety": ""} for a in top]
        print(f"  [안내] 원가표(COSTS_CSV)가 없어 매출 상위 ASIN {len(costs)}개로 자동 구성했습니다. 원가를 넣어야 이익 계산이 정확해집니다.")
    asin_list = [c["asin"] for c in costs]
    inv_raw = sp.report("GET_FBA_INVENTORY_PLANNING_DATA", tag="now")
    inventory = {r.get("asin"): r for r in tsv(inv_raw)} if isinstance(inv_raw, str) else {}
    for a, r in inventory.items(): price_fb[a] = f(r.get("your-price"))
    perf = sp.report("GET_V2_SELLER_PERFORMANCE_REPORT", tag="now")
    sqp = {}
    if not args.no_sqp:
        try:
            last = weeks[-1]
            doc = sp.report("GET_BRAND_ANALYTICS_SEARCH_QUERY_PERFORMANCE_REPORT", f"{last}T00:00:00Z", f"{last + dt.timedelta(days=6)}T23:59:59Z",
                            {"reportPeriod": "WEEK", "asin": " ".join(asin_list[:20])}, tag=last.isoformat())
            sqp = parse_sqp(doc)
        except Exception as e:
            print(f"  [건너뜀] SQP: {e}  (브랜드 등록·Brand Analytics 권한 필요)")

    print("[3/3] CSV 합치기 · 변환")
    out = {"meta": {"brand": brand or "우리 브랜드", "marketplace": sp.mp, "currency": "USD", "asOf": dt.date.today().isoformat(),
                    "generatedAt": dt.datetime.now().isoformat(timespec="seconds"), "sample": False},
           "weeks": [w.isoformat() for w in weeks],
           "asins": build_asins(weeks, sales_by_week, ads_daily, costs, read_csv(args.reviews), inventory, price_fb),
           "campaigns": build_campaigns(sp_c, sb_c, sd_c, costs), "searchTerms": build_terms(terms, brand),
           "keywords": build_keywords(weeks, read_csv(args.ranks), sqp, brand),
           "health": build_health(perf, int(args.ipi) if args.ipi else None)}
    validate(out)
    write_out(out, args.out, args.encrypt)
    print(f"완료 → {args.out}  (주 {len(weeks)}, 상품 {len(out['asins'])}, 캠페인 {len(out['campaigns'])}, 검색어 {len(out['searchTerms'])}, 키워드 {len(out['keywords'])})")

# ---------------------------------------------------------------- 양식 · 자체 점검
def templates():
    files = {
        "costs.csv": "asin,short,key,name,cogs,fbaFee,leadtime,safety,inbound,refPct,launch\nB0XXXXXXX1,세럼,serum,비타민C 세럼 30ml,6.5,4.1,45,14,0,,\n",
        "ranks.csv": "keyword,date,rank,key,volume,target,spRank\nvitamin c serum,2026-09-20,9,serum,14000,4,3\n",
        "reviews.csv": "asin,date,rating,reviews,theme1,share1,theme2,share2\nB0XXXXXXX1,2026-09-20,4.50,2140,효과 체감 느림,38,산화·변색,24\n",
    }
    for n, t in files.items():
        with open(n, "w", encoding="utf-8-sig") as fp: fp.write(t)
        print("작성:", n)

def selftest():
    weeks = week_list(4, dt.date(2026, 10, 1))
    W = [w.isoformat() for w in weeks]
    costs = [{"asin": "A1", "short": "세럼", "key": "serum", "cogs": "6.5", "fbaFee": "4.1", "leadtime": "45", "safety": "14"}]
    sbw = {w: {"A1": {"units": 100 + i, "sales": (100 + i) * 24.9, "sessions": 800, "buyBox": 99}} for i, w in enumerate(W)}
    ads_daily = [{"date": (weeks[0] + dt.timedelta(days=d)).isoformat(), "asin": "A1", "cost": 10, "sales": 30} for d in range(28)]
    reviews = [{"asin": "A1", "date": W[1], "rating": "4.4", "reviews": "100", "theme1": "누수", "share1": "30"}]
    ranks = [{"keyword": "Vitamin C Serum", "date": W[i], "rank": str(5 + i), "key": "serum", "volume": "14000"} for i in range(4)]
    sqp = parse_sqp({"dataByAsin": [{"searchQueryData": {"searchQuery": "vitamin c serum", "searchQueryVolume": 14000},
                                     "impressionData": {"asinImpressionShare": 0.068}, "clickData": {"asinClickShare": 0.061},
                                     "purchaseData": {"asinPurchaseShare": 0.074}}]})
    a = build_asins(weeks, sbw, ads_daily, costs, reviews, {"A1": {"available": "700"}}, {})[0]
    k = build_keywords(weeks, ranks, sqp, "luma")[0]
    c = build_campaigns([{"campaignId": 1, "campaignName": "SP · 세럼 · 자동", "cost": 100, "sales7d": 300, "purchases7d": 12}], [], [], costs)[0]
    assert a["weeks"]["units"] == [100, 101, 102, 103], a["weeks"]["units"]
    assert a["weeks"]["adSpend"] == [70, 70, 70, 70], a["weeks"]["adSpend"]
    assert a["weeks"]["rating"] == [4.4, 4.4, 4.4, 4.4] and a["reviewThemes"] == [["누수", 30]]
    assert k["rank"] == [5, 6, 7, 8] and abs(k["sqp"]["purchase"] - 7.4) < 1e-6
    assert c["key"] == "serum" and c["targeting"] == "AUTO"
    print("selftest 통과:", json.dumps({"asin": a["weeks"], "keyword": k, "campaign": c}, ensure_ascii=False)[:400], "…")

if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="마켓 레이더 v2 수집기")
    ap.add_argument("--weeks", type=int, default=13, help="수집 주 수 (최소 8, 권장 13)")
    ap.add_argument("--costs", default=os.getenv("COSTS_CSV"), help="상품 원가·리드타임 CSV 경로/URL (권장, 없으면 판매 상위 ASIN 자동 구성)")
    ap.add_argument("--ranks", default=os.getenv("RANKS_CSV"), help="키워드 자연 순위 CSV 경로/URL (환경변수 RANKS_CSV 가능)")
    ap.add_argument("--reviews", default=os.getenv("REVIEWS_CSV"), help="주간 평점·리뷰 수 CSV 경로/URL (환경변수 REVIEWS_CSV 가능)")
    ap.add_argument("--ipi", default=os.getenv("IPI_SCORE"), help="IPI 점수 (Seller Central 값)")
    ap.add_argument("--no-sqp", action="store_true", help="브랜드 분석 SQP 생략")
    ap.add_argument("--out", default="radar_data.json")
    ap.add_argument("--templates", action="store_true", help="입력 CSV 양식 만들기")
    ap.add_argument("--selftest", action="store_true", help="API 없이 변환 로직 점검")
    ap.add_argument("--encrypt", action="store_true", help="결과를 RADAR_PASSWORD로 암호화 (공개 웹사이트에 올릴 때 필수)")
    ap.add_argument("--encrypt-file", metavar="JSON", help="이미 만든 radar_data.json을 암호화만 해서 --out에 저장")
    args = ap.parse_args()
    if args.encrypt_file:
        with open(args.encrypt_file, encoding="utf-8") as fp: write_out(json.load(fp), args.out, True)
        print("암호화 완료 →", args.out)
    elif args.templates: templates()
    elif args.selftest: selftest()
    else: collect(args)
