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
import urllib.request, json, gzip, time, datetime, os, sys

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

def fetch_fund(fid, zh, note=''):
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
    return {
        'name': zh, 'code': fid, 'kind': 'fund', 'currency': d.get('classCurrencyName') or '新台幣',
        'nav': round(nav, 2) if nav else None, 'navLabel': '淨值', 'navDate': nav_date,
        'change': round(nav - prev, 2) if (nav and prev) else None,
        'changePct': pct(nav, prev) if (nav and prev) else _r(d.get('return1Day')),
        'inception': datetime.date.fromtimestamp(d['inceptionDate']).isoformat() if d.get('inceptionDate') else None,
        'returns': rets, 'series': series, 'seriesType': 'nav' if has else 'bars',
        'seriesLabel': '淨值走勢（成立以來）' if has else '各期間報酬率',
        'source': '鉅亨 fund.api（晨星官方淨值/各期報酬）', 'note': note,
    }

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
    {'name': '元大全球優質龍頭平衡基金', 'kind': 'fund',
     'note': '元大龍頭系列「抗波動」；跨國股債平衡基金（2022/08 成立）；A 類型不配息，含新台幣／美元級別',
     'variants': [
         {'code': 'A05158', 'label': '新台幣'},
         {'code': 'A05160', 'label': '美元'}]},
]

def build_data():
    fetch = {'etf': fetch_etf, 'fund': fetch_fund, 'disc': fetch_carteblanche}
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

def main():
    groups = build_data()
    payload = {
        'updated': datetime.datetime.now(TPE).strftime('%Y-%m-%d %H:%M'),
        'groups': groups,
    }
    here = os.path.dirname(os.path.abspath(__file__))
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
html,body{margin:0;background:var(--bg);color:var(--ink);
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
.card .ctop{display:flex;justify-content:space-between;align-items:flex-start;gap:10px}
.card .ctop>div:first-child{min-width:0}
.card h3{margin:0;font-size:16px;line-height:1.35}
.card .code{font-size:12px;color:var(--sub);margin-top:4px;display:flex;align-items:center;gap:6px;flex-wrap:wrap}
.vsel{font-family:inherit;font-size:12px;font-weight:700;color:var(--brand);background:var(--chip);border:1px solid var(--line);border-radius:7px;padding:3px 22px 3px 8px;cursor:pointer;
  appearance:none;-webkit-appearance:none;background-image:url("data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg' width='10' height='6'><path d='M1 1l4 4 4-4' stroke='%23003580' stroke-width='1.6' fill='none' stroke-linecap='round'/></svg>");background-repeat:no-repeat;background-position:right 7px center}
.vsel:hover{border-color:var(--brand2)}
.vsel-sm{font-size:11.5px;padding:1px 20px 1px 7px;background-position:right 6px center;font-weight:600}
.card .navnow{text-align:right;flex-shrink:0}
.card .navnow .v{font-size:22px;font-weight:800;line-height:1}
.card .navnow .d{font-size:11px;color:var(--sub);margin-top:3px;white-space:nowrap}
.card .navnow .chg{font-size:12.5px;font-weight:700;margin-top:3px}
.retrow{display:flex;gap:6px;margin:12px 0 6px;flex-wrap:wrap}
.ret{flex:1;min-width:52px;background:#f7f9fd;border:1px solid var(--line);border-radius:9px;padding:7px 6px;text-align:center}
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
.mrow .mtop{display:flex;justify-content:space-between;align-items:flex-start;gap:12px}
.mrow .mname{font-size:16px;font-weight:800;line-height:1.32}
.mrow .msub{font-size:12px;color:var(--sub);margin-top:4px;display:flex;gap:6px;align-items:center;flex-wrap:wrap}
.mrow .mnav{text-align:right;flex-shrink:0}
.mrow .mnav .v{font-size:27px;font-weight:800;line-height:1}
.mrow .mnav .u{font-size:11px;color:var(--sub);margin-top:3px;white-space:nowrap}
.mrow .mnav .c{font-size:13px;font-weight:800;margin-top:4px}
.mrets{display:flex;gap:4px;margin-top:11px}
.mrets .mr{flex:1;min-width:0;text-align:center;background:#f7f9fd;border:1px solid var(--line);border-radius:8px;padding:5px 2px}
.mrets .mr .l{font-size:9.5px;color:var(--sub)}
.mrets .mr .n{font-size:12.5px;font-weight:800;margin-top:2px}
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

  <div class="grid" id="cards"></div>
  </div><!-- /deskview -->

  <div id="mobileview"></div>

  <footer>
    <b>資料來源</b>：ETF（00990A）＝FinMind／TWSE 日線（市價收盤、<b>不含息</b>之市價報酬）；共同基金＝鉅亨 fund.api（晨星官方各期報酬）；全委帳戶＝鉅亨 co.cnyes 元大人壽全委 API（官方含撥回報酬與報酬指數）。<br>
    <b>說明</b>：有多幣別／配息級別的標的，於卡片右上下拉切換（總覽表顯示預設級別）。全委帳戶（元元致富·財富雙利/雙享）為投資型保單之類全權委託帳戶，走勢圖為「含撥回報酬指數（基準 100）」而非單位淨值曲線；「近1年/近3年」在成立未滿期間顯示「—」。本頁僅供參考，非投資建議；實際淨值以各機構公告為準。
  </footer>
</div>

<script>
const DATA = $DATA;
const fmtPct = v => (v===null||v===undefined) ? '<span class="na">—</span>' :
  '<span class="'+(v>=0?'pos':'neg')+'">'+(v>=0?'+':'')+v.toFixed(2)+'%</span>';
const kmap = {etf:['ETF','k-etf'],fund:['基金','k-fund'],disc:['全委','k-disc']};
const fnav = v => (v.nav!==null&&v.nav!==undefined)?v.nav:'—';

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
function lineChart(f){
  const s=f.series;
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
    : `<span>${v.currency||v.label||''}</span>`;
  const chgtxt=(v.change!==null&&v.change!==undefined?((v.change>=0?'+':'')+v.change+' '):'')+
    ((v.changePct===null||v.changePct===undefined)?'':((v.changePct>=0?'+':'')+v.changePct+'%'));
  const chgcls=(v.changePct>=0)?'pos':'neg';
  const rets=[['YTD',r.ytd],['近1月',r.m1],['近3月',r.m3],['近1年',r.y1],['近3年',r.y3]];
  const retHtml=rets.map(x=>`<div class="ret"><div class="l">${x[0]}</div><div class="n ${x[1]===null||x[1]===undefined?'na':(x[1]>=0?'pos':'neg')}">${x[1]===null||x[1]===undefined?'—':((x[1]>=0?'+':'')+x[1].toFixed(2)+'%')}</div></div>`).join('');
  return `<div class="ctop">
      <div><h3>${g.name}<span class="kchip ${k[1]}">${k[0]}</span></h3>
        <div class="code">${v.code} · ${sel}</div></div>
      <div class="navnow"><div class="v">${fnav(v)}</div><div class="d">${v.navLabel} · ${v.navDate||''}</div>${chgtxt?`<div class="chg ${chgcls}">${chgtxt}</div>`:''}</div>
    </div>
    <div class="retrow">${retHtml}</div>
    <div style="font-size:11.5px;color:var(--sub);margin:2px 2px 4px">${v.seriesLabel}</div>
    ${lineChart(v)}
    <div class="cnote">${g.note||''}</div>`;
}
function switchVar(gi, idx){
  document.getElementById('card'+gi).innerHTML = cardHTML(DATA.groups[gi], gi, idx);
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
    : `<span>${v.currency||v.label||''}</span>`;
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
    <div class="mrets">${retHtml}</div>`;
}
function switchMob(gi, idx){
  document.getElementById('mrow'+gi).innerHTML = mobileHTML(DATA.groups[gi], gi, idx);
}
function mobile(){
  const box=document.getElementById('mobileview');
  DATA.groups.forEach((g,gi)=>{
    box.insertAdjacentHTML('beforeend', `<div class="mrow" id="mrow${gi}">${mobileHTML(g,gi,0)}</div>`);
  });
  box.insertAdjacentHTML('beforeend',
    `<div class="mnote">資料更新：${DATA.updated}（台北）· 紅漲綠跌<br>ETF＝市價不含息報酬；基金/全委＝官方含息含撥回。僅供參考，非投資建議。</div>`);
}

/* ---- 檢視切換：預設依螢幕寬度，選擇記在 localStorage ---- */
function setView(mob, save){
  document.body.classList.toggle('mob', mob);
  const bm=document.getElementById('btnMob'), bd=document.getElementById('btnDesk');
  if(bm) bm.classList.toggle('on', mob);
  if(bd) bd.classList.toggle('on', !mob);
  if(save){ try{ localStorage.setItem('yf_view', mob?'m':'d'); }catch(e){} }
}

overview();cards();mobile();
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
