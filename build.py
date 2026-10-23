#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
元大基金淨值與表現 — 資料抓取 + 自含 HTML 產生器
執行： python3 build.py   →  產生「元大基金淨值與表現.html」（資料內嵌、離線可看）

資料源（三個，各自官方）：
  - ETF（00990A / 00850）：Yahoo Finance 日線（市價；報酬用還原收盤 adjclose 算含息）
  - 共同基金（元大實質多重資產-新台幣 A1HCPBH）：鉅亨 fund.api 官方各期報酬
  - 全委帳戶（財富雙利 MDUSD0001 / 財富雙享 MDTWD0002）：鉅亨 co.cnyes YuantaLife（官方含撥回報酬 + 報酬指數走勢）
"""
import urllib.request, json, gzip, time, datetime, os, sys, csv, io

TPE = datetime.timezone(datetime.timedelta(hours=8))
TODAY = datetime.datetime.now(TPE).date()

def _get(url, headers=None, tries=4):
    h = {
        'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/151.0 Safari/537.36',
        'Accept': 'application/json, text/plain, */*',
        'Accept-Language': 'zh-TW,zh;q=0.9',
    }
    if headers:
        h.update(headers)
    last = None
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers=h)
            r = urllib.request.urlopen(req, timeout=30)
            data = r.read()
            if r.headers.get('Content-Encoding') == 'gzip' or data[:2] == b'\x1f\x8b':
                data = gzip.decompress(data)
            return data.decode('utf-8', 'replace')
        except Exception as e:
            last = e
            time.sleep(2.5 * (i + 1))
    raise last

def getj(url, headers=None, tries=4):
    return json.loads(_get(url, headers, tries))

def d2ts(d):
    return int(datetime.datetime(d.year, d.month, d.day, tzinfo=TPE).timestamp())

# ---------- 報酬計算工具（給 ETF 用日線算） ----------
def nearest_on_or_before(dates, target):
    """dates: 升冪 date list; 回傳 <=target 的最後一個 index，找不到回 None"""
    lo, hi, ans = 0, len(dates) - 1, None
    while lo <= hi:
        mid = (lo + hi) // 2
        if dates[mid] <= target:
            ans = mid; lo = mid + 1
        else:
            hi = mid - 1
    return ans

def pct(a, b):
    if a is None or b is None or b == 0:
        return None
    return round((a / b - 1) * 100, 2)

def horizon_returns(dates, vals):
    """dates 升冪、vals 對應（用還原淨值算含息報酬）。回傳 YTD/1M/3M/1Y/3Y。"""
    if not dates:
        return {}
    last_d, last_v = dates[-1], vals[-1]
    out = {}
    # YTD：去年最後一個交易日
    jan1 = datetime.date(last_d.year, 1, 1)
    idx = nearest_on_or_before(dates, jan1 - datetime.timedelta(days=1))
    out['ytd'] = pct(last_v, vals[idx]) if idx is not None else None
    for key, days in [('m1', 30), ('m3', 91), ('y1', 365), ('y3', 365 * 3)]:
        tgt = last_d - datetime.timedelta(days=days)
        if tgt < dates[0]:
            out[key] = None
            continue
        idx = nearest_on_or_before(dates, tgt)
        out[key] = pct(last_v, vals[idx]) if idx is not None else None
    return out

# ---------- 各源抓取 ----------
def fetch_etf(stk, zh, note='', start='2021-01-01'):
    # FinMind TaiwanStockPrice：穩定、無 Yahoo 限流問題（市價收盤，報酬為不含息之市價報酬）
    url = (f'https://api.finmindtrade.com/api/v4/data?dataset=TaiwanStockPrice'
           f'&data_id={stk}&start_date={start}')
    rows = getj(url).get('data', [])
    rows = [r for r in rows if r.get('close')]
    rows.sort(key=lambda r: r['date'])
    series, dates, vals = [], [], []
    for r in rows:
        d = datetime.date.fromisoformat(r['date'])
        series.append([r['date'], round(r['close'], 2)])
        dates.append(d); vals.append(r['close'])
    rets = horizon_returns(dates, vals)
    last_close = series[-1][1]
    prev_close = series[-2][1] if len(series) > 1 else None
    return {
        'name': zh, 'code': stk, 'kind': 'etf', 'currency': '新台幣',
        'nav': last_close, 'navLabel': '市價', 'navDate': series[-1][0],
        'change': round(last_close - prev_close, 2) if prev_close else None,
        'changePct': pct(last_close, prev_close),
        'inception': dates[0].isoformat() if dates else None,
        'returns': rets, 'series': series, 'seriesType': 'price',
        'seriesLabel': '市價走勢', 'source': 'FinMind（TWSE 市價；報酬為不含息之市價報酬）', 'note': note,
    }

def fetch_fund(fid, zh, note='', fresh=None):
    d = getj(f'https://fund.api.cnyes.com/fund/api/v1/funds/{fid}/nav?days=1500')['items']
    navs = d.get('nav') or []
    tds = d.get('tradeDate') or []
    pairs = sorted(zip(tds, navs))  # 升冪
    series = [[datetime.date.fromtimestamp(t).isoformat(), round(v, 4)] for t, v in pairs] if len(pairs) >= 2 else None
    nav = pairs[-1][1] if pairs else d.get('dayPrice')
    nav_date = (datetime.date.fromtimestamp(pairs[-1][0]).isoformat() if pairs
                else (datetime.date.fromtimestamp(d['priceDate']).isoformat() if d.get('priceDate') else None))
    prev = pairs[-2][1] if len(pairs) >= 2 else None
    rets = {
        'ytd': _r(d.get('returnYTD')), 'm1': _r(d.get('return1Month')),
        'm3': _r(d.get('return3Month')), 'y1': _r(d.get('return1Year')),
        'y3': _r(d.get('return3Year')),
    }
    has = series is not None
    result = {
        'name': zh, 'code': fid, 'kind': 'fund', 'currency': d.get('classCurrencyName') or '新台幣',
        'nav': round(nav, 2) if nav else None, 'navLabel': '淨值', 'navDate': nav_date,
        'change': round(nav - prev, 2) if (nav and prev) else None,
        'changePct': pct(nav, prev) if (nav and prev) else _r(d.get('return1Day')),
        'inception': datetime.date.fromtimestamp(d['inceptionDate']).isoformat() if d.get('inceptionDate') else None,
        'returns': rets, 'series': series, 'seriesType': 'nav' if has else 'bars',
        'seriesLabel': '淨值走勢（成立以來）' if has else '各期間報酬率',
        'source': '鉅亨 fund.api（晨星官方淨值/各期報酬）', 'note': note,
    }
    return _apply_fresh_nav(result, fid, fresh or {})

def fetch_carteblanche(code, zh, note=''):
    base = 'https://co.cnyes.com/api/YuantaLife/carteblanche'
    hd = {'Origin': 'https://co.cnyes.com', 'Referer': 'https://co.cnyes.com/'}
    pp = getj(f'{base}/periodPerformance/{code}', hd)['data']
    bi = getj(f'{base}/basicInfo/{code}', hd)['data']
    pf = getj(f'{base}/performance/{code}?months=0', hd)['data']['items']
    di, cr = bi['detailInfo'], bi['CarteblancheReport']
    # 報酬指數走勢（含撥回）：index = 100*(1+cumret/100)
    t, perf = pf['t'], pf['performance']
    pairs = sorted(zip(t, perf))
    series = []
    for ts, rp in pairs:
        d = datetime.datetime.fromtimestamp(ts, TPE).date()
        series.append([d.isoformat(), round(100 * (1 + rp / 100.0), 2)])
    rets = {
        'ytd': _r(pp.get('returnYTD')), 'm1': _r(pp.get('return1Month')),
        'm3': _r(pp.get('return3Month')), 'y1': _r(pp.get('return1Year')),
        'y3': _r(pp.get('return3Year')),
    }
    nav_date = di['tradeDate']
    nav_date = f'{nav_date[:4]}-{nav_date[4:6]}-{nav_date[6:]}' if nav_date else None
    return {
        'name': zh, 'code': code, 'kind': 'disc', 'currency': cr.get('currency') or di.get('currCode'),
        'nav': di['nav'], 'navLabel': '淨值', 'navDate': nav_date,
        'change': di.get('change'), 'changePct': di.get('changePercent'),
        'inception': cr.get('inceptionDate', '').replace('/', '-') or None,
        'returns': rets, 'series': series, 'seriesType': 'index',
        'seriesLabel': '含撥回報酬指數（基準 100）',
        'source': '鉅亨 co.cnyes（元大人壽全委官方，含撥回）', 'note': note,
    }

def _r(x):
    return round(x, 2) if isinstance(x, (int, float)) else None

# ---------- 官方每日淨值校正層（治 cnyes fund.api 落後） ----------
# 背景：cnyes fund.api 資料落後不是單純 GitHub 美國 runner IP 慢——2026-08-17 從本機直連
# 驗證，cnyes 自己的 origin 就卡在舊日期（updatedAt 剛 refresh 但 tradeDate 沒進）。
# 要開放給全公司同仁用，不能只靠「永不倒退」被動等 cnyes 追上，改用官方源校正「最新一筆」淨值。
# cnyes fund.api 仍保留，是唯一有長期歷史走勢＋官方 YTD/1M/3M/1Y/3Y 期間報酬的來源。
#
# 2026-08-19 debug：Evan 回報多重資產基金比元大投信官網慢 2+ 小時、「正常應該中午就有數字」。
# 查證：SITCA CSV／fundclear 兩個法定揭露源當天下午 17:30~18:00 台北才把前一交易日淨值放出來，
# 元大投信自家網站（www.yuantafunds.com）比這兩個產業彙整源快。用 Playwright 掛網也發現該站
# 個別基金頁會呼叫 api.yuantafunds.com/ectranslation/api/trans?...&FuncId=FundNAV&FundId={id}，
# 直接回傳逐日 NAV 序列。原以為此站是「FUND_ID+FuncId+PublicKey handshake 私有加密 gateway」
# 無法用（2026-08-17 判斷），但實測這支 FundNAV 端點本身**不需要**PublicKey handshake、不需要
# cookie/session，純 GET 帶查詢參數即可（DeviceId 可隨意填、不驗證）——是可以直接 curl 的公開
# 端點。改為官方源優先序：**元大投信官方 FundNAV → SITCA CSV → fundclear**，SITCA/fundclear
# 保留當備援（怕元大投信改版擋掉這支端點）。
YUANTA_NAV_API = 'https://api.yuantafunds.com/ectranslation/api/trans'
SITCA_NAV_CSV = 'https://www.sitca.org.tw/MemberK0000/F/03/nav.csv'
FUNDCLEAR_API = 'https://www.fundclear.com.tw/api/search/fund/query-fund'

# cnyesId -> 元大投信官網內部 FundId（Playwright 掛網 + 搜尋各級別頁面取得，2026-08-19）
YUANTA_ID_MAP = {
    'A05219': '1256', 'A05225': '1262', 'A05231': '1268',
    'A05183': '1234', 'A05185': '1240', 'A05187': '1242',
    'A3PCXt7': '1202', 'A3i7xFp': '1203',
    'A05158': '1209', 'A05160': '1210',
    'A05004': '1003', 'A05013': '1009',   # 多多A／經貿A（2026-10-23 由 FuncId=FundList 查得）
}

def _yuanta_nav(fund_id):
    """回傳 {nav, navDate} 或 None；元大投信官方 FundNAV 端點，NAV[0] 是最新一筆。"""
    url = (f'{YUANTA_NAV_API}?APIType=EC2API&AppName=FundWeb&PageName=%2F&Device=4'
           f'&DeviceId=00000000-0000-0000-0000-000000000000&FuncId=FundNAV&FundId={fund_id}')
    data = getj(url, headers={'Referer': f'https://www.yuantafunds.com/myfund/information/{fund_id}'})
    nav = (data.get('Data') or {}).get('NAV') or []
    if not nav:
        return None
    n = nav[0]
    d = str(n['NAV_DATE'])
    return {'nav': float(n['NAV_B']), 'navDate': f'{d[:4]}-{d[4:6]}-{d[6:]}'}

# cnyesId -> (SITCA/fundclear 基金統編「基金代號」, fundclear 查詢用基金名稱關鍵字)
FRESH_NAV_MAP = {
    'A05219': ('00684942A', '元大優質收益成長多重資產'),
    'A05225': ('00684942G', '元大優質收益成長多重資產'),
    'A05231': ('00684942M', '元大優質收益成長多重資產'),
    'A05183': ('93100953A', '元大日本龍頭企業'),
    'A05185': ('93100953C', '元大日本龍頭企業'),
    'A05187': ('93100953E', '元大日本龍頭企業'),
    'A3PCXt7': ('76959044A', '元大台灣高股息優質龍頭'),
    'A3i7xFp': ('76959044B', '元大台灣高股息優質龍頭'),
    'A05158': ('88320598A', '元大全球優質龍頭平衡'),
    'A05160': ('88320598B', '元大全球優質龍頭平衡'),
    'A05004': ('92001765A', '元大多多'),
    'A05013': ('83863951A', '元大經貿'),
}

def _sitca_nav_map():
    text = _get(SITCA_NAV_CSV)
    if text.startswith('﻿'):
        text = text[1:]
    out = {}
    for row in csv.DictReader(io.StringIO(text)):
        code = row.get('基金統編')
        nav, d = row.get('基金淨值'), row.get('日期')
        if not code or not nav or nav == '-' or not d:
            continue
        out[code] = {'nav': float(nav), 'navDate': f'{d[:4]}-{d[4:6]}-{d[6:]}'}
    return out

def _fundclear_nav_map(names):
    out = {}
    for name in names:
        body = json.dumps({
            '_pageSize': 20, '_pageNum': 1, 'column': '', 'asc': False, 'fundSite': 'all',
            'searchKey': name, 'fundTypeList': ['all'], 'currencyList': ['all'],
            'asiFreqList': ['all'], 'fundRiskLevelList': ['all'],
        }).encode('utf-8')
        req = urllib.request.Request(FUNDCLEAR_API, data=body, headers={
            'Content-Type': 'application/json', 'Accept': 'application/json',
            'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36',
        })
        data = json.loads(urllib.request.urlopen(req, timeout=20).read().decode('utf-8'))
        for row in data.get('data', []):
            code, nav, d = row.get('fundCode'), row.get('navValue'), row.get('navTxnDate')
            if not code or not nav or not d:
                continue
            out[code] = {'nav': float(nav), 'navDate': d.replace('/', '-')}
        time.sleep(0.5)
    return out

def _fresh_nav_map():
    """回傳 {基金統編: {nav, navDate, change, changePct}}；SITCA CSV 為主、fundclear 備援。"""
    try:
        m = _sitca_nav_map()
        print(f'[fresh-nav] SITCA CSV OK，{len(m)} 筆')
        return m
    except Exception as e:
        print(f'[fresh-nav] SITCA CSV 失敗，改用 fundclear 備援：{e}', file=sys.stderr)
    try:
        names = sorted({n for _, n in FRESH_NAV_MAP.values()})
        m = _fundclear_nav_map(names)
        print(f'[fresh-nav] fundclear API OK，{len(m)} 筆')
        return m
    except Exception as e:
        print(f'[fresh-nav] fundclear 也失敗，本次不校正最新淨值（回退純 cnyes）：{e}', file=sys.stderr)
        return {}

def _apply_fresh_nav(result, fid, fresh):
    # change/changePct 一律用「前一筆序列淨值」自算，不信任來源自帶的漲跌欄位——
    # 實測 fundclear changeRatio 語意不是日漲跌%（量級對不上，疑似累積報酬），SITCA CSV
    # 的漲跌欄位雖驗證正確，但統一自算可避免任一來源語意跑掉時悄悄帶入錯數字。
    #
    # 源優先序：元大投信官方 FundNAV（該基金公司自己最快公告）→ SITCA CSV → fundclear。
    fx, source_label = None, None
    yuanta_id = YUANTA_ID_MAP.get(fid)
    if yuanta_id:
        try:
            fx = _yuanta_nav(yuanta_id)
            source_label = '元大投信 FundNAV'
        except Exception as e:
            print(f'[fresh-nav] 元大投信官方 FundNAV 失敗 fid={fid}: {e}', file=sys.stderr)
    if not fx:
        stat_id = FRESH_NAV_MAP.get(fid, (None,))[0]
        fx = fresh.get(stat_id) if stat_id else None
        source_label = 'SITCA／fundclear'
    if not fx or not fx['navDate'] or fx['navDate'] <= (result.get('navDate') or ''):
        return result
    prev_nav = None
    if result.get('series') is not None:
        s = list(result['series'])
        # cnyes 對「最新一筆」的 tradeDate 標記會事後修正（同一交易日的淨值，
        # 隔次抓取時日期往前挪一天）。若官方新值跟序列最後一筆數值相同，代表
        # 那其實是同一個交易日被標了兩次日期，不是真的「一天沒漲跌」——
        # 直接校正最後一筆的日期，改跟倒數第二筆比算漲跌，避免假的 0.00%。
        if s and round(s[-1][1], 4) == round(fx['nav'], 4):
            if len(s) >= 2:
                prev_nav = s[-2][1]
            s[-1] = [fx['navDate'], round(fx['nav'], 4)]
        elif not s or s[-1][0] != fx['navDate']:
            if s:
                prev_nav = s[-1][1]
            s.append([fx['navDate'], round(fx['nav'], 4)])
        result['series'] = s
    result['nav'] = round(fx['nav'], 2)
    result['navDate'] = fx['navDate']
    if prev_nav:
        result['change'] = round(fx['nav'] - prev_nav, 2)
        result['changePct'] = pct(fx['nav'], prev_nav)
    result['source'] = result['source'] + f'；最新一筆以{source_label}官方公告日淨值校正'
    return result

# ---------- 抓全部 ----------
# 每個 group 一張卡；variants 多於 1 個時卡片右上出現幣別/級別下拉
GROUPS = [
    {'name': '主動元大AI新經濟', 'kind': 'etf',
     'note': '主動式 ETF（2025/12 上市，未滿 1 年故 1年/3年無值）',
     'variants': [{'code': '00990A', 'label': '新台幣'}]},
    {'name': '元元致富·財富雙享', 'kind': 'disc',
     'note': '元大人壽全委帳戶（新台幣，2025/08 成立，月撥回；報酬含撥回）',
     'variants': [{'code': 'MDTWD0002', 'label': '新台幣'}]},
    {'name': '元元致富·財富雙利', 'kind': 'disc',
     'note': '元大人壽全委帳戶（美元，2026/04 成立，月撥回；報酬含撥回）',
     'variants': [{'code': 'MDUSD0001', 'label': '美元'}]},
    {'name': '元大優質收益成長多重資產', 'kind': 'fund',
     'note': '多重資產型共同基金（2026/07/16 成立）；含新台幣／美元／日圓級別（皆 A 類型累積），未滿各期間故報酬「—」，走勢為成立以來每日淨值',
     'variants': [
         {'code': 'A05219', 'label': '新台幣'},
         {'code': 'A05225', 'label': '美元'},
         {'code': 'A05231', 'label': '日圓'}]},
    {'name': '元大日本龍頭企業基金', 'kind': 'fund',
     'note': '元大龍頭系列「追成長」；日本大型成長股基金（2023/07 成立）；A 類型累積，含新台幣／美元／日圓級別（美元、日圓級別近3年資料源未提供）',
     'variants': [
         {'code': 'A05183', 'label': '新台幣'},
         {'code': 'A05185', 'label': '美元'},
         {'code': 'A05187', 'label': '日圓'}]},
    {'name': '元大台灣高股息優質龍頭基金', 'kind': 'fund',
     'note': '元大龍頭系列「抓收益」；台股高股息龍頭基金（2020/03 成立）；預設新台幣A不配息（累積、看含息總報酬），另可切新台幣B配息級別',
     'variants': [
         {'code': 'A3PCXt7', 'label': '新台幣A（不配息）'},
         {'code': 'A3i7xFp', 'label': '新台幣B（配息）'}]},
    {'name': '元大多多基金', 'kind': 'fund',
     'note': '國內股票型（1994/10 成立，元大投信旗艦台股基金之一）；新台幣A類型不配息（另有 2026/09 新增之 N 類型，淨值與 A 相同故不另列）',
     'variants': [{'code': 'A05004', 'label': '新台幣A'}]},
    {'name': '元大經貿基金', 'kind': 'fund',
     'note': '國內股票型（1998/11 成立）；新台幣A類型不配息（另有 2026/09 新增之 N 類型，淨值與 A 相同故不另列）',
     'variants': [{'code': 'A05013', 'label': '新台幣A'}]},
    {'name': '元大全球優質龍頭平衡基金', 'kind': 'fund',
     'note': '元大龍頭系列「抗波動」；跨國股債平衡基金（2022/08 成立）；A 類型不配息，含新台幣／美元級別',
     'variants': [
         {'code': 'A05158', 'label': '新台幣'},
         {'code': 'A05160', 'label': '美元'}]},
]

def build_data():
    fresh = _fresh_nav_map()
    fetch = {
        'etf': fetch_etf,
        'fund': lambda fid, zh, note='': fetch_fund(fid, zh, note, fresh=fresh),
        'disc': fetch_carteblanche,
    }
    groups = []
    for g in GROUPS:
        variants = []
        for v in g['variants']:
            try:
                r = fetch[g['kind']](v['code'], g['name'], g['note'])
                r['label'] = v['label']
                variants.append(r)
                print(f'[OK] {g["name"]} / {v["label"]}: nav={r["nav"]} ({r["navDate"]}) YTD={r["returns"].get("ytd")}')
                time.sleep(1.0)
            except Exception as e:
                print(f'[FAIL] {g["name"]} / {v["label"]} ({v["code"]}): {e}', file=sys.stderr)
        if variants:
            groups.append({'name': g['name'], 'kind': g['kind'], 'note': g['note'],
                           'source': variants[0]['source'], 'variants': variants})
    return groups


# ---------- 台股基金比較（多多 vs 經貿 vs 台優龍） ----------
# 三檔皆為元大投信國內股票型、新台幣A類型不配息（台優龍以A不配息級別比，看含息總報酬）。
# 資料源：元大投信官方 FundNAV 端點（成立以來逐日淨值，不需 handshake）＋ FundList（規模／成立日／風險等級／
# 最低申購／理柏官方月底報酬）。期間報酬、年化波動、最大回撤皆由逐日淨值自算，公式見 _compare_metrics()。
COMPARE_FUNDS = [
    {'code': 'A05004', 'yid': '1003', 'short': '多多', 'name': '元大多多基金', 'cls': '新台幣A類型', 'color': '#003580'},
    {'code': 'A05013', 'yid': '1009', 'short': '經貿', 'name': '元大經貿基金', 'cls': '新台幣A類型', 'color': '#c25e12'},
    {'code': 'A3PCXt7', 'yid': '1202', 'short': '台優龍', 'name': '元大台灣高股息優質龍頭基金', 'cls': '新台幣A類型不配息', 'color': '#0a7c4a'},
]
COMPARE_COMMON_START = '2020-03-23'   # 台優龍成立日＝三檔共同起點

def _yuanta_series(fund_id):
    """元大投信官方 FundNAV 逐日淨值（成立以來），回傳升冪 [(date_iso, nav)]。"""
    url = (f'{YUANTA_NAV_API}?APIType=EC2API&AppName=FundWeb&PageName=%2F&Device=4'
           f'&DeviceId=00000000-0000-0000-0000-000000000000&FuncId=FundNAV&FundId={fund_id}')
    data = getj(url, headers={'Referer': f'https://www.yuantafunds.com/myfund/information/{fund_id}'})
    out = []
    for n in (data.get('Data') or {}).get('NAV') or []:
        d = str(n.get('NAV_DATE') or '')
        v = n.get('NAV_B')
        if len(d) == 8 and isinstance(v, (int, float)) and v > 0:
            out.append((f'{d[:4]}-{d[4:6]}-{d[6:]}', float(v)))
    out.sort()
    return out

def _yuanta_fund_list():
    url = (f'{YUANTA_NAV_API}?APIType=EC2API&AppName=FundWeb&PageName=%2F&Device=4'
           f'&DeviceId=00000000-0000-0000-0000-000000000000&FuncId=FundList')
    data = getj(url, headers={'Referer': 'https://www.yuantafunds.com/'})
    return {str(f.get('FUND_ID')): f for f in (data.get('Data') or {}).get('FUND') or []}

def _compare_metrics(series):
    """series 升冪 [(date_iso, nav)]。
    期間報酬 = 最新淨值 / 期初淨值 − 1（期初取「<= 目標日」最近一筆；YTD 取去年最後一筆）。
    年化波動（近1年）= 日對數報酬標準差 × √252。
    最大回撤 = 區間內 (淨值 / 累計最高淨值 − 1) 的最小值。"""
    import math
    dates = [datetime.date.fromisoformat(d) for d, _ in series]
    vals = [v for _, v in series]
    last_d, last_v = dates[-1], vals[-1]
    rets = horizon_returns(dates, vals)            # ytd/m1/m3/y1/y3
    for key, days in [('m6', 182), ('y5', 365 * 5)]:
        tgt = last_d - datetime.timedelta(days=days)
        idx = nearest_on_or_before(dates, tgt) if tgt >= dates[0] else None
        rets[key] = pct(last_v, vals[idx]) if idx is not None else None
    cs = datetime.date.fromisoformat(COMPARE_COMMON_START)
    idx = nearest_on_or_before(dates, cs - datetime.timedelta(days=1))
    # 共同起點：台優龍成立日當天淨值 10 為基準；其他兩檔取成立日前一交易日淨值
    base_idx = idx if idx is not None else (0 if dates[0] >= cs else None)
    rets['common'] = pct(last_v, vals[base_idx]) if base_idx is not None else None

    def window(days):
        tgt = last_d - datetime.timedelta(days=days)
        i = nearest_on_or_before(dates, tgt)
        return vals[i:] if i is not None else vals
    def mdd(w):
        peak, worst = w[0], 0.0
        for v in w:
            peak = max(peak, v)
            worst = min(worst, v / peak - 1)
        return round(worst * 100, 2)
    w1 = window(365); w3 = window(365 * 3)
    lr = [math.log(w1[i] / w1[i - 1]) for i in range(1, len(w1)) if w1[i - 1] > 0]
    vol = None
    if len(lr) > 20:
        m = sum(lr) / len(lr)
        vol = round(math.sqrt(sum((x - m) ** 2 for x in lr) / (len(lr) - 1)) * math.sqrt(252) * 100, 2)
    return {
        'returns': rets,
        'vol1y': vol,
        'mdd1y': mdd(w1), 'mdd3y': mdd(w3),
        'hi1y': max(w1), 'lo1y': min(w1),
        'hi1yDate': series[len(series) - len(w1):][w1.index(max(w1))][0],
        'lo1yDate': series[len(series) - len(w1):][w1.index(min(w1))][0],
    }

def build_compare():
    fl = {}
    try:
        fl = _yuanta_fund_list()
    except Exception as e:
        print(f'[compare] FundList 失敗（基本資料欄位留空）：{e}', file=sys.stderr)
    funds = []
    for cf in COMPARE_FUNDS:
        series = _yuanta_series(cf['yid'])
        if len(series) < 30:
            raise RuntimeError(f'{cf["name"]} 官方淨值序列過短 ({len(series)})')
        m = _compare_metrics(series)
        info = fl.get(cf['yid']) or {}
        def _n(x):
            return round(float(x), 2) if isinstance(x, (int, float)) else None
        setup = str(info.get('FUND_SETUP_DATE') or '')
        sz_d = str(info.get('FUND_SIZE_DATE') or '')
        funds.append({
            **{k: cf[k] for k in ('code', 'short', 'name', 'cls', 'color')},
            'nav': series[-1][1], 'navDate': series[-1][0],
            'change': round(series[-1][1] - series[-2][1], 2),
            'changePct': pct(series[-1][1], series[-2][1]),
            'inception': f'{setup[:4]}-{setup[4:6]}-{setup[6:]}' if len(setup) == 8 else series[0][0],
            'sizeNtd': _n(info.get('FUND_SIZE')),
            'sizeDate': f'{sz_d[:4]}-{sz_d[4:6]}-{sz_d[6:]}' if len(sz_d) == 8 else None,
            'risk': info.get('RISK_CLASS'),
            'payout': info.get('CYCLE_TYPE') or '不配息',
            'minBuy': _n(info.get('EC_ALLOT_MIN_AMT')),
            'minRsp': _n(info.get('RSP_ALLOT_MIN_AMT')),
            'lipper': {
                'asOf': info.get('DATEYM'),
                'm3': _n(info.get('M3_RATE')), 'm6': _n(info.get('M6_RATE')), 'ytd': _n(info.get('Y_RATE')),
                'y1': _n(info.get('Y1_RATE')), 'y2': _n(info.get('Y2_RATE')), 'y3': _n(info.get('Y3_RATE')),
                'y5': _n(info.get('Y5_RATE')), 'incept': _n(info.get('RATE')),
            },
            **m,
            # 走勢只帶共同起點以後（成立以來全序列太大）；5年／3年報酬已在上面用全序列算好
            'series': [[d, round(v, 2)] for d, v in series if d >= COMPARE_COMMON_START],
        })
        print(f'[compare] {cf["name"]}: nav={series[-1][1]} ({series[-1][0]}) pts={len(series)} YTD={m["returns"].get("ytd")} 1Y={m["returns"].get("y1")}')
        time.sleep(0.8)
    return {
        'commonStart': COMPARE_COMMON_START,
        'source': '元大投信官方 FundNAV 逐日淨值（期間報酬／年化波動／最大回撤自算）＋ FundList（規模、成立日、風險等級、理柏月底報酬）',
        'funds': funds,
    }

# ---------- 永不倒退高水位保護 ----------
# 背景：鉅亨基金 API（fund.api.cnyes.com）對 GitHub 美國 runner 的 IP 會慢約一天
# （origin 分區資料落後，實測 2026-08 確認；cache 招式無效）。若每次都用抓到的值直接覆蓋，
# 網站會被舊值蓋回、看起來「卡住/沒變」。這裡用「已部署的 data.json」當跨 run 持久層：
# 每次 build 先抓上一版已部署的 data.json，逐 variant 比 navDate，只有新抓到的 >= 已存的才採用，
# 否則保留舊高水位；抓取失敗的 variant 也由舊資料回填，卡片不掉列。
PAGES_DATA_URL = 'https://timchang1977o-maker.github.io/yuanta-funds/data.json'

def _load_prev(here):
    """回傳 (variant_idx, prev_groups_by_name)，取『線上已部署』與『本地 committed』中 navDate 最新者。"""
    sources = []
    try:
        live = getj(PAGES_DATA_URL, tries=2)
        sources.append(live)
        print(f'[prev] 線上已部署 data.json updated={live.get("updated")}')
    except Exception as e:
        print(f'[prev] 線上 data.json 取不到（首次部署或 Pages 未含 data.json）：{e}')
    try:
        with open(os.path.join(here, 'data.json'), encoding='utf-8') as fp:
            sources.append(json.load(fp))
            print('[prev] 讀入本地 committed data.json 作為 seed/fallback')
    except Exception:
        pass
    vidx, gidx = {}, {}
    for src in sources:
        for g in src.get('groups', []):
            gidx.setdefault(g.get('name'), g)
            for v in g.get('variants', []):
                k = (g.get('name'), v.get('code'))
                cur = vidx.get(k)
                if cur is None or (v.get('navDate') or '') > (cur.get('navDate') or ''):
                    vidx[k] = v
    return vidx, gidx

def _apply_never_regress(groups, vidx, gidx):
    kept = carried = backfilled = 0
    present = set()
    for g in groups:
        present.add(g['name'])
        seen = set()
        for i, v in enumerate(g['variants']):
            code = v.get('code'); seen.add(code)
            p = vidx.get((g['name'], code))
            if p and (p.get('navDate') or '') > (v.get('navDate') or ''):
                g['variants'][i] = {**p, 'label': v.get('label', p.get('label'))}
                carried += 1
            else:
                kept += 1
        # 回填本次抓取失敗、但舊資料有的 variant（維持定義順序）
        order = [vv['code'] for gg in GROUPS if gg['name'] == g['name'] for vv in gg['variants']]
        for (gn, code), p in vidx.items():
            if gn == g['name'] and code not in seen:
                g['variants'].append(dict(p)); backfilled += 1
        if order:
            g['variants'].sort(key=lambda v: order.index(v['code']) if v.get('code') in order else 99)
    # 整個 group 都抓失敗 → 用舊資料整組還原
    restored = 0
    for name, pg in gidx.items():
        if name not in present:
            groups.append(pg); restored += 1
    print(f'[never-regress] 用新值 {kept}、保留舊高水位 {carried}、回填失敗 variant {backfilled}、還原整組 {restored}')
    return groups

def main():
    here = os.path.dirname(os.path.abspath(__file__))
    groups = build_data()
    vidx, gidx = _load_prev(here)
    groups = _apply_never_regress(groups, vidx, gidx)
    # 保留 GROUPS 定義的組別順序
    gorder = [g['name'] for g in GROUPS]
    groups.sort(key=lambda g: gorder.index(g['name']) if g['name'] in gorder else 99)
    compare = None
    try:
        compare = build_compare()
    except Exception as e:
        print(f'[compare] 本次比較表抓取失敗，沿用上一版：{e}', file=sys.stderr)
    if not compare:
        for src_url in (PAGES_DATA_URL, None):
            try:
                prev = getj(src_url, tries=2) if src_url else json.load(open(os.path.join(here, 'data.json'), encoding='utf-8'))
                if prev.get('compare'):
                    compare = prev['compare']; break
            except Exception:
                pass
    payload = {
        'updated': datetime.datetime.now(TPE).strftime('%Y-%m-%d %H:%M'),
        'dataAsOf': max((v.get('navDate') or '' for g in groups for v in g['variants']), default=''),
        'groups': groups,
        'compare': compare,
    }
    with open(os.path.join(here, 'data.json'), 'w', encoding='utf-8') as fp:
        json.dump(payload, fp, ensure_ascii=False, indent=1)
    html = render_html(payload)
    out = os.path.join(here, '元大基金淨值與表現.html')
    with open(out, 'w', encoding='utf-8') as fp:
        fp.write(html)
    print('\nWrote', out)
    # Artifact 版：去掉 doctype/html/head/body 外殼，只留 style + 內容 + script
    style = html[html.find('<style>'):html.find('</style>') + len('</style>')]
    body = html[html.find('<body>') + len('<body>'):html.find('</body>')]
    with open(os.path.join(here, 'artifact.html'), 'w', encoding='utf-8') as fp:
        fp.write(style + '\n' + body)
    print('Wrote', os.path.join(here, 'artifact.html'))

# ---------- HTML ----------
def render_html(payload):
    from string import Template
    data_js = json.dumps(payload, ensure_ascii=False)
    tpl = Template(HTML_TEMPLATE)
    return tpl.safe_substitute(DATA=data_js, UPDATED=payload['updated'])

HTML_TEMPLATE = r"""<!doctype html>
<html lang="zh-Hant">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>元大基金淨值與表現</title>
<style>
:root{
  --bg:#f4f6fb; --card:#ffffff; --ink:#12233b; --sub:#5b6b82; --line:#e4e9f2;
  --brand:#003580; --brand2:#1763c6; --accent:#0a7c4a; --up:#d13438; --down:#0a7c4a;
  --chip:#eef3fb; --shadow:0 1px 3px rgba(16,38,72,.08),0 8px 24px rgba(16,38,72,.06);
}
*{box-sizing:border-box}
html,body{margin:0;background:var(--bg);color:var(--ink);overflow-x:hidden;max-width:100%;
  font-family:"PingFang TC","Noto Sans TC","Microsoft JhengHei",system-ui,-apple-system,sans-serif;
  -webkit-font-smoothing:antialiased}
.wrap{max-width:1160px;margin:0 auto;padding:28px 20px 60px}
header.top{display:flex;flex-wrap:wrap;align-items:flex-end;gap:14px;justify-content:space-between;margin-bottom:6px}
h1{font-size:26px;margin:0;letter-spacing:.5px}
h1 .badge{font-size:12px;font-weight:700;color:#fff;background:var(--brand);padding:3px 9px;border-radius:999px;vertical-align:middle;margin-left:8px}
.meta{font-size:13px;color:var(--sub)}
.legend{display:flex;flex-wrap:wrap;gap:8px;margin:16px 0 22px}
.tag{font-size:12px;color:var(--sub);background:var(--chip);border:1px solid var(--line);border-radius:999px;padding:4px 10px}
.tag b{color:var(--brand)}
/* 總覽表 */
.tablecard{background:var(--card);border:1px solid var(--line);border-radius:16px;box-shadow:var(--shadow);overflow:hidden;margin-bottom:26px}
.tablecard .hd{padding:14px 18px;border-bottom:1px solid var(--line);font-weight:700;font-size:15px;color:var(--brand);display:flex;align-items:center;gap:8px}
table{border-collapse:collapse;width:100%;font-size:14px}
th,td{padding:11px 12px;text-align:right;white-space:nowrap}
th{background:#f7f9fd;color:var(--sub);font-weight:600;font-size:12.5px;border-bottom:1px solid var(--line)}
td:first-child,th:first-child{text-align:left;position:sticky;left:0;background:var(--card)}
th:first-child{background:#f7f9fd}
tbody tr{border-bottom:1px solid var(--line)}
tbody tr:last-child{border-bottom:none}
tbody tr:hover td{background:#fafcff}
tbody tr:hover td:first-child{background:#fafcff}
.nm{font-weight:700;color:var(--ink)}
.nm small{display:block;font-weight:500;color:var(--sub);font-size:11.5px;margin-top:1px}
.kchip{display:inline-block;font-size:11px;font-weight:700;padding:2px 7px;border-radius:6px;margin-left:6px;vertical-align:middle}
.k-etf{background:#e7f0ff;color:#1763c6}.k-fund{background:#eafaf1;color:#0a7c4a}.k-disc{background:#fff1e6;color:#c25e12}
.pos{color:var(--up)}.neg{color:var(--down)}.na{color:#a9b4c4}
.navbig{font-weight:800;font-size:15px}
.tblnote{font-size:11.5px;color:var(--sub);padding:10px 14px;background:#fbfcfe}
/* 卡片區 */
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(340px,1fr));gap:18px}
.card{background:var(--card);border:1px solid var(--line);border-radius:16px;box-shadow:var(--shadow);padding:16px 16px 12px;display:flex;flex-direction:column}
.card .ctop{display:flex;justify-content:space-between;align-items:flex-start;gap:10px;flex-wrap:wrap}
.card .ctop>div:first-child{min-width:0}
.card h3{margin:0;font-size:16px;line-height:1.35}
.card .code{font-size:12px;color:var(--sub);margin-top:4px;display:flex;align-items:center;gap:6px;flex-wrap:wrap}
.vsel{font-family:inherit;font-size:12px;font-weight:700;color:var(--brand);background:var(--chip);border:1px solid var(--line);border-radius:7px;padding:3px 22px 3px 8px;cursor:pointer;
  appearance:none;-webkit-appearance:none;background-image:url("data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg' width='10' height='6'><path d='M1 1l4 4 4-4' stroke='%23003580' stroke-width='1.6' fill='none' stroke-linecap='round'/></svg>");background-repeat:no-repeat;background-position:right 7px center}
.vsel:hover{border-color:var(--brand2)}
.vsel-sm{font-size:11.5px;padding:1px 20px 1px 7px;background-position:right 6px center;font-weight:600}
.curbadge{display:inline-block;font-family:inherit;font-size:12px;font-weight:800;color:var(--brand);background:var(--chip);border:1px solid var(--line);border-radius:7px;padding:3px 8px}
.curbadge-sm{font-size:11.5px;padding:1px 7px;font-weight:700}
.card .navnow{text-align:right;flex-shrink:0;margin-left:auto}
.card .navnow .v{font-size:22px;font-weight:800;line-height:1}
.card .navnow .d{font-size:11px;color:var(--sub);margin-top:3px;white-space:nowrap}
.card .navnow .chg{font-size:12.5px;font-weight:700;margin-top:3px}
.retrow{display:flex;gap:6px;margin:12px 0 6px;flex-wrap:wrap}
.ret{flex:1;min-width:0;flex-basis:52px;background:#f7f9fd;border:1px solid var(--line);border-radius:9px;padding:7px 4px;text-align:center;cursor:pointer;transition:background .12s,border-color .12s;font-family:inherit}
.ret:hover{background:#eef2f9}
.ret.active{background:var(--brand);border-color:var(--brand)}
.ret.active .l,.ret.active .n{color:#fff}
.ret .l{font-size:10.5px;color:var(--sub)}
.ret .n{font-size:14px;font-weight:800;margin-top:2px}
.chartbox{position:relative;margin-top:6px}
.chartbox svg{display:block;width:100%;height:132px;overflow:visible}
.axislbl{font-size:10px;fill:var(--sub)}
.cnote{font-size:11px;color:var(--sub);margin:8px 2px 2px;line-height:1.5}
.tip{position:absolute;pointer-events:none;background:#12233b;color:#fff;font-size:11px;padding:4px 8px;border-radius:6px;opacity:0;transform:translate(-50%,-120%);white-space:nowrap;transition:opacity .08s}
footer{margin-top:34px;font-size:12px;color:var(--sub);line-height:1.7;border-top:1px solid var(--line);padding-top:16px}
footer b{color:var(--ink)}
@media(max-width:560px){.wrap{padding:18px 12px 40px}h1{font-size:21px}.card .navnow .v{font-size:19px}}
/* 檢視切換（完整版 / 手機好讀版） */
.hdright{display:flex;flex-direction:column;align-items:flex-end;gap:9px}
.viewtoggle{display:inline-flex;background:var(--chip);border:1px solid var(--line);border-radius:999px;padding:3px}
.viewtoggle button{font-family:inherit;font-size:12.5px;font-weight:700;color:var(--sub);background:none;border:none;border-radius:999px;padding:6px 13px;cursor:pointer;transition:background .12s,color .12s}
.viewtoggle button.on{background:var(--brand);color:#fff}
/* 手機好讀版：一檔一列、大字淨值，適合每日截圖 */
#mobileview{display:none;max-width:520px;margin:0 auto}
body.mob #deskview{display:none}
body.mob #mobileview{display:block}
body.mob .legend{display:none}
body.mob footer{display:none}
.mnote{font-size:11.5px;color:var(--sub);text-align:center;line-height:1.6;margin:16px 4px 0}
.mrow{background:var(--card);border:1px solid var(--line);border-radius:14px;box-shadow:var(--shadow);padding:14px 15px;margin-bottom:11px}
.mrow .mtop{display:flex;justify-content:space-between;align-items:flex-start;gap:12px;flex-wrap:wrap}
.mrow .mname{font-size:16px;font-weight:800;line-height:1.32}
.mrow .msub{font-size:12px;color:var(--sub);margin-top:4px;display:flex;gap:6px;align-items:center;flex-wrap:wrap}
.mrow .mnav{text-align:right;flex-shrink:0;margin-left:auto}
.mrow .mnav .v{font-size:27px;font-weight:800;line-height:1}
.mrow .mnav .u{font-size:11px;color:var(--sub);margin-top:3px;white-space:nowrap}
.mrow .mnav .c{font-size:13px;font-weight:800;margin-top:4px}
.mrets{display:flex;gap:4px;margin-top:11px;flex-wrap:wrap}
.mrets .mr{flex:1;min-width:56px;text-align:center;background:#f7f9fd;border:1px solid var(--line);border-radius:8px;padding:5px 2px}
.mrets .mr .l{font-size:9.5px;color:var(--sub)}
.mrets .mr .n{font-size:12.5px;font-weight:800;margin-top:2px}
.mchart{margin-top:10px}
.mchart svg{display:block;width:100%;height:120px;overflow:visible}
.mimgbtns{display:flex;gap:8px;margin-top:10px}
.imgbtn{flex:1;font-family:inherit;font-size:12px;font-weight:700;color:var(--brand);background:var(--chip);border:1px solid var(--line);border-radius:9px;padding:8px 4px;cursor:pointer;transition:background .12s,transform .05s}
.imgbtn:active{transform:scale(.98)}
.imgbtn.ok{background:var(--accent);color:#fff;border-color:var(--accent)}
.imgbtn.err{background:#fdeceb;color:#c0392b;border-color:#f3c9c5}
.imgbar{display:flex;gap:10px;margin-bottom:14px}
.imgbar .copybtn{margin-bottom:0;width:auto;flex:1;min-width:0}
.copybtn{display:block;width:100%;font-family:inherit;font-size:15px;font-weight:800;color:#fff;background:var(--brand);border:none;border-radius:12px;padding:13px;margin-bottom:14px;cursor:pointer;box-shadow:var(--shadow);transition:background .12s,transform .05s}
.copybtn:hover{background:var(--brand2)}
.copybtn:active{transform:scale(.99)}
.copybtn.ok{background:var(--accent)}
.copybtn.ghost{background:#eef2f9;color:var(--brand);box-shadow:none;font-size:13.5px;padding:10px;margin-bottom:12px}
.copybtn.ghost:hover{background:#e2e9f5}
.cppanel{display:none;background:#f7f9fd;border:1px solid var(--line);border-radius:12px;padding:12px 12px 4px;margin-bottom:14px}
.cppanel.open{display:block}
.cpsel-head{display:flex;justify-content:space-between;align-items:center;font-size:12.5px;font-weight:800;color:var(--sub);margin-bottom:10px}
.cpmini{font-family:inherit;font-size:11.5px;font-weight:700;color:var(--brand);background:#fff;border:1px solid var(--line);border-radius:7px;padding:3px 9px;margin-left:5px;cursor:pointer}
.cpmini:active{transform:scale(.97)}
.cpgrp{margin-bottom:9px}
.cpgname{font-size:12.5px;font-weight:800;color:var(--fg);margin-bottom:3px}
.cpvars{display:flex;flex-direction:column;gap:1px}
.cprow{display:flex;align-items:center;gap:9px;font-size:13px;font-weight:600;color:var(--fg);background:#fff;border:1px solid var(--line);border-radius:8px;padding:7px 10px;cursor:pointer;user-select:none}
.cprow:active{transform:scale(.995)}
.cprow.on{color:var(--brand);border-color:var(--brand);background:#f2f6fd}
.cprow input{margin:0;width:17px;height:17px;accent-color:var(--brand);cursor:pointer;flex:none}

/* 台股基金比較區 */
.cmp .hd .hdsub{font-size:12px;font-weight:500;color:var(--sub);margin-left:4px}
.cmpwrap{display:grid;grid-template-columns:minmax(0,1.05fr) minmax(0,1fr);gap:0}
.cmpchart{padding:14px 16px 10px;border-right:1px solid var(--line);min-width:0}
.cmpctl{display:flex;gap:6px;flex-wrap:wrap;align-items:center;margin-bottom:8px}
.cmpctl .lbl{font-size:12px;color:var(--sub);margin-right:2px}
.rbtn{font-family:inherit;font-size:12px;font-weight:700;color:var(--sub);background:#f7f9fd;border:1px solid var(--line);border-radius:999px;padding:4px 11px;cursor:pointer;transition:background .12s,color .12s}
.rbtn:hover{background:#eef2f9}
.rbtn.on{background:var(--brand);border-color:var(--brand);color:#fff}
.cmplegend{display:flex;gap:8px;flex-wrap:wrap;margin:6px 0 4px}
.cmplegend .lg{display:inline-flex;align-items:center;gap:6px;font-size:12px;font-weight:700;background:#f7f9fd;border:1px solid var(--line);border-radius:8px;padding:4px 9px}
.cmplegend .lg i{display:inline-block;width:12px;height:3px;border-radius:2px}
.cmplegend .lg small{font-weight:800}
.cmpchart .chartbox svg{height:230px}
.cmpt th,.cmpt td{padding:8px 12px}
.cmpt td:first-child,.cmpt th:first-child{color:var(--sub);font-weight:600;font-size:12.5px;white-space:normal;min-width:118px}
.cmpt th.fh{text-align:right;color:var(--ink);font-size:13px;font-weight:800}
.cmpt th.fh small{display:block;font-weight:500;color:var(--sub);font-size:11px;margin-top:1px}
.cmpt th.fh i{display:inline-block;width:8px;height:8px;border-radius:50%;margin-right:5px;vertical-align:middle}
.cmpt tr.sec td{background:#f7f9fd;color:var(--brand);font-weight:800;font-size:12px;padding:6px 12px;text-align:left}
.cmpt td.best{font-weight:800;position:relative}
.cmpt td.best::after{content:"★";font-size:9px;color:#d9a300;margin-left:3px;vertical-align:top}
.cmpt td.big{font-weight:800;font-size:15px;color:var(--ink)}
.cmpt td small{color:var(--sub);font-size:11px;font-weight:500}
@media(max-width:860px){.cmpwrap{grid-template-columns:1fr}.cmpchart{border-right:none;border-bottom:1px solid var(--line)}}
/* 手機好讀版比較區 */
.mcmp{background:var(--card);border:1px solid var(--line);border-radius:14px;box-shadow:var(--shadow);padding:14px 15px;margin:4px 0 11px}
.mcmp .mname{font-size:15px;font-weight:800;margin-bottom:2px}
.mcmp .msub{font-size:11.5px;color:var(--sub);margin-bottom:8px}
.mcmp .chartbox svg{height:170px}
.mcmp table{font-size:12.5px;margin-top:8px}
.mcmp th,.mcmp td{padding:6px 6px}
.mcmp td:first-child,.mcmp th:first-child{color:var(--sub);font-weight:600;font-size:11.5px;min-width:0}
.mcmp th.fh{font-size:12px;font-weight:800;color:var(--ink)}
.mcmp th.fh i{display:inline-block;width:7px;height:7px;border-radius:50%;margin-right:4px;vertical-align:middle}
.mcmp td.big{font-weight:800}
</style>
</head>
<body>
<div class="wrap">
  <header class="top">
    <h1>元大基金淨值與表現<span class="badge">Yuanta</span></h1>
    <div class="hdright">
      <div class="meta">資料更新：<b>$UPDATED</b>（台北）</div>
      <div class="viewtoggle">
        <button id="btnDesk" onclick="setView(false,true)">🖥 完整版</button>
        <button id="btnMob" onclick="setView(true,true)">📱 手機好讀版</button>
      </div>
    </div>
  </header>
  <div id="deskview">
  <div class="legend">
    <span class="tag"><b>ETF</b> 市價 · 不含息報酬（FinMind）</span>
    <span class="tag"><b>共同基金</b> 淨值 · 官方各期報酬（鉅亨/晨星）</span>
    <span class="tag"><b>全委帳戶</b> 淨值 · 含撥回（元大人壽官方）</span>
    <span class="tag">紅漲綠跌（台股慣例）</span>
    <span class="tag">多幣別/級別可用卡片右上下拉切換 ⤵</span>
  </div>

  <div class="tablecard">
    <div class="hd">📊 淨值與各期報酬總覽</div>
    <div style="overflow-x:auto">
      <table id="ovt">
        <thead><tr>
          <th>標的</th><th>幣別</th><th>最新淨值/市價</th><th>當日</th>
          <th>YTD</th><th>近1月</th><th>近3月</th><th>近1年</th><th>近3年</th>
        </tr></thead>
        <tbody></tbody>
      </table>
    </div>
    <div class="tblnote">※ 報酬率：ETF 為市價報酬（<b>不含息</b>）、全委為含撥回報酬、基金為晨星官方各期報酬。「—」＝該標的成立未滿該期間或資料源未提供。</div>
  </div>

  <div class="tablecard cmp" id="cmpcard"></div>

  <div class="grid" id="cards"></div>
  </div><!-- /deskview -->

  <div id="mobileview"></div>

  <footer>
    <b>資料來源</b>：ETF（00990A）＝FinMind／TWSE 日線（市價收盤、<b>不含息</b>之市價報酬）；共同基金＝鉅亨 fund.api（晨星官方歷史走勢/各期報酬），最新一筆淨值以元大投信官方 FundNAV／SITCA 投信投顧公會／fundclear 基金資訊觀測站三個官方源依序校正（避免 cnyes 資料落後）；全委帳戶＝鉅亨 co.cnyes 元大人壽全委 API（官方含撥回報酬與報酬指數）。<br>
    <b>說明</b>：有多幣別／配息級別的標的，於卡片右上下拉切換（總覽表顯示預設級別）。全委帳戶（元元致富·財富雙利/雙享）為投資型保單之類全權委託帳戶，走勢圖為「含撥回報酬指數（基準 100）」而非單位淨值曲線；「近1年/近3年」在成立未滿期間顯示「—」。「台股基金比較」區塊三檔均採元大投信官方 FundNAV 成立以來逐日淨值自算累積報酬、年化波動與最大回撤，基本資料與理柏月底報酬取自元大投信官網 FundList。本頁僅供參考，非投資建議；實際淨值以各機構公告為準。
  </footer>
</div>

<script>
const DATA = $DATA;
const fmtPct = v => (v===null||v===undefined) ? '<span class="na">—</span>' :
  '<span class="'+(v>=0?'pos':'neg')+'">'+(v>=0?'+':'')+v.toFixed(2)+'%</span>';
const kmap = {etf:['ETF','k-etf'],fund:['基金','k-fund'],disc:['全委','k-disc']};
const fnav = v => (v.nav!==null&&v.nav!==undefined)?v.nav:'—';
const mobSel = (DATA.groups||[]).map(()=>0);  // 手機好讀版每張卡目前選的幣別/級別 index
const cardIdx = (DATA.groups||[]).map(()=>0);  // 完整版每張卡目前選的幣別/級別 index
const chartRange = {};  // gi -> 'ytd'|'m1'|'m3'|'y1'|'y3'（點選走勢圖上方期間按鈕後套用），未設=全部走勢
function filterSeries(series, key){
  if(!series||series.length<2||!key) return series;
  const last=new Date(series[series.length-1][0]);
  let from;
  if(key==='ytd') from=new Date(last.getFullYear(),0,1);
  else if(key==='m1'){ from=new Date(last); from.setMonth(from.getMonth()-1); }
  else if(key==='m3'){ from=new Date(last); from.setMonth(from.getMonth()-3); }
  else if(key==='y1'){ from=new Date(last); from.setFullYear(from.getFullYear()-1); }
  else if(key==='y3'){ from=new Date(last); from.setFullYear(from.getFullYear()-3); }
  else return series;
  const filtered=series.filter(p=>new Date(p[0])>=from);
  return filtered.length>=2 ? filtered : series;
}

function rowCells(g, gi, idx){
  const v=g.variants[idx], r=v.returns||{};
  const chg = (v.changePct===null||v.changePct===undefined)?'<span class="na">—</span>':fmtPct(v.changePct);
  const k=kmap[g.kind];
  const sel = g.variants.length>1
    ? ` · <select class="vsel vsel-sm" aria-label="切換幣別/級別" onchange="switchRow(${gi}, this.selectedIndex)">`+
      g.variants.map((x,i)=>`<option ${i===idx?'selected':''}>${x.label}</option>`).join('')+`</select>`
    : '';
  return `<td><span class="nm">${g.name}<span class="kchip ${k[1]}">${k[0]}</span></span><small>${v.code}${sel}</small></td>
        <td>${v.currency||''}</td>
        <td><span class="navbig">${fnav(v)}</span> <small style="color:var(--sub)">${v.navLabel}</small><br><small style="color:var(--sub)">${v.navDate||''}</small></td>
        <td>${chg}</td>
        <td>${fmtPct(r.ytd)}</td><td>${fmtPct(r.m1)}</td><td>${fmtPct(r.m3)}</td>
        <td>${fmtPct(r.y1)}</td><td>${fmtPct(r.y3)}</td>`;
}
function switchRow(gi, idx){
  document.getElementById('row'+gi).innerHTML = rowCells(DATA.groups[gi], gi, idx);
}
function overview(){
  const tb = document.querySelector('#ovt tbody');
  DATA.groups.forEach((g,gi)=>{
    tb.insertAdjacentHTML('beforeend', `<tr id="row${gi}">${rowCells(g,gi,0)}</tr>`);
  });
}

/* ---- 迷你折線圖（inline SVG） ---- */
function lineChart(f, rangeKey){
  const s=filterSeries(f.series, rangeKey);
  const W=340,H=132,padL=6,padR=6,padT=14,padB=18;
  if(!s||s.length<2){
    return barChart(f);
  }
  const ys=s.map(p=>p[1]);
  let mn=Math.min(...ys), mx=Math.max(...ys);
  if(mn===mx){mn-=1;mx+=1;}
  const pad=(mx-mn)*0.12; mn-=pad; mx+=pad;
  const n=s.length;
  const X=i=>padL+(W-padL-padR)*i/(n-1);
  const Y=v=>padT+(H-padT-padB)*(1-(v-mn)/(mx-mn));
  let d='';
  s.forEach((p,i)=>{d+=(i?'L':'M')+X(i).toFixed(1)+' '+Y(p[1]).toFixed(1)+' ';});
  const area=d+`L ${X(n-1).toFixed(1)} ${H-padB} L ${X(0).toFixed(1)} ${H-padB} Z`;
  const up = s[n-1][1]>=s[0][1];
  const col = up?'var(--up)':'var(--down)';
  const gid='g'+Math.random().toString(36).slice(2,7);
  // x 軸標籤：頭尾
  const lbl0=s[0][0].slice(2), lbl1=s[n-1][0].slice(2);
  // 基準線（index 型畫 100）
  let base='';
  if(f.seriesType==='index' && 100>=mn && 100<=mx){
    const yb=Y(100).toFixed(1);
    base=`<line x1="${padL}" y1="${yb}" x2="${W-padR}" y2="${yb}" stroke="#c7d2e3" stroke-width="1" stroke-dasharray="3 3"/><text x="${W-padR}" y="${(+yb-3)}" text-anchor="end" class="axislbl">100</text>`;
  }
  const dots = JSON.stringify(s.map((p,i)=>[+X(i).toFixed(1),+Y(p[1]).toFixed(1),p[0],p[1]]));
  return `<div class="chartbox">
    <svg viewBox="0 0 ${W} ${H}" data-pts='${dots}' onmousemove="hoverChart(event)" onmouseleave="hideTip(event)">
      <defs><linearGradient id="${gid}" x1="0" x2="0" y1="0" y2="1">
        <stop offset="0" stop-color="${col}" stop-opacity="0.16"/><stop offset="1" stop-color="${col}" stop-opacity="0"/>
      </linearGradient></defs>
      ${base}
      <path d="${area}" fill="url(#${gid})" stroke="none"/>
      <path d="${d}" fill="none" stroke="${col}" stroke-width="2" stroke-linejoin="round" stroke-linecap="round"/>
      <circle class="hcz" r="3.2" fill="${col}" opacity="0"/>
      <text x="${padL}" y="${H-5}" class="axislbl">${lbl0}</text>
      <text x="${W-padR}" y="${H-5}" text-anchor="end" class="axislbl">${lbl1}</text>
    </svg>
    <div class="tip"></div>
  </div>`;
}

/* ---- 各期報酬長條（基金用） ---- */
function barChart(f){
  const r=f.returns||{};
  const items=[['YTD',r.ytd],['1月',r.m1],['3月',r.m3],['1年',r.y1],['3年',r.y3]];
  const vals=items.map(x=>x[1]).filter(v=>typeof v==='number');
  const mx=Math.max(1,...vals.map(Math.abs));
  const W=340,H=132,padB=18,zeroY=(H-padB)/2+8,half=(H-padB)/2-6;
  const bw=(W-20)/items.length, gap=10;
  let bars='';
  items.forEach((it,i)=>{
    const v=it[1]; const x=10+i*bw;
    if(typeof v==='number'){
      const h=Math.abs(v)/mx*half;
      const y=v>=0?zeroY-h:zeroY;
      const col=v>=0?'var(--up)':'var(--down)';
      bars+=`<rect x="${(x+gap/2).toFixed(1)}" y="${y.toFixed(1)}" width="${(bw-gap).toFixed(1)}" height="${Math.max(1,h).toFixed(1)}" rx="3" fill="${col}"/>
        <text x="${(x+bw/2).toFixed(1)}" y="${(v>=0?y-3:y+h+11).toFixed(1)}" text-anchor="middle" class="axislbl" style="font-weight:700;fill:${col}">${v>=0?'+':''}${v.toFixed(1)}</text>`;
    }else{
      bars+=`<text x="${(x+bw/2).toFixed(1)}" y="${zeroY+3}" text-anchor="middle" class="axislbl">—</text>`;
    }
    bars+=`<text x="${(x+bw/2).toFixed(1)}" y="${H-4}" text-anchor="middle" class="axislbl">${it[0]}</text>`;
  });
  return `<div class="chartbox"><svg viewBox="0 0 ${W} ${H}">
    <line x1="6" y1="${zeroY}" x2="${W-6}" y2="${zeroY}" stroke="var(--line)" stroke-width="1"/>
    ${bars}
  </svg></div>`;
}

function hoverChart(e){
  const svg=e.currentTarget; const pts=JSON.parse(svg.getAttribute('data-pts'));
  const box=svg.getBoundingClientRect();
  const rx=(e.clientX-box.left)/box.width*340;
  let best=pts[0],bd=1e9;
  for(const p of pts){const dd=Math.abs(p[0]-rx);if(dd<bd){bd=dd;best=p;}}
  const dot=svg.querySelector('.hcz'); dot.setAttribute('cx',best[0]);dot.setAttribute('cy',best[1]);dot.setAttribute('opacity','1');
  const tip=svg.parentNode.querySelector('.tip');
  tip.innerHTML=best[2]+'　<b>'+best[3]+'</b>';
  tip.style.left=(best[0]/340*box.width)+'px';
  tip.style.top=(best[1]/132*box.height)+'px';
  tip.style.opacity='1';
}
function hideTip(e){const svg=e.currentTarget;svg.querySelector('.hcz').setAttribute('opacity','0');svg.parentNode.querySelector('.tip').style.opacity='0';}

function cardHTML(g, gi, idx){
  const v=g.variants[idx], r=v.returns||{}, k=kmap[g.kind];
  const sel = g.variants.length>1
    ? `<select class="vsel" aria-label="切換幣別/級別" onchange="switchVar(${gi}, this.selectedIndex)">`+
      g.variants.map((x,i)=>`<option ${i===idx?'selected':''}>${x.label}</option>`).join('')+`</select>`
    : `<span class="curbadge">${v.currency||v.label||''}</span>`;
  const chgtxt=(v.change!==null&&v.change!==undefined?((v.change>=0?'+':'')+v.change+' '):'')+
    ((v.changePct===null||v.changePct===undefined)?'':((v.changePct>=0?'+':'')+v.changePct+'%'));
  const chgcls=(v.changePct>=0)?'pos':'neg';
  const rk = chartRange[gi];
  const rets=[['YTD','ytd',r.ytd],['近1月','m1',r.m1],['近3月','m3',r.m3],['近1年','y1',r.y1],['近3年','y3',r.y3]];
  const retHtml=rets.map(x=>`<div class="ret${rk===x[1]?' active':''}" onclick="setChartRange(${gi},'${x[1]}')" title="點選切換走勢圖區間"><div class="l">${x[0]}</div><div class="n ${x[2]===null||x[2]===undefined?'na':(x[2]>=0?'pos':'neg')}">${x[2]===null||x[2]===undefined?'—':((x[2]>=0?'+':'')+x[2].toFixed(2)+'%')}</div></div>`).join('');
  return `<div class="ctop">
      <div><h3>${g.name}<span class="kchip ${k[1]}">${k[0]}</span></h3>
        <div class="code">${v.code} · ${sel}</div></div>
      <div class="navnow"><div class="v">${fnav(v)}</div><div class="d">${v.navLabel} · ${v.navDate||''}</div>${chgtxt?`<div class="chg ${chgcls}">${chgtxt}</div>`:''}</div>
    </div>
    <div class="retrow">${retHtml}</div>
    <div style="font-size:11.5px;color:var(--sub);margin:2px 2px 4px">${v.seriesLabel}${rk?'（區間：'+({ytd:'YTD',m1:'近1月',m3:'近3月',y1:'近1年',y3:'近3年'}[rk])+'，點同一按鈕可還原全部走勢）':''}</div>
    ${lineChart(v, rk)}
    <div class="cnote">${g.note||''}</div>`;
}
function switchVar(gi, idx){
  cardIdx[gi]=idx;
  chartRange[gi]=null;
  document.getElementById('card'+gi).innerHTML = cardHTML(DATA.groups[gi], gi, idx);
}
function setChartRange(gi, key){
  chartRange[gi] = (chartRange[gi]===key) ? null : key;
  document.getElementById('card'+gi).innerHTML = cardHTML(DATA.groups[gi], gi, cardIdx[gi]);
}
function cards(){
  const box=document.getElementById('cards');
  DATA.groups.forEach((g,gi)=>{
    box.insertAdjacentHTML('beforeend', `<div class="card" id="card${gi}">${cardHTML(g,gi,0)}</div>`);
  });
}
/* ---- 手機好讀版：一檔一列，大字最新淨值，適合每日截圖追蹤 ---- */
function mobileHTML(g, gi, idx){
  const v=g.variants[idx], r=v.returns||{}, k=kmap[g.kind];
  const sel = g.variants.length>1
    ? `<select class="vsel vsel-sm" aria-label="切換幣別/級別" onchange="switchMob(${gi}, this.selectedIndex)">`+
      g.variants.map((x,i)=>`<option ${i===idx?'selected':''}>${x.label}</option>`).join('')+`</select>`
    : `<span class="curbadge curbadge-sm">${v.currency||v.label||''}</span>`;
  const chgtxt=(v.change!==null&&v.change!==undefined?((v.change>=0?'+':'')+v.change+' '):'')+
    ((v.changePct===null||v.changePct===undefined)?'':((v.changePct>=0?'+':'')+v.changePct+'%'));
  const chgcls=(v.changePct>=0)?'pos':'neg';
  const rets=[['YTD',r.ytd],['1月',r.m1],['3月',r.m3],['1年',r.y1],['3年',r.y3]];
  const retHtml=rets.map(x=>`<div class="mr"><div class="l">${x[0]}</div><div class="n ${x[1]===null||x[1]===undefined?'na':(x[1]>=0?'pos':'neg')}">${x[1]===null||x[1]===undefined?'—':((x[1]>=0?'+':'')+x[1].toFixed(1)+'%')}</div></div>`).join('');
  return `<div class="mtop">
      <div style="min-width:0"><div class="mname"><span class="kchip ${k[1]}">${k[0]}</span> ${g.name}</div>
        <div class="msub">${v.code} · ${sel} · ${v.navDate||''}</div></div>
      <div class="mnav"><div class="v">${fnav(v)}</div><div class="u">${v.navLabel}</div>${chgtxt?`<div class="c ${chgcls}">${chgtxt}</div>`:''}</div>
    </div>
    <div class="mrets">${retHtml}</div>
    <div class="mchart">${lineChart(v, null)}</div>
    <div class="mimgbtns">
      <button class="imgbtn" onclick="saveCardImage(${gi},${idx},this)">📤 存到相簿</button>
      <button class="imgbtn" onclick="copyCardImage(${gi},${idx},this)">📋 複製圖片</button>
    </div>`;
}
function switchMob(gi, idx){
  mobSel[gi]=idx;
  document.getElementById('mrow'+gi).innerHTML = mobileHTML(DATA.groups[gi], gi, idx);
}
function mobile(){
  const box=document.getElementById('mobileview');
  box.insertAdjacentHTML('beforeend', `<button class="copybtn" onclick="copyNavs(this)">📋 一鍵複製最新淨值</button>`);
  box.insertAdjacentHTML('beforeend', `<button class="copybtn ghost" onclick="toggleCopyPanel(this)">☑️ 自訂複製項目／幣別</button>`);
  box.insertAdjacentHTML('beforeend', `<div class="cppanel" id="cppanel"></div>`);
  box.insertAdjacentHTML('beforeend', `<div class="imgbar">
      <button class="copybtn ghost" style="margin-bottom:0" onclick="saveAllImage(this)">📤 存整頁圖片</button>
      <button class="copybtn ghost" style="margin-bottom:0" onclick="copyAllImage(this)">📋 複製整頁圖片</button>
    </div>`);
  DATA.groups.forEach((g,gi)=>{
    box.insertAdjacentHTML('beforeend', `<div class="mrow" id="mrow${gi}">${mobileHTML(g,gi,0)}</div>`);
  });
  box.insertAdjacentHTML('beforeend', `<div class="mcmp" id="mcmp"></div>`);
  box.insertAdjacentHTML('beforeend',
    `<div class="mnote">資料更新：${DATA.updated}（台北）· 紅漲綠跌<br>ETF＝市價不含息報酬；基金/全委＝官方含息含撥回。僅供參考，非投資建議。</div>`);
}


/* ---- 台股基金比較：多多 vs 經貿 vs 台優龍（元大投信官方逐日淨值自算） ---- */
const CMP = DATA.compare || null;
let cmpRange = 'y1';
const CMP_RANGES = [['ytd','YTD'],['y1','近1年'],['y3','近3年'],['y5','近5年'],['common','2020/3 以來']];
const CMP_RLBL = Object.fromEntries(CMP_RANGES);
function cmpFrom(key, lastIso){
  const last=new Date(lastIso);
  if(key==='ytd') return new Date(last.getFullYear(),0,1);
  if(key==='y1'){const d=new Date(last); d.setFullYear(d.getFullYear()-1); return d;}
  if(key==='y3'){const d=new Date(last); d.setFullYear(d.getFullYear()-3); return d;}
  if(key==='y5'){const d=new Date(last); d.setFullYear(d.getFullYear()-5); return d;}
  const c=new Date(CMP.commonStart); c.setDate(c.getDate()-1); return c;   // 共同起點前一交易日（台優龍成立日淨值 10）
}
/* 多線圖：各檔在區間起點前一筆（<起點最近一筆）重設為 100 */
function cmpChart(key, H){
  H = H||230;
  const W=560,padL=8,padR=8,padT=16,padB=20;
  const last = CMP.funds.map(f=>f.navDate).sort().slice(-1)[0];
  const from = cmpFrom(key, last);
  const lines = CMP.funds.map(f=>{
    const s=f.series; let i0=0;
    // 基準＝區間起點當日或之前最近一筆（與表格期間報酬同口徑）
    for(let i=0;i<s.length;i++){ if(new Date(s[i][0])<=from) i0=i; else break; }
    if(new Date(s[0][0])>from) i0=0;
    const base=s[i0][1];
    const pts=s.slice(i0).map(p=>[p[0], +(p[1]/base*100).toFixed(2)]);
    return {f, pts, end: pts[pts.length-1][1]};
  });
  // 共同 x 軸：取所有日期聯集（三檔交易日幾乎一致）
  const dateSet=new Set(); lines.forEach(l=>l.pts.forEach(p=>dateSet.add(p[0])));
  const dates=[...dateSet].sort(); const n=dates.length;
  if(n<2) return '<div class="cnote">資料不足</div>';
  const di=Object.fromEntries(dates.map((d,i)=>[d,i]));
  let mn=Infinity,mx=-Infinity;
  lines.forEach(l=>l.pts.forEach(p=>{mn=Math.min(mn,p[1]);mx=Math.max(mx,p[1]);}));
  const pad=(mx-mn)*0.08; mn-=pad; mx+=pad;
  const X=i=>padL+(W-padL-padR)*i/(n-1);
  const Y=v=>padT+(H-padT-padB)*(1-(v-mn)/(mx-mn));
  let paths='';
  lines.forEach(l=>{
    let d='';
    l.pts.forEach((p,j)=>{d+=(j?'L':'M')+X(di[p[0]]).toFixed(1)+' '+Y(p[1]).toFixed(1)+' ';});
    paths+=`<path d="${d}" fill="none" stroke="${l.f.color}" stroke-width="2" stroke-linejoin="round" stroke-linecap="round"/>`;
    paths+=`<circle class="hcz" data-k="${l.f.code}" r="3.4" fill="${l.f.color}" opacity="0"/>`;
  });
  let base='';
  if(100>=mn && 100<=mx){
    const yb=Y(100).toFixed(1);
    base=`<line x1="${padL}" y1="${yb}" x2="${W-padR}" y2="${yb}" stroke="#c7d2e3" stroke-width="1" stroke-dasharray="3 3"/><text x="${W-padR}" y="${(+yb-3)}" text-anchor="end" class="axislbl">100</text>`;
  }
  // 網格：y 軸 3 條
  let grid='';
  for(let k=1;k<=3;k++){ const v=mn+(mx-mn)*k/4; const y=Y(v).toFixed(1);
    grid+=`<line x1="${padL}" y1="${y}" x2="${W-padR}" y2="${y}" stroke="#eef2f8" stroke-width="1"/><text x="${padL}" y="${(+y-2)}" class="axislbl">${v.toFixed(0)}</text>`; }
  // hover 資料：每個日期 → 各檔值
  const byDate=dates.map((d,i)=>{ const row={x:+X(i).toFixed(1), d, v:{}};
    lines.forEach(l=>{ const p=l.pts.find(q=>q[0]===d); if(p){ row.v[l.f.code]=[+Y(p[1]).toFixed(1), p[1]]; } });
    return row; });
  const legend = lines.map(l=>`<span class="lg"><i style="background:${l.f.color}"></i>${l.f.short}<small class="${l.end>=100?'pos':'neg'}">${l.end>=100?'+':''}${(l.end-100).toFixed(1)}%</small></span>`).join('');
  const lbl0=dates[0].slice(2), lbl1=dates[n-1].slice(2);
  return `<div class="cmplegend">${legend}</div>
  <div class="chartbox"><svg viewBox="0 0 ${W} ${H}" data-cmp='${JSON.stringify(byDate)}' data-w="${W}" data-h="${H}" onmousemove="hoverCmp(event)" onmouseleave="hideCmp(event)" ontouchstart="hoverCmp(event.touches[0], event.currentTarget)" ontouchmove="hoverCmp(event.touches[0], event.currentTarget)">
    ${grid}${base}${paths}
    <line class="vln" x1="0" y1="${padT}" x2="0" y2="${H-padB}" stroke="#9aa8bd" stroke-width="1" stroke-dasharray="2 2" opacity="0"/>
    <text x="${padL}" y="${H-5}" class="axislbl">${lbl0}</text>
    <text x="${W-padR}" y="${H-5}" text-anchor="end" class="axislbl">${lbl1}</text>
  </svg><div class="tip"></div></div>`;
}
function hoverCmp(e, svgEl){
  const svg=svgEl||e.currentTarget; const rows=JSON.parse(svg.getAttribute('data-cmp'));
  const W=+svg.getAttribute('data-w'), H=+svg.getAttribute('data-h');
  const box=svg.getBoundingClientRect();
  const rx=(e.clientX-box.left)/box.width*W;
  let best=rows[0],bd=1e9;
  for(const r of rows){const dd=Math.abs(r.x-rx);if(dd<bd){bd=dd;best=r;}}
  const vl=svg.querySelector('.vln'); vl.setAttribute('x1',best.x); vl.setAttribute('x2',best.x); vl.setAttribute('opacity','1');
  let html=best.d, topY=H;
  CMP.funds.forEach(f=>{
    const dot=svg.querySelector('.hcz[data-k="'+f.code+'"]'); const v=best.v[f.code];
    if(v){ dot.setAttribute('cx',best.x); dot.setAttribute('cy',v[0]); dot.setAttribute('opacity','1'); topY=Math.min(topY,v[0]);
      html+=`<br><span style="color:${f.color==='#003580'?'#8fb3ff':f.color}">●</span> ${f.short} <b>${v[1].toFixed(1)}</b>（${v[1]>=100?'+':''}${(v[1]-100).toFixed(1)}%）`; }
    else dot.setAttribute('opacity','0');
  });
  const tip=svg.parentNode.querySelector('.tip');
  tip.innerHTML=html; tip.style.left=(best.x/W*box.width)+'px'; tip.style.top=(topY/H*box.height)+'px'; tip.style.opacity='1';
}
function hideCmp(e){ const svg=e.currentTarget; svg.querySelectorAll('.hcz').forEach(d=>d.setAttribute('opacity','0')); svg.querySelector('.vln').setAttribute('opacity','0'); svg.parentNode.querySelector('.tip').style.opacity='0'; }
function setCmpRange(k){ cmpRange=k; renderCmp(); }
function cmpRangeBtns(){
  return `<div class="cmpctl"><span class="lbl">報酬指數（起點＝100）</span>`+
    CMP_RANGES.map(([k,l])=>`<button class="rbtn${cmpRange===k?' on':''}" onclick="setCmpRange('${k}')">${l}</button>`).join('')+`</div>`;
}
const fmtNum = (v,d)=> (v===null||v===undefined)?'<span class="na">—</span>':v.toLocaleString('zh-TW',{minimumFractionDigits:d===undefined?2:d,maximumFractionDigits:d===undefined?2:d});
const fmtYi = v => (v===null||v===undefined)?'<span class="na">—</span>':(v/1e8).toLocaleString('zh-TW',{maximumFractionDigits:1})+' 億';
/* 一列：label、各檔值（getter）、是否標最佳（高者佳 'hi' / 低者佳 'lo' / 不標） */
function cmpRow(label, get, best, fmt){
  const vals=CMP.funds.map(get);
  let bi=-1;
  if(best){ const nums=vals.map((v,i)=>typeof v==='number'?[v,i]:null).filter(Boolean);
    if(nums.length>1){ nums.sort((a,b)=>best==='hi'?b[0]-a[0]:a[0]-b[0]); bi=nums[0][1]; } }
  return `<tr><td>${label}</td>`+vals.map((v,i)=>`<td class="${i===bi?'best':''}">${fmt?fmt(v,i):fmtPct(v)}</td>`).join('')+`</tr>`;
}
function cmpSec(t){ return `<tr class="sec"><td colspan="${CMP.funds.length+1}">${t}</td></tr>`; }
function cmpTable(){
  const head=`<thead><tr><th>項目</th>`+CMP.funds.map(f=>`<th class="fh"><i style="background:${f.color}"></i>${f.short}<small>${f.name}<br>${f.cls}</small></th>`).join('')+`</tr></thead>`;
  const lp=CMP.funds[0].lipper||{}; const lpAs = lp.asOf? (lp.asOf.slice(0,4)+'/'+lp.asOf.slice(4)) : '';
  const body=`<tbody>
    ${cmpSec('最新淨值')}
    ${cmpRow('淨值（新台幣）', f=>f.nav, null, (v,i)=>`<span class="big">${fmtNum(v)}</span><br><small>${CMP.funds[i].navDate||''}</small>`)}
    ${cmpRow('當日漲跌', f=>f.changePct, null, (v,i)=>{const f=CMP.funds[i]; return (f.change!==null&&f.change!==undefined?`<small>${f.change>=0?'+':''}${f.change}</small> `:'')+fmtPct(v);})}
    ${cmpSec('累積報酬率（非年化，官方逐日淨值自算；三檔皆不配息級別＝含息總報酬）')}
    ${cmpRow('今年以來', f=>f.returns.ytd, 'hi')}
    ${cmpRow('近 1 月', f=>f.returns.m1, 'hi')}
    ${cmpRow('近 3 月', f=>f.returns.m3, 'hi')}
    ${cmpRow('近 6 月', f=>f.returns.m6, 'hi')}
    ${cmpRow('近 1 年', f=>f.returns.y1, 'hi')}
    ${cmpRow('近 3 年', f=>f.returns.y3, 'hi')}
    ${cmpRow('近 5 年', f=>f.returns.y5, 'hi')}
    ${cmpRow('2020/3/23 以來<br><small>（台優龍成立日，共同起點）</small>', f=>f.returns.common, 'hi')}
    ${cmpSec('風險（近 1 年／近 3 年）')}
    ${cmpRow('年化波動度（近1年）', f=>f.vol1y, 'lo', v=>v===null||v===undefined?'<span class="na">—</span>':v.toFixed(2)+'%')}
    ${cmpRow('最大回撤（近1年）', f=>f.mdd1y, 'hi')}
    ${cmpRow('最大回撤（近3年）', f=>f.mdd3y, 'hi')}
    ${cmpRow('近1年最高淨值', f=>f.hi1y, null, (v,i)=>fmtNum(v)+`<br><small>${CMP.funds[i].hi1yDate||''}</small>`)}
    ${cmpRow('近1年最低淨值', f=>f.lo1y, null, (v,i)=>fmtNum(v)+`<br><small>${CMP.funds[i].lo1yDate||''}</small>`)}
    ${cmpRow('距近1年高點', f=>(f.nav&&f.hi1y)?+((f.nav/f.hi1y-1)*100).toFixed(2):null, 'hi')}
    ${cmpSec('基本資料（元大投信官網）')}
    ${cmpRow('基金規模', f=>f.sizeNtd, null, (v,i)=>fmtYi(v)+(CMP.funds[i].sizeDate?`<br><small>${CMP.funds[i].sizeDate}</small>`:''))}
    ${cmpRow('成立日', f=>f.inception, null, v=>v||'—')}
    ${cmpRow('風險等級', f=>f.risk, null, v=>v||'—')}
    ${cmpRow('配息', f=>f.payout, null, v=>v||'—')}
    ${cmpRow('最低申購／定期定額', f=>f.minBuy, null, (v,i)=>(v?fmtNum(v,0):'—')+' / '+(CMP.funds[i].minRsp?fmtNum(CMP.funds[i].minRsp,0):'—'))}
    ${cmpSec('官網揭露報酬（理柏 Lipper，截至 '+lpAs+' 月底，累積）')}
    ${cmpRow('今年以來', f=>(f.lipper||{}).ytd, 'hi')}
    ${cmpRow('近 1 年', f=>(f.lipper||{}).y1, 'hi')}
    ${cmpRow('近 3 年', f=>(f.lipper||{}).y3, 'hi')}
    ${cmpRow('近 5 年', f=>(f.lipper||{}).y5, 'hi')}
    ${cmpRow('成立以來', f=>(f.lipper||{}).incept, null)}
  </tbody>`;
  return `<div style="overflow-x:auto"><table class="cmpt">${head}${body}</table></div>`;
}
function renderCmp(){
  const box=document.getElementById('cmpcard'); if(!box) return;
  if(!CMP||!CMP.funds||!CMP.funds.length){ box.style.display='none'; return; }
  box.innerHTML=`<div class="hd">⚖️ 台股基金比較：多多 vs 經貿 vs 台優龍 <span class="hdsub">新台幣A類型不配息 · 元大投信官方逐日淨值</span></div>
    <div class="cmpwrap">
      <div class="cmpchart">${cmpRangeBtns()}${cmpChart(cmpRange, 230)}
        <div class="cnote">各檔於區間起點重設為 100，曲線＝累積報酬指數；滑鼠移過可看三檔同日數值。★＝該列表現最佳（報酬取高、波動取低、回撤取淺）。</div>
      </div>
      <div style="min-width:0">${cmpTable()}</div>
    </div>
    <div class="tblnote">※ 資料源：${CMP.source}。期間報酬＝最新淨值÷期初淨值−1（期初取該日或之前最近一筆；今年以來取去年最後一筆）；年化波動＝近1年日對數報酬標準差×√252；最大回撤＝區間內淨值相對前高的最大跌幅。三檔皆為不配息級別，淨值報酬即含息總報酬。上方總覽表「近3年」為鉅亨／晨星之年化值，與此表累積值口徑不同。</div>`;
  const m=document.getElementById('mcmp'); if(m) m.innerHTML=mobileCmpHTML();
}
function mobileCmpHTML(){
  if(!CMP||!CMP.funds||!CMP.funds.length) return '';
  const head=`<thead><tr><th></th>`+CMP.funds.map(f=>`<th class="fh"><i style="background:${f.color}"></i>${f.short}</th>`).join('')+`</tr></thead>`;
  const rows=[
    cmpRow('淨值', f=>f.nav, null, v=>`<span class="big">${fmtNum(v)}</span>`),
    cmpRow('當日', f=>f.changePct, null),
    cmpRow('今年以來', f=>f.returns.ytd, 'hi'),
    cmpRow('近1年', f=>f.returns.y1, 'hi'),
    cmpRow('近3年', f=>f.returns.y3, 'hi'),
    cmpRow('近1年最大回撤', f=>f.mdd1y, 'hi'),
    cmpRow('規模', f=>f.sizeNtd, null, v=>fmtYi(v)),
  ].join('');
  return `<div class="mname">⚖️ 多多 vs 經貿 vs 台優龍</div>
    <div class="msub">新台幣A不配息 · 元大投信官方淨值 · ${CMP.funds[0].navDate||''}</div>
    ${cmpRangeBtns()}${cmpChart(cmpRange, 170)}
    <div style="overflow-x:auto"><table>${head}<tbody>${rows}</tbody></table></div>`;
}

/* ---- 一鍵複製：版本 B（分類 + 完整名稱 + 淨值日 + 幣別，emoji 易讀） ---- */
const KGROUP={etf:'〔ETF〕', disc:'〔全委帳戶〕', fund:'〔共同基金〕'};
const curEmoji=c=>/美元|USD/i.test(c||'')?' 💵':(/日圓|日圆|JPY/i.test(c||'')?' 💴':'');
const dshort=d=>d?d.slice(5).replace('-','/'):'—';   // 2026-08-12 → 08/12
function fmtChgTxt(p){
  if(p===null||p===undefined) return '—';
  if(p===0) return '0.00%';
  return (p>0?'+':'−')+Math.abs(p).toFixed(2)+'%';
}
function chgArrow(p){
  if(p===null||p===undefined||p===0) return '⚪';
  return p>0?'🔴':'🟢';  // 紅漲綠跌（三角形 emoji 無綠色版，故用紅/綠圓點）
}
/* 一行淨值：例「10.52 +0.30%｜新台幣｜08/12」 */
function navLine(v){
  const nav=(v.nav!==null&&v.nav!==undefined)?v.nav:'—';
  return nav+' '+fmtChgTxt(v.changePct)+'｜'+(v.currency||v.label||'')+'｜'+dshort(v.navDate);
}
/* sel = Set of "gi:vi"（勾選的 group:variant）。未帶則用各組目前選的幣別（＝一鍵複製舊行為） */
function buildCopyText(sel){
  if(!sel){ sel=new Set(); DATA.groups.forEach((g,gi)=>sel.add(gi+':'+(mobSel[gi]||0))); }
  const has=(gi,vi)=>sel.has(gi+':'+vi);
  const grpSelVars=(g,gi)=>g.variants.map((v,vi)=>[v,vi]).filter(([v,vi])=>has(gi,vi));
  const d=(DATA.updated||'').slice(0,10).replace(/-/g,'/');
  const DIV='————————————————';
  const kinds=['etf','disc','fund'].filter(k=>
    DATA.groups.some((g,gi)=>g.kind===k && grpSelVars(g,gi).length));
  let out='📈 元大基金淨值追蹤\n🗓 '+d+'（台北）\n';
  kinds.forEach((kind,ki)=>{
    const star = ki>0 ? '🌟' : '';       // 首組不加星，其餘組標題加 🌟
    out+='\n'+star+KGROUP[kind]+'\n\n';   // 標題後空一行
    const gs=DATA.groups.map((g,gi)=>[g,gi]).filter(([g,gi])=>g.kind===kind && grpSelVars(g,gi).length);
    gs.forEach(([g,gi],j)=>{
      const vs=grpSelVars(g,gi);
      if(vs.length===1){                  // 單一幣別：名稱行帶漲跌燈（維持舊格式）
        const [v]=vs[0];
        out+=chgArrow(v.changePct)+' '+g.name+'\n'+navLine(v)+'\n';
      }else{                              // 多幣別：名稱一行，各幣別各一行帶燈
        out+='📊 '+g.name+'\n';
        vs.forEach(([v])=>{ out+=chgArrow(v.changePct)+' '+navLine(v)+'\n'; });
      }
      if(j<gs.length-1) out+='\n';        // 檔與檔之間空一行
    });
    if(ki<kinds.length-1) out+=DIV+'\n';  // 組間分隔線（最後一組不放）
  });
  return out;
}

/* ---- 自訂複製：勾選要哪幾檔／哪些幣別 ---- */
let copySel=null;                         // Set of "gi:vi"，延後初始化
function initCopySel(){                   // 預設＝各組目前顯示的幣別
  copySel=new Set(); DATA.groups.forEach((g,gi)=>copySel.add(gi+':'+(mobSel[gi]||0)));
}
function copyPanelHTML(){
  let h='<div class="cpsel-head"><span>勾選要複製的項目／幣別</span>'+
        '<span><button class="cpmini" onclick="copySelAll(true)">全選</button>'+
        '<button class="cpmini" onclick="copySelAll(false)">清除</button></span></div>';
  DATA.groups.forEach((g,gi)=>{
    h+='<div class="cpgrp"><div class="cpgname">'+g.name+'</div><div class="cpvars">';
    g.variants.forEach((v,vi)=>{
      const on=copySel.has(gi+':'+vi);
      h+='<label class="cprow'+(on?' on':'')+'"><input type="checkbox" '+(on?'checked':'')+
         ' onchange="toggleCopySel('+gi+','+vi+',this)">'+(v.label||v.currency||'預設')+'</label>';
    });
    h+='</div></div>';
  });
  h+='<button class="copybtn" onclick="copySelected(this)">📋 複製所選文字（<span id="cpcount">'+
     copySel.size+'</span> 項）</button>';
  h+='<div class="imgbar"><button class="imgbtn" onclick="saveSelectedImage(this)">📤 存所選圖片</button>'+
     '<button class="imgbtn" onclick="copySelectedImage(this)">📋 複製所選圖片</button></div>';
  return h;
}
function toggleCopyPanel(btn){
  const p=document.getElementById('cppanel');
  if(!copySel) initCopySel();
  const open=p.classList.toggle('open');
  if(open) p.innerHTML=copyPanelHTML();
  btn.textContent=open?'▲ 收合自訂選取':'☑️ 自訂複製項目／幣別';
}
function toggleCopySel(gi,vi,el){
  const k=gi+':'+vi;
  if(el.checked) copySel.add(k); else copySel.delete(k);
  el.closest('.cprow').classList.toggle('on', el.checked);
  const c=document.getElementById('cpcount'); if(c) c.textContent=copySel.size;
}
function copySelAll(on){
  copySel=new Set();
  if(on) DATA.groups.forEach((g,gi)=>g.variants.forEach((v,vi)=>copySel.add(gi+':'+vi)));
  document.getElementById('cppanel').innerHTML=copyPanelHTML();
}
function copySelected(btn){
  if(!copySel||!copySel.size){ btn.textContent='⚠ 尚未勾選項目';
    setTimeout(()=>{btn.innerHTML='📋 複製所選（<span id="cpcount">0</span> 項）';},1400); return; }
  doCopy(buildCopyText(copySel), btn, '✓ 已複製 '+copySel.size+' 項');
}
function copyNavs(btn){ doCopy(buildCopyText(), btn, '✓ 已複製'); }
function doCopy(txt, btn, okLabel){
  const done=()=>{const o=btn.innerHTML;btn.textContent=okLabel;btn.classList.add('ok');
    setTimeout(()=>{btn.innerHTML=o;btn.classList.remove('ok');},1600);};
  if(navigator.clipboard&&navigator.clipboard.writeText){
    navigator.clipboard.writeText(txt).then(done).catch(()=>fallbackCopy(txt,done));
  }else{fallbackCopy(txt,done);}
}
function fallbackCopy(txt,done){
  const ta=document.createElement('textarea');ta.value=txt;
  ta.style.position='fixed';ta.style.top='0';ta.style.opacity='0';
  document.body.appendChild(ta);ta.focus();ta.select();
  try{document.execCommand('copy');done();}catch(e){alert('複製失敗，請手動選取');}
  document.body.removeChild(ta);
}

/* ---- 檢視切換：預設依螢幕寬度，選擇記在 localStorage ---- */
function setView(mob, save){
  document.body.classList.toggle('mob', mob);
  const bm=document.getElementById('btnMob'), bd=document.getElementById('btnDesk');
  if(bm) bm.classList.toggle('on', mob);
  if(bd) bd.classList.toggle('on', !mob);
  if(save){ try{ localStorage.setItem('yf_view', mob?'m':'d'); }catch(e){} }
}

/* ---- 圖片匯出（canvas，純內建、不依賴外部套件）：手機好讀版每檔卡片 / 整頁 一鍵複製圖片・存到相簿 ---- */
const CV_FONT = '"PingFang TC","Noto Sans TC","Microsoft JhengHei",sans-serif';
const CV = {
  ink:'#12233b', sub:'#5b6b82', line:'#e4e9f2', card:'#ffffff', bg:'#f4f6fb', chip:'#eef3fb',
  brand:'#003580', up:'#d13438', down:'#0a7c4a',
  kind:{etf:['ETF','#e7f0ff','#1763c6'], fund:['基金','#eafaf1','#0a7c4a'], disc:['全委','#fff1e6','#c25e12']}
};
function crRoundRect(ctx,x,y,w,h,r){
  ctx.beginPath();
  ctx.moveTo(x+r,y);
  ctx.lineTo(x+w-r,y); ctx.arcTo(x+w,y,x+w,y+r,r);
  ctx.lineTo(x+w,y+h-r); ctx.arcTo(x+w,y+h,x+w-r,y+h,r);
  ctx.lineTo(x+r,y+h); ctx.arcTo(x,y+h,x,y+h-r,r);
  ctx.lineTo(x,y+r); ctx.arcTo(x,y,x+r,y,r);
  ctx.closePath();
}
function crWrapCJK(ctx, text, maxW, font){
  ctx.font=font;
  if(ctx.measureText(text).width<=maxW) return [text];
  let lines=[], cur='';
  for(const ch of text){
    const t=cur+ch;
    if(ctx.measureText(t).width>maxW && cur){ lines.push(cur); cur=ch; }
    else cur=t;
  }
  if(cur) lines.push(cur);
  return lines.slice(0,2);
}
function crPctTxt(v){ return (v===null||v===undefined)?'—':((v>=0?'+':'')+v.toFixed(2)+'%'); }
function crFitFont(ctx, text, maxW, px, weight){
  let f=px;
  ctx.font=(weight?weight+' ':'')+f+'px '+CV_FONT;
  while(f>8 && ctx.measureText(text).width>maxW){ f-=0.5; ctx.font=(weight?weight+' ':'')+f+'px '+CV_FONT; }
  return f;
}

function crLineChart(ctx, x0, y0, f){
  const W=340,H=132,padL=6,padR=6,padT=14,padB=18;
  const s=f.series;
  if(!s||s.length<2){ crBarChart(ctx,x0,y0,f); return; }
  const ys=s.map(p=>p[1]); let mn=Math.min(...ys), mx=Math.max(...ys);
  if(mn===mx){mn-=1;mx+=1;}
  const pad=(mx-mn)*0.12; mn-=pad; mx+=pad;
  const n=s.length;
  const X=i=>x0+padL+(W-padL-padR)*i/(n-1);
  const Y=v=>y0+padT+(H-padT-padB)*(1-(v-mn)/(mx-mn));
  const up = s[n-1][1]>=s[0][1];
  const col = up?CV.up:CV.down;
  const rgb = up?'209,52,56':'10,124,74';
  const grad = ctx.createLinearGradient(0,y0+padT,0,y0+H-padB);
  grad.addColorStop(0, 'rgba('+rgb+',0.16)'); grad.addColorStop(1, 'rgba('+rgb+',0)');
  ctx.beginPath();
  s.forEach((p,i)=>{ const x=X(i), y=Y(p[1]); if(i===0) ctx.moveTo(x,y); else ctx.lineTo(x,y); });
  ctx.lineTo(X(n-1), y0+H-padB); ctx.lineTo(X(0), y0+H-padB); ctx.closePath();
  ctx.fillStyle=grad; ctx.fill();
  if(f.seriesType==='index' && 100>=mn && 100<=mx){
    const yb=Y(100);
    ctx.save(); ctx.strokeStyle='#c7d2e3'; ctx.lineWidth=1; ctx.setLineDash([3,3]);
    ctx.beginPath(); ctx.moveTo(x0+padL,yb); ctx.lineTo(x0+W-padR,yb); ctx.stroke(); ctx.restore();
    ctx.fillStyle=CV.sub; ctx.font='10px '+CV_FONT; ctx.textAlign='right'; ctx.textBaseline='alphabetic';
    ctx.fillText('100', x0+W-padR, yb-3);
  }
  ctx.beginPath();
  s.forEach((p,i)=>{ const x=X(i), y=Y(p[1]); if(i===0) ctx.moveTo(x,y); else ctx.lineTo(x,y); });
  ctx.strokeStyle=col; ctx.lineWidth=2; ctx.lineJoin='round'; ctx.lineCap='round'; ctx.stroke();
  ctx.fillStyle=CV.sub; ctx.font='10px '+CV_FONT; ctx.textBaseline='alphabetic';
  ctx.textAlign='left'; ctx.fillText(s[0][0].slice(2), x0+padL, y0+H-5);
  ctx.textAlign='right'; ctx.fillText(s[n-1][0].slice(2), x0+W-padR, y0+H-5);
}
function crBarChart(ctx, x0, y0, f){
  const r=f.returns||{};
  const items=[['YTD',r.ytd],['1月',r.m1],['3月',r.m3],['1年',r.y1],['3年',r.y3]];
  const vals=items.map(x=>x[1]).filter(v=>typeof v==='number');
  const mx=Math.max(1,...vals.map(Math.abs));
  const W=340,H=132,padB=18,zeroY=y0+(H-padB)/2+8,half=(H-padB)/2-6;
  const bw=(W-20)/items.length, gap=10;
  ctx.strokeStyle=CV.line; ctx.lineWidth=1;
  ctx.beginPath(); ctx.moveTo(x0+6,zeroY); ctx.lineTo(x0+W-6,zeroY); ctx.stroke();
  ctx.textBaseline='alphabetic';
  items.forEach((it,i)=>{
    const v=it[1]; const x=x0+10+i*bw;
    ctx.textAlign='center';
    if(typeof v==='number'){
      const h=Math.abs(v)/mx*half;
      const y=v>=0?zeroY-h:zeroY;
      const col=v>=0?CV.up:CV.down;
      crRoundRect(ctx, x+gap/2, y, bw-gap, Math.max(1,h), 3);
      ctx.fillStyle=col; ctx.fill();
      ctx.font='bold 10px '+CV_FONT; ctx.fillStyle=col;
      ctx.fillText((v>=0?'+':'')+v.toFixed(1), x+bw/2, v>=0?y-3:y+h+11);
    }else{
      ctx.font='10px '+CV_FONT; ctx.fillStyle=CV.sub;
      ctx.fillText('—', x+bw/2, zeroY+3);
    }
    ctx.font='10px '+CV_FONT; ctx.fillStyle=CV.sub;
    ctx.fillText(it[0], x+bw/2, y0+H-4);
  });
}

const CR_PAD=16, CR_CW=340, CR_CARD_W=CR_CW+CR_PAD*2;
function crHeaderMetrics(ctx, g, idx){
  const v=g.variants[idx];
  const chgtxt=(v.changePct===null||v.changePct===undefined)?'':crPctTxt(v.changePct);
  const nameFont='bold 15px '+CV_FONT;
  const nameLines=crWrapCJK(ctx, g.name, 190, nameFont);
  const nameBlockH = 20 + (nameLines.length-1)*20 + 18;
  const navBlockH = 24 + 15 + (chgtxt?16:0);
  return {v, nameLines, chgtxt, headerBlockH: Math.max(nameBlockH, navBlockH, 40)};
}
function crCardHeight(ctx, g, idx){
  const {headerBlockH} = crHeaderMetrics(ctx, g, idx);
  return CR_PAD + headerBlockH + 10 + 40 + 8 + 14 + 132 + 10 + 16 + CR_PAD;
}
function crDrawCard(ctx, x0, y0, g, idx){
  const k=CV.kind[g.kind];
  const hm=crHeaderMetrics(ctx, g, idx), v=hm.v;
  const cardH=crCardHeight(ctx, g, idx);
  crRoundRect(ctx, x0, y0, CR_CARD_W, cardH, 14);
  ctx.fillStyle=CV.card; ctx.fill(); ctx.strokeStyle=CV.line; ctx.lineWidth=1; ctx.stroke();
  let y=y0+CR_PAD;
  ctx.font='bold 10px '+CV_FONT; ctx.textBaseline='middle';
  const chipW=ctx.measureText(k[0]).width+14;
  crRoundRect(ctx, x0+CR_PAD, y, chipW, 16, 8); ctx.fillStyle=k[1]; ctx.fill();
  ctx.fillStyle=k[2]; ctx.textAlign='center'; ctx.fillText(k[0], x0+CR_PAD+chipW/2, y+8);
  ctx.font='bold 15px '+CV_FONT; ctx.fillStyle=CV.ink; ctx.textAlign='left'; ctx.textBaseline='alphabetic';
  ctx.fillText(hm.nameLines[0], x0+CR_PAD+chipW+6, y+12);
  hm.nameLines.slice(1).forEach((ln,i)=>ctx.fillText(ln, x0+CR_PAD, y+12+(i+1)*20));
  const nameEndY = y+12+(hm.nameLines.length-1)*20;
  const selLabel=v.label||v.currency||'';
  {
    const yln=nameEndY+18; let cx=x0+CR_PAD;
    ctx.textAlign='left'; ctx.textBaseline='alphabetic';
    ctx.font='11px '+CV_FONT; ctx.fillStyle=CV.sub;
    ctx.fillText(v.code, cx, yln); cx+=ctx.measureText(v.code).width;
    if(selLabel){
      ctx.font='11px '+CV_FONT; ctx.fillStyle=CV.sub;
      ctx.fillText(' · ', cx, yln); cx+=ctx.measureText(' · ').width;
      ctx.font='bold 12px '+CV_FONT; ctx.fillStyle=CV.brand;
      ctx.fillText(selLabel, cx, yln); cx+=ctx.measureText(selLabel).width;
    }
    if(v.navDate){
      ctx.font='11px '+CV_FONT; ctx.fillStyle=CV.sub;
      ctx.fillText(' · '+v.navDate, cx, yln);
    }
  }
  ctx.textAlign='right';
  ctx.font='bold 22px '+CV_FONT; ctx.fillStyle=CV.ink;
  ctx.fillText(String(fnav(v)), x0+CR_CARD_W-CR_PAD, y+20);
  ctx.font='11px '+CV_FONT; ctx.fillStyle=CV.sub;
  ctx.fillText(v.navLabel||'', x0+CR_CARD_W-CR_PAD, y+35);
  if(hm.chgtxt){
    ctx.font='bold 12.5px '+CV_FONT; ctx.fillStyle=(v.changePct>=0)?CV.up:CV.down;
    ctx.fillText(hm.chgtxt, x0+CR_CARD_W-CR_PAD, y+51);
  }
  y=y0+CR_PAD+hm.headerBlockH+10;
  const r=v.returns||{};
  const rets=[['YTD',r.ytd],['近1月',r.m1],['近3月',r.m3],['近1年',r.y1],['近3年',r.y3]];
  const bw=(CR_CW-16)/5, gap=4;
  rets.forEach((it,i)=>{
    const bx=x0+CR_PAD+i*bw;
    crRoundRect(ctx, bx+gap/2, y, bw-gap, 40, 8); ctx.fillStyle='#f7f9fd'; ctx.fill();
    ctx.textAlign='center'; ctx.textBaseline='alphabetic';
    ctx.font='9.5px '+CV_FONT; ctx.fillStyle=CV.sub; ctx.fillText(it[0], bx+bw/2, y+15);
    const pctTxt=crPctTxt(it[1]);
    crFitFont(ctx, pctTxt, bw-gap-6, 12.5, 'bold');
    ctx.fillStyle=(it[1]===null||it[1]===undefined)?'#a9b4c4':(it[1]>=0?CV.up:CV.down);
    ctx.fillText(pctTxt, bx+bw/2, y+31);
  });
  y+=40+8;
  ctx.font='11px '+CV_FONT; ctx.fillStyle=CV.sub; ctx.textAlign='left';
  ctx.fillText(v.seriesLabel||'', x0+CR_PAD, y+11);
  y+=14;
  crLineChart(ctx, x0+CR_PAD, y, v);
  y+=132+10;
  ctx.font='10.5px '+CV_FONT; ctx.fillStyle=CV.sub; ctx.textAlign='left'; ctx.textBaseline='alphabetic';
  ctx.fillText('資料更新：'+(DATA.updated||'')+'（台北）· 元大基金淨值與表現', x0+CR_PAD, y+11);
  return cardH;
}
function crMakeCanvas(w,h,scale){
  const c=document.createElement('canvas');
  c.width=Math.round(w*scale); c.height=Math.round(h*scale);
  const ctx=c.getContext('2d'); ctx.scale(scale,scale);
  return {c, ctx};
}
function renderCardCanvas(gi, idx){
  const g=DATA.groups[gi];
  const probe=document.createElement('canvas').getContext('2d');
  const cardH=crCardHeight(probe, g, idx);
  const {c, ctx}=crMakeCanvas(CR_CARD_W, cardH, 2.5);
  ctx.fillStyle=CV.bg; ctx.fillRect(0,0,CR_CARD_W,cardH);
  crDrawCard(ctx, 0, 0, g, idx);
  return c;
}
/* items = [[g, idx], ...]，idx 為該 group 要畫的 variant index */
function renderImagesCanvas(items, titleSuffix){
  const probe=document.createElement('canvas').getContext('2d');
  const titleH=64, gap=12, outer=14;
  const heights=items.map(([g,idx])=>crCardHeight(probe, g, idx));
  let totalH=titleH + heights.reduce((a,b)=>a+b+gap,0) + 40;
  const W=CR_CARD_W+outer*2;
  const {c, ctx}=crMakeCanvas(W, totalH+outer, 2.5);
  ctx.fillStyle=CV.bg; ctx.fillRect(0,0,W,totalH+outer);
  ctx.fillStyle=CV.brand; ctx.font='bold 20px '+CV_FONT; ctx.textAlign='left'; ctx.textBaseline='alphabetic';
  ctx.fillText('元大基金淨值與表現'+(titleSuffix||''), outer, outer+22);
  ctx.font='12px '+CV_FONT; ctx.fillStyle=CV.sub;
  ctx.fillText('資料更新：'+(DATA.updated||'')+'（台北）· 紅漲綠跌', outer, outer+42);
  let y=outer+titleH;
  items.forEach(([g,idx],i)=>{
    crDrawCard(ctx, outer, y, g, idx);
    y+=heights[i]+gap;
  });
  ctx.font='10.5px '+CV_FONT; ctx.fillStyle=CV.sub; ctx.textAlign='left'; ctx.textBaseline='alphabetic';
  ctx.fillText('僅供參考，非投資建議；實際淨值以各機構公告為準。', outer, y+14);
  return c;
}
function renderFullPageCanvas(){
  return renderImagesCanvas(DATA.groups.map((g,gi)=>[g, mobSel[gi]||0]));
}
function selectedImageItems(){
  const items=[];
  DATA.groups.forEach((g,gi)=>g.variants.forEach((v,vi)=>{
    if(copySel && copySel.has(gi+':'+vi)) items.push([g,vi]);
  }));
  return items;
}
function btnFlash(btn, label, isErr){
  const o=btn.innerHTML; btn.textContent=label; btn.classList.add(isErr?'err':'ok');
  setTimeout(()=>{ btn.innerHTML=o; btn.classList.remove('ok','err'); }, isErr?2200:1600);
}
function crFileName(prefix){
  const d=(DATA.updated||'').slice(0,10).replace(/-/g,'');
  return prefix+'_'+d+'.png';
}
function crSave(canvas, fname, btn){
  canvas.toBlob(blob=>{
    if(!blob){ btnFlash(btn,'⚠ 產生圖片失敗', true); return; }
    const file=new File([blob], fname, {type:'image/png'});
    if(navigator.canShare && navigator.canShare({files:[file]})){
      navigator.share({files:[file], title:fname}).then(()=>btnFlash(btn,'✓ 已開啟分享')).catch(()=>{});
    }else{
      const url=URL.createObjectURL(blob);
      const a=document.createElement('a'); a.href=url; a.download=fname;
      document.body.appendChild(a); a.click(); document.body.removeChild(a);
      setTimeout(()=>URL.revokeObjectURL(url), 4000);
      btnFlash(btn,'✓ 已產生圖片，請儲存');
    }
  }, 'image/png');
}
function crCopy(canvas, btn){
  try{
    const blobPromise=new Promise((resolve,reject)=>canvas.toBlob(b=>b?resolve(b):reject(new Error('no blob')), 'image/png'));
    navigator.clipboard.write([new ClipboardItem({'image/png': blobPromise})])
      .then(()=>btnFlash(btn,'✓ 已複製圖片'))
      .catch(()=>btnFlash(btn,'⚠ 此瀏覽器不支援複製，改用存檔', true));
  }catch(e){ btnFlash(btn,'⚠ 此瀏覽器不支援複製，改用存檔', true); }
}
function saveCardImage(gi, idx, btn){ crSave(renderCardCanvas(gi, idx), crFileName(DATA.groups[gi].name), btn); }
function copyCardImage(gi, idx, btn){ crCopy(renderCardCanvas(gi, idx), btn); }
function saveAllImage(btn){ crSave(renderFullPageCanvas(), crFileName('元大基金淨值與表現'), btn); }
function copyAllImage(btn){ crCopy(renderFullPageCanvas(), btn); }
function saveSelectedImage(btn){
  if(!copySel||!copySel.size){ btnFlash(btn,'⚠ 尚未勾選項目', true); return; }
  crSave(renderImagesCanvas(selectedImageItems(),'（自選）'), crFileName('元大基金淨值與表現_自選'), btn);
}
function copySelectedImage(btn){
  if(!copySel||!copySel.size){ btnFlash(btn,'⚠ 尚未勾選項目', true); return; }
  crCopy(renderImagesCanvas(selectedImageItems(),'（自選）'), btn);
}

overview();cards();mobile();renderCmp();
(function(){
  let saved=null; try{ saved=localStorage.getItem('yf_view'); }catch(e){}
  const mob = saved ? (saved==='m') : window.matchMedia('(max-width:560px)').matches;
  setView(mob, false);
})();
</script>
</body>
</html>
"""

if __name__ == '__main__':
    main()
