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

# --- 「…」による文字切れ防止 & 見やすさ向上のためのカスタムCSS ---
st.markdown("""
<style>
[data-testid="stMetricValue"] {
    white-space: normal !important;
    overflow: visible !important;
    text-overflow: unset !important;
    font-size: 1.7rem !important;
    line-height: 1.25 !important;
}
[data-testid="stMetricLabel"] {
    white-space: normal !important;
    overflow: visible !important;
    text-overflow: unset !important;
    font-size: 0.95rem !important;
}
[data-testid="stMetricDelta"] {
    white-space: normal !important;
    overflow: visible !important;
    text-overflow: unset !important;
}
[data-testid="stMetricValue"] > div,
[data-testid="stMetricLabel"] > div,
[data-testid="stMetricDelta"] > div {
    white-space: normal !important;
    overflow: visible !important;
    text-overflow: unset !important;
}
</style>
""", unsafe_allow_html=True)

st.title("💎 資産管理 & サイドFIRE 統合シミュレーター")

# ==============================================================================
# 1. ユーティリティ & 金額フォーマット関数（「100k」表記を完全廃止）
# ==============================================================================

def clean_num(val):
    """文字列から数値(float)へ安全に変換する（株・口・円・%等の単位や全角文字も処理）"""
    if pd.isna(val):
        return 0.0
    if isinstance(val, (int, float, np.number)):
        return float(val)
    s = unicodedata.normalize('NFKC', str(val))
    for unit in [',', '円', '万円', '千円', '株', '口', 'USD', 'US$', '$', '%', '％', '+', ' ', ' ']:
        s = s.replace(unit, '')
    s = s.strip()
    if s in ['', '-', '--', '---', 'nan', 'None', 'N/A']:
        return 0.0
    m = re.search(r'[-+]?[0-9]+(?:\.[0-9]+)?', s)
    if m:
        try:
            return float(m.group(0))
        except ValueError:
            return 0.0
    return 0.0

def fmt_man_or_sen(yen_val):
    """
    金額（円）を直感的な日本語表記に変換する（「100k」のような表記は一切使わない）
    - 1万円以上: 「〇万円」（小数点なし整数）
    - 1万円未満〜1,000円以上: 「〇.〇千円」
    """
    v = float(yen_val)
    abs_v = abs(v)
    if abs_v == 0:
        return "0 円"
    elif abs_v >= 10000:
        return f"{v / 10000:,.0f} 万円"
    elif abs_v >= 1000:
        return f"{v / 1000:,.1f} 千円"
    else:
        return f"{v:,.0f} 円"

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

def classify_asset_type(name, ticker):
    """銘柄名とティッカーから資産クラスを自動分類"""
    s = unicodedata.normalize('NFKC', str(name))
    t = unicodedata.normalize('NFKC', str(ticker)).strip()
    if t == "FUND_NO_DIV":
        return '投資信託'
    if re.match(r'^[0-9]{3}[0-9A-Z]$', t):
        return '日本株・東証ETF'
    if re.match(r'^[A-Z]{1,5}$', t):
        return '米国株・米国ETF'
    if any(k in s for k in ['eMAXIS', 'オルカン', 'オール・カントリー', 'インデックス', 'ファンド', 'スリム', 'たわら', 'ニッセイ', 'iFree', '投信']) and 'ETF' not in s:
        return '投資信託'
    return '日本株・東証ETF'

def is_fx_sensitive_asset(name, ticker, asset_class):
    """米ドル等の外貨為替レート（円高・円安）の影響を受ける資産かどうかを判定"""
    s = unicodedata.normalize('NFKC', str(name))
    t = unicodedata.normalize('NFKC', str(ticker)).strip()
    if 'ヘッジあり' in s or '為替ヘッジ有' in s:
        return False
    if asset_class == '米国株・米国ETF':
        return True
    if any(k in s for k in ['全世界', 'オール・カントリー', 'オルカン', '全米', '米国', 'S&P', '先進国', '新興国', 'NASDAQ', 'ナスダック', 'FANG', 'ダウ', '外国', 'グローバル']):
        return True
    if t in ['1655', '2558', '2559', '2631', '1545', '1557', '2568', '2569', '2633', '2634', '2244', '2013', '2014']:
        return True
    return False

def get_tax_info(asset_class, account_type, tax_mode="standard"):
    """
    税計算モードに応じて正確な配当手取り率（1 - 税率）と税区分ラベルを返す
    """
    if tax_mode == "pre_tax":
        return 1.0, "税引前(額面100%)"

    is_us = (asset_class == '米国株・米国ETF')
    is_nisa = (account_type == 'NISA')

    if tax_mode == "jp_tax_only":
        if is_nisa:
            return 1.0, "NISA(非課税100%)"
        else:
            return 1.0 - 0.20315, "特定/一般(税20.315%・手取り79.7%)"

    if not is_us and is_nisa:
        return 1.0, "国内NISA(非課税100%)"
    elif not is_us and not is_nisa:
        return 1.0 - 0.20315, "国内特定(税20.315%・手取り79.7%)"
    elif is_us and is_nisa:
        return 0.90, "米国NISA(現地税10%・手取り90.0%)"
    else:
        return 0.90 * (1.0 - 0.20315), "米国特定(現地10%+国内20.3%・手取り71.7%)"

def extract_ticker_from_text(name_str, code_str=""):
    """CSVの銘柄コード列または銘柄名から証券コード・ティッカーを正確に抽出（全角対応）"""
    c = unicodedata.normalize('NFKC', str(code_str)).strip().upper()
    if c.endswith('.0'):
        c = c[:-2]
    if re.match(r'^[0-9]{3}[0-9A-Z]$', c):
        return c
    if re.match(r'^[A-Z]{1,5}$', c) and c not in {'NAN', 'NONE', 'NISA', 'ETF'}:
        return c

    # code_strの中に4桁コードが含まれている場合（例: "8058 三菱商事"）
    m_c_jp = re.search(r'(?<![0-9A-Z])([1-9][0-9]{2}[0-9A-Z])(?![0-9A-Z])', c)
    if m_c_jp:
        return m_c_jp.group(1)

    s = unicodedata.normalize('NFKC', str(name_str)).strip()

    # 1) 4桁の証券コードが含まれていれば最優先で抽出（西暦表記 2024年 や 2025/03 等は除外）
    for m_jp in re.finditer(r'(?<![0-9A-Z/.-])([1-9][0-9]{2}[0-9A-Z])(?![0-9A-Z年/.-])', s.upper()):
        cand_code = m_jp.group(1)
        if cand_code not in {'2024', '2025', '2026', '2027', '2028', '2029', '2030', '1000', '2000', '3000', '5000'}:
            return cand_code

    # 2) カッコ内の米国ティッカー（例: (VYM), (SPYD), (AAPL)）
    ignore_words = {'NISA', 'ETF', 'REIT', 'SPDR', 'MAXIS', 'SLIM', 'INDEX', 'FUND', 'NEXT', 'FUNDS', 'ISHARES', 'VANGUARD', 'CORE', 'HIGH', 'DIV', 'US', 'JP', 'NYSE', 'NASDAQ'}
    m_us_paren = re.findall(r'[\(\[]([A-Z]{1,5})[\)\]]', s.upper())
    for cand in m_us_paren:
        if cand not in ignore_words:
            return cand

    # 3) 無配の一般的なインデックス投資信託の判定（分配型・高配当型は除外しない）
    is_dist_fund = any(k in s for k in ['分配', '高配当', '四半期決算', '年4回', '年2回', '毎月決算', 'Dコース', 'インカム'])
    if not is_dist_fund and 'ETF' not in s.upper():
        if any(k in s for k in ['eMAXIS', 'オール・カントリー', 'オルカン', 'インデックス・ファンド', 'インデックスファンド', 'たわらノーロード', 'ニッセイ', 'iFree', '楽天・全米株式', '楽天・全世界株式', '楽天・プラス', 'SBI・V', 'SBI・iシェアーズ']):
            return "FUND_NO_DIV"

    # 4) 空白区切りの米国ティッカー
    m_us_word = re.findall(r'\b([A-Z]{2,5})\b', s.upper())
    for cand in m_us_word:
        if cand not in ignore_words:
            return cand

    return ""

@st.cache_data(ttl=1800, show_spinner=False)
def fetch_usdjpy_rate():
    """現在のドル円（USD/JPY）為替レートをリアルタイム取得"""
    headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'}
    try:
        url = "https://query1.finance.yahoo.com/v8/finance/chart/JPY=X?range=1d&interval=1d"
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=4) as res:
            data = json.loads(res.read().decode('utf-8'))
        result = data.get('chart', {}).get('result', [])
        if result:
            rate = float(result[0].get('meta', {}).get('regularMarketPrice', 0.0))
            if 50 < rate < 300:
                return round(rate, 2)
    except Exception:
        pass
    return 150.0

@st.cache_data(ttl=3600, show_spinner=False)
def search_jp_ticker_by_name(stock_name):
    """銘柄名しか記載がない場合、Yahoo!ファイナンスまたは株探から証券コードを特定する"""
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
        m = re.search(r'/quote/([0-9]{3}[0-9A-Z])\.T', html)
        if m:
            return m.group(1)
    except Exception:
        pass
    return ""

def default_pay_months(ticker):
    """履歴から入金月が特定できない場合の標準入金月"""
    t = str(ticker).upper()
    if t == "FUND_NO_DIV":
        return "-"
    if t in {'JEPI', 'JEPQ', 'XYLD', 'QYLD', 'RYLD', 'AGG', 'BND', 'LQD', 'TLT', 'PFF', 'ARCC', 'MAIN', 'O'}:
        return "1,2,3,4,5,6,7,8,9,10,11,12"
    if t in {'2914', '4911', '2502', '2503', '4452', '5108', '4519', '3003'}:
        return "3,9"
    if t in {'8267', '3382', '9983', '7532', '8233', '3086'}:
        return "5,11"
    if re.match(r'^[A-Z]{1,5}$', t):
        return "3,6,9,12"
    if re.match(r'^[0-9]{3}[0-9A-Z]$', t):
        return "6,12"
    return "6,12"

def parse_pay_months_str(months_str):
    """'6,12' や '毎月' などの文字列から月番号(1〜12)のリストを返す"""
    s = str(months_str).strip()
    if not s or s in ['-', '無配', 'nan', 'None']:
        return []
    if '毎月' in s or 'すべて' in s:
        return list(range(1, 13))
    nums = re.findall(r'\d+', s)
    valid = sorted(list({int(m) for m in nums if 1 <= int(m) <= 12}))
    return valid

# ==============================================================================
# 2. 【核心】マルチソース 1株配当(DPS) ＆ 最新利回り 厳密調査エンジン
# ==============================================================================

@st.cache_data(ttl=3600, show_spinner=False)
def fetch_exact_dividend_data(ticker, stock_name="", usdjpy=150.0):
    """
    1株あたり年間配当金（円換算DPS）と配当利回り(%)、入金月を3重ソースから厳密に取得する
    - ソース1: Yahoo!ファイナンス日本版（会社予想1株配当・予想利回り）
    - ソース2: 株探 Kabutan（今期予想1株配当・利回り — 米国クラウド環境からも確実・高速）
    - ソース3: Yahoo Finance Chart API（直近配当実績・分割判定・権利確定月）
    戻り値: (dps_yen, yield_pct, source_label, pay_months_str)
    """
    if ticker == "FUND_NO_DIV":
        return 0.0, 0.0, "投信(無配・再投資)", "-"

    if not ticker and stock_name:
        s_norm = unicodedata.normalize('NFKC', str(stock_name))
        is_dist = any(k in s_norm for k in ['分配', '高配当', '四半期決算', '年4回', '年2回', '毎月決算', 'Dコース'])
        if not is_dist and any(k in s_norm for k in ['ファンド', 'インデックス', 'eMAXIS', 'オルカン', '投信']) and 'ETF' not in s_norm.upper():
            return 0.0, 0.0, "投信(無配・再投資)", "-"
        ticker = search_jp_ticker_by_name(stock_name)

    if not ticker:
        return 0.0, 0.0, "未特定(手動入力可)", "6,12"

    headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'}
    is_jp = bool(re.match(r'^[0-9]{3}[0-9A-Z]$', ticker))
    yf_symbol = f"{ticker}.T" if is_jp else ticker

    jp_dps_yen = None
    jp_forecast_yield = None
    jp_source_label = ""

    # --- ソース1: Yahoo!ファイナンス日本版（全出現箇所を走査） ---
    if is_jp:
        try:
            url_jp = f"https://finance.yahoo.co.jp/quote/{ticker}.T"
            req = urllib.request.Request(url_jp, headers=headers)
            with urllib.request.urlopen(req, timeout=5) as res:
                html = res.read().decode('utf-8', errors='ignore')

            # JSON内の dps / dividendYield 抽出
            m_yield_json = re.search(r'"dividendYield"\s*:\s*(?:\{\s*"raw"\s*:\s*)?"?([0-9]+\.[0-9]+)"?', html)
            if m_yield_json:
                yv = float(m_yield_json.group(1))
                if 0 < yv < 25:
                    jp_forecast_yield = round(yv, 2)

            m_dps_json = re.search(r'"dps"\s*:\s*(?:\{\s*"raw"\s*:\s*)?"?([0-9,]+(?:\.[0-9]+)?)"?', html)
            if m_dps_json:
                dv = clean_num(m_dps_json.group(1))
                if 0 < dv < 50000:
                    jp_dps_yen = dv

            # タグ除去テキストから全出現箇所を走査
            clean_text = unicodedata.normalize('NFKC', re.sub(r'<[^>]+>', ' ', html))
            clean_text = re.sub(r'\s+', ' ', clean_text)

            if jp_dps_yen is None:
                for m_d in re.finditer(r'(?:1株配当|分配金)\s*(?:(?:\(会社予想\)|\(税込\)|\(連\)|\(単\))\s*)*([0-9]{1,5}(?:,[0-9]{3})*(?:\.[0-9]{1,2})?)\s*円', clean_text):
                    dv = clean_num(m_d.group(1))
                    if 0 < dv < 50000:
                        jp_dps_yen = dv
                        break

            if jp_forecast_yield is None:
                for m_y in re.finditer(r'(?:配当利回り|分配金利回り)\s*(?:(?:\(会社予想\)|\(税込\))\s*)*([0-9]{1,2}\.[0-9]{1,2})\s*%', clean_text):
                    yv = float(m_y.group(1))
                    if 0 < yv < 25:
                        jp_forecast_yield = round(yv, 2)
                        break

            if jp_dps_yen is not None or jp_forecast_yield is not None:
                jp_source_label = f"Yahoo会社予想({ticker}.T)"
        except Exception:
            pass

    # --- ソース2: 株探 (Kabutan) から今期予想1株配当・利回りを取得（バックアップ＆クロスチェック） ---
    if is_jp and (jp_dps_yen is None or jp_forecast_yield is None):
        try:
            url_kb = f"https://kabutan.jp/stock/?code={ticker}"
            req_kb = urllib.request.Request(url_kb, headers=headers)
            with urllib.request.urlopen(req_kb, timeout=5) as res_kb:
                html_kb = res_kb.read().decode('utf-8', errors='ignore')

            text_kb = unicodedata.normalize('NFKC', re.sub(r'<[^>]+>', ' ', html_kb))
            text_kb = re.sub(r'\s+', ' ', text_kb)

            # 株探の上部指標テーブル「PER PBR 利回り 信用倍率」から利回りを抽出
            if jp_forecast_yield is None:
                m_kb_y = re.search(r'PER\s+PBR\s+利回り\s+信用倍率\s+(?:[0-9.]+|---)\s*倍\s+(?:[0-9.]+|---)\s*倍\s+([0-9]{1,2}\.[0-9]{1,2})\s*%', text_kb)
                if m_kb_y:
                    yv = float(m_kb_y.group(1))
                    if 0 < yv < 25:
                        jp_forecast_yield = round(yv, 2)
                        if not jp_source_label:
                            jp_source_label = f"株探予想({ticker})"

            # 株探の業績テーブルから「予 ... 1株配」を抽出
            if jp_dps_yen is None:
                # 「予 2026.03 ...」の行の末尾付近にある1株配を抽出
                for m_row in re.finditer(r'予\s*202[5-8]\.[0-1][0-9]\s+[0-9,.\-]+\s+[0-9,.\-]+\s+[0-9,.\-]+\s+[0-9,.\-]+\s+[0-9,.\-]+\s+([0-9]{1,5}(?:\.[0-9]{1,2})?)', text_kb):
                    dv = clean_num(m_row.group(1))
                    if 0 < dv < 50000:
                        jp_dps_yen = dv
                        if not jp_source_label:
                            jp_source_label = f"株探予想1株配({ticker})"
                        break
        except Exception:
            pass

    # --- ソース3: Yahoo Finance Chart API (現在株価・米国株DPS・入金月・分割判定) ---
    api_dps_native = None
    api_price_native = None
    api_yield = None
    pay_months_str = default_pay_months(ticker)

    try:
        url_api = f"https://query1.finance.yahoo.com/v8/finance/chart/{urllib.parse.quote(yf_symbol)}?range=1y&interval=1d&events=div,split"
        req = urllib.request.Request(url_api, headers=headers)
        with urllib.request.urlopen(req, timeout=5) as res:
            data = json.loads(res.read().decode('utf-8'))

        result = data.get('chart', {}).get('result', [])
        if result:
            meta = result[0].get('meta', {})
            api_price_native = float(meta.get('regularMarketPrice', 0.0))
            events = result[0].get('events', {})
            divs = events.get('dividends', {})
            splits = events.get('splits', {})

            if divs:
                div_items = sorted(divs.values(), key=lambda x: int(x.get('date', 0)))

                # 前年同月の重複配当（例: 年2回配当で3回分、年4回配当で5回分入っている場合）を自動除外
                if len(div_items) in (3, 5, 13):
                    first_m = datetime.fromtimestamp(int(div_items[0].get('date', 0))).month
                    last_m = datetime.fromtimestamp(int(div_items[-1].get('date', 0))).month
                    span_days = (int(div_items[-1].get('date', 0)) - int(div_items[0].get('date', 0))) / 86400.0
                    if first_m == last_m or span_days > 345:
                        div_items = div_items[1:]

                # 株式分割があった場合、分割前の配当データが未調整かどうかを前後の配当比率から賢く判定して補正
                split_events = []
                for _, sp in splits.items():
                    num = float(sp.get('numerator', 1.0))
                    den = float(sp.get('denominator', 1.0))
                    if den > 0 and num != den:
                        split_events.append((int(sp.get('date', 0)), num / den))

                ex_months = set()
                total_div = 0.0

                for d_obj in div_items:
                    ts = int(d_obj.get('date', 0))
                    d_amt = float(d_obj.get('amount', 0.0))

                    for sp_date, ratio in split_events:
                        if ts < sp_date and ratio > 1.1:
                            # 分割後の配当がある場合は比較し、未調整（分割前の額がそのまま残っている）なら割る
                            post_divs = [float(x.get('amount', 0.0)) for x in div_items if int(x.get('date', 0)) >= sp_date]
                            if post_divs and post_divs[-1] > 0:
                                if (d_amt / post_divs[-1]) > (ratio * 0.65):
                                    d_amt = d_amt / ratio
                            elif api_price_native and api_price_native > 0:
                                # 分割後の配当がまだない場合、未調整だと利回りが異常値になるかで判定
                                if (d_amt * 2.0 / api_price_native * 100.0) > 9.0:
                                    d_amt = d_amt / ratio

                    total_div += d_amt
                    if ts > 0:
                        m_num = datetime.fromtimestamp(ts).month
                        if is_jp:
                            pay_m = ((m_num + 3 - 1) % 12) + 1
                            ex_months.add(pay_m)
                        else:
                            ex_months.add(m_num)

                if ex_months:
                    pay_months_str = ",".join(str(m) for m in sorted(list(ex_months)))

                if total_div > 0:
                    api_dps_native = total_div
                    if api_price_native and api_price_native > 0:
                        calc_yield = (total_div / api_price_native) * 100.0
                        if 0 < calc_yield < 30:
                            api_yield = round(calc_yield, 2)
            elif api_price_native and api_price_native > 0 and not divs:
                api_dps_native = 0.0
                api_yield = 0.0
    except Exception:
        pass

    # --- 統合決定 ---
    if is_jp:
        if jp_dps_yen is not None and jp_dps_yen > 0:
            y_out = jp_forecast_yield if (jp_forecast_yield is not None and jp_forecast_yield > 0) else (
                round(jp_dps_yen / api_price_native * 100.0, 2) if (api_price_native and api_price_native > 0) else 0.0
            )
            return round(jp_dps_yen, 2), y_out, jp_source_label, pay_months_str

        if jp_forecast_yield is not None and jp_forecast_yield > 0:
            dps_calc = round(api_price_native * (jp_forecast_yield / 100.0), 2) if (api_price_native and api_price_native > 0) else 0.0
            return dps_calc, jp_forecast_yield, jp_source_label, pay_months_str

        if api_dps_native is not None and api_dps_native > 0:
            return round(api_dps_native, 2), (api_yield or 0.0), f"直近実績({ticker}.T)", pay_months_str

        if jp_forecast_yield == 0.0 or api_dps_native == 0.0:
            return 0.0, 0.0, f"無配({ticker}.T)", "-"
    else:
        if api_dps_native is not None and api_dps_native > 0:
            dps_yen_us = round(api_dps_native * usdjpy, 2)
            return dps_yen_us, (api_yield or 0.0), f"米国実績({yf_symbol}: ${api_dps_native:.2f}/株)", pay_months_str
        elif api_dps_native == 0.0:
            return 0.0, 0.0, f"無配({yf_symbol})", "-"

    return 0.0, 0.0, "未取得(手動入力可)", pay_months_str

def enrich_with_latest_dividends(df, usdjpy=150.0):
    """DataFrameの全銘柄に対して並列で『1株配当(円)』『最新利回り(%)』『入金月』を自動付与する"""
    if df.empty:
        return df

    df = df.copy()
    for col, default_v in [('銘柄コード', ''), ('保有数量', 0.0), ('1株配当(円)', 0.0), ('配当利回り(%)', 0.0), ('配当入金月', ''), ('配当ソース', '')]:
        if col not in df.columns:
            df[col] = default_v

    tickers = []
    for _, row in df.iterrows():
        t = extract_ticker_from_text(row.get('銘柄名', ''), row.get('銘柄コード', ''))
        tickers.append(t)
    df['銘柄コード'] = tickers

    tasks = []
    for idx, row in df.iterrows():
        cur_y = clean_num(row.get('配当利回り(%)', 0.0))
        cur_dps = clean_num(row.get('1株配当(円)', 0.0))
        cur_src = str(row.get('配当ソース', ''))
        cur_pm = str(row.get('配当入金月', ''))
        if (cur_y <= 0 and cur_dps <= 0 and cur_src == "") or cur_pm == "":
            tasks.append((idx, row['銘柄コード'], row['銘柄名'], cur_dps, cur_y, cur_src, cur_pm))

    if tasks:
        def _worker(task):
            idx, t_code, s_name, cur_dps, cur_y, cur_src, cur_pm = task
            dps_val, y_val, src_label, pay_m = fetch_exact_dividend_data(t_code, s_name, usdjpy)
            final_dps = cur_dps if (cur_dps > 0 and cur_src != "") else dps_val
            final_y = cur_y if (cur_y > 0 and cur_src != "") else y_val
            final_src = cur_src if ((cur_y > 0 or cur_dps > 0) and cur_src != "") else src_label
            final_pm = cur_pm if cur_pm not in ["", "nan", "None"] else pay_m
            return idx, final_dps, final_y, final_src, final_pm

        with ThreadPoolExecutor(max_workers=8) as executor:
            results = list(executor.map(_worker, tasks))

        for idx, final_dps, final_y, final_src, final_pm in results:
            df.at[idx, '1株配当(円)'] = final_dps
            df.at[idx, '配当利回り(%)'] = final_y
            df.at[idx, '配当ソース'] = final_src
            df.at[idx, '配当入金月'] = final_pm

    df['資産クラス'] = [
        classify_asset_type(r['銘柄名'], r['銘柄コード'])
        for _, r in df.iterrows()
    ]
    df['為替影響'] = [
        "米ドル連動(為替影響あり)" if is_fx_sensitive_asset(r['銘柄名'], r['銘柄コード'], r['資産クラス']) else "円建て(為替影響なし)"
        for _, r in df.iterrows()
    ]
    return df

def calc_exact_annual_dividend(df, tax_mode="standard", calc_formula="dps_priority"):
    """
    配当金の厳密計算関数
    - calc_formula == "dps_priority": 個別株・ETFで「保有数量 > 0」かつ「1株配当(円) > 0」なら『保有数量 × 1株配当(円)』で1円単位まで正確に計算
    - calc_formula == "yield_priority": 『評価額 × 配当利回り(%)』で計算
    """
    if df.empty:
        return df

    df = df.copy()
    pre_tax_list = []
    calc_method_list = []
    eff_yield_list = []

    for _, row in df.iterrows():
        asset_cls = str(row.get('資産クラス', ''))
        qty = clean_num(row.get('保有数量', 0.0))
        dps = clean_num(row.get('1株配当(円)', 0.0))
        val = clean_num(row.get('評価額', 0.0))
        y_pct = clean_num(row.get('配当利回り(%)', 0.0))

        if calc_formula == "dps_priority" and asset_cls != '投資信託' and qty > 0 and dps > 0:
            ann_pre = round(qty * dps)
            method_str = f"株数×1株配当 ({qty:,.0f}株×{dps:,.1f}円)"
            eff_y = (ann_pre / val * 100.0) if val > 0 else y_pct
        elif val > 0 and y_pct > 0:
            ann_pre = round(val * (y_pct / 100.0))
            method_str = f"評価額×利回り ({y_pct:.2f}%)"
            eff_y = y_pct
        elif asset_cls != '投資信託' and qty > 0 and dps > 0:
            ann_pre = round(qty * dps)
            method_str = f"株数×1株配当 ({qty:,.0f}株×{dps:,.1f}円)"
            eff_y = (ann_pre / val * 100.0) if val > 0 else y_pct
        else:
            ann_pre = 0.0
            method_str = "無配 / 0円"
            eff_y = 0.0

        pre_tax_list.append(float(ann_pre))
        calc_method_list.append(method_str)
        eff_yield_list.append(round(eff_y, 2))

    df['年間配当金(税引前)'] = pre_tax_list
    df['計算根拠'] = calc_method_list
    df['実質配当利回り(%)'] = eff_yield_list

    tax_results = [get_tax_info(r['資産クラス'], r['口座区分'], tax_mode) for _, r in df.iterrows()]
    df['手取り率'] = [t[0] for t in tax_results]
    df['税区分'] = [t[1] for t in tax_results]
    df['年間配当金(税引後)'] = [round(pre * rate) for pre, rate in zip(df['年間配当金(税引前)'], df['手取り率'])]
    return df

# ==============================================================================
# 3. 証券会社CSVの自動判別 & マルチブロック高精度解析エンジン
# ==============================================================================

def is_csv_header_line(line):
    """行がCSVのヘッダー（見出し）行かどうかを厳密に判定（データ行の誤判定を防止）"""
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

    # 1) このアプリで保存したCSVかどうかの判定
    if '証券会社' in first_lines and '配当利回り(%)' in first_lines and '評価額' in first_lines:
        try:
            df_saved = pd.read_csv(io.StringIO(text_data))
            if '配当ソース' not in df_saved.columns:
                df_saved['配当ソース'] = '保存データ'
            if '銘柄コード' not in df_saved.columns:
                df_saved['銘柄コード'] = [extract_ticker_from_text(n, '') for n in df_saved['銘柄名']]
            if '保有数量' not in df_saved.columns:
                df_saved['保有数量'] = 0.0
            if '1株配当(円)' not in df_saved.columns:
                df_saved['1株配当(円)'] = 0.0
            if '配当入金月' not in df_saved.columns:
                df_saved['配当入金月'] = [default_pay_months(c) for c in df_saved['銘柄コード']]
            return df_saved
        except Exception:
            pass

    # 2) 楽天証券 vs SBI証券 の自動判別
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
    i = 0
    n = len(lines)

    while i < n:
        line = lines[i].strip()
        if not line:
            i += 1
            continue

        # セクション見出し行から口座区分を検出（例: 「株式（NISA預り（成長投資枠））」）
        acc_detect = normalize_account(line)
        if acc_detect and line.count(',') <= 3 and not is_csv_header_line(line):
            current_account = acc_detect
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
                if next_line.count(',') <= 2 and any(k in next_line for k in ['合計', 'トータル', '預り', '口座', '一覧', '■', '◆']):
                    break
                block_lines.append(next_line)
                i += 1

            try:
                df_block = pd.read_csv(io.StringIO("\n".join(block_lines)), on_bad_lines='skip')

                col_code = next((c for c in df_block.columns if any(k in str(c) for k in ['銘柄コード', 'ティッカー', 'コード']) and '名' not in str(c) and '銘柄（コード）' not in str(c) and '銘柄(コード)' not in str(c)), None)
                col_name = next((c for c in df_block.columns if any(k in str(c) for k in ['銘柄・ファンド名', '銘柄名称', '銘柄名', 'ファンド名', '銘柄（コード）', '銘柄(コード)', 'ファンド', '銘柄', '商品名']) and c != col_code), None)
                if not col_name:
                    col_name = next((c for c in df_block.columns if any(k in str(c) for k in ['銘柄', 'ファンド', '商品'])), None)

                col_qty = next((c for c in df_block.columns if any(k in str(c) for k in ['保有数量', '保有株数', '保有口数', '数量', '株数', '口数']) and '注文' not in str(c)), None)
                col_val = next((c for c in df_block.columns if any(k in str(c) for k in ['時価評価額[円]', '評価額[円]', '評価金額[円]', '時価評価額', '評価金額', '評価額']) and '外貨' not in str(c) and 'US' not in str(c) and '損益' not in str(c)), None)
                # SBI証券の「損益」列にも完全対応
                col_pnl = next((c for c in df_block.columns if any(k in str(c) for k in ['評価損益[円]', '評価損益額', '評価損益', 'トータルリターン[円]', 'トータルリターン', '含み損益', '損益']) and '外貨' not in str(c) and 'US' not in str(c) and '率' not in str(c) and '％' not in str(c) and '%' not in str(c)), None)
                col_cost = next((c for c in df_block.columns if any(k in str(c) for k in ['取得総額[円]', '取得総額', '取得金額', '買付金額']) and '外貨' not in str(c)), None)
                col_unit_cost = next((c for c in df_block.columns if any(k in str(c) for k in ['取得単価', '平均取得価額']) and '外貨' not in str(c)), None)
                col_acc = next((c for c in df_block.columns if any(k in str(c) for k in ['口座区分', '口座', '預り区分'])), None)
                col_div = next((c for c in df_block.columns if any(k in str(c) for k in ['配当利回り', '分配金利回り', '利回り'])), None)

                if col_name and col_val:
                    for _, row in df_block.iterrows():
                        name_str = str(row[col_name]).strip()
                        if not name_str or name_str in ['nan', 'None', '-', '---', '合計', '総合計'] or name_str.startswith('合計'):
                            continue

                        val_num = clean_num(row[col_val])
                        if val_num <= 0:
                            continue

                        qty_num = clean_num(row[col_qty]) if col_qty else 0.0
                        has_pnl = (col_pnl is not None) and (str(row[col_pnl]).strip() not in ['', '-', '--', '---', 'nan', 'None'])
                        pnl_num = clean_num(row[col_pnl]) if has_pnl else 0.0

                        if col_cost and clean_num(row[col_cost]) > 0:
                            cost_num = clean_num(row[col_cost])
                        elif has_pnl:
                            cost_num = max(val_num - pnl_num, 0.0)
                        elif col_unit_cost and qty_num > 0 and clean_num(row[col_unit_cost]) > 0:
                            u_cost = clean_num(row[col_unit_cost])
                            # 投資信託（1万口あたり単価）か個別株かを判定
                            if qty_num >= 10000 and (qty_num * u_cost) > val_num * 500:
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

                        code_str = str(row[col_code]).strip() if col_code and not pd.isna(row[col_code]) else ""
                        div_yield = clean_num(row[col_div]) if col_div else 0.0
                        ext_code = extract_ticker_from_text(name_str, code_str)

                        records.append({
                            '証券会社': broker_name,
                            '口座区分': row_acc,
                            '銘柄コード': ext_code,
                            '銘柄名': name_str,
                            '保有数量': qty_num,
                            '評価額': val_num,
                            '取得額': cost_num,
                            '評価損益': val_num - cost_num,
                            '1株配当(円)': 0.0,
                            '配当利回り(%)': div_yield,
                            '配当入金月': '',
                            '配当ソース': 'CSV記載' if div_yield > 0 else ''
                        })
            except Exception:
                pass
        else:
            i += 1

    return pd.DataFrame(records)

# ==============================================================================
# 4. サイドバー：一括ドラッグ＆ドロップ & 配当計算・税計算ルール設定
# ==============================================================================

current_usdjpy = fetch_usdjpy_rate()

with st.sidebar:
    st.header("📂 1. CSV一括ドラッグ＆ドロップ")
    st.caption("楽天証券・SBI証券・保存済みCSVを区別せず、ここにまとめて一気にドロップできます（自動判別）")
    uploaded_files = st.file_uploader(
        "CSVファイルをまとめてドロップ（複数選択可）",
        type=['csv'],
        accept_multiple_files=True
    )

    st.markdown("---")
    st.header("🧮 2. 配当金の計算・税金ルール")
    calc_formula_label = st.radio(
        "配当額の算出方法",
        options=[
            "① 保有株数 × 1株配当(円) で厳密計算（推奨）",
            "② 評価額 × 配当利回り(%) で計算"
        ],
        index=0,
        help="個別株・ETFについて、①は「株数×1株あたり年間配当金」で1円単位まで正確に計算します。ご自身のExcel表が「評価額×利回り(%)」で計算されている場合は②を選ぶと一致します。"
    )
    selected_calc_formula = "dps_priority" if "①" in calc_formula_label else "yield_priority"

    tax_mode_label = st.radio(
        "税金の差し引きルール",
        options=[
            "① 日米税制完全準拠（米国株の現地課税10%も考慮）",
            "② 国内税のみ考慮（NISA 100% / 特定 79.685%）",
            "③ すべて税引前（額面 100%）で統一"
        ],
        index=0,
        help="お手元の管理表の税金計算ルールに合わせて切り替えられます。"
    )
    if "①" in tax_mode_label:
        selected_tax_mode = "standard"
    elif "②" in tax_mode_label:
        selected_tax_mode = "jp_tax_only"
    else:
        selected_tax_mode = "pre_tax"

    st.markdown("---")
    st.header("🌐 3. リアルタイム市場データ")
    st.metric("現在のドル円レート", f"{current_usdjpy:.1f} 円/$")
    if st.button("🔄 最新配当＆為替データを再取得", use_container_width=True):
        st.cache_data.clear()
        if "portfolio_df" in st.session_state:
            del st.session_state["portfolio_df"]
        st.rerun()

    st.markdown("---")
    use_demo = st.checkbox("🧪 デモデータで試す（CSVなしで確認）", value=False)

# --- データの読み込みと最新1株配当(DPS)・利回りの自動付与 ---
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
            {'証券会社': 'SBI証券', '口座区分': 'NISA', '銘柄コード': 'FUND_NO_DIV', '銘柄名': 'eMAXIS Slim 全世界株式(オール・カントリー)', '保有数量': 1800000, '評価額': 4500000, '取得額': 3200000, '評価損益': 1300000, '1株配当(円)': 0.0, '配当利回り(%)': 0.0, '配当入金月': '-', '配当ソース': ''},
            {'証券会社': 'SBI証券', '口座区分': 'NISA', '銘柄コード': 'VYM', '銘柄名': 'バンガード・米国高配当株式ETF (VYM)', '保有数量': 150, '評価額': 2800000, '取得額': 2100000, '評価損益': 700000, '1株配当(円)': 0.0, '配当利回り(%)': 0.0, '配当入金月': '', '配当ソース': ''},
            {'証券会社': 'SBI証券', '口座区分': '特定', '銘柄コード': '8058', '銘柄名': '三菱商事 (8058)', '保有数量': 600, '評価額': 1800000, '取得額': 1200000, '評価損益': 600000, '1株配当(円)': 0.0, '配当利回り(%)': 0.0, '配当入金月': '', '配当ソース': ''},
            {'証券会社': 'SBI証券', '口座区分': '特定', '銘柄コード': '8316', '銘柄名': '三井住友フィナンシャルG (8316)', '保有数量': 400, '評価額': 1500000, '取得額': 950000, '評価損益': 550000, '1株配当(円)': 0.0, '配当利回り(%)': 0.0, '配当入金月': '', '配当ソース': ''},
            {'証券会社': '楽天証券', '口座区分': 'NISA', '銘柄コード': 'FUND_NO_DIV', '銘柄名': '楽天・全米株式インデックス・ファンド', '保有数量': 1200000, '評価額': 3200000, '取得額': 2400000, '評価損益': 800000, '1株配当(円)': 0.0, '配当利回り(%)': 0.0, '配当入金月': '-', '配当ソース': ''},
            {'証券会社': '楽天証券', '口座区分': '特定', '銘柄コード': 'SPYD', '銘柄名': 'SPDR ポートフォリオS&P 500高配当株式ETF (SPYD)', '保有数量': 340, '評価額': 2200000, '取得額': 1900000, '評価損益': 300000, '1株配当(円)': 0.0, '配当利回り(%)': 0.0, '配当入金月': '', '配当ソース': ''},
            {'証券会社': '楽天証券', '口座区分': '一般', '銘柄コード': '2914', '銘柄名': '日本たばこ産業 (2914)', '保有数量': 300, '評価額': 1200000, '取得額': 900000, '評価損益': 300000, '1株配当(円)': 0.0, '配当利回り(%)': 0.0, '配当入金月': '', '配当ソース': ''},
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

        with st.spinner("🔍 各銘柄の「1株あたり予想配当金(DPS)」「最新利回り」「入金月」を厳密調査中..."):
            enriched_master = enrich_with_latest_dividends(raw_master, current_usdjpy)
        st.session_state["portfolio_df"] = enriched_master
    else:
        st.session_state["portfolio_df"] = pd.DataFrame(
            columns=['証券会社', '口座区分', '資産クラス', '為替影響', '銘柄コード', '銘柄名', '保有数量', '評価額', '取得額', '評価損益', '1株配当(円)', '配当利回り(%)', '配当入金月', '配当ソース']
        )
    st.session_state["last_file_sig"] = file_sig

df_master = calc_exact_annual_dividend(st.session_state["portfolio_df"], selected_tax_mode, selected_calc_formula)

# 現在の総資産額（円 & 万円[整数]）
current_total_valuation = float(df_master['評価額'].sum()) if not df_master.empty else 10000000.0
current_total_cost = float(df_master['取得額'].sum()) if not df_master.empty else 8000000.0
current_total_man = int(round(current_total_valuation / 10000.0))
current_annual_div_post = float(df_master['年間配当金(税引後)'].sum()) if not df_master.empty else 0.0

# --- タブ構成 ---
tab1, tab2, tab3, tab4, tab5 = st.tabs([
    "📊 1. 総合ダッシュボード & 新NISA枠",
    "🎁 2. 配当厳密検算 & 入金月カレンダー",
    "📈 3. 積立シミュレーション(万円)",
    "🏖️ 4. サイドFIRE診断 & 5段階ロードマップ",
    "💱 5. 為替(ドル円) & リバランス・暴落分析"
])

# ==============================================================================
# TAB 1: 総合ダッシュボード & 【採用機能1】新NISA 1,800万円枠トラッカー
# ==============================================================================
with tab1:
    if df_master.empty:
        st.info("👈 左のサイドバーに楽天証券・SBI証券のCSVをまとめてドラッグ＆ドロップするか、「🧪 デモデータで試す」にチェックを入れてください。")
    else:
        total_pnl = current_total_valuation - current_total_cost
        pnl_ratio = (total_pnl / current_total_cost * 100.0) if current_total_cost > 0 else 0.0
        total_div_pre = float(df_master['年間配当金(税引前)'].sum())
        total_div_after = float(df_master['年間配当金(税引後)'].sum())
        total_yield_after = (total_div_after / current_total_valuation * 100.0) if current_total_valuation > 0 else 0.0

        c1, c2, c3, c4 = st.columns(4)
        c1.metric("総資産額（現時点評価額）", f"{current_total_valuation / 10000:,.0f} 万円", f"{current_total_valuation:,.0f} 円")
        c2.metric("投資元本（取得額合計）", f"{current_total_cost / 10000:,.0f} 万円", f"{current_total_cost:,.0f} 円")
        c3.metric("トータル含み損益", f"{total_pnl / 10000:+,.0f} 万円", f"{pnl_ratio:+.2f} % ({total_pnl:+,.0f} 円)")
        c4.metric(
            "年間手取り配当金（厳密算出）",
            fmt_man_or_sen(total_div_after),
            f"実額 {total_div_after:,.0f} 円 / 税引前 {total_div_pre:,.0f} 円"
        )

        # --- 【採用機能1】新NISA 生涯非課税枠（1,800万円）消化 ＆ 節税メリット診断トラッカー ---
        nisa_df = df_master[df_master['口座区分'] == 'NISA']
        nisa_cost_yen = float(nisa_df['取得額'].sum())
        nisa_val_yen = float(nisa_df['評価額'].sum())
        nisa_pnl_yen = max(nisa_val_yen - nisa_cost_yen, 0.0)
        nisa_div_yen = float(nisa_df['年間配当金(税引後)'].sum())
        # もし特定口座だったら課税されていたはずの税金（譲渡益税20.315% ＋ 配当国内税20.315%）
        nisa_saved_tax_gain = nisa_pnl_yen * 0.20315
        nisa_saved_tax_div = nisa_div_yen * 0.20315

        nisa_limit_yen = 18000000.0
        nisa_rem_yen = max(nisa_limit_yen - nisa_cost_yen, 0.0)
        nisa_prog = min(nisa_cost_yen / nisa_limit_yen, 1.0)

        with st.container(border=True):
            st.markdown("##### 🛡️ 新NISA 生涯非課税保有限度額（1,800万円）消化 ＆ 非課税メリット診断（取得額ベース）")
            nc1, nc2, nc3, nc4 = st.columns(4)
            nc1.metric("NISA 投資元本（取得額）", f"{nisa_cost_yen / 10000:,.0f} 万円", f"枠消化率 {nisa_prog*100:.1f} % ({nisa_cost_yen:,.0f} 円)")
            nc2.metric("NISA 評価額 & 非課税含み益", f"{nisa_val_yen / 10000:,.0f} 万円", f"含み益 +{nisa_pnl_yen/10000:,.0f} 万円")
            nc3.metric("NISAによる節税メリット額", fmt_man_or_sen(nisa_saved_tax_gain), f"含み益非課税 {nisa_saved_tax_gain:,.0f}円 + 年間配当節税 {nisa_saved_tax_div:,.0f}円/年")
            nc4.metric("1,800万円枠の残り投資可能額", f"{nisa_rem_yen / 10000:,.0f} 万円", f"月10万積立であと {nisa_rem_yen / 1200000:.1f} 年" if nisa_rem_yen > 0 else "1,800万円満額達成🎉")
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
            fig_bar = px.bar(
                grp_bar,
                x='証券会社', y='評価額(万円)', color='口座区分', barmode='group',
                title="証券会社・口座別の資産残高（万円）"
            )
            fig_bar.update_traces(texttemplate='%{y:,.0f}万円', textposition='outside')
            fig_bar.update_layout(
                yaxis_title="評価額（万円）",
                yaxis=dict(tickformat=",d", ticksuffix="万円")
            )
            st.plotly_chart(fig_bar, use_container_width=True)

        st.subheader("📋 全保有銘柄一覧（保有株数 × 1株配当 厳密計算反映済）")
        st.caption("※「計算根拠」列に、各銘柄の配当金が『株数×1株配当』または『評価額×利回り』のどちらで算出されたかを明記しています。")
        disp_cols_t1 = [
            '証券会社', '口座区分', '資産クラス', '銘柄コード', '銘柄名',
            '保有数量', '評価額', '取得額', '評価損益',
            '1株配当(円)', '実質配当利回り(%)', '年間配当金(税引前)', '年間配当金(税引後)', '計算根拠', '税区分'
        ]
        st.dataframe(
            df_master[disp_cols_t1].sort_values('評価額', ascending=False).style.format({
                '保有数量': '{:,.0f}',
                '評価額': '{:,.0f} 円',
                '取得額': '{:,.0f} 円',
                '評価損益': '{:+,.0f} 円',
                '1株配当(円)': '{:,.2f} 円',
                '実質配当利回り(%)': '{:.2f} %',
                '年間配当金(税引前)': '{:,.0f} 円',
                '年間配当金(税引後)': '{:,.0f} 円'
            }),
            use_container_width=True,
            hide_index=True
        )

# ==============================================================================
# TAB 2: 配当データ厳密検算 & 入金月カレンダー（1月〜12月）
# ==============================================================================
with tab2:
    st.subheader("🎁 条件別 ポートフォリオ＆配当データの厳密検算 ＋ 入金月カレンダー")

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

        # --- 検算・手動微調整エディタ（保有株数・1株配当(円)・利回りを直接編集可能） ---
        with st.expander("🛠️ 【配当金の検算・微調整パネル】保有株数・1株配当(円)・利回り(%)を確認・修正する（クリックで開閉）", expanded=True):
            st.info("""
            💡 **あなたの計算と1円単位で一致させるための検算ポイント**：
            1. **「保有数量（株数）」と「1株配当(円)」**：個別株・ETFは **`保有数量 × 1株配当(円)`** で年間配当金（税引前）を算出しています。もし特別配当や記念配当でお手元の「1株配当」と異なる場合は、下の表の **`1株配当(円換算) ✏️`** を直接書き換えると即座に再計算されます。
            2. **計算式・税金モードの切り替え**：左サイドバーの「🧮 2. 配当金の計算・税金ルール」で、『株数×1株配当』と『評価額×利回り』、および米国現地課税の有無をワンクリックで切り替えられます。
            """)
            edit_cols = ['証券会社', '口座区分', '銘柄コード', '銘柄名', '保有数量', '評価額', '取得額', '1株配当(円)', '配当利回り(%)', '配当入金月', '配当ソース']
            edited_df = st.data_editor(
                st.session_state["portfolio_df"][edit_cols],
                column_config={
                    "保有数量": st.column_config.NumberColumn("保有数量(株/口) ✏️", min_value=0.0, step=1.0, format="%d"),
                    "1株配当(円)": st.column_config.NumberColumn("1株配当(円換算) ✏️", help="1株あたりの年間配当金(円)。『保有数量×1株配当』で厳密計算されます", min_value=0.0, step=1.0, format="%.2f 円"),
                    "配当利回り(%)": st.column_config.NumberColumn("配当利回り(%) ✏️", help="1株配当が0円の銘柄や『評価額×利回り』モード時に使用されます", min_value=0.0, max_value=50.0, step=0.05, format="%.2f %%"),
                    "配当入金月": st.column_config.TextColumn("配当入金月 ✏️", help="カンマ区切りで入金月を入力（例: 6,12 や 3,6,9,12 や 毎月）"),
                    "評価額": st.column_config.NumberColumn("現時点評価額(円)", format="¥%d"),
                    "取得額": st.column_config.NumberColumn("取得額(円)", format="¥%d"),
                },
                disabled=['証券会社', '口座区分', '銘柄名', '配当ソース'],
                use_container_width=True,
                hide_index=True,
                key="dividend_editor"
            )

            orig_df = st.session_state["portfolio_df"]
            if (not edited_df['保有数量'].equals(orig_df['保有数量'])) or \
               (not edited_df['1株配当(円)'].equals(orig_df['1株配当(円)'])) or \
               (not edited_df['配当利回り(%)'].equals(orig_df['配当利回り(%)'])) or \
               (not edited_df['配当入金月'].equals(orig_df['配当入金月'])) or \
               (not edited_df['銘柄コード'].equals(orig_df['銘柄コード'])):
                for c_up in ['保有数量', '1株配当(円)', '配当利回り(%)', '配当入金月', '銘柄コード']:
                    st.session_state["portfolio_df"][c_up] = edited_df[c_up]
                st.rerun()

            csv_save = st.session_state["portfolio_df"].to_csv(index=False).encode('utf-8-sig')
            st.download_button(
                label="💾 現在のポートフォリオ＆1株配当データをCSVで保存（次回からそのままドロップ可能）",
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
            monthly_div_post = sum_div_post / 12.0
            portfolio_yield_pre = (sum_div_pre / sum_target_asset * 100.0) if sum_target_asset > 0 else 0.0
            portfolio_yield_post = (sum_div_post / sum_target_asset * 100.0) if sum_target_asset > 0 else 0.0

            st.markdown(f"#### 📊 選択条件での分析結果（基準：{asset_basis}）")
            k1, k2, k3, k4 = st.columns(4)
            k1.metric(f"対象総資産額 ({basis_col})", f"{sum_target_asset / 10000:,.0f} 万円", f"{sum_target_asset:,.0f} 円")
            k2.metric("年間配当金（税引前・額面）", fmt_man_or_sen(sum_div_pre), f"実額 {sum_div_pre:,.0f} 円 (利回り {portfolio_yield_pre:.2f}%)")
            k3.metric("年間配当金（税引後・手取り）", fmt_man_or_sen(sum_div_post), f"実額 {sum_div_post:,.0f} 円 (実質利回り {portfolio_yield_post:.2f}%)")
            k4.metric("月平均の手取り配当金", fmt_man_or_sen(monthly_div_post), f"実額 {monthly_div_post:,.0f} 円/月")

            with st.container(border=True):
                st.markdown("##### 🏆 月間手取り配当金 マイルストーン達成状況")
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

            # --- 月別 配当金入金カレンダー（「100k」表記を完全廃止し、万円または千円で明瞭表示） ---
            st.markdown("---")
            st.markdown("#### 🗓️ 月別 配当金入金カレンダー（1月〜12月・手取りベース）")
            cal_unit = st.radio("カレンダーグラフの表示単位", ["万円（例：3.5万円）", "千円（例：35.0千円）", "円（例：35,000円）"], index=0, horizontal=True)

            cal_records = []
            matrix_rows = []
            for _, row in df_filtered.iterrows():
                ann_post = float(row['年間配当金(税引後)'])
                if ann_post <= 0:
                    continue
                p_months = parse_pay_months_str(row.get('配当入金月', ''))
                if not p_months:
                    p_months = [6, 12]
                per_payment = ann_post / len(p_months)

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
                            '手取り配当額(円)': round(amt),
                            '手取り配当額(千円)': round(amt / 1000.0, 1),
                            '手取り配当額(万円)': round(amt / 10000.0, 2)
                        })
                m_dict['年間合計'] = round(ann_post)
                matrix_rows.append(m_dict)

            if cal_records:
                df_cal = pd.DataFrame(cal_records)
                month_order = [f"{m}月" for m in range(1, 13)]

                if "万円" in cal_unit:
                    y_col_cal = '手取り配当額(万円)'
                    y_suffix = "万円"
                    txt_tmpl = '%{y:,.1f}万円'
                    y_fmt = ",.1f"
                elif "千円" in cal_unit:
                    y_col_cal = '手取り配当額(千円)'
                    y_suffix = "千円"
                    txt_tmpl = '%{y:,.1f}千円'
                    y_fmt = ",.1f"
                else:
                    y_col_cal = '手取り配当額(円)'
                    y_suffix = "円"
                    txt_tmpl = '%{y:,.0f}円'
                    y_fmt = ",d"

                fig_cal = px.bar(
                    df_cal,
                    x='月', y=y_col_cal, color='銘柄名',
                    category_orders={'月': month_order},
                    title=f"1月〜12月 月別の手取り配当金 受取スケジュール（単位：{y_suffix}）"
                )
                fig_cal.update_traces(texttemplate=txt_tmpl, textposition='inside')
                fig_cal.update_layout(
                    xaxis_title="入金月",
                    yaxis_title=f"受取配当金（{y_suffix}）",
                    yaxis=dict(tickformat=y_fmt, ticksuffix=y_suffix),
                    hovermode="x unified"
                )
                st.plotly_chart(fig_cal, use_container_width=True)

                with st.expander("📋 1月〜12月 銘柄別・月別配当入金マトリクス表を見る", expanded=False):
                    df_cal_mat = pd.DataFrame(matrix_rows)
                    month_cols = [f"{m}月" for m in range(1, 13)] + ['年間合計']
                    fmt_dict = {mc: '{:,.0f} 円' for mc in month_cols}
                    st.dataframe(df_cal_mat.style.format(fmt_dict), use_container_width=True, hide_index=True)

            st.markdown("---")
            g_col1, g_col2 = st.columns(2)
            with g_col1:
                df_div_only = df_filtered[df_filtered['年間配当金(税引後)'] > 0]
                if not df_div_only.empty:
                    fig_div_pie = px.pie(
                        df_div_only, values='年間配当金(税引後)', names='銘柄名',
                        title="銘柄別 手取り配当金 構成比", hole=0.4
                    )
                    fig_div_pie.update_traces(textposition='inside', textinfo='percent+label')
                    st.plotly_chart(fig_div_pie, use_container_width=True)

            with g_col2:
                grp_acc_div = df_filtered.groupby(['口座区分', '証券会社'], as_index=False)[['年間配当金(税引後)']].sum()
                grp_acc_div['年間配当金(万円)'] = (grp_acc_div['年間配当金(税引後)'] / 10000.0).round(2)
                fig_acc_div = px.bar(
                    grp_acc_div,
                    x='口座区分', y='年間配当金(万円)', color='証券会社', barmode='group',
                    title="口座区分 × 証券会社別 年間手取り配当額（単位：万円）"
                )
                fig_acc_div.update_traces(texttemplate='%{y:,.1f}万円', textposition='outside')
                fig_acc_div.update_layout(
                    yaxis_title="年間手取り配当（万円）",
                    yaxis=dict(tickformat=",.1f", ticksuffix="万円")
                )
                st.plotly_chart(fig_acc_div, use_container_width=True)

            st.markdown("##### 📋 条件抽出銘柄の配当詳細・検算リスト")
            display_cols = [
                '証券会社', '口座区分', '銘柄コード', '銘柄名', '保有数量', '基準資産額',
                '1株配当(円)', '実質配当利回り(%)', '年間配当金(税引前)', '年間配当金(税引後)',
                '計算根拠', '税区分', '基準対比利回り(税引後%)', '配当入金月', '配当ソース'
            ]
            st.dataframe(
                df_filtered[display_cols].sort_values('年間配当金(税引後)', ascending=False).style.format({
                    '保有数量': '{:,.0f}',
                    '基準資産額': '{:,.0f} 円',
                    '1株配当(円)': '{:,.2f} 円',
                    '実質配当利回り(%)': '{:.2f} %',
                    '年間配当金(税引前)': '{:,.0f} 円',
                    '年間配当金(税引後)': '{:,.0f} 円',
                    '基準対比利回り(税引後%)': '{:.2f} %',
                }),
                use_container_width=True,
                hide_index=True
            )

# ==============================================================================
# TAB 3: 積み立て想定シミュレーション（すべて「万円」整数単位 & 「100k」表記なし）
# ==============================================================================
with tab3:
    st.subheader("📈 積立投資 将来資産シミュレーション（1〜30年 / 単位：万円）")

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
            '年間増加額(万円)': int(round(yearly_gain_man))
        })

    df_sim = pd.DataFrame(sim_rows)

    st.markdown("#### 🎯 10年目・20年目・30年後の到達マイルストーン（単位：万円）")
    m10 = df_sim[df_sim['年数_数値'] == 10].iloc[0]
    m20 = df_sim[df_sim['年数_数値'] == 20].iloc[0]
    m30 = df_sim[df_sim['年数_数値'] == 30].iloc[0]

    mc1, mc2, mc3 = st.columns(3)
    mc1.metric(f"【10年目 / {m10['西暦']}】総資産想定額", f"{m10['総資産想定額(万円)']:,.0f} 万円", f"うち運用益 +{m10['運用益合計(万円)']:,.0f} 万円")
    mc2.metric(f"【20年目 / {m20['西暦']}】総資産想定額", f"{m20['総資産想定額(万円)']:,.0f} 万円", f"うち運用益 +{m20['運用益合計(万円)']:,.0f} 万円")
    mc3.metric(f"【30年目 / {m30['西暦']}】総資産想定額", f"{m30['総資産想定額(万円)']:,.0f} 万円", f"うち運用益 +{m30['運用益合計(万円)']:,.0f} 万円")

    st.markdown("#### 📊 年次別 総資産推移グラフ（元本 ＋ 運用益 / 単位：万円）")
    view_span = st.radio("表示期間の切り替え", ["10年目まで", "20年目まで", "30年目まで（全期間）"], index=2, horizontal=True)
    max_y = 10 if "10" in view_span else (20 if "20" in view_span else 30)
    df_sim_filtered = df_sim[df_sim['年数_数値'] <= max_y]

    fig_sim = go.Figure()
    fig_sim.add_trace(go.Bar(
        x=df_sim_filtered['年表示(グラフ用)'], y=df_sim_filtered['元本合計(万円)'],
        name='投資元本（初期資産＋積立累計）', marker_color='#3b82f6',
        hovertemplate='%{y:,.0f} 万円'
    ))
    fig_sim.add_trace(go.Bar(
        x=df_sim_filtered['年表示(グラフ用)'], y=df_sim_filtered['運用益合計(万円)'],
        name='複利運用益', marker_color='#10b981',
        hovertemplate='%{y:,.0f} 万円'
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

    st.markdown("#### 📋 年次別 総資産想定額 一覧表（〇年目 ＆ 西暦 / 単位：万円）")
    df_sim_table = df_sim_filtered[['経過年数', '西暦', '元本合計(万円)', '運用益合計(万円)', '総資産想定額(万円)', '年間増加額(万円)']]
    st.dataframe(
        df_sim_table.style.format({
            '元本合計(万円)': '{:,.0f} 万円',
            '運用益合計(万円)': '{:,.0f} 万円',
            '総資産想定額(万円)': '{:,.0f} 万円',
            '年間増加額(万円)': '+{:,.0f} 万円'
        }),
        use_container_width=True,
        hide_index=True
    )

# ==============================================================================
# TAB 4: サイドFIRE シミュレーション & 【採用機能3】5段階FIREステージ到達ロードマップ
# ==============================================================================
with tab4:
    st.subheader("🏖️ サイドFIRE 実現可能性シミュレーター ＆ 5段階FIREロードマップ（単位：万円）")

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

    # --- 【採用機能3】5段階のFIREステージ別 到達ロードマップ診断 ---
    st.markdown("#### 🏔️ あなたの「5段階 FIREステージ」到達ロードマップ診断")
    st.caption("現在の資産・生活費・副業収入・配当利回りから、5つの代表的なFIREステージの目標金額と到達予想時期を診断します。")

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
    eff_div_yield_for_fire = max(cur_div_yield_post, 0.03)
    div_side_fire_target_man = int(round((max(living_cost_man - side_income_man, 0) * 12) / eff_div_yield_for_fire))

    lean_fire_target_man = int(round((living_cost_man * 0.8) * 12 * 25))
    full_fire_target_man = int(round(living_cost_man * 12 * 25))

    stage_rows = [
        {
            'FIREステージ': '① コーストFIRE (Coast FIRE)',
            '状態・定義': f'追加積立を0円にしても、現在の資産の複利成長だけで年金開始({pension_year}年)時に老後資金が足りる状態',
            '必要目標資産': f'{coast_fire_target_man:,.0f} 万円',
            '現在の達成率': f'{min(fire_init_man / max(coast_fire_target_man, 1) * 100, 100):.1f} %',
            'あと必要額': f'{max(coast_fire_target_man - fire_init_man, 0):,.0f} 万円',
            '到達予想時期': estimate_years_to_target(fire_init_man, coast_fire_target_man, pre_fire_saving_man, fire_yield)
        },
        {
            'FIREステージ': '② サイドFIRE (4%ルール基準)',
            '状態・定義': f'資産の4%取崩し＋副業収入(月{side_income_man}万円)で生活費(月{living_cost_man}万円)を100%賄える状態',
            '必要目標資産': f'{side_fire_target_man:,.0f} 万円',
            '現在の達成率': f'{min(fire_init_man / max(side_fire_target_man, 1) * 100, 100):.1f} %',
            'あと必要額': f'{max(side_fire_target_man - fire_init_man, 0):,.0f} 万円',
            '到達予想時期': estimate_years_to_target(fire_init_man, side_fire_target_man, pre_fire_saving_man, fire_yield)
        },
        {
            'FIREステージ': '③ 配当型サイドFIRE (元本取崩しゼロ)',
            '状態・定義': f'元本を一切売らず、手取り配当金(利回り{eff_div_yield_for_fire*100:.1f}%想定)＋副業収入だけで暮らせる状態',
            '必要目標資産': f'{div_side_fire_target_man:,.0f} 万円',
            '現在の達成率': f'{min(fire_init_man / max(div_side_fire_target_man, 1) * 100, 100):.1f} %',
            'あと必要額': f'{max(div_side_fire_target_man - fire_init_man, 0):,.0f} 万円',
            '到達予想時期': estimate_years_to_target(fire_init_man, div_side_fire_target_man, pre_fire_saving_man, fire_yield)
        },
        {
            'FIREステージ': '④ リーンFIRE (倹約型・完全リタイア)',
            '状態・定義': f'副業なしで、基礎生活費(月{living_cost_man*0.8:.0f}万円)を資産の4%取崩しだけで賄える状態',
            '必要目標資産': f'{lean_fire_target_man:,.0f} 万円',
            '現在の達成率': f'{min(fire_init_man / max(lean_fire_target_man, 1) * 100, 100):.1f} %',
            'あと必要額': f'{max(lean_fire_target_man - fire_init_man, 0):,.0f} 万円',
            '到達予想時期': estimate_years_to_target(fire_init_man, lean_fire_target_man, pre_fire_saving_man, fire_yield)
        },
        {
            'FIREステージ': '⑤ フルFIRE (完全FIRE)',
            '状態・定義': f'副業なしで、現在の生活費(月{living_cost_man}万円)をすべて資産の4%取崩しだけで生涯賄える状態',
            '必要目標資産': f'{full_fire_target_man:,.0f} 万円',
            '現在の達成率': f'{min(fire_init_man / max(full_fire_target_man, 1) * 100, 100):.1f} %',
            'あと必要額': f'{max(full_fire_target_man - fire_init_man, 0):,.0f} 万円',
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
                    '年末資産残高(万円)': int(round(max(bal, 0.0)))
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
    fk1.metric(f"FIRE時点({fire_year}/{fire_month})想定資産", f"{asset_at_fire_man:,.0f} 万円")
    fk2.metric("FIRE直後の毎月不足額", f"{fire_gap_man:,.0f} 万円/月", f"生活費{living_cost_man}万 - 副業{side_income_man}万")
    fk3.metric("4%ルール上の必要目標資産", f"{required_fire_man_4pct:,.0f} 万円", f"過不足 {asset_at_fire_man - required_fire_man_4pct:+,.0f} 万円")
    fk4.metric(f"{sim_years_total}年後の最終資産残高", f"{df_fire_sim.iloc[-1]['年末資産残高(万円)']:,.0f} 万円")

    fig_fire = go.Figure()
    fig_fire.add_trace(go.Scatter(
        x=df_fire_sim['西暦_数値'], y=df_fire_sim['年末資産残高(万円)'],
        mode='lines+markers', name='年末資産残高(万円)',
        fill='tozeroy', line=dict(color='#10b981', width=3),
        hovertemplate='%{y:,.0f} 万円'
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
        title="サイドFIRE後の長期資産残高推移（単位：万円）",
        xaxis_title="西暦（年）",
        yaxis_title="資産残高（万円）",
        xaxis=dict(tickformat="d", ticksuffix="年"),
        yaxis=dict(tickformat=",d", ticksuffix="万円"),
        hovermode="x unified"
    )
    st.plotly_chart(fig_fire, use_container_width=True)

    with st.expander("🔥 サイドFIRE 感度分析マトリクス（利回り × 副業収入別の最終資産早見表）", expanded=True):
        st.caption(f"「もし運用利回りが想定と違ったら？」「副業収入が増減したら？」のシナリオ別に、{sim_years_total}年後の最終資産残高（万円）を比較します。")
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
                    matrix_data[col_label].append(f"{end_bal:,.0f} 万円")
        df_matrix = pd.DataFrame(matrix_data, index=[f"利回り {y:.1f}%" for y in test_yields])
        st.table(df_matrix)

    st.markdown("##### 📋 年次別 キャッシュフロー＆資産推移表（単位：万円）")
    df_fire_table = df_fire_sim.drop(columns=['西暦_数値'])
    st.dataframe(
        df_fire_table.style.format({
            '年間積立額(万円)': '{:,.0f} 万円',
            '年間副業収入(万円)': '{:,.0f} 万円',
            '年間年金収入(万円)': '{:,.0f} 万円',
            '年間生活費(万円)': '{:,.0f} 万円',
            '年間運用益(万円)': '{:,.0f} 万円',
            '年間資産増減(万円)': '{:+,.0f} 万円',
            '年末資産残高(万円)': '{:,.0f} 万円',
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
        st.markdown(f"#### 💱 1. ドル円（USD/JPY）為替変動シミュレーター（現在レート: 1ドル = {current_usdjpy:.1f} 円）")
        st.caption("米国株・米国ETFおよび為替ヘッジなしの外国株インデックス投信（オルカン・S&P500等）が、円高・円安によって何万円増減するかをリアルタイム試算します。")

        if df_master.empty:
            st.info("CSVを取り込むかデモデータを有効にすると、保有銘柄の外貨エクスポージャーを自動計算します。")
        else:
            fx_mask = df_master['為替影響'] == "米ドル連動(為替影響あり)"
            fx_asset_yen = float(df_master.loc[fx_mask, '評価額'].sum())
            jp_asset_yen = float(df_master.loc[~fx_mask, '評価額'].sum())
            fx_div_yen = float(df_master.loc[fx_mask, '年間配当金(税引後)'].sum())
            jp_div_yen = float(df_master.loc[~fx_mask, '年間配当金(税引後)'].sum())

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
                diff_total_man = (new_total_yen - current_total_valuation) / 10000.0

                new_total_div_yen = jp_div_yen + (fx_div_yen * fx_change_ratio)
                diff_div_yen = new_total_div_yen - (jp_div_yen + fx_div_yen)

                st.metric("外貨連動資産の割合", f"{fx_ratio_pct:.1f} %", f"対象額 {fx_asset_yen/10000:,.0f} 万円")
                st.metric(
                    f"1ドル={sim_usdjpy}円時の総資産額",
                    f"{new_total_yen/10000:,.0f} 万円",
                    f"{diff_total_man:+,.0f} 万円 (為替 {fx_change_pct:+.1f}%)"
                )
                st.metric(
                    f"1ドル={sim_usdjpy}円時の年間手取り配当",
                    fmt_man_or_sen(new_total_div_yen),
                    f"実額 {new_total_div_yen:,.0f} 円 ({diff_div_yen:+,.0f} 円)"
                )

            with fx_c2:
                rate_scenarios = [110, 120, 130, 140, int(round(current_usdjpy)), 160, 170]
                fx_rows = []
                for r_val in rate_scenarios:
                    r_ratio = r_val / current_usdjpy if current_usdjpy > 0 else 1.0
                    s_total_man = (jp_asset_yen + fx_asset_yen * r_ratio) / 10000.0
                    s_diff_man = s_total_man - (current_total_valuation / 10000.0)
                    s_div_yen = jp_div_yen + fx_div_yen * r_ratio
                    label_str = f"1ドル = {r_val} 円 (現在)" if r_val == int(round(current_usdjpy)) else f"1ドル = {r_val} 円"
                    fx_rows.append({
                        '為替シナリオ': label_str,
                        '為替変動率': f"{(r_ratio - 1.0)*100:+.1f} %",
                        '想定総資産額': f"{s_total_man:,.0f} 万円",
                        '現在からの資産増減': f"{s_diff_man:+,.0f} 万円",
                        '年間手取り配当金': f"{fmt_man_or_sen(s_div_yen)} ({s_div_yen:,.0f} 円)"
                    })
                st.markdown("**📋 ドル円レート別 資産額＆配当金 シミュレーション一覧表**")
                st.table(pd.DataFrame(fx_rows))

    st.markdown("---")
    col_r1, col_r2 = st.columns(2)

    with col_r1:
        st.markdown("#### ⚖️ 2. アセットクラス別 リバランス計算機")
        st.caption("現在の資産配分と目標配分のズレを計算し、どの資産クラスを何万円買い増し（または売却）すべきか算出します。")

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
                action = f"+{diff_man:,.0f} 万円 買増" if diff_man >= 0 else f"{diff_man:,.0f} 万円 売却"
                reb_rows.append({
                    '資産クラス': c_name,
                    '現在評価額': f"{c_man:,.0f} 万円",
                    '現在比率': f"{c_pct:.1f} %",
                    '目標比率': f"{t_pct:.0f} %",
                    'リバランス目安': action
                })
            st.table(pd.DataFrame(reb_rows))

    with col_r2:
        st.markdown("#### 📉 3. 暴落ストレステスト（単位：万円）")
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
                '暴落後の総資産額': f"{stressed_man:,.0f} 万円",
                '暴落後のトータル損益': f"{stressed_pnl_man:+,.0f} 万円",
                '4%ルール月間取崩枠': fmt_man_or_sen(safe_4pct_yen)
            })
        st.table(pd.DataFrame(stress_rows))
