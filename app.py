import streamlit as st
import pandas as pd
import numpy as np
import plotly.express as px
import plotly.graph_objects as go
import io
import re
import json
import unicodedata
import urllib.request
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from dateutil.relativedelta import relativedelta

# --- ページ設定 ---
st.set_page_config(page_title="資産管理 & サイドFIRE統合シミュレーター", layout="wide")

# --- 「…」による文字切れ（見出し・数値・デルタすべて）を完全に防ぐカスタムCSS ---
st.markdown("""
<style>
[data-testid="stMetricLabel"],
[data-testid="stMetricLabel"] *,
[data-testid="stMetricValue"],
[data-testid="stMetricValue"] *,
[data-testid="stMetricDelta"],
[data-testid="stMetricDelta"] * {
    white-space: normal !important;
    overflow: visible !important;
    text-overflow: clip !important;
    -webkit-line-clamp: unset !important;
    display: block !important;
    max-width: 100% !important;
}
[data-testid="stMetricValue"] {
    font-size: 1.55rem !important;
    line-height: 1.25 !important;
}
[data-testid="stMetricLabel"] {
    font-size: 0.9rem !important;
    line-height: 1.3 !important;
}
</style>
""", unsafe_allow_html=True)

st.title("💎 資産管理 & サイドFIRE 統合シミュレーター")

# ==============================================================================
# 1. バックアップ用マスター辞書（通信制限・スクレイピング遮断時のフォールバック用）
# ==============================================================================
FALLBACK_STOCK_DIV_MASTER = {
    '8058': (100.0, '今期予想'),   # 三菱商事
    '8316': (120.0, '今期予想'),   # 三井住友FG (3分割後基準)
    '8306': (60.0,  '今期予想'),   # 三菱UFJ
    '8411': (130.0, '今期予想'),   # みずほFG
    '2914': (194.0, '今期予想'),   # JT
    '8001': (200.0, '今期予想'),   # 伊藤忠商事
    '8031': (100.0, '今期予想'),   # 三井物産 (2分割後基準)
    '9432': (5.2,   '今期予想'),   # NTT
    '9433': (75.0,  '今期予想'),   # KDDI (2分割後基準)
    '9434': (8.6,   '今期予想'),   # ソフトバンク (10分割後基準)
    '8766': (162.0, '今期予想'),   # 東京海上HD
    '8591': (120.0, '今期予想'),   # オリックス
    '7203': (90.0,  '今期予想'),   # トヨタ自動車
    '4502': (196.0, '今期予想'),   # 武田薬品
    '1928': (135.0, '今期予想'),   # 積水ハウス
    '1489': (2250.0, '直近実績'),  # NEXT FUNDS 日経平均高配当株50指数連動型上場投信
    '1577': (880.0,  '直近実績'),  # NEXT FUNDS 野村日本株高配当70連動型上場投信
    '2558': (0.0,    '無配'),      # MAXIS米国株式(S&P500)上場投信
    'VYM':  (3.75,   '直近実績'),  # USD
    'SPYD': (1.85,   '直近実績'),  # USD
    'HDV':  (4.10,   '直近実績'),  # USD
    'JEPI': (4.30,   '直近実績'),  # USD
    'JEPQ': (5.10,   '直近実績'),  # USD
    'VIG':  (3.35,   '直近実績'),  # USD
    'VOO':  (6.70,   '直近実績'),  # USD
    'VTI':  (3.65,   '直近実績'),  # USD
    'QQQ':  (2.85,   '直近実績'),  # USD
    'AGG':  (3.60,   '直近実績'),  # USD
    'BND':  (2.70,   '直近実績'),  # USD
}

FALLBACK_TRUST_DIV_MASTER = [
    ('SBI日本高配当株式', 410.0, 3.6, '1,4,7,10'),
    ('楽天・高配当株式・米国ファンド', 380.0, 3.3, '2,5,8,11'),
    ('楽天・米国高配当株式', 380.0, 3.3, '2,5,8,11'),
    ('SBI・V・米国高配当株式', 350.0, 3.0, '2,5,8,11'),
    ('SBI・iシェアーズ・米国高配当株式', 350.0, 3.0, '2,5,8,11'),
    ('アライアンス・バーンスタイン・米国成長株投信Dコース', 2400.0, 18.0, '毎月'),
    ('アライアンス・バーンスタイン・米国成長株投信Cコース', 2000.0, 16.0, '毎月'),
    ('インベスコ 世界厳選株式オープン', 1800.0, 17.0, '毎月'),
    ('ピクテ・グローバル・インカム株式', 360.0, 12.0, '毎月'),
]

# ==============================================================================
# 2. ユーティリティ & 金額フォーマット関数（5桁万円 ➔ 〇億〇万円 自動変換対応）
# ==============================================================================

def clean_num(val):
    """文字列から数値(float)へ安全に変換する"""
    if pd.isna(val):
        return 0.0
    if isinstance(val, (int, float, np.number)):
        return float(val)
    s = unicodedata.normalize('NFKC', str(val))
    for unit in [',', '円', '万円', '千円', '万口', '株', '口', 'USD', 'US$', '$', '%', '％', '+', ' ', ' ']:
        s = s.replace(unit, '')
    s = s.strip()
    if s in ['', '-', '--', '---', 'nan', 'None', 'N/A', '未定']:
        return 0.0
    m = re.search(r'[-+]?[0-9]+(?:\.[0-9]+)?', s)
    if m:
        try:
            return float(m.group(0))
        except ValueError:
            return 0.0
    return 0.0

def fmt_man(man_val, signed=False):
    """
    「万円」単位の数値をフォーマットする関数
    - 5桁万円以上（10,000万円＝1億円以上）の場合：『〇億〇万円』表記にする（例：12,883万円 ➔ 1億2,883万円）
    - 4桁万円以下の場合：『〇万円』（整数）表記にする
    """
    if pd.isna(man_val):
        return "0 万円"
    m = int(round(float(man_val)))
    if m == 0:
        return "+0 万円" if signed else "0 万円"

    sign = "+" if (signed and m > 0) else ("-" if m < 0 else "")
    abs_m = abs(m)

    if abs_m >= 10000:
        oku = abs_m // 10000
        rem_man = abs_m % 10000
        if rem_man == 0:
            return f"{sign}{oku:,}億0万円"
        return f"{sign}{oku:,}億{rem_man:,}万円"
    else:
        return f"{sign}{abs_m:,} 万円"

def fmt_man_or_sen(yen_val, signed=False):
    """
    「円」単位の数値を直感的な日本語表記に変換する（「100k」表記は一切使わない）
    - 1億円（10,000万円＝5桁万円）以上：『〇億〇万円』
    - 1万円以上〜1億円未満：『〇万円』（小数点なし整数）
    - 1,000円以上〜1万円未満：『〇.〇千円』
    - 1,000円未満：『〇円』
    """
    if pd.isna(yen_val):
        return "0 円"
    v = float(yen_val)
    abs_v = abs(v)
    if abs_v == 0:
        return "0 円"

    sign = "+" if (signed and v > 0) else ("-" if v < 0 else "")
    if abs_v >= 10000:
        man_int = int(round(abs_v / 10000.0))
        if man_int >= 10000:
            oku = man_int // 10000
            rem_man = man_int % 10000
            if rem_man == 0:
                return f"{sign}{oku:,}億0万円"
            return f"{sign}{oku:,}億{rem_man:,}万円"
        return f"{sign}{man_int:,} 万円"
    elif abs_v >= 1000:
        return f"{sign}{abs_v / 1000.0:,.1f} 千円"
    else:
        return f"{sign}{abs_v:,.0f} 円"

def normalize_account(text):
    """口座区分を『NISA』『特定』『一般』の3つに名寄せする"""
    s = unicodedata.normalize('NFKC', str(text))
    if any(k in s for k in ['NISA', 'nisa', 'ニーサ', '成長投資', 'つみたて']):
        return 'NISA'
    elif '特定' in s:
        return '特定'
    elif '一般' in s:
        return '一般'
    return None

def is_investment_trust_row(name_str, code_str="", raw_qty_str="", type_str="", val_num=0.0, qty_num=0.0):
    """その行が『投資信託（1万口単位のファンド）』かどうかを厳密に判定する"""
    t_str = unicodedata.normalize('NFKC', str(type_str))
    if any(k in t_str for k in ['投資信託', '投信', 'ファンド', 'MMF']):
        return True
    if any(k in t_str for k in ['国内株', '米国株', '外国株', '現物', 'ETF']):
        return False

    c_str = unicodedata.normalize('NFKC', str(code_str)).strip().upper()
    if c_str.startswith("FUND"):
        return True
    if re.match(r'^[1-9][0-9]{2}[0-9A-Z]$', c_str):
        return False

    n_str = unicodedata.normalize('NFKC', str(name_str)).strip()
    q_str = unicodedata.normalize('NFKC', str(raw_qty_str)).strip()

    if '口' in q_str and 'ETF' not in n_str.upper() and '投資法人' not in n_str:
        return True

    fund_keywords = [
        'eMAXIS', 'オルカン', 'オール・カントリー', 'インデックス', 'ファンド',
        'スリム', 'たわら', 'ニッセイ', 'iFree', '楽天・', 'SBI・',
        'ひふみ', 'プラス', 'つみたて', 'ノーロード', '投信', 'アライアンス',
        'インベスコ', 'ピクテ', 'グロイン', '年4回決算', '毎月決算', '予想分配金'
    ]
    if any(k in n_str for k in fund_keywords) and 'ETF' not in n_str.upper():
        return True

    if qty_num >= 10000 and val_num > 0 and (val_num / qty_num) < 25.0:
        return True

    return False

def classify_asset_type(name, ticker, is_trust=False):
    """銘柄名とティッカーから資産クラスを自動分類"""
    if is_trust or str(ticker).startswith("FUND"):
        return '投資信託'
    t = unicodedata.normalize('NFKC', str(ticker)).strip().upper()
    if re.match(r'^[1-9][0-9]{2}[0-9A-Z]$', t):
        return '日本株・東証ETF'
    if re.match(r'^[A-Z]{1,5}$', t):
        return '米国株・米国ETF'
    s = unicodedata.normalize('NFKC', str(name))
    if any(k in s for k in ['eMAXIS', 'オルカン', 'オール・カントリー', 'インデックス', 'ファンド', 'スリム', 'たわら', 'ニッセイ', 'iFree', '投信']) and 'ETF' not in s.upper():
        return '投資信託'
    return '日本株・東証ETF'

def is_fx_sensitive_asset(name, ticker, asset_class):
    """米ドル等の外貨為替レート（円高・円安）の影響を受ける資産かどうかを判定"""
    s = unicodedata.normalize('NFKC', str(name))
    t = unicodedata.normalize('NFKC', str(ticker)).strip().upper()
    if 'ヘッジあり' in s or '為替ヘッジ有' in s:
        return False
    if asset_class == '米国株・米国ETF':
        return True
    if any(k in s for k in ['全世界', 'オール・カントリー', 'オルカン', '全米', '米国', 'S&P', '先進国', '新興国', 'NASDAQ', 'ナスダック', 'FANG', 'ダウ', '外国', 'グローバル', '世界厳選']):
        return True
    if t in ['1655', '2558', '2559', '2631', '1545', '1557', '2568', '2569', '2633', '2634', '2244', '2013', '2014']:
        return True
    return False

def get_tax_info(asset_class, account_type):
    """
    日米税制完全準拠の手取り率（1 - 税率）と税区分ラベルを返す
    """
    is_us = (asset_class == '米国株・米国ETF')
    is_nisa = (account_type == 'NISA')

    if not is_us and is_nisa:
        return 1.0, "国内NISA (非課税・手取り100%)"
    elif not is_us and not is_nisa:
        return 1.0 - 0.20315, "国内特定/一般 (税20.315%控除・手取り79.7%)"
    elif is_us and is_nisa:
        return 0.90, "米国NISA (米国税10%控除・手取り90.0%)"
    else:
        return 0.90 * (1.0 - 0.20315), "米国特定/一般 (米国10%+国内20.3%・手取り71.7%)"

def extract_ticker_from_text(name_str, code_str="", is_trust=False):
    """CSVの銘柄コード列または銘柄名から証券コード・ティッカーを正確に抽出"""
    if is_trust:
        return "FUND"

    c = unicodedata.normalize('NFKC', str(code_str)).strip().upper()
    if c.endswith('.0'):
        c = c[:-2]
    if re.match(r'^[1-9][0-9]{2}[0-9A-Z]$', c):
        return c
    if re.match(r'^[A-Z]{1,5}$', c) and c not in {'NAN', 'NONE', 'NISA', 'ETF', 'FUND'}:
        return c

    m_c_jp = re.search(r'(?<![0-9A-Z])([1-9][0-9]{2}[0-9A-Z])(?![0-9A-Z])', c)
    if m_c_jp:
        return m_c_jp.group(1)

    s = unicodedata.normalize('NFKC', str(name_str)).strip()

    if any(k in s for k in ['eMAXIS', 'オール・カントリー', 'オルカン', 'インデックス・ファンド', 'インデックスファンド', 'たわらノーロード', 'ニッセイ', 'iFree', '楽天・', 'SBI・', 'ひふみ', '投信']) and 'ETF' not in s.upper():
        return "FUND"

    exclude_4digits = {'2024', '2025', '2026', '2027', '2028', '2029', '2030', '1000', '2000', '3000', '5000', '2250'}
    for m_jp in re.finditer(r'(?<![0-9A-Z/.-])([1-9][0-9]{2}[0-9A-Z])(?![0-9A-Z年/.-])', s.upper()):
        cand_code = m_jp.group(1)
        if cand_code not in exclude_4digits:
            return cand_code

    ignore_words = {'NISA', 'ETF', 'REIT', 'SPDR', 'MAXIS', 'SLIM', 'INDEX', 'FUND', 'NEXT', 'FUNDS', 'ISHARES', 'VANGUARD', 'CORE', 'HIGH', 'DIV', 'US', 'JP', 'NYSE', 'NASDAQ'}
    m_us_paren = re.findall(r'[\(\[]([A-Z]{1,5})[\)\]]', s.upper())
    for cand in m_us_paren:
        if cand not in ignore_words:
            return cand

    m_us_word = re.findall(r'\b([A-Z]{2,5})\b', s.upper())
    for cand in m_us_word:
        if cand not in ignore_words:
            return cand

    return ""

# ==============================================================================
# 3. リアルタイム市場データ取得エンジン（ドル円・日経平均・S&P500・ナスダック 現物＆先物）
# ==============================================================================

def _fetch_single_quote(symbol):
    """Yahoo Finance APIから指定シンボルの最新値と前日比(%)を取得する"""
    headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'}
    try:
        url = f"https://query1.finance.yahoo.com/v8/finance/chart/{urllib.parse.quote(symbol)}?range=5d&interval=1d"
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=4) as res:
            data = json.loads(res.read().decode('utf-8'))
        result = data.get('chart', {}).get('result', [])
        if result:
            meta = result[0].get('meta', {})
            price = float(meta.get('regularMarketPrice', 0.0))
            prev = float(meta.get('chartPreviousClose') or meta.get('previousClose') or 0.0)
            # メタに前日終値がない場合はヒストリカル終値から取得
            closes = result[0].get('indicators', {}).get('quote', [{}])[0].get('close', [])
            valid_closes = [float(c) for c in closes if c is not None and float(c) > 0]
            if price <= 0 and valid_closes:
                price = valid_closes[-1]
            if prev <= 0 and len(valid_closes) >= 2:
                prev = valid_closes[-2]
            if price > 0:
                diff = (price - prev) if prev > 0 else 0.0
                pct = (diff / prev * 100.0) if prev > 0 else 0.0
                return price, diff, pct
    except Exception:
        pass
    return None, 0.0, 0.0

@st.cache_data(ttl=900, show_spinner=False)
def fetch_realtime_market_indices():
    """
    ドル円・日経平均・S&P500・ナスダックの「現物」および「先物」を並列でリアルタイム取得する
    """
    targets = {
        'usdjpy': ['JPY=X'],
        'nikkei_spot': ['^N225'],
        'nikkei_fut': ['NIY=F', 'NKD=F'],
        'sp500_spot': ['^GSPC'],
        'sp500_fut': ['ES=F'],
        'nasdaq_spot': ['^IXIC'],
        'nasdaq_fut': ['NQ=F'],
    }

    def _get_item(key_syms):
        key, syms = key_syms
        for sym in syms:
            p, d, pct = _fetch_single_quote(sym)
            if p is not None and p > 0:
                return key, {'price': p, 'diff': d, 'pct': pct, 'symbol': sym}
        return key, {'price': None, 'diff': 0.0, 'pct': 0.0, 'symbol': syms[0]}

    with ThreadPoolExecutor(max_workers=7) as ex:
        res_list = list(ex.map(_get_item, targets.items()))

    market_dict = dict(res_list)
    if not market_dict['usdjpy']['price']:
        market_dict['usdjpy'] = {'price': 150.0, 'diff': 0.0, 'pct': 0.0, 'symbol': 'JPY=X'}
    return market_dict

def default_pay_months(ticker):
    """履歴から入金月が特定できない場合の標準入金月"""
    t = str(ticker).upper()
    if t.startswith("FUND") or not t:
        return "-"
    if t in {'JEPI', 'JEPQ', 'XYLD', 'QYLD', 'RYLD', 'AGG', 'BND', 'LQD', 'TLT', 'PFF', 'ARCC', 'MAIN', 'O'}:
        return "1,2,3,4,5,6,7,8,9,10,11,12"
    if t in {'2914', '4911', '2502', '2503', '4452', '5108', '4519', '3003'}:
        return "3,9"
    if t in {'8267', '3382', '9983', '7532', '8233', '3086'}:
        return "5,11"
    if re.match(r'^[A-Z]{1,5}$', t):
        return "3,6,9,12"
    if re.match(r'^[1-9][0-9]{2}[0-9A-Z]$', t):
        return "6,12"
    return "6,12"

def parse_pay_months_str(months_str):
    """'6,12' や '毎月' などの文字列から月番号(1〜12)のリストを返す"""
    s = str(months_str).strip()
    if not s or s in ['-', '無配', '無分配', 'nan', 'None']:
        return []
    if '毎月' in s or 'すべて' in s:
        return list(range(1, 13))
    nums = re.findall(r'\d+', s)
    return sorted(list({int(m) for m in nums if 1 <= int(m) <= 12}))

# ==============================================================================
# 4. 【核心】今期予想優先 ➔ 直近実績フォールバック（個別株・ETF・投資信託 完全対応）
# ==============================================================================

def _find_in_json(obj, target_key):
    """ネストされたJSON辞書から指定キーのオブジェクトを再帰探索する"""
    if isinstance(obj, dict):
        if target_key in obj:
            return obj[target_key]
        for v in obj.values():
            res = _find_in_json(v, target_key)
            if res is not None:
                return res
    elif isinstance(obj, list):
        for item in obj:
            res = _find_in_json(item, target_key)
            if res is not None:
                return res
    return None

@st.cache_data(ttl=3600, show_spinner=False)
def search_jp_ticker_by_name(stock_name):
    """銘柄名しか記載がない場合、Yahoo!ファイナンス検索から4桁証券コードを特定する"""
    clean_q = unicodedata.normalize('NFKC', str(stock_name))
    clean_q = re.sub(r'[\(\[].*?[\)\]]', '', clean_q).replace('株式会社', '').strip()
    if not clean_q or len(clean_q) < 2:
        return ""
    headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'}
    try:
        url = f"https://finance.yahoo.co.jp/search/?query={urllib.parse.quote(clean_q)}"
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=4) as res:
            html = res.read().decode('utf-8', errors='ignore')
        m = re.search(r'/quote/([1-9][0-9]{2}[0-9A-Z])\.T', html)
        if m:
            return m.group(1)
    except Exception:
        pass
    return ""

@st.cache_data(ttl=3600, show_spinner=False)
def fetch_trust_distribution_data(fund_name, expected_nav=10000.0):
    """
    投資信託（ファンド）の分配金を調査する関数
    - 無分配・再投資型インデックス投信（オルカン・S&P500等）は0円と高速判定
    - 分配型投信はYahoo!ファイナンスの投信ページ等から「今期予想分配金」または「直近1年の実績分配金（1万口あたり）」を取得
    """
    s = unicodedata.normalize('NFKC', str(fund_name)).strip()

    no_dist_keywords = [
        'eMAXIS Slim', 'eMAXIS', 'オール・カントリー', 'オルカン', 'たわらノーロード',
        'ニッセイ', 'iFree', '楽天・全米株式', '楽天・全世界株式', '楽天・プラス',
        '楽天・S&P500', 'SBI・V・S&P500', 'SBI・V・全米', 'SBI・V・全世界', 'ひふみ'
    ]
    is_explicit_dist = any(k in s for k in ['分配', '年4回', '年2回', '四半期', '毎月', 'Cコース', 'Dコース', 'インカム'])
    if any(k in s for k in no_dist_keywords) and not is_explicit_dist:
        return 0.0, 0.0, "無分配(再投資)", "投信実績(0円)", "-"

    headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'}

    try:
        q_str = re.sub(r'[（\(\[【].*?[）\)\]】]', ' ', s).strip()
        url_search = f"https://finance.yahoo.co.jp/search/?query={urllib.parse.quote(q_str[:30])}"
        req_s = urllib.request.Request(url_search, headers=headers)
        with urllib.request.urlopen(req_s, timeout=4) as res_s:
            html_s = res_s.read().decode('utf-8', errors='ignore')

        m_fcode = re.search(r'/quote/([0-9]{7}[0-9A-Z]|[0-9]{8})(?:"|\?|\s|/)', html_s)
        if m_fcode:
            fcode = m_fcode.group(1)
            url_q = f"https://finance.yahoo.co.jp/quote/{fcode}"
            req_q = urllib.request.Request(url_q, headers=headers)
            with urllib.request.urlopen(req_q, timeout=5) as res_q:
                html_q = res_q.read().decode('utf-8', errors='ignore')

            nav_price = expected_nav if expected_nav >= 1000 else 10000.0
            dist_annual = None
            dist_yield = None

            m_state = re.search(r'window\.__PRELOADED_STATE__\s*=\s*(\{.*?\});?\s*</script>', html_q, re.DOTALL)
            if m_state:
                state_data = json.loads(m_state.group(1))
                pb = _find_in_json(state_data, "fundPriceBoard") or _find_in_json(state_data, "priceBoard")
                if isinstance(pb, dict):
                    p_v = clean_num(pb.get("price", 0))
                    if p_v >= 500:
                        nav_price = p_v
                ref = _find_in_json(state_data, "fundReferenceIndex") or _find_in_json(state_data, "referenceIndex")
                if isinstance(ref, dict):
                    y_obj = ref.get("distributionYield") or ref.get("dividendYield")
                    if isinstance(y_obj, dict):
                        yv = clean_num(y_obj.get("value", 0))
                        if 0 <= yv <= 30.0:
                            dist_yield = round(yv, 2)
                    d_obj = ref.get("annualDistribution") or ref.get("distribution")
                    if isinstance(d_obj, dict):
                        dv = clean_num(d_obj.get("value", 0))
                        if 0 <= dv <= 30000:
                            dist_annual = dv

            clean_txt = unicodedata.normalize('NFKC', re.sub(r'<[^>]+>', ' ', html_q))
            if dist_yield is None:
                m_dy = re.search(r'分配金利回り[^\d]*([0-9]{1,2}\.[0-9]{1,2})\s*%', clean_txt)
                if m_dy:
                    dist_yield = float(m_dy.group(1))

            if dist_annual is not None and dist_annual > 0:
                y_out = dist_yield if (dist_yield and dist_yield > 0) else round(dist_annual / nav_price * 100.0, 2)
                pay_m = "毎月" if "毎月" in s else ("1,4,7,10" if any(k in s for k in ['年4回', '四半期']) else "6,12")
                return round(dist_annual, 2), y_out, "直近実績(投信)", f"Yahoo投信({fcode})", pay_m

            if dist_yield is not None and dist_yield > 0:
                d_calc = round(nav_price * (dist_yield / 100.0), 2)
                pay_m = "毎月" if "毎月" in s else ("1,4,7,10" if any(k in s for k in ['年4回', '四半期']) else "6,12")
                return d_calc, dist_yield, "直近実績(投信)", f"Yahoo投信利回り({fcode})", pay_m

            if dist_annual == 0.0 or dist_yield == 0.0:
                return 0.0, 0.0, "無分配(再投資)", f"Yahoo投信({fcode}:0円)", "-"
    except Exception:
        pass

    for kw, dist_val, y_val, pm_str in FALLBACK_TRUST_DIV_MASTER:
        if kw in s:
            return dist_val, y_val, "直近実績(投信)", f"投信マスター({kw})", pm_str

    return 0.0, 0.0, "無分配(再投資)", "投信(0円・手動入力可)", "-"

@st.cache_data(ttl=3600, show_spinner=False)
def fetch_exact_dividend_data(ticker, stock_name="", usdjpy=150.0, expected_unit_price_yen=0.0, is_trust=False):
    """
    【核心ルール】
    - 今期予想が出ていれば（＞0円なら）「今期予想額」を最優先で採用
    - 今期予想がなければ（0円・未定・ETF・米国株など）「直近実績額（前期実績または直近1年実績合計）」を採用
    - 投資信託の分配金も同様に調査
    """
    if is_trust or str(ticker).startswith("FUND"):
        d_val, y_val, b_type, src_lbl, pm = fetch_trust_distribution_data(stock_name, expected_unit_price_yen)
        return d_val, y_val, b_type, src_lbl, pm, expected_unit_price_yen

    if not ticker:
        ticker = search_jp_ticker_by_name(stock_name)
    if not ticker:
        return 0.0, 0.0, "未特定", "コード未特定(手動入力可)", "6,12", expected_unit_price_yen

    t_upper = ticker.upper()
    headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'}
    is_jp = bool(re.match(r'^[1-9][0-9]{2}[0-9A-Z]$', t_upper))
    yf_symbol = f"{t_upper}.T" if is_jp else t_upper

    forecast_dps_yen = None
    forecast_yield = None
    forecast_src = ""

    actual_dps_yen = None
    actual_yield = None
    actual_src = ""

    detected_price_yen = None
    pay_months_str = default_pay_months(t_upper)

    # A. 日本株・東証ETF: Yahoo!ファイナンス日本版 & 株探 (Kabutan)
    if is_jp:
        try:
            url_jp = f"https://finance.yahoo.co.jp/quote/{t_upper}.T"
            req = urllib.request.Request(url_jp, headers=headers)
            with urllib.request.urlopen(req, timeout=5) as res:
                html = res.read().decode('utf-8', errors='ignore')

            m_state = re.search(r'window\.__PRELOADED_STATE__\s*=\s*(\{.*?\});?\s*</script>', html, re.DOTALL)
            if m_state:
                state_data = json.loads(m_state.group(1))
                pb = _find_in_json(state_data, "priceBoard")
                if isinstance(pb, dict):
                    p_v = clean_num(pb.get("price", 0))
                    if p_v > 0:
                        detected_price_yen = p_v

                ref_idx = _find_in_json(state_data, "referenceIndex")
                if isinstance(ref_idx, dict):
                    dps_obj = ref_idx.get("dps")
                    if isinstance(dps_obj, dict):
                        dps_v = clean_num(dps_obj.get("value", 0))
                        if 0 < dps_v < 30000:
                            forecast_dps_yen = dps_v
                            forecast_src = f"Yahoo今期予想({t_upper}.T)"
                    yld_obj = ref_idx.get("dividendYield") or ref_idx.get("distributionYield")
                    if isinstance(yld_obj, dict):
                        yld_v = clean_num(yld_obj.get("value", 0))
                        if 0 < yld_v <= 18.0:
                            forecast_yield = round(yld_v, 2)
                            if not forecast_src:
                                forecast_src = f"Yahoo予想利回り({t_upper}.T)"
        except Exception:
            pass

        try:
            url_kb = f"https://kabutan.jp/stock/?code={t_upper}"
            req_kb = urllib.request.Request(url_kb, headers=headers)
            with urllib.request.urlopen(req_kb, timeout=5) as res_kb:
                html_kb = res_kb.read().decode('utf-8', errors='ignore')

            m_kabuka = re.search(r'<span class="kabuka">([0-9,]+(?:\.[0-9]+)?)円</span>', html_kb)
            if m_kabuka and detected_price_yen is None:
                detected_price_yen = clean_num(m_kabuka.group(1))

            m_i3 = re.search(r'<div id="stockinfo_i3">(.*?)</div>', html_kb, re.DOTALL)
            if m_i3 and forecast_yield is None:
                tds = re.findall(r'<td[^>]*>(.*?)</td>', m_i3.group(1), re.DOTALL)
                if len(tds) >= 3:
                    y_cand = clean_num(re.sub(r'<[^>]+>', '', tds[2]))
                    if 0 < y_cand <= 18.0:
                        forecast_yield = round(y_cand, 2)

            perf_rows = []
            for tr_html in re.findall(r'<tr[^>]*>(.*?)</tr>', html_kb, re.DOTALL):
                if re.search(r'202[3-8]\.[0-1][0-9]', tr_html):
                    th_txt = "".join(re.sub(r'<[^>]+>', '', th).strip() for th in re.findall(r'<th[^>]*>(.*?)</th>', tr_html, re.DOTALL))
                    td_list = [re.sub(r'<[^>]+>', '', td).strip() for td in re.findall(r'<td[^>]*>(.*?)</td>', tr_html, re.DOTALL)]
                    if len(td_list) >= 6:
                        dps_cell = clean_num(td_list[-2])
                        is_est = ('予' in th_txt)
                        perf_rows.append((is_est, dps_cell))

            for is_est, dps_cell in perf_rows:
                if is_est and dps_cell > 0 and dps_cell < 30000:
                    if forecast_dps_yen is None:
                        forecast_dps_yen = dps_cell
                        forecast_src = f"株探今期予想({t_upper})"
                elif not is_est and dps_cell > 0 and dps_cell < 30000:
                    actual_dps_yen = dps_cell
                    actual_src = f"株探前期実績({t_upper})"
        except Exception:
            pass

    # B. Yahoo Finance Chart API (直近1年間の実績配当合計・米国株・東証ETF・入金月)
    try:
        url_api = f"https://query1.finance.yahoo.com/v8/finance/chart/{urllib.parse.quote(yf_symbol)}?range=1y&interval=1d&events=div,split"
        req = urllib.request.Request(url_api, headers=headers)
        with urllib.request.urlopen(req, timeout=5) as res:
            data = json.loads(res.read().decode('utf-8'))

        result = data.get('chart', {}).get('result', [])
        if result:
            meta = result[0].get('meta', {})
            api_price_native = float(meta.get('regularMarketPrice', 0.0))
            if api_price_native > 0:
                if is_jp and detected_price_yen is None:
                    detected_price_yen = api_price_native
                elif not is_jp:
                    detected_price_yen = api_price_native * usdjpy

            events = result[0].get('events', {})
            divs = events.get('dividends', {})
            splits = events.get('splits', {})

            if divs:
                div_items = sorted(divs.values(), key=lambda x: int(x.get('date', 0)))
                if len(div_items) in (3, 5, 13):
                    first_m = datetime.fromtimestamp(int(div_items[0].get('date', 0))).month
                    last_m = datetime.fromtimestamp(int(div_items[-1].get('date', 0))).month
                    span_days = (int(div_items[-1].get('date', 0)) - int(div_items[0].get('date', 0))) / 86400.0
                    if first_m == last_m or span_days > 345:
                        div_items = div_items[1:]

                split_events = []
                for _, sp in splits.items():
                    num = float(sp.get('numerator', 1.0))
                    den = float(sp.get('denominator', 1.0))
                    if den > 0 and num != den:
                        split_events.append((int(sp.get('date', 0)), num / den))

                ex_months = set()
                total_div_native = 0.0

                for d_obj in div_items:
                    ts = int(d_obj.get('date', 0))
                    d_amt = float(d_obj.get('amount', 0.0))

                    for sp_date, ratio in split_events:
                        if ts < sp_date and ratio > 1.1:
                            post_divs = [float(x.get('amount', 0.0)) for x in div_items if int(x.get('date', 0)) >= sp_date]
                            if post_divs and post_divs[-1] > 0:
                                if (d_amt / post_divs[-1]) > (ratio * 0.65):
                                    d_amt = d_amt / ratio
                            elif api_price_native > 0:
                                if (d_amt * 2.0 / api_price_native * 100.0) > 9.0:
                                    d_amt = d_amt / ratio

                    total_div_native += d_amt
                    if ts > 0:
                        m_num = datetime.fromtimestamp(ts).month
                        ex_months.add(((m_num + 3 - 1) % 12) + 1 if is_jp else m_num)

                if ex_months:
                    pay_months_str = ",".join(str(m) for m in sorted(list(ex_months)))

                if total_div_native > 0:
                    if is_jp:
                        if actual_dps_yen is None:
                            actual_dps_yen = round(total_div_native, 2)
                            actual_src = f"直近1年実績({t_upper}.T)"
                        if api_price_native > 0:
                            actual_yield = round(actual_dps_yen / api_price_native * 100.0, 2)
                    else:
                        actual_dps_yen = round(total_div_native * usdjpy, 2)
                        actual_src = f"米国直近実績({yf_symbol}: ${total_div_native:.2f}/株)"
                        if api_price_native > 0:
                            actual_yield = round(total_div_native / api_price_native * 100.0, 2)
    except Exception:
        pass

    ref_price_yen = detected_price_yen or expected_unit_price_yen

    # C. 【優先順位の適用】① 今期予想 (＞0円) ➔ ② 直近実績 (＞0円) ➔ ③ バックアップ辞書
    if forecast_dps_yen is not None and forecast_dps_yen > 0:
        if not (ref_price_yen and ref_price_yen > 0 and (forecast_dps_yen / ref_price_yen * 100.0) > 18.0):
            y_out = forecast_yield if (forecast_yield and forecast_yield > 0) else (
                round(forecast_dps_yen / ref_price_yen * 100.0, 2) if (ref_price_yen and ref_price_yen > 0) else 0.0
            )
            return round(forecast_dps_yen, 2), y_out, "今期予想", forecast_src, pay_months_str, (ref_price_yen or 0.0)

    if forecast_yield is not None and forecast_yield > 0 and ref_price_yen and ref_price_yen > 0:
        dps_calc = round(ref_price_yen * (forecast_yield / 100.0), 2)
        return dps_calc, forecast_yield, "今期予想", forecast_src, pay_months_str, ref_price_yen

    if actual_dps_yen is not None and actual_dps_yen > 0:
        if not (ref_price_yen and ref_price_yen > 0 and (actual_dps_yen / ref_price_yen * 100.0) > 20.0):
            y_out = actual_yield if (actual_yield and actual_yield > 0) else (
                round(actual_dps_yen / ref_price_yen * 100.0, 2) if (ref_price_yen and ref_price_yen > 0) else 0.0
            )
            return round(actual_dps_yen, 2), y_out, "直近実績", actual_src, pay_months_str, (ref_price_yen or 0.0)

    if t_upper in FALLBACK_STOCK_DIV_MASTER:
        fb_val, fb_type = FALLBACK_STOCK_DIV_MASTER[t_upper]
        if fb_val > 0:
            dps_fb = fb_val if is_jp else round(fb_val * usdjpy, 2)
            y_fb = round(dps_fb / ref_price_yen * 100.0, 2) if (ref_price_yen and ref_price_yen > 0) else 0.0
            return dps_fb, y_fb, fb_type, f"マスター({t_upper})", pay_months_str, (ref_price_yen or 0.0)

    return 0.0, 0.0, "無配(0円)", f"無配/未取得({t_upper})", "-", (ref_price_yen or 0.0)

def enrich_with_latest_dividends(df, usdjpy=150.0):
    """DataFrameの全銘柄に対して並列で『1株/1万口配当(円)』『採用基準(今期予想/直近実績)』『入金月』を自動付与する"""
    if df.empty:
        return df

    df = df.copy()
    for col, default_v in [
        ('銘柄コード', ''), ('保有数量', 0.0), ('1株配当(円)', 0.0),
        ('配当利回り(%)', 0.0), ('採用基準', ''), ('配当入金月', ''), ('配当ソース', ''), ('資産クラス', '')
    ]:
        if col not in df.columns:
            df[col] = default_v

    tickers = []
    asset_classes = []
    for _, row in df.iterrows():
        val_n = clean_num(row.get('評価額', 0.0))
        qty_n = clean_num(row.get('保有数量', 0.0))
        is_tr = (str(row.get('資産クラス', '')) == '投資信託') or is_investment_trust_row(
            row.get('銘柄名', ''), row.get('銘柄コード', ''), '', '', val_n, qty_n
        )
        t = extract_ticker_from_text(row.get('銘柄名', ''), row.get('銘柄コード', ''), is_trust=is_tr)
        a_cls = classify_asset_type(row.get('銘柄名', ''), t, is_trust=is_tr)
        if a_cls == '投資信託':
            t = "FUND"
        tickers.append(t)
        asset_classes.append(a_cls)

    df['銘柄コード'] = tickers
    df['資産クラス'] = asset_classes

    tasks = []
    for idx, row in df.iterrows():
        cur_dps = clean_num(row.get('1株配当(円)', 0.0))
        cur_src = str(row.get('配当ソース', ''))
        cur_pm = str(row.get('配当入金月', ''))
        val_n = clean_num(row.get('評価額', 0.0))
        qty_n = clean_num(row.get('保有数量', 0.0))
        is_tr = (row['資産クラス'] == '投資信託')

        if is_tr:
            unit_p = (val_n / (qty_n / 10000.0)) if qty_n >= 1000 else 10000.0
        else:
            unit_p = (val_n / qty_n) if qty_n > 0 else 0.0

        if not (cur_dps > 0 and cur_src == "手動/保存"):
            tasks.append((idx, row['銘柄コード'], row['銘柄名'], cur_dps, cur_src, cur_pm, unit_p, is_tr, val_n, qty_n))

    if tasks:
        def _worker(task):
            idx, t_code, s_name, cur_dps, cur_src, cur_pm, unit_p, is_tr, val_n, qty_n = task
            dps_val, y_val, basis_t, src_label, pay_m, price_yen = fetch_exact_dividend_data(t_code, s_name, usdjpy, unit_p, is_tr)
            final_pm = cur_pm if cur_pm not in ["", "nan", "None"] else pay_m
            restored_qty = qty_n
            if not is_tr and qty_n <= 0 and val_n > 0 and price_yen > 0:
                restored_qty = float(round(val_n / price_yen))
            return idx, dps_val, y_val, basis_t, src_label, final_pm, restored_qty

        with ThreadPoolExecutor(max_workers=8) as executor:
            results = list(executor.map(_worker, tasks))

        for idx, dps_val, y_val, basis_t, src_label, final_pm, restored_qty in results:
            df.at[idx, '1株配当(円)'] = dps_val
            df.at[idx, '配当利回り(%)'] = y_val
            df.at[idx, '採用基準'] = basis_t
            df.at[idx, '配当ソース'] = src_label
            df.at[idx, '配当入金月'] = final_pm
            if restored_qty > 0 and clean_num(df.at[idx, '保有数量']) <= 0:
                df.at[idx, '保有数量'] = restored_qty

    df['為替影響'] = [
        "米ドル連動(為替影響あり)" if is_fx_sensitive_asset(r['銘柄名'], r['銘柄コード'], r['資産クラス']) else "円建て(為替影響なし)"
        for _, r in df.iterrows()
    ]
    return df

def calc_exact_annual_dividend(df):
    """
    【方式①完全統一】
    - 個別株・ETF：『保有株数 × 1株あたり年間配当金(円)』
    - 投資信託：『(保有口数 ÷ 10,000) × 1万口あたり年間分配金(円)』
    - 税金は「税引前（額面100%）」と「税引後（米国税10%＋国内税20.315%控除）」の2パターンを常時算出
    """
    if df.empty:
        return df

    df = df.copy()
    pre_tax_list = []
    calc_method_list = []
    eff_yield_list = []

    for idx, row in df.iterrows():
        asset_cls = str(row.get('資産クラス', ''))
        qty = clean_num(row.get('保有数量', 0.0))
        dps = clean_num(row.get('1株配当(円)', 0.0))
        val = clean_num(row.get('評価額', 0.0))
        y_pct = clean_num(row.get('配当利回り(%)', 0.0))
        b_type = str(row.get('採用基準', '')) or "今期予想/実績"

        if asset_cls == '投資信託':
            if dps > 0:
                if qty >= 10000 and val > 0 and (val / qty) < 25.0:
                    man_kuchi = qty / 10000.0
                elif qty > 0 and val > 0 and 0.5 <= (val / (qty * 10000.0)) <= 10.0:
                    man_kuchi = qty
                else:
                    man_kuchi = (val / 10000.0) if val > 0 else 0.0

                ann_pre = round(man_kuchi * dps)
                eff_y = (ann_pre / val * 100.0) if val > 0 else y_pct
                method_str = f"① {man_kuchi:,.1f}万口 × {dps:,.0f}円/万口 ({b_type})"
            else:
                ann_pre = 0.0
                eff_y = 0.0
                method_str = "無分配 (0円 / 再投資型)"
        else:
            if qty > 0 and dps > 0:
                ann_pre = round(qty * dps)
                eff_y = (ann_pre / val * 100.0) if val > 0 else y_pct
                if val > 0 and eff_y > 20.0:
                    if 0 < y_pct <= 15.0:
                        dps = round((val / qty) * (y_pct / 100.0), 2)
                        df.at[idx, '1株配当(円)'] = dps
                        ann_pre = round(qty * dps)
                        eff_y = (ann_pre / val * 100.0)
                        method_str = f"① {qty:,.0f}株 × {dps:,.2f}円 ({b_type})"
                    else:
                        ann_pre = 0.0
                        eff_y = 0.0
                        df.at[idx, '1株配当(円)'] = 0.0
                        method_str = "異常値除外 (0円)"
                else:
                    method_str = f"① {qty:,.0f}株 × {dps:,.2f}円 ({b_type})"
            else:
                ann_pre = 0.0
                eff_y = 0.0
                method_str = "無配 / 0円"

        pre_tax_list.append(float(ann_pre))
        calc_method_list.append(method_str)
        eff_yield_list.append(round(eff_y, 2))

    df['年間配当金(税引前)'] = pre_tax_list
    df['計算式'] = calc_method_list
    df['実質配当利回り(税引前%)'] = eff_yield_list

    tax_results = [get_tax_info(r['資産クラス'], r['口座区分']) for _, r in df.iterrows()]
    df['手取り率'] = [t[0] for t in tax_results]
    df['税区分'] = [t[1] for t in tax_results]
    df['年間配当金(税引後)'] = [round(pre * rate) for pre, rate in zip(df['年間配当金(税引前)'], df['手取り率'])]
    df['実質配当利回り(税引後%)'] = [
        round((post / clean_num(val) * 100.0), 2) if clean_num(val) > 0 else 0.0
        for post, val in zip(df['年間配当金(税引後)'], df['評価額'])
    ]
    return df

# ==============================================================================
# 5. 証券会社CSVの自動判別 & マルチブロック高精度解析エンジン
# ==============================================================================

def is_csv_header_line(line):
    """行がCSVのヘッダー（見出し）行かどうかを厳密に判定"""
    if ',' not in line:
        return False
    cells = [unicodedata.normalize('NFKC', c.strip().strip('"').strip("'")) for c in line.split(',')]
    exact_headers = {
        '種別', '資産クラス', '銘柄コード', '銘柄コード・ティッカー', 'ティッカー', 'コード',
        '銘柄名', '銘柄名称', '銘柄・ファンド名', 'ファンド名', 'ファンド', '銘柄(コード)', '銘柄', '商品名',
        '口座', '口座区分', '預り区分', '保有数量', '保有株数', '保有口数', '数量',
        '取得単価', '平均取得価額', '現在値', '基準価額',
        '評価額', '評価金額', '時価評価額[円]', '時価評価額', '損益', '評価損益', '評価損益[円]'
    }
    matches = sum(1 for c in cells if c in exact_headers)
    return matches >= 2

def detect_and_parse_csv(uploaded_file):
    """アップロードされたCSVの中身を自動判別（楽天 / SBI / 保存済みCSV）して漏れなく解析する"""
    bytes_data = uploaded_file.getvalue()
    text_data = ""
    for enc in ['utf-8-sig', 'shift_jis', 'cp932', 'utf-8']:
        try:
            text_data = bytes_data.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    if not text_data:
        text_data = bytes_data.decode('utf-8', errors='ignore')

    first_lines = "\n".join(text_data.splitlines()[:10])

    if '証券会社' in first_lines and '評価額' in first_lines and ('1株配当(円)' in first_lines or '配当利回り(%)' in first_lines):
        try:
            df_saved = pd.read_csv(io.StringIO(text_data))
            if '配当ソース' not in df_saved.columns:
                df_saved['配当ソース'] = '手動/保存'
            if '採用基準' not in df_saved.columns:
                df_saved['採用基準'] = '手動/保存'
            if '保有数量' not in df_saved.columns:
                df_saved['保有数量'] = 0.0
            if '1株配当(円)' not in df_saved.columns:
                df_saved['1株配当(円)'] = 0.0
            return df_saved
        except Exception:
            pass

    fname = uploaded_file.name.lower()
    if '銘柄・ファンド名' in text_data or '時価評価額[円]' in text_data or 'rakuten' in fname or '楽天' in text_data:
        broker_name = '楽天証券'
    elif 'SBI' in text_data or 'sbi' in fname or 'ポートフォリオ一覧' in text_data or '評価金額' in text_data or '銘柄（コード）' in text_data:
        broker_name = 'SBI証券'
    else:
        broker_name = 'SBI証券' if ('評価金額' in text_data or '損益' in text_data) else '楽天証券'

    lines = text_data.splitlines()
    records = []
    current_account = '特定'
    current_section_is_trust = False
    i = 0
    n = len(lines)

    while i < n:
        line = lines[i].strip()
        if not line:
            i += 1
            continue

        if line.count(',') <= 3 and not is_csv_header_line(line):
            acc_detect = normalize_account(line)
            if acc_detect:
                current_account = acc_detect
            norm_line = unicodedata.normalize('NFKC', line)
            if any(k in norm_line for k in ['投資信託', '投信', 'ファンド']):
                current_section_is_trust = True
            elif any(k in norm_line for k in ['株式', '現物', '米国', 'ETF']):
                current_section_is_trust = False
            i += 1
            continue

        if is_csv_header_line(line):
            block_lines = [line]
            i += 1
            while i < n:
                next_line = lines[i].strip()
                if not next_line:
                    break
                if is_csv_header_line(next_line):
                    break
                if next_line.count(',') <= 2 and any(k in next_line for k in ['合計', 'トータル', '預り', '口座', '一覧', '■', '◆', '投資信託', '株式']):
                    break
                block_lines.append(next_line)
                i += 1

            try:
                df_block = pd.read_csv(io.StringIO("\n".join(block_lines)), on_bad_lines='skip')

                col_type = next((c for c in df_block.columns if any(k in str(c) for k in ['種別', '商品区分', '資産クラス'])), None)
                col_code = next((c for c in df_block.columns if any(k in str(c) for k in ['銘柄コード', 'ティッカー', 'コード']) and '名' not in str(c) and '銘柄（コード）' not in str(c) and '銘柄(コード)' not in str(c)), None)
                col_name = next((c for c in df_block.columns if any(k in str(c) for k in ['銘柄・ファンド名', '銘柄名称', '銘柄名', 'ファンド名', '銘柄（コード）', '銘柄(コード)', 'ファンド', '銘柄', '商品名']) and c != col_code), None)
                if not col_name:
                    col_name = next((c for c in df_block.columns if any(k in str(c) for k in ['銘柄', 'ファンド', '商品'])), None)

                header_is_trust = current_section_is_trust or (col_name is not None and 'ファンド名' in str(col_name) and '銘柄' not in str(col_name))

                col_qty = next((c for c in df_block.columns if any(k in str(c) for k in ['保有数量', '保有株数', '保有口数', '数量', '株数', '口数', '残高']) and '注文' not in str(c)), None)
                col_unit = next((c for c in df_block.columns if str(c).strip() in ['単位', '数量単位']), None)
                col_price = next((c for c in df_block.columns if any(k in str(c) for k in ['現在値', '基準価額']) and '外貨' not in str(c) and 'US' not in str(c)), None)
                col_val = next((c for c in df_block.columns if any(k in str(c) for k in ['時価評価額[円]', '評価額[円]', '評価金額[円]', '時価評価額', '評価金額', '評価額']) and '外貨' not in str(c) and 'US' not in str(c) and '損益' not in str(c)), None)
                col_pnl = next((c for c in df_block.columns if any(k in str(c) for k in ['評価損益[円]', '評価損益額', '評価損益', 'トータルリターン[円]', 'トータルリターン', '含み損益', '損益']) and '外貨' not in str(c) and 'US' not in str(c) and '率' not in str(c) and '％' not in str(c) and '%' not in str(c)), None)
                col_cost = next((c for c in df_block.columns if any(k in str(c) for k in ['取得総額[円]', '取得総額', '取得金額', '買付金額']) and '外貨' not in str(c)), None)
                col_unit_cost = next((c for c in df_block.columns if any(k in str(c) for k in ['取得単価', '平均取得価額']) and '外貨' not in str(c)), None)
                col_acc = next((c for c in df_block.columns if any(k in str(c) for k in ['口座区分', '口座', '預り区分'])), None)

                if col_name and col_val:
                    for _, row in df_block.iterrows():
                        name_str = str(row[col_name]).strip()
                        if not name_str or name_str in ['nan', 'None', '-', '---', '合計', '総合計'] or name_str.startswith('合計'):
                            continue

                        val_num = clean_num(row[col_val])
                        if val_num <= 0:
                            continue

                        raw_qty_str = str(row[col_qty]) if col_qty and not pd.isna(row[col_qty]) else ""
                        if col_unit and not pd.isna(row[col_unit]):
                            raw_qty_str += str(row[col_unit])
                        qty_num = clean_num(row[col_qty]) if col_qty else 0.0

                        type_str = str(row[col_type]) if col_type and not pd.isna(row[col_type]) else ("投資信託" if header_is_trust else "")
                        code_str = str(row[col_code]).strip() if col_code and not pd.isna(row[col_code]) else ""

                        is_tr = is_investment_trust_row(name_str, code_str, raw_qty_str, type_str, val_num, qty_num)
                        ext_code = extract_ticker_from_text(name_str, code_str, is_trust=is_tr)
                        a_cls = classify_asset_type(name_str, ext_code, is_trust=is_tr)

                        if a_cls != '投資信託' and qty_num <= 0 and col_price and clean_num(row[col_price]) > 0:
                            qty_num = float(round(val_num / clean_num(row[col_price])))

                        has_pnl = (col_pnl is not None) and (str(row[col_pnl]).strip() not in ['', '-', '--', '---', 'nan', 'None'])
                        pnl_num = clean_num(row[col_pnl]) if has_pnl else 0.0

                        if col_cost and clean_num(row[col_cost]) > 0:
                            cost_num = clean_num(row[col_cost])
                        elif has_pnl:
                            cost_num = max(val_num - pnl_num, 0.0)
                        elif col_unit_cost and qty_num > 0 and clean_num(row[col_unit_cost]) > 0:
                            u_cost = clean_num(row[col_unit_cost])
                            if a_cls == '投資信託' or (qty_num >= 10000 and (qty_num * u_cost) > val_num * 500):
                                cost_num = (qty_num / 10000.0) * u_cost
                            else:
                                cost_num = qty_num * u_cost
                        else:
                            cost_num = val_num

                        row_acc = current_account
                        if col_acc:
                            detected = normalize_account(row[col_acc])
                            if detected:
                                row_acc = detected

                        records.append({
                            '証券会社': broker_name,
                            '口座区分': row_acc,
                            '資産クラス': a_cls,
                            '銘柄コード': ext_code,
                            '銘柄名': name_str,
                            '保有数量': qty_num,
                            '評価額': val_num,
                            '取得額': cost_num,
                            '評価損益': val_num - cost_num,
                            '1株配当(円)': 0.0,
                            '配当利回り(%)': 0.0,
                            '採用基準': '',
                            '配当入金月': '',
                            '配当ソース': ''
                        })
            except Exception:
                pass
        else:
            i += 1

    return pd.DataFrame(records)

# ==============================================================================
# 6. サイドバー：一括ドラッグ＆ドロップ & リアルタイム市場データ（日経・S&P500・ナスダック 現物＆先物）
# ==============================================================================

market_data = fetch_realtime_market_indices()
current_usdjpy = float(market_data['usdjpy']['price'] or 150.0)

def _fmt_idx_val(m_item, unit="", decimals=0):
    p = m_item.get('price')
    if p is None or p <= 0:
        return "取得中...", None
    fmt_p = f"{p:,.{decimals}f}{unit}"
    pct = m_item.get('pct', 0.0)
    diff = m_item.get('diff', 0.0)
    delta_str = f"{diff:+,.{decimals}f} ({pct:+.2f}%)"
    return fmt_p, delta_str

with st.sidebar:
    st.header("📂 1. CSV一括ドラッグ＆ドロップ")
    st.caption("楽天証券・SBI証券・保存済みCSVを区別せず、ここにまとめて一気にドロップできます（自動判別）")
    uploaded_files = st.file_uploader(
        "CSVファイルをまとめてドロップ（複数選択可）",
        type=['csv'],
        accept_multiple_files=True
    )

    st.markdown("---")
    st.header("🌐 2. リアルタイム市場データ")

    # ドル円
    fx_v, fx_d = _fmt_idx_val(market_data['usdjpy'], " 円/$", 2)
    st.metric("為替 ドル円 (USD/JPY)", fx_v, fx_d)

    # 日経平均（現物 ＆ 先物）
    nk_s_v, nk_s_d = _fmt_idx_val(market_data['nikkei_spot'], " 円", 0)
    nk_f_v, nk_f_d = _fmt_idx_val(market_data['nikkei_fut'], " 円", 0)
    s_c1, s_c2 = st.columns(2)
    s_c1.metric("日経平均 (現物)", nk_s_v, nk_s_d)
    s_c2.metric("日経平均 (先物)", nk_f_v, nk_f_d)

    # S&P 500（現物 ＆ 先物）
    sp_s_v, sp_s_d = _fmt_idx_val(market_data['sp500_spot'], "", 1)
    sp_f_v, sp_f_d = _fmt_idx_val(market_data['sp500_fut'], "", 1)
    s_c3, s_c4 = st.columns(2)
    s_c3.metric("S&P500 (現物)", sp_s_v, sp_s_d)
    s_c4.metric("S&P500 (先物)", sp_f_v, sp_f_d)

    # ナスダック（現物 ＆ 先物）
    nq_s_v, nq_s_d = _fmt_idx_val(market_data['nasdaq_spot'], "", 1)
    nq_f_v, nq_f_d = _fmt_idx_val(market_data['nasdaq_fut'], "", 1)
    s_c5, s_c6 = st.columns(2)
    s_c5.metric("ナスダック (現物)", nq_s_v, nq_s_d)
    s_c6.metric("ナスダック (先物)", nq_f_v, nq_f_d)

    st.markdown("""
    <div style="font-size:0.82rem; color:#555; background:#f8fafc; padding:8px 10px; border-radius:6px; border:1px solid #e2e8f0; margin-top:6px; margin-bottom:8px;">
    <b>【配当・分配金 算出ルール】</b><br>
    ・優先順位：<b>①今期予想額</b> ➔ なければ（0円なら）<b>②直近実績額</b> を自動採用（投信の分配金も同様）<br>
    ・算出方法：<b>① 保有株数(万口数) × 1株(1万口)配当</b><br>
    ・税金表示：<b>税引前</b>と<b>税引後（米国現地税10%＋国内税20.315%控除）</b>の2パターンを常時併記
    </div>
    """, unsafe_allow_html=True)

    if st.button("🔄 最新配当＆市場データを再取得", use_container_width=True):
        st.cache_data.clear()
        if "portfolio_df" in st.session_state:
            del st.session_state["portfolio_df"]
        st.rerun()

    st.markdown("---")
    use_demo = st.checkbox("🧪 デモデータで試す（CSVなしで確認）", value=False)

# --- データの読み込みと最新1株配当(DPS)・投信分配金の自動付与 ---
file_sig = "|".join(sorted([f"{f.name}_{f.size}" for f in uploaded_files])) if uploaded_files else ("DEMO" if use_demo else "EMPTY")

if st.session_state.get("last_file_sig") != file_sig:
    df_list = []
    if uploaded_files:
        for f in uploaded_files:
            parsed = detect_and_parse_csv(f)
            if not parsed.empty:
                df_list.append(parsed)
            else:
                st.sidebar.warning(f"「{f.name}」から有効な保有明細を検出できませんでした。")

    elif use_demo:
        demo_data = [
            {'証券会社': 'SBI証券', '口座区分': 'NISA', '資産クラス': '投資信託', '銘柄コード': 'FUND', '銘柄名': 'eMAXIS Slim 全世界株式(オール・カントリー)', '保有数量': 1800000, '評価額': 4500000, '取得額': 3200000, '評価損益': 1300000, '1株配当(円)': 0.0, '配当利回り(%)': 0.0, '採用基準': '', '配当入金月': '', '配当ソース': ''},
            {'証券会社': 'SBI証券', '口座区分': 'NISA', '資産クラス': '投資信託', '銘柄コード': 'FUND', '銘柄名': 'SBI日本高配当株式（分配）ファンド（年4回決算型）', '保有数量': 1500000, '評価額': 1800000, '取得額': 1500000, '評価損益': 300000, '1株配当(円)': 0.0, '配当利回り(%)': 0.0, '採用基準': '', '配当入金月': '', '配当ソース': ''},
            {'証券会社': 'SBI証券', '口座区分': 'NISA', '資産クラス': '米国株・米国ETF', '銘柄コード': 'VYM', '銘柄名': 'バンガード・米国高配当株式ETF (VYM)', '保有数量': 150, '評価額': 2800000, '取得額': 2100000, '評価損益': 700000, '1株配当(円)': 0.0, '配当利回り(%)': 0.0, '採用基準': '', '配当入金月': '', '配当ソース': ''},
            {'証券会社': 'SBI証券', '口座区分': '特定', '資産クラス': '日本株・東証ETF', '銘柄コード': '8058', '銘柄名': '三菱商事 (8058)', '保有数量': 600, '評価額': 1800000, '取得額': 1200000, '評価損益': 600000, '1株配当(円)': 0.0, '配当利回り(%)': 0.0, '採用基準': '', '配当入金月': '', '配当ソース': ''},
            {'証券会社': 'SBI証券', '口座区分': '特定', '資産クラス': '日本株・東証ETF', '銘柄コード': '8316', '銘柄名': '三井住友フィナンシャルG (8316)', '保有数量': 400, '評価額': 1500000, '取得額': 950000, '評価損益': 550000, '1株配当(円)': 0.0, '配当利回り(%)': 0.0, '採用基準': '', '配当入金月': '', '配当ソース': ''},
            {'証券会社': '楽天証券', '口座区分': 'NISA', '資産クラス': '投資信託', '銘柄コード': 'FUND', '銘柄名': '楽天・全米株式インデックス・ファンド', '保有数量': 1200000, '評価額': 3200000, '取得額': 2400000, '評価損益': 800000, '1株配当(円)': 0.0, '配当利回り(%)': 0.0, '採用基準': '', '配当入金月': '', '配当ソース': ''},
            {'証券会社': '楽天証券', '口座区分': '特定', '資産クラス': '米国株・米国ETF', '銘柄コード': 'SPYD', '銘柄名': 'SPDR ポートフォリオS&P 500高配当株式ETF (SPYD)', '保有数量': 340, '評価額': 2200000, '取得額': 1900000, '評価損益': 300000, '1株配当(円)': 0.0, '配当利回り(%)': 0.0, '採用基準': '', '配当入金月': '', '配当ソース': ''},
            {'証券会社': '楽天証券', '口座区分': '一般', '資産クラス': '日本株・東証ETF', '銘柄コード': '2914', '銘柄名': '日本たばこ産業 (2914)', '保有数量': 300, '評価額': 1200000, '取得額': 900000, '評価損益': 300000, '1株配当(円)': 0.0, '配当利回り(%)': 0.0, '採用基準': '', '配当入金月': '', '配当ソース': ''},
        ]
        df_list.append(pd.DataFrame(demo_data))

    if df_list:
        raw_master = pd.concat(df_list, ignore_index=True)
        for col in ['保有数量', '評価額', '取得額', '評価損益', '1株配当(円)', '配当利回り(%)']:
            if col not in raw_master.columns:
                raw_master[col] = 0.0
            raw_master[col] = raw_master[col].apply(clean_num)
        if '口座区分' not in raw_master.columns:
            raw_master['口座区分'] = '特定'

        with st.spinner("🔍 各銘柄・投信の「今期予想配当（未定なら直近実績）」と「入金月」を厳密調査中..."):
            enriched_master = enrich_with_latest_dividends(raw_master, current_usdjpy)
        st.session_state["portfolio_df"] = enriched_master
    else:
        st.session_state["portfolio_df"] = pd.DataFrame(
            columns=['証券会社', '口座区分', '資産クラス', '為替影響', '銘柄コード', '銘柄名', '保有数量', '評価額', '取得額', '評価損益', '1株配当(円)', '配当利回り(%)', '採用基準', '配当入金月', '配当ソース']
        )
    st.session_state["last_file_sig"] = file_sig

df_master = calc_exact_annual_dividend(st.session_state["portfolio_df"])

# 現在の総資産額（円 & 万円[整数]）
current_total_valuation = float(df_master['評価額'].sum()) if not df_master.empty else 10000000.0
current_total_cost = float(df_master['取得額'].sum()) if not df_master.empty else 8000000.0
current_total_man = int(round(current_total_valuation / 10000.0))
current_annual_div_pre = float(df_master['年間配当金(税引前)'].sum()) if not df_master.empty else 0.0
current_annual_div_post = float(df_master['年間配当金(税引後)'].sum()) if not df_master.empty else 0.0

# --- タブ構成 ---
tab1, tab2, tab3, tab4, tab5 = st.tabs([
    "📊 1. 総合ダッシュボード & 新NISA枠",
    "🎁 2. 配当分析(税引前/税引後) & カレンダー",
    "📈 3. 積立シミュレーション(万円)",
    "🏖️ 4. サイドFIRE診断 & 5段階ロードマップ",
    "💱 5. 為替(ドル円) & リバランス・暴落分析"
])

# ==============================================================================
# TAB 1: 総合ダッシュボード & 新NISA 1,800万円枠トラッカー
# ==============================================================================
with tab1:
    if df_master.empty:
        st.info("👈 左のサイドバーに楽天証券・SBI証券のCSVをまとめてドラッグ＆ドロップするか、「🧪 デモデータで試す」にチェックを入れてください。")
    else:
        total_pnl = current_total_valuation - current_total_cost
        pnl_ratio = (total_pnl / current_total_cost * 100.0) if current_total_cost > 0 else 0.0
        total_yield_pre = (current_annual_div_pre / current_total_valuation * 100.0) if current_total_valuation > 0 else 0.0
        total_yield_post = (current_annual_div_post / current_total_valuation * 100.0) if current_total_valuation > 0 else 0.0

        c1, c2, c3, c4, c5 = st.columns(5)
        c1.metric("総資産額（現時点評価額）", fmt_man_or_sen(current_total_valuation), f"{current_total_valuation:,.0f} 円")
        c2.metric("投資元本（取得額合計）", fmt_man_or_sen(current_total_cost), f"{current_total_cost:,.0f} 円")
        c3.metric("トータル含み損益", fmt_man_or_sen(total_pnl, signed=True), f"{pnl_ratio:+.2f} % ({total_pnl:+,.0f} 円)")
        c4.metric(
            "年間配当・分配金（税引前）",
            fmt_man_or_sen(current_annual_div_pre),
            f"実額 {current_annual_div_pre:,.0f} 円 (利回り {total_yield_pre:.2f}%)"
        )
        c5.metric(
            "年間配当・分配金（税引後・米国税控除済）",
            fmt_man_or_sen(current_annual_div_post),
            f"実額 {current_annual_div_post:,.0f} 円 (利回り {total_yield_post:.2f}%)"
        )

        # --- 新NISA 生涯非課税枠（1,800万円）消化 ＆ 節税メリット診断トラッカー ---
        nisa_df = df_master[df_master['口座区分'] == 'NISA']
        nisa_cost_yen = float(nisa_df['取得額'].sum())
        nisa_val_yen = float(nisa_df['評価額'].sum())
        nisa_pnl_yen = max(nisa_val_yen - nisa_cost_yen, 0.0)
        nisa_div_yen = float(nisa_df['年間配当金(税引後)'].sum())
        nisa_saved_tax_gain = nisa_pnl_yen * 0.20315
        nisa_saved_tax_div = nisa_div_yen * 0.20315

        nisa_limit_yen = 18000000.0
        nisa_rem_yen = max(nisa_limit_yen - nisa_cost_yen, 0.0)
        nisa_prog = min(nisa_cost_yen / nisa_limit_yen, 1.0)

        with st.container(border=True):
            st.markdown("##### 🛡️ 新NISA 生涯非課税保有限度額（1,800万円）消化 ＆ 非課税メリット診断（取得額ベース）")
            nc1, nc2, nc3, nc4 = st.columns(4)
            nc1.metric("NISA 投資元本（取得額）", fmt_man_or_sen(nisa_cost_yen), f"枠消化率 {nisa_prog*100:.1f} % ({nisa_cost_yen:,.0f} 円)")
            nc2.metric("NISA 評価額 & 非課税含み益", fmt_man_or_sen(nisa_val_yen), f"含み益 {fmt_man_or_sen(nisa_pnl_yen, signed=True)}")
            nc3.metric("NISAによる節税メリット額", fmt_man_or_sen(nisa_saved_tax_gain), f"含み益非課税 {nisa_saved_tax_gain:,.0f}円 + 配当節税 {nisa_saved_tax_div:,.0f}円/年")
            nc4.metric("1,800万円枠の残り投資可能額", fmt_man_or_sen(nisa_rem_yen), f"月10万積立であと {nisa_rem_yen / 1200000:.1f} 年" if nisa_rem_yen > 0 else "1,800万円満額達成🎉")
            st.progress(nisa_prog)

        col_g1, col_g2 = st.columns(2)

        with col_g1:
            st.subheader("🗺️ ポートフォリオ・ツリーマップ")
            st.caption("面積＝評価額の大きさ、色＝含み損益額を表します")
            fig_tree = px.treemap(
                df_master,
                path=[px.Constant("全資産"), '証券会社', '口座区分', '銘柄名'],
                values='評価額',
                color='評価損益',
                color_continuous_scale='RdYlGn',
                color_continuous_midpoint=0
            )
            fig_tree.update_layout(margin=dict(t=20, l=10, r=10, b=10))
            st.plotly_chart(fig_tree, use_container_width=True)

        with col_g2:
            st.subheader("🏦 証券会社 × 口座区分 内訳（単位：万円）")
            grp_bar = df_master.groupby(['証券会社', '口座区分'], as_index=False)['評価額'].sum()
            grp_bar['評価額(万円)'] = (grp_bar['評価額'] / 10000.0).round(0)
            grp_bar['表示ラベル'] = [fmt_man(v) for v in grp_bar['評価額(万円)']]
            fig_bar = px.bar(
                grp_bar,
                x='証券会社', y='評価額(万円)', color='口座区分', barmode='group',
                text='表示ラベル',
                title="証券会社・口座別の資産残高"
            )
            fig_bar.update_traces(textposition='outside')
            fig_bar.update_layout(
                yaxis_title="評価額（万円）",
                yaxis=dict(tickformat=",d", ticksuffix="万円")
            )
            st.plotly_chart(fig_bar, use_container_width=True)

        st.subheader("📋 全保有銘柄一覧（今期予想 ➔ 直近実績 ＆ 税引前/税引後 併記）")
        st.caption("※「1株配当(円)」列には、株式・ETFは『1株あたり年間配当金』、投資信託は『1万口あたり年間分配金』を表示しています。")
        disp_cols_t1 = [
            '証券会社', '口座区分', '資産クラス', '銘柄コード', '銘柄名',
            '保有数量', '評価額', '取得額', '評価損益',
            '1株配当(円)', '採用基準', '年間配当金(税引前)', '年間配当金(税引後)',
            '実質配当利回り(税引前%)', '実質配当利回り(税引後%)', '計算式', '税区分'
        ]
        st.dataframe(
            df_master[disp_cols_t1].sort_values('評価額', ascending=False).style.format({
                '保有数量': '{:,.0f}',
                '評価額': '{:,.0f} 円',
                '取得額': '{:,.0f} 円',
                '評価損益': '{:+,.0f} 円',
                '1株配当(円)': '{:,.2f} 円',
                '年間配当金(税引前)': '{:,.0f} 円',
                '年間配当金(税引後)': '{:,.0f} 円',
                '実質配当利回り(税引前%)': '{:.2f} %',
                '実質配当利回り(税引後%)': '{:.2f} %',
            }),
            use_container_width=True,
            hide_index=True
        )

# ==============================================================================
# TAB 2: 配当データ分析（税引前・税引後併記）& 入金月カレンダー & 増配シミュレーター
# ==============================================================================
with tab2:
    st.subheader("🎁 条件別 配当・分配金データ分析（今期予想優先 ➔ 直近実績 / 税引前・税引後併記）")

    if df_master.empty:
        st.warning("分析するデータがありません。サイドバーからCSVをドロップするか、デモデータを有効にしてください。")
    else:
        with st.container(border=True):
            st.markdown("##### 🔍 分析条件の選択（自由組み合わせ）")
            f_col1, f_col2, f_col3 = st.columns(3)

            with f_col1:
                selected_brokers = st.multiselect(
                    "① 対象証券会社",
                    options=['SBI証券', '楽天証券'],
                    default=['SBI証券', '楽天証券']
                )
            with f_col2:
                asset_basis = st.radio(
                    "② 総資産額・利回り算出基準",
                    options=['現時点評価額（時価ベース）', '取得額（簿価 / YOCベース）'],
                    horizontal=False
                )
            with f_col3:
                selected_accounts = st.multiselect(
                    "③ 口座別フィルタ",
                    options=['NISA', '特定', '一般'],
                    default=['NISA', '特定', '一般']
                )

        basis_col = '評価額' if '現時点評価額' in asset_basis else '取得額'

        # --- 検算・手動微調整エディタ（保有株数・1株/1万口配当(円)を直接確認・修正可能） ---
        with st.expander("🛠️ 【1株配当・投信分配金の検算・微調整パネル】保有数量・配当単価(円)・入金月を確認・修正する（クリックで開閉）", expanded=True):
            st.info("""
            💡 **配当・分配金の自動採用ルールと1円単位の検算について**：
            * **今期予想 ➔ 直近実績の自動切替**：今期会社予想が出ている銘柄は**「今期予想」**を採用し、今期予想が未定・非開示（0円）の銘柄やETF・米国株・分配型投信は**「直近実績」**を自動採用しています（『採用基準』列に表示）。
            * **手動での微調整**：もしお手元の管理シートと1株配当（投信は1万口あたり分配金）が異なる場合は、下の表の **`1株/1万口配当(円) ✏️`** をダブルクリックして書き換えるだけで、アプリ全体の合計金額が即座に1円単位で一致します。
            """)
            edit_cols = ['証券会社', '口座区分', '資産クラス', '銘柄コード', '銘柄名', '保有数量', '評価額', '取得額', '1株配当(円)', '採用基準', '配当入金月', '配当ソース']
            edited_df = st.data_editor(
                st.session_state["portfolio_df"][edit_cols],
                column_config={
                    "保有数量": st.column_config.NumberColumn("保有数量(株/口) ✏️", min_value=0.0, step=1.0, format="%d"),
                    "1株配当(円)": st.column_config.NumberColumn("1株/1万口配当(円) ✏️", help="株・ETFは1株あたり年間配当(円)、投資信託は1万口あたり年間分配金(円)", min_value=0.0, step=1.0, format="%.2f 円"),
                    "配当入金月": st.column_config.TextColumn("配当入金月 ✏️", help="カンマ区切りで入金月を入力（例: 6,12 や 3,6,9,12 や 毎月）"),
                    "評価額": st.column_config.NumberColumn("現時点評価額(円)", format="¥%d"),
                    "取得額": st.column_config.NumberColumn("取得額(円)", format="¥%d"),
                },
                disabled=['証券会社', '口座区分', '資産クラス', '銘柄名', '採用基準', '配当ソース'],
                use_container_width=True,
                hide_index=True,
                key="dividend_editor"
            )

            orig_df = st.session_state["portfolio_df"]
            if (not edited_df['保有数量'].equals(orig_df['保有数量'])) or \
               (not edited_df['1株配当(円)'].equals(orig_df['1株配当(円)'])) or \
               (not edited_df['配当入金月'].equals(orig_df['配当入金月'])) or \
               (not edited_df['銘柄コード'].equals(orig_df['銘柄コード'])):
                for c_up in ['保有数量', '1株配当(円)', '配当入金月', '銘柄コード']:
                    st.session_state["portfolio_df"][c_up] = edited_df[c_up]
                st.session_state["portfolio_df"]['配当ソース'] = "手動/保存"
                st.session_state["portfolio_df"]['採用基準'] = "手動設定"
                st.rerun()

            csv_save = st.session_state["portfolio_df"].to_csv(index=False).encode('utf-8-sig')
            st.download_button(
                label="💾 現在のポートフォリオ＆配当データをCSVで保存（次回からそのままドロップ可能）",
                data=csv_save,
                file_name="my_portfolio_with_dividend.csv",
                mime="text/csv"
            )

        df_filtered = df_master[
            (df_master['証券会社'].isin(selected_brokers)) &
            (df_master['口座区分'].isin(selected_accounts))
        ].copy()

        if df_filtered.empty:
            st.warning("選択された条件に一致する銘柄がありません。")
        else:
            df_filtered['基準資産額'] = df_filtered[basis_col]
            df_filtered['基準対比利回り(税引前%)'] = np.where(
                df_filtered['基準資産額'] > 0,
                df_filtered['年間配当金(税引前)'] / df_filtered['基準資産額'] * 100.0,
                0.0
            )
            df_filtered['基準対比利回り(税引後%)'] = np.where(
                df_filtered['基準資産額'] > 0,
                df_filtered['年間配当金(税引後)'] / df_filtered['基準資産額'] * 100.0,
                0.0
            )

            sum_target_asset = float(df_filtered['基準資産額'].sum())
            sum_div_pre = float(df_filtered['年間配当金(税引前)'].sum())
            sum_div_post = float(df_filtered['年間配当金(税引後)'].sum())
            monthly_div_pre = sum_div_pre / 12.0
            monthly_div_post = sum_div_post / 12.0
            portfolio_yield_pre = (sum_div_pre / sum_target_asset * 100.0) if sum_target_asset > 0 else 0.0
            portfolio_yield_post = (sum_div_post / sum_target_asset * 100.0) if sum_target_asset > 0 else 0.0

            st.markdown(f"#### 📊 選択条件での配当分析結果（基準：{asset_basis}）")
            k1, k2, k3, k4, k5 = st.columns(5)
            k1.metric(f"対象総資産額 ({basis_col})", fmt_man_or_sen(sum_target_asset), f"{sum_target_asset:,.0f} 円")
            k2.metric("年間配当金（税引前）", fmt_man_or_sen(sum_div_pre), f"実額 {sum_div_pre:,.0f} 円 (利回り {portfolio_yield_pre:.2f}%)")
            k3.metric("年間配当金（税引後・米国税控除済）", fmt_man_or_sen(sum_div_post), f"実額 {sum_div_post:,.0f} 円 (利回り {portfolio_yield_post:.2f}%)")
            k4.metric("月平均配当（税引前）", fmt_man_or_sen(monthly_div_pre), f"実額 {monthly_div_pre:,.0f} 円/月")
            k5.metric("月平均配当（税引後・手取り）", fmt_man_or_sen(monthly_div_post), f"実額 {monthly_div_post:,.0f} 円/月")

            with st.container(border=True):
                st.markdown("##### 🏆 月間手取り配当金（税引後）マイルストーン達成状況")
                milestones = [
                    ("Step 1: 月1万円（通信費・サブスク無料化）", 10000),
                    ("Step 2: 月3万円（水道光熱費＋食費補助）", 30000),
                    ("Step 3: 月5万円（趣味・旅行・日用品カバー）", 50000),
                    ("Step 4: 月10万円（家賃・住宅ローンカバー / サイドFIRE基盤）", 100000),
                ]
                m_cols = st.columns(4)
                for idx_m, (m_title, m_target) in enumerate(milestones):
                    m_prog = min(monthly_div_post / m_target, 1.0) if m_target > 0 else 0.0
                    with m_cols[idx_m]:
                        st.caption(m_title)
                        st.progress(m_prog)
                        st.write(f"**{m_prog*100:.1f}%** (あと {max(m_target - monthly_div_post, 0):,.0f}円/月)")

            # --- 月別 配当金入金カレンダー（税引前 / 税引後 切り替え可能 ＆ 万円/千円表示） ---
            st.markdown("---")
            st.markdown("#### 🗓️ 月別 配当・分配金入金カレンダー（1月〜12月）")
            c_opt1, c_opt2 = st.columns(2)
            with c_opt1:
                cal_tax_basis = st.radio("カレンダーの税金表示", ["税引後（米国税10%＋国内税控除後の手取り）", "税引前（額面）"], index=0, horizontal=True)
            with c_opt2:
                cal_unit = st.radio("カレンダーグラフの表示単位", ["万円（例：3.5万円）", "千円（例：35.0千円）", "円（例：35,000円）"], index=0, horizontal=True)

            target_div_col = '年間配当金(税引後)' if '税引後' in cal_tax_basis else '年間配当金(税引前)'
            tax_short_label = '税引後' if '税引後' in cal_tax_basis else '税引前'

            cal_records = []
            matrix_rows = []
            for _, row in df_filtered.iterrows():
                ann_val = float(row[target_div_col])
                if ann_val <= 0:
                    continue
                p_months = parse_pay_months_str(row.get('配当入金月', ''))
                if not p_months:
                    p_months = [6, 12]
                per_payment = ann_val / len(p_months)

                m_dict = {
                    '証券会社': row['証券会社'],
                    '口座区分': row['口座区分'],
                    '銘柄名': row['銘柄名'],
                    '入金月': row.get('配当入金月', '')
                }
                for m_idx in range(1, 13):
                    amt = per_payment if m_idx in p_months else 0.0
                    m_dict[f"{m_idx}月"] = round(amt)
                    if amt > 0:
                        cal_records.append({
                            '月': f"{m_idx}月",
                            '銘柄名': f"{row['銘柄名']} ({row['口座区分']})",
                            '配当額(円)': round(amt),
                            '配当額(千円)': round(amt / 1000.0, 1),
                            '配当額(万円)': round(amt / 10000.0, 2)
                        })
                m_dict['年間合計'] = round(ann_val)
                matrix_rows.append(m_dict)

            if cal_records:
                df_cal = pd.DataFrame(cal_records)
                month_order = [f"{m}月" for m in range(1, 13)]

                if "万円" in cal_unit:
                    y_col_cal = '配当額(万円)'
                    y_suffix = "万円"
                    txt_tmpl = '%{y:,.1f}万円'
                    y_fmt = ",.1f"
                elif "千円" in cal_unit:
                    y_col_cal = '配当額(千円)'
                    y_suffix = "千円"
                    txt_tmpl = '%{y:,.1f}千円'
                    y_fmt = ",.1f"
                else:
                    y_col_cal = '配当額(円)'
                    y_suffix = "円"
                    txt_tmpl = '%{y:,.0f}円'
                    y_fmt = ",d"

                fig_cal = px.bar(
                    df_cal,
                    x='月', y=y_col_cal, color='銘柄名',
                    category_orders={'月': month_order},
                    title=f"1月〜12月 月別の配当・分配金 受取スケジュール（{tax_short_label} / 単位：{y_suffix}）"
                )
                fig_cal.update_traces(texttemplate=txt_tmpl, textposition='inside')
                fig_cal.update_layout(
                    xaxis_title="入金月",
                    yaxis_title=f"受取配当金（{tax_short_label}・{y_suffix}）",
                    yaxis=dict(tickformat=y_fmt, ticksuffix=y_suffix),
                    hovermode="x unified"
                )
                st.plotly_chart(fig_cal, use_container_width=True)

                with st.expander(f"📋 1月〜12月 銘柄別・月別配当入金マトリクス表（{tax_short_label}）を見る", expanded=False):
                    df_cal_mat = pd.DataFrame(matrix_rows)
                    month_cols = [f"{m}月" for m in range(1, 13)] + ['年間合計']
                    fmt_dict = {mc: '{:,.0f} 円' for mc in month_cols}
                    st.dataframe(df_cal_mat.style.format(fmt_dict), use_container_width=True, hide_index=True)

            # --- 配当再投資（DRIP）＆ 増配（YOC成長）シミュレーター ---
            st.markdown("---")
            with st.container(border=True):
                st.markdown("#### 🔄 配当再投資 ＆ 増配（YOC成長）シミュレーター（向こう1〜20年）")
                st.caption("高配当株投資の最大の武器である「企業の増配（毎年配当が増える力）」と「受取配当金の再投資」によって、現在の配当金が将来いくらに育つかを試算します。")
                dc1, dc2, dc3 = st.columns(3)
                with dc1:
                    div_growth_rate = st.number_input("想定 年間増配率（%）", min_value=0.0, max_value=20.0, value=4.0, step=0.5, help="連続増配株やVYM等の過去平均増配率は年3%〜6%程度です")
                with dc2:
                    reinvest_flag = st.radio("受取配当金の再投資", ["毎年全額を再投資する（複利加速）", "再投資しない（配当を生活費に使う）"], index=0)
                with dc3:
                    add_yearly_high_div = st.number_input("高配当株への年間追加投資額（万円/年）", min_value=0, max_value=1000, value=0, step=10)

                if sum_div_post > 0 and sum_target_asset > 0:
                    cur_y_post = portfolio_yield_post / 100.0
                    g_rate = div_growth_rate / 100.0
                    do_reinv = ("全額を再投資" in reinvest_flag)

                    drip_rows = []
                    sim_div_yen = sum_div_post
                    sim_cost_yen = float(df_filtered['取得額'].sum())
                    for y_i in range(1, 21):
                        sim_div_yen = sim_div_yen * (1.0 + g_rate)
                        if do_reinv:
                            sim_div_yen += sim_div_yen * cur_y_post
                        if add_yearly_high_div > 0:
                            add_yen = add_yearly_high_div * 10000.0
                            sim_cost_yen += add_yen
                            sim_div_yen += add_yen * cur_y_post

                        yoc_future = (sim_div_yen / sim_cost_yen * 100.0) if sim_cost_yen > 0 else 0.0
                        drip_rows.append({
                            '経過年': f"{y_i}年後({datetime.now().year + y_i}年)",
                            '年間手取り配当金(万円)': round(sim_div_yen / 10000.0, 1),
                            '表示ラベル': fmt_man_or_sen(sim_div_yen),
                            '年間手取り配当金(円)': int(round(sim_div_yen)),
                            '将来YOC(元本利回り)': f"{yoc_future:.2f} %"
                        })
                    df_drip = pd.DataFrame(drip_rows)
                    fig_drip = px.bar(
                        df_drip, x='経過年', y='年間手取り配当金(万円)',
                        text='表示ラベル',
                        title="増配＆再投資による 将来の年間手取り配当金 推移"
                    )
                    fig_drip.update_traces(textposition='outside', marker_color='#f59e0b')
                    fig_drip.update_layout(
                        yaxis_title="年間手取り配当金（万円）",
                        yaxis=dict(tickformat=",.1f", ticksuffix="万円")
                    )
                    st.plotly_chart(fig_drip, use_container_width=True)

            st.markdown("---")
            g_col1, g_col2 = st.columns(2)
            with g_col1:
                df_div_only = df_filtered[df_filtered['年間配当金(税引後)'] > 0]
                if not df_div_only.empty:
                    fig_div_pie = px.pie(
                        df_div_only, values='年間配当金(税引後)', names='銘柄名',
                        title="銘柄別 手取り配当金（税引後）構成比", hole=0.4
                    )
                    fig_div_pie.update_traces(textposition='inside', textinfo='percent+label')
                    st.plotly_chart(fig_div_pie, use_container_width=True)

            with g_col2:
                grp_acc_div = df_filtered.groupby(['口座区分'], as_index=False)[['年間配当金(税引前)', '年間配当金(税引後)']].sum()
                grp_acc_div['税引前(万円)'] = (grp_acc_div['年間配当金(税引前)'] / 10000.0).round(2)
                grp_acc_div['税引後(万円)'] = (grp_acc_div['年間配当金(税引後)'] / 10000.0).round(2)
                fig_acc_div = go.Figure()
                fig_acc_div.add_trace(go.Bar(
                    x=grp_acc_div['口座区分'], y=grp_acc_div['税引前(万円)'],
                    name='税引前（額面）', marker_color='#93c5fd',
                    text=grp_acc_div['税引前(万円)'], texttemplate='%{text:,.1f}万円', textposition='outside'
                ))
                fig_acc_div.add_trace(go.Bar(
                    x=grp_acc_div['口座区分'], y=grp_acc_div['税引後(万円)'],
                    name='税引後（米国税10%+国内税控除）', marker_color='#10b981',
                    text=grp_acc_div['税引後(万円)'], texttemplate='%{text:,.1f}万円', textposition='outside'
                ))
                fig_acc_div.update_layout(
                    barmode='group',
                    title="口座区分別 年間配当金 比較（税引前 vs 税引後 / 単位：万円）",
                    yaxis_title="年間配当金（万円）",
                    yaxis=dict(tickformat=",.1f", ticksuffix="万円")
                )
                st.plotly_chart(fig_acc_div, use_container_width=True)

            st.markdown("##### 📋 条件抽出銘柄の配当詳細・検算リスト（今期予想優先 ➔ 直近実績 / 税引前・税引後併記）")
            display_cols = [
                '証券会社', '口座区分', '資産クラス', '銘柄コード', '銘柄名',
                '保有数量', '基準資産額', '1株配当(円)', '採用基準',
                '年間配当金(税引前)', '年間配当金(税引後)',
                '基準対比利回り(税引前%)', '基準対比利回り(税引後%)',
                '計算式', '税区分', '配当入金月', '配当ソース'
            ]
            st.dataframe(
                df_filtered[display_cols].sort_values('年間配当金(税引後)', ascending=False).style.format({
                    '保有数量': '{:,.0f}',
                    '基準資産額': '{:,.0f} 円',
                    '1株配当(円)': '{:,.2f} 円',
                    '年間配当金(税引前)': '{:,.0f} 円',
                    '年間配当金(税引後)': '{:,.0f} 円',
                    '基準対比利回り(税引前%)': '{:.2f} %',
                    '基準対比利回り(税引後%)': '{:.2f} %',
                }),
                use_container_width=True,
                hide_index=True
            )

# ==============================================================================
# TAB 3: 積み立て想定シミュレーション（5桁万円は「〇億〇万円」表記に自動変換）
# ==============================================================================
with tab3:
    st.subheader("📈 積立投資 将来資産シミュレーション（1〜30年 / 1億円以上は『〇億〇万円』表示）")

    base_year = datetime.now().year

    with st.container(border=True):
        s_col1, s_col2, s_col3 = st.columns(3)
        with s_col1:
            sim_initial_man = st.number_input(
                "現在の総資産額（万円）",
                min_value=0, max_value=100000,
                value=int(current_total_man), step=10,
                help="CSVから読み取った現在の総資産額（万円）が初期セットされています"
            )
        with s_col2:
            sim_monthly_man = st.number_input(
                "毎月の積立額（万円）",
                min_value=0, max_value=1000,
                value=10, step=1
            )
        with s_col3:
            sim_rate = st.number_input(
                "想定運用利回り（年率 %）",
                min_value=0.0, max_value=30.0,
                value=5.0, step=0.1
            )

    monthly_rate = (sim_rate / 100.0) / 12.0
    sim_rows = []
    balance_man = float(sim_initial_man)
    principal_man = float(sim_initial_man)

    for year in range(1, 31):
        prev_balance_man = balance_man
        for _ in range(12):
            balance_man = balance_man * (1.0 + monthly_rate) + sim_monthly_man
            principal_man += sim_monthly_man
        profit_man = balance_man - principal_man
        yearly_gain_man = balance_man - prev_balance_man
        cal_year = base_year + year
        sim_rows.append({
            '年数_数値': year,
            '経過年数': f"{year}年目",
            '西暦': f"{cal_year}年",
            '年表示(グラフ用)': f"{year}年目({cal_year}年)",
            '元本合計(万円)': int(round(principal_man)),
            '運用益合計(万円)': int(round(profit_man)),
            '総資産想定額(万円)': int(round(balance_man)),
            '年間増加額(万円)': int(round(yearly_gain_man)),
            '元本_表示': fmt_man(principal_man),
            '運用益_表示': fmt_man(profit_man),
            '総資産_表示': fmt_man(balance_man),
        })

    df_sim = pd.DataFrame(sim_rows)

    st.markdown("#### 🎯 10年目・20年目・30年後の到達マイルストーン")
    m10 = df_sim[df_sim['年数_数値'] == 10].iloc[0]
    m20 = df_sim[df_sim['年数_数値'] == 20].iloc[0]
    m30 = df_sim[df_sim['年数_数値'] == 30].iloc[0]

    mc1, mc2, mc3 = st.columns(3)
    mc1.metric(
        f"【10年目 / {m10['西暦']}】総資産想定額",
        fmt_man(m10['総資産想定額(万円)']),
        f"うち運用益 {fmt_man(m10['運用益合計(万円)'], signed=True)}"
    )
    mc2.metric(
        f"【20年目 / {m20['西暦']}】総資産想定額",
        fmt_man(m20['総資産想定額(万円)']),
        f"うち運用益 {fmt_man(m20['運用益合計(万円)'], signed=True)}"
    )
    mc3.metric(
        f"【30年目 / {m30['西暦']}】総資産想定額",
        fmt_man(m30['総資産想定額(万円)']),
        f"うち運用益 {fmt_man(m30['運用益合計(万円)'], signed=True)}"
    )

    st.markdown("#### 📊 年次別 総資産推移グラフ（元本 ＋ 運用益）")
    view_span = st.radio("表示期間の切り替え", ["10年目まで", "20年目まで", "30年目まで（全期間）"], index=2, horizontal=True)
    max_y = 10 if "10" in view_span else (20 if "20" in view_span else 30)
    df_sim_filtered = df_sim[df_sim['年数_数値'] <= max_y]

    fig_sim = go.Figure()
    fig_sim.add_trace(go.Bar(
        x=df_sim_filtered['年表示(グラフ用)'], y=df_sim_filtered['元本合計(万円)'],
        name='投資元本（初期資産＋積立累計）', marker_color='#3b82f6',
        customdata=df_sim_filtered['元本_表示'],
        hovertemplate='%{customdata}<extra>投資元本</extra>'
    ))
    fig_sim.add_trace(go.Bar(
        x=df_sim_filtered['年表示(グラフ用)'], y=df_sim_filtered['運用益合計(万円)'],
        name='複利運用益', marker_color='#10b981',
        customdata=df_sim_filtered[['運用益_表示', '総資産_表示']],
        hovertemplate='%{customdata[0]}（総資産合計: %{customdata[1]}）<extra>複利運用益</extra>'
    ))
    fig_sim.update_layout(
        barmode='stack',
        xaxis_title='経過年数（西暦）',
        yaxis_title='金額（万円）',
        yaxis=dict(tickformat=",d", ticksuffix="万円"),
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
        hovermode="x unified"
    )
    st.plotly_chart(fig_sim, use_container_width=True)

    st.markdown("#### 📋 年次別 総資産想定額 一覧表（〇年目 ＆ 西暦 / 5桁万円は『〇億〇万円』表記）")
    df_sim_table = df_sim_filtered[['経過年数', '西暦', '元本合計(万円)', '運用益合計(万円)', '総資産想定額(万円)', '年間増加額(万円)']]
    st.dataframe(
        df_sim_table.style.format({
            '元本合計(万円)': lambda v: fmt_man(v),
            '運用益合計(万円)': lambda v: fmt_man(v),
            '総資産想定額(万円)': lambda v: fmt_man(v),
            '年間増加額(万円)': lambda v: fmt_man(v, signed=True),
        }),
        use_container_width=True,
        hide_index=True
    )

# ==============================================================================
# TAB 4: サイドFIRE シミュレーション & 5段階FIREステージ到達ロードマップ
# ==============================================================================
with tab4:
    st.subheader("🏖️ サイドFIRE 実現可能性シミュレーター ＆ 5段階FIREロードマップ")

    now_dt = datetime.now()
    cur_year, cur_month = now_dt.year, now_dt.month

    with st.container(border=True):
        st.markdown("##### ⚙️ シミュレーション前提条件の入力（単位：万円）")
        f_c1, f_c2, f_c3 = st.columns(3)

        with f_c1:
            st.markdown("**① 現在の資産と運用条件**")
            fire_init_man = st.number_input("現在の総資産額（万円）", min_value=0, max_value=100000, value=int(current_total_man), step=10, key="f_init")
            fire_yield = st.number_input(
                "想定運用利回り（年率 %）",
                min_value=0.0, max_value=25.0, value=5.0, step=0.1, key="f_yield"
            )
            pre_fire_saving_man = st.number_input("サイドFIRE達成までの毎月積立額（万円）", min_value=0, max_value=500, value=10, step=1)
            inflation_rate = st.number_input(
                "想定インフレ率（生活費の上昇率・年率 %）",
                min_value=0.0, max_value=10.0, value=1.0, step=0.1
            )

        with f_c2:
            st.markdown("**② サイドFIRE・年金スケジュール**")
            sc1, sc2 = st.columns(2)
            fire_year = sc1.number_input("サイドFIRE予定（年）", min_value=cur_year, max_value=cur_year + 50, value=cur_year + 5)
            fire_month = sc2.number_input("サイドFIRE予定（月）", min_value=1, max_value=12, value=4)

            pc1, pc2 = st.columns(2)
            pension_year = pc1.number_input("年金受給開始（年）", min_value=cur_year, max_value=cur_year + 70, value=cur_year + 20)
            pension_month = pc2.number_input("年金受給開始（月）", min_value=1, max_value=12, value=4)

            sim_years_total = st.slider("シミュレーション検証期間（今後何年間を検証するか）", min_value=20, max_value=60, value=40)

        with f_c3:
            st.markdown("**③ サイドFIRE後の月次収支（万円）**")
            living_cost_man = st.number_input("想定生活費（月額・万円）", min_value=5, max_value=200, value=25, step=1)
            side_income_man = st.number_input("副業想定収入（手取り月額・万円）", min_value=0, max_value=200, value=10, step=1)
            side_job_end_option = st.selectbox("副業の継続期間", ["年金受給開始まで継続", "サイドFIRE後10年間のみ", "サイドFIRE後15年間のみ", "生涯継続"])
            pension_man = st.number_input("年金予定額（手取り月額・万円）", min_value=0, max_value=100, value=15, step=1)

        st.info("""
        💡 **【設定ガイド】「想定運用利回り」と「想定インフレ率」はどう設定すべき？（過去の実績データからの目安）**
        * **📈 想定運用利回り（年率 %）の目安**：
          * **全世界株式（オルカン等）**：過去30年の平均リターンは **年率 約7%〜8%**（円建て）ですが、暴落期や税金も考慮したサイドFIRE計画では **【4.0% 〜 5.0%】** に設定するのがFP・FIRE実践者の王道です。
          * **米国株式（S&P500等）**：過去30年平均は **年率 約9%〜10%**。長期計画で堅実に見積もるなら **【5.0% 〜 6.0%】** が目安です。
          * **日米高配当株・ETF中心（VYM / SPYD / 日本高配当株）**：配当利回り（税引後3〜3.5%）＋緩やかな増配・株価成長を合わせて **【4.0% 〜 5.0%】** が現実的です。
        * **🛒 想定インフレ率（生活費の上昇率・年率 %）の目安**：
          * **日本の過去実績と現在**：日本の過去30年平均は **年率 約0.3%〜0.5%**、直近の物価上昇期は **2%〜3%**（日本銀行の物価安定目標は **年率 2.0%**）。
          * **推奨される設定値**：標準的なシミュレーションなら **【1.0% 〜 1.5%】**、将来の物価上昇リスクを厳しめに織り込むなら **【2.0%】** に設定するのがおすすめです。
        """)

    # --- 5段階のFIREステージ別 到達ロードマップ診断（5桁万円は〇億〇万円表示） ---
    st.markdown("#### 🏔️ あなたの「5段階 FIREステージ」到達ロードマップ診断")
    st.caption("現在の資産・生活費・副業収入・手取り配当利回りから、5つの代表的なFIREステージの目標金額と到達予想時期を診断します。")

    def estimate_years_to_target(curr_m, target_m, monthly_save_m, annual_r_pct):
        if curr_m >= target_m:
            return "達成済 🎉"
        mr = (annual_r_pct / 100.0) / 12.0
        b = float(curr_m)
        for m_i in range(1, 601):
            b = b * (1.0 + mr) + monthly_save_m
            if b >= target_m:
                years_needed = m_i / 12.0
                return f"あと約 {years_needed:.1f} 年 ({cur_year + int(round(years_needed))}年頃)"
        return "50年以上"

    years_to_pension = max(1, int(pension_year - cur_year))
    net_retire_need_man = max(living_cost_man - pension_man, 5) * 12 * 25
    real_return = max((fire_yield - inflation_rate) / 100.0, 0.01)
    coast_fire_target_man = int(round(net_retire_need_man / ((1.0 + real_return) ** years_to_pension)))

    side_fire_target_man = int(round(max(living_cost_man - side_income_man, 0) * 12 * 25))

    cur_div_yield_post = (current_annual_div_post / current_total_valuation) if current_total_valuation > 0 else 0.035
    eff_div_yield_for_fire = min(max(cur_div_yield_post, 0.03), 0.08)
    div_side_fire_target_man = int(round((max(living_cost_man - side_income_man, 0) * 12) / eff_div_yield_for_fire))

    lean_fire_target_man = int(round((living_cost_man * 0.8) * 12 * 25))
    full_fire_target_man = int(round(living_cost_man * 12 * 25))

    stage_rows = [
        {
            'FIREステージ': '① コーストFIRE (Coast FIRE)',
            '状態・定義': f'追加積立を0円にしても、現在の資産の複利成長だけで年金開始({pension_year}年)時に老後資金が足りる状態',
            '必要目標資産': fmt_man(coast_fire_target_man),
            '現在の達成率': f'{min(fire_init_man / max(coast_fire_target_man, 1) * 100, 100):.1f} %',
            'あと必要額': fmt_man(max(coast_fire_target_man - fire_init_man, 0)),
            '到達予想時期': estimate_years_to_target(fire_init_man, coast_fire_target_man, pre_fire_saving_man, fire_yield)
        },
        {
            'FIREステージ': '② サイドFIRE (4%ルール基準)',
            '状態・定義': f'資産の4%取崩し＋副業収入(月{side_income_man}万円)で生活費(月{living_cost_man}万円)を100%賄える状態',
            '必要目標資産': fmt_man(side_fire_target_man),
            '現在の達成率': f'{min(fire_init_man / max(side_fire_target_man, 1) * 100, 100):.1f} %',
            'あと必要額': fmt_man(max(side_fire_target_man - fire_init_man, 0)),
            '到達予想時期': estimate_years_to_target(fire_init_man, side_fire_target_man, pre_fire_saving_man, fire_yield)
        },
        {
            'FIREステージ': '③ 配当型サイドFIRE (元本取崩しゼロ)',
            '状態・定義': f'元本を一切売らず、手取り配当金(利回り{eff_div_yield_for_fire*100:.1f}%想定)＋副業収入だけで暮らせる状態',
            '必要目標資産': fmt_man(div_side_fire_target_man),
            '現在の達成率': f'{min(fire_init_man / max(div_side_fire_target_man, 1) * 100, 100):.1f} %',
            'あと必要額': fmt_man(max(div_side_fire_target_man - fire_init_man, 0)),
            '到達予想時期': estimate_years_to_target(fire_init_man, div_side_fire_target_man, pre_fire_saving_man, fire_yield)
        },
        {
            'FIREステージ': '④ リーンFIRE (倹約型・完全リタイア)',
            '状態・定義': f'副業なしで、基礎生活費(月{living_cost_man*0.8:.0f}万円)を資産の4%取崩しだけで賄える状態',
            '必要目標資産': fmt_man(lean_fire_target_man),
            '現在の達成率': f'{min(fire_init_man / max(lean_fire_target_man, 1) * 100, 100):.1f} %',
            'あと必要額': fmt_man(max(lean_fire_target_man - fire_init_man, 0)),
            '到達予想時期': estimate_years_to_target(fire_init_man, lean_fire_target_man, pre_fire_saving_man, fire_yield)
        },
        {
            'FIREステージ': '⑤ フルFIRE (完全FIRE)',
            '状態・定義': f'副業なしで、現在の生活費(月{living_cost_man}万円)をすべて資産の4%取崩しだけで生涯賄える状態',
            '必要目標資産': fmt_man(full_fire_target_man),
            '現在の達成率': f'{min(fire_init_man / max(full_fire_target_man, 1) * 100, 100):.1f} %',
            'あと必要額': fmt_man(max(full_fire_target_man - fire_init_man, 0)),
            '到達予想時期': estimate_years_to_target(fire_init_man, full_fire_target_man, pre_fire_saving_man, fire_yield)
        },
    ]
    st.table(pd.DataFrame(stage_rows))

    # --- キャッシュフローシミュレーション実行 ---
    def run_fire_simulation(init_man, y_rate, save_man, inf_rate, f_year, f_month, p_year, p_month, years_len, liv_man, side_man, side_opt, pen_man):
        start_d = datetime(cur_year, cur_month, 1)
        fire_d = datetime(int(f_year), int(f_month), 1)
        pen_d = datetime(int(p_year), int(p_month), 1)

        m_ret = (y_rate / 100.0) / 12.0
        m_inf = (inf_rate / 100.0) / 12.0

        bal = float(init_man)
        at_fire = float(init_man) if fire_d <= start_d else None
        dep_date = None
        records = []

        y_save = y_side = y_pen = y_liv = y_gain = 0.0

        for m in range(years_len * 12):
            c_dt = start_d + relativedelta(months=m)
            is_f = c_dt >= fire_d
            is_p = c_dt >= pen_d

            if is_f and at_fire is None:
                at_fire = bal

            adj_liv = liv_man * ((1.0 + m_inf) ** m)

            if not is_f:
                gain = bal * m_ret
                bal = bal + gain + save_man
                y_save += save_man
                y_gain += gain
            else:
                m_since_f = (c_dt.year - fire_d.year) * 12 + (c_dt.month - fire_d.month)
                act_side = True
                if side_opt == "年金受給開始まで継続" and is_p:
                    act_side = False
                elif side_opt == "サイドFIRE後10年間のみ" and m_since_f >= 120:
                    act_side = False
                elif side_opt == "サイドFIRE後15年間のみ" and m_since_f >= 180:
                    act_side = False

                c_side = side_man if act_side else 0.0
                c_pen = pen_man if is_p else 0.0
                net_cf = c_side + c_pen - adj_liv
                gain = bal * m_ret if bal > 0 else 0.0
                bal = bal + gain + net_cf

                if bal <= 0 and dep_date is None:
                    dep_date = c_dt

                y_side += c_side
                y_pen += c_pen
                y_liv += adj_liv
                y_gain += gain

            if (m + 1) % 12 == 0:
                y_num = (m + 1) // 12
                st_str = "積立期" if not is_f else ("年金＋サイドFIRE" if is_p else "サイドFIRE期")
                end_b = int(round(max(bal, 0.0)))
                records.append({
                    '西暦_数値': c_dt.year,
                    '経過年数': f"{y_num}年目",
                    '西暦': f"{c_dt.year}年",
                    'ステータス': st_str,
                    '年間積立額(万円)': int(round(y_save)),
                    '年間副業収入(万円)': int(round(y_side)),
                    '年間年金収入(万円)': int(round(y_pen)),
                    '年間生活費(万円)': int(round(y_liv)),
                    '年間運用益(万円)': int(round(y_gain)),
                    '年間資産増減(万円)': int(round(y_save + y_side + y_pen - y_liv + y_gain)),
                    '年末資産残高(万円)': end_b,
                    '残高_表示': fmt_man(end_b),
                })
                y_save = y_side = y_pen = y_liv = y_gain = 0.0

        if at_fire is None:
            at_fire = bal
        return pd.DataFrame(records), int(round(at_fire)), dep_date

    df_fire_sim, asset_at_fire_man, depletion_date = run_fire_simulation(
        fire_init_man, fire_yield, pre_fire_saving_man, inflation_rate,
        fire_year, fire_month, pension_year, pension_month,
        sim_years_total, living_cost_man, side_income_man, side_job_end_option, pension_man
    )

    st.markdown("---")
    st.markdown("#### 🩺 サイドFIRE 実現可能性 判定結果")

    fire_gap_man = max(living_cost_man - side_income_man, 0)
    required_fire_man_4pct = int(round(fire_gap_man * 12 * 25))

    if depletion_date is None and df_fire_sim.iloc[-1]['年末資産残高(万円)'] >= asset_at_fire_man:
        st.success(f"🎉 **【判定：大成功（余裕でサイドFIRE可能）】** {fire_year}年{fire_month}月にサイドFIREしても資産は一切枯渇せず、運用益が取り崩しを上回り資産が増え続けます！")
    elif depletion_date is None:
        st.info(f"✅ **【判定：サイドFIRE可能】** 資産を取り崩しながらも、今後{sim_years_total}年間（{cur_year + sim_years_total}年まで）資産が枯渇せず維持できます。")
    else:
        st.error(f"⚠️ **【判定：資金枯渇リスクあり】** この条件では **{depletion_date.year}年{depletion_date.month}月頃** に資産が枯渇する見込みです。サイドFIRE時期を遅らせるか、副業収入・生活費の見直しを推奨します。")

    fk1, fk2, fk3, fk4 = st.columns(4)
    fk1.metric(f"FIRE時点({fire_year}/{fire_month})想定資産", fmt_man(asset_at_fire_man))
    fk2.metric("FIRE直後の毎月不足額", f"{fire_gap_man:,.0f} 万円/月", f"生活費{living_cost_man}万 - 副業{side_income_man}万")
    fk3.metric("4%ルール上の必要目標資産", fmt_man(required_fire_man_4pct), f"過不足 {fmt_man(asset_at_fire_man - required_fire_man_4pct, signed=True)}")
    fk4.metric(f"{sim_years_total}年後の最終資産残高", fmt_man(df_fire_sim.iloc[-1]['年末資産残高(万円)']))

    fig_fire = go.Figure()
    fig_fire.add_trace(go.Scatter(
        x=df_fire_sim['西暦_数値'], y=df_fire_sim['年末資産残高(万円)'],
        mode='lines+markers', name='年末資産残高',
        fill='tozeroy', line=dict(color='#10b981', width=3),
        customdata=df_fire_sim['残高_表示'],
        hovertemplate='%{customdata}<extra>年末資産残高</extra>'
    ))
    fig_fire.add_vline(
        x=int(fire_year), line_dash="dash", line_color="orange",
        annotation_text="サイドFIRE開始", annotation_position="top left"
    )
    if pension_year <= cur_year + sim_years_total:
        fig_fire.add_vline(
            x=int(pension_year), line_dash="dash", line_color="blue",
            annotation_text="年金受給開始", annotation_position="top left"
        )
    fig_fire.update_layout(
        title="サイドFIRE後の長期資産残高推移",
        xaxis_title="西暦（年）",
        yaxis_title="資産残高（万円）",
        xaxis=dict(tickformat="d", ticksuffix="年"),
        yaxis=dict(tickformat=",d", ticksuffix="万円"),
        hovermode="x unified"
    )
    st.plotly_chart(fig_fire, use_container_width=True)

    with st.expander("🔥 サイドFIRE 感度分析マトリクス（利回り × 副業収入別の最終資産早見表）", expanded=True):
        st.caption(f"「もし運用利回りが想定と違ったら？」「副業収入が増減したら？」のシナリオ別に、{sim_years_total}年後の最終資産残高を比較します（1億円以上は『〇億〇万円』表示）。")
        test_yields = [3.0, 4.0, 5.0, 6.0, 7.0]
        test_sides = [max(0, side_income_man - 5), side_income_man, side_income_man + 5, side_income_man + 10]
        matrix_data = {}
        for s_val in test_sides:
            col_label = f"副業 月{s_val:,.0f}万円"
            matrix_data[col_label] = []
            for y_val in test_yields:
                df_tmp, _, dep_tmp = run_fire_simulation(
                    fire_init_man, y_val, pre_fire_saving_man, inflation_rate,
                    fire_year, fire_month, pension_year, pension_month,
                    sim_years_total, living_cost_man, s_val, side_job_end_option, pension_man
                )
                end_bal = df_tmp.iloc[-1]['年末資産残高(万円)']
                if dep_tmp is not None:
                    matrix_data[col_label].append(f"枯渇({dep_tmp.year}年)")
                else:
                    matrix_data[col_label].append(fmt_man(end_bal))
        df_matrix = pd.DataFrame(matrix_data, index=[f"利回り {y:.1f}%" for y in test_yields])
        st.table(df_matrix)

    st.markdown("##### 📋 年次別 キャッシュフロー＆資産推移表（5桁万円は『〇億〇万円』表記）")
    df_fire_table = df_fire_sim.drop(columns=['西暦_数値', '残高_表示'])
    st.dataframe(
        df_fire_table.style.format({
            '年間積立額(万円)': lambda v: fmt_man(v),
            '年間副業収入(万円)': lambda v: fmt_man(v),
            '年間年金収入(万円)': lambda v: fmt_man(v),
            '年間生活費(万円)': lambda v: fmt_man(v),
            '年間運用益(万円)': lambda v: fmt_man(v),
            '年間資産増減(万円)': lambda v: fmt_man(v, signed=True),
            '年末資産残高(万円)': lambda v: fmt_man(v),
        }),
        use_container_width=True,
        hide_index=True
    )

# ==============================================================================
# TAB 5: 為替レート（ドル円）連動シミュレーター & リバランス・暴落ストレステスト
# ==============================================================================
with tab5:
    st.subheader("💱 為替レート（ドル円）連動シミュレーター & リバランス・暴落分析")

    with st.container(border=True):
        st.markdown(f"#### 💱 1. ドル円（USD/JPY）為替変動シミュレーター（現在レート: 1ドル = {current_usdjpy:.2f} 円）")
        st.caption("米国株・米国ETFおよび為替ヘッジなしの外国株インデックス投信（オルカン・S&P500等）が、円高・円安によっていくら増減するかをリアルタイム試算します。")

        if df_master.empty:
            st.info("CSVを取り込むかデモデータを有効にすると、保有銘柄の外貨エクスポージャーを自動計算します。")
        else:
            fx_mask = df_master['為替影響'] == "米ドル連動(為替影響あり)"
            fx_asset_yen = float(df_master.loc[fx_mask, '評価額'].sum())
            jp_asset_yen = float(df_master.loc[~fx_mask, '評価額'].sum())
            fx_div_pre_yen = float(df_master.loc[fx_mask, '年間配当金(税引前)'].sum())
            jp_div_pre_yen = float(df_master.loc[~fx_mask, '年間配当金(税引前)'].sum())
            fx_div_post_yen = float(df_master.loc[fx_mask, '年間配当金(税引後)'].sum())
            jp_div_post_yen = float(df_master.loc[~fx_mask, '年間配当金(税引後)'].sum())

            fx_ratio_pct = (fx_asset_yen / current_total_valuation * 100.0) if current_total_valuation > 0 else 0.0

            fx_c1, fx_c2 = st.columns([1, 2])
            with fx_c1:
                sim_usdjpy = st.slider(
                    "想定ドル円レート（円/$）を動かして試算",
                    min_value=100, max_value=180,
                    value=int(round(current_usdjpy)), step=1
                )
                fx_change_ratio = sim_usdjpy / current_usdjpy if current_usdjpy > 0 else 1.0
                fx_change_pct = (fx_change_ratio - 1.0) * 100.0

                new_fx_asset_yen = fx_asset_yen * fx_change_ratio
                new_total_yen = jp_asset_yen + new_fx_asset_yen
                diff_total_yen = new_total_yen - current_total_valuation

                new_total_div_post_yen = jp_div_post_yen + (fx_div_post_yen * fx_change_ratio)
                diff_div_post_yen = new_total_div_post_yen - (jp_div_post_yen + fx_div_post_yen)

                st.metric("外貨連動資産の割合", f"{fx_ratio_pct:.1f} %", f"対象額 {fmt_man_or_sen(fx_asset_yen)}")
                st.metric(
                    f"1ドル={sim_usdjpy}円時の総資産額",
                    fmt_man_or_sen(new_total_yen),
                    f"{fmt_man_or_sen(diff_total_yen, signed=True)} (為替 {fx_change_pct:+.1f}%)"
                )
                st.metric(
                    f"1ドル={sim_usdjpy}円時の年間配当（税引後）",
                    fmt_man_or_sen(new_total_div_post_yen),
                    f"実額 {new_total_div_post_yen:,.0f} 円 ({diff_div_post_yen:+,.0f} 円)"
                )

            with fx_c2:
                rate_scenarios = [110, 120, 130, 140, int(round(current_usdjpy)), 160, 170]
                fx_rows = []
                for r_val in rate_scenarios:
                    r_ratio = r_val / current_usdjpy if current_usdjpy > 0 else 1.0
                    s_total_man = (jp_asset_yen + fx_asset_yen * r_ratio) / 10000.0
                    s_diff_man = s_total_man - (current_total_valuation / 10000.0)
                    s_div_pre = jp_div_pre_yen + fx_div_pre_yen * r_ratio
                    s_div_post = jp_div_post_yen + fx_div_post_yen * r_ratio
                    label_str = f"1ドル = {r_val} 円 (現在)" if r_val == int(round(current_usdjpy)) else f"1ドル = {r_val} 円"
                    fx_rows.append({
                        '為替シナリオ': label_str,
                        '為替変動率': f"{(r_ratio - 1.0)*100:+.1f} %",
                        '想定総資産額': fmt_man(s_total_man),
                        '資産増減': fmt_man(s_diff_man, signed=True),
                        '年間配当(税引前)': f"{s_div_pre:,.0f} 円",
                        '年間配当(税引後)': f"{s_div_post:,.0f} 円"
                    })
                st.markdown("**📋 ドル円レート別 資産額＆配当金（税引前・税引後）シミュレーション一覧表**")
                st.table(pd.DataFrame(fx_rows))

    st.markdown("---")
    col_r1, col_r2 = st.columns(2)

    with col_r1:
        st.markdown("#### ⚖️ 2. アセットクラス別 リバランス計算機")
        st.caption("現在の資産配分と目標配分のズレを計算し、どの資産クラスをいくら買い増し（または売却）すべきか算出します。")

        if df_master.empty:
            st.info("CSVデータを取り込むとリバランス計算が有効になります。")
        else:
            class_grp = df_master.groupby('資産クラス', as_index=False)['評価額'].sum()
            all_classes = ['投資信託', '日本株・東証ETF', '米国株・米国ETF']
            cur_map = {row['資産クラス']: row['評価額'] / 10000.0 for _, row in class_grp.iterrows()}

            rc1, rc2, rc3 = st.columns(3)
            t_fund = rc1.number_input("目標：投資信託(%)", 0, 100, 50, step=5)
            t_jp = rc2.number_input("目標：日本株・ETF(%)", 0, 100, 25, step=5)
            t_us = rc3.number_input("目標：米国株・ETF(%)", 0, 100, 25, step=5)

            if t_fund + t_jp + t_us != 100:
                st.warning(f"目標比率の合計が {t_fund + t_jp + t_us}% です（100%になるように調整してください）。")

            targets = {'投資信託': t_fund, '日本株・東証ETF': t_jp, '米国株・米国ETF': t_us}
            total_man_exact = current_total_valuation / 10000.0
            reb_rows = []
            for c_name in all_classes:
                c_man = cur_map.get(c_name, 0.0)
                c_pct = (c_man / total_man_exact * 100.0) if total_man_exact > 0 else 0.0
                t_pct = targets[c_name]
                ideal_man = total_man_exact * (t_pct / 100.0)
                diff_man = ideal_man - c_man
                action = f"{fmt_man(diff_man, signed=True)} 買増" if diff_man >= 0 else f"{fmt_man(diff_man, signed=True)} 売却"
                reb_rows.append({
                    '資産クラス': c_name,
                    '現在評価額': fmt_man(c_man),
                    '現在比率': f"{c_pct:.1f} %",
                    '目標比率': f"{t_pct:.0f} %",
                    'リバランス目安': action
                })
            st.table(pd.DataFrame(reb_rows))

    with col_r2:
        st.markdown("#### 📉 3. 暴落ストレステスト")
        st.caption("金融危機で株価が下落した際の総資産額・含み損益・4%ルール安全取崩額を試算します。")

        stress_scenarios = [-10, -20, -30, -40, -50]
        stress_rows = []
        cost_man = current_total_cost / 10000.0
        total_man_exact = current_total_valuation / 10000.0
        for drop in stress_scenarios:
            stressed_man = total_man_exact * (1.0 + drop / 100.0)
            stressed_pnl_man = stressed_man - cost_man
            safe_4pct_yen = (stressed_man * 10000.0 * 0.04) / 12.0
            stress_rows.append({
                '暴落シナリオ': f"株価 {drop}% 下落",
                '暴落後の総資産額': fmt_man(stressed_man),
                '暴落後のトータル損益': fmt_man(stressed_pnl_man, signed=True),
                '4%ルール月間取崩枠': fmt_man_or_sen(safe_4pct_yen)
            })
        st.table(pd.DataFrame(stress_rows))
