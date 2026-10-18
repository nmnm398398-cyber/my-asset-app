import streamlit as st
import pandas as pd
import numpy as np
import plotly.express as px
import plotly.graph_objects as go
import io
import re
import json
import urllib.request
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from dateutil.relativedelta import relativedelta

# --- ページ設定 ---
st.set_page_config(page_title="資産管理 & サイドFIRE統合シミュレーター", layout="wide")
st.title("💎 資産管理 & サイドFIRE 統合シミュレーター")

# ==============================================================================
# 1. ユーティリティ & 最新配当データ自動調査エンジン（分割・増配・減配対応）
# ==============================================================================

def clean_num(val):
    """文字列から数値(float)へ安全に変換する"""
    if pd.isna(val):
        return 0.0
    if isinstance(val, (int, float, np.number)):
        return float(val)
    s = str(val).replace(',', '').replace('円', '').replace('万円', '').replace('USD', '').replace('US$', '').replace('%', '').replace('％', '').replace('+', '').replace(' ', '').replace(' ', '').strip()
    if s in ['', '-', '--', '---', 'nan', 'None', 'N/A']:
        return 0.0
    s = s.translate(str.maketrans('０１２３４５６７８９－．', '0123456789-.'))
    try:
        return float(s)
    except ValueError:
        return 0.0

def normalize_account(text):
    """口座区分を『NISA』『特定』『一般』の3つに名寄せする"""
    s = str(text)
    if any(k in s for k in ['NISA', 'nisa', 'ニーサ', '成長投資', 'つみたて']):
        return 'NISA'
    elif '特定' in s:
        return '特定'
    elif '一般' in s:
        return '一般'
    return None

def classify_asset_type(name, ticker):
    """銘柄名とティッカーから資産クラスを自動分類"""
    s = str(name)
    t = str(ticker)
    if any(k in s for k in ['eMAXIS', 'オルカン', 'オール・カントリー', 'インデックス', 'ファンド', 'スリム', 'たわら', 'ニッセイ', 'iFree', '楽天・', 'ＳＢＩ・', 'SBI・', '投信']) and 'ETF' not in s and not re.match(r'^[0-9A-Z]{4}$', t):
        return '投資信託'
    if re.match(r'^[0-9]{3}[0-9A-Z]$', t):
        return '日本株・東証ETF'
    if re.match(r'^[A-Z]{1,5}$', t):
        return '米国株・米国ETF'
    return '日本株・東証ETF'

def extract_ticker_from_text(name_str, code_str=""):
    """CSVの銘柄コード列または銘柄名から証券コード・ティッカーを抽出"""
    c = str(code_str).strip()
    if re.match(r'^[0-9]{3}[0-9A-Z]$', c):
        return c
    if re.match(r'^[A-Z]{1,5}$', c):
        return c

    s = str(name_str).strip()
    # 投資信託（無配・再投資型）の早期判定
    if any(k in s for k in ['eMAXIS', 'オール・カントリー', 'オルカン', '楽天・全米', '楽天・全世界', 'ＳＢＩ・Ｖ', 'SBI・V', 'ニッセイ', 'たわら', 'iFree', 'インデックス・ファンド', 'インデックスファンド']) and 'ETF' not in s:
        return "FUND_NO_DIV"

    # 4桁の日本株・ETFコード（例: 8058, 2914, 1489）
    m_jp = re.search(r'(?:^|[\s\(\[（【/])([0-9]{3}[0-9A-Z])(?:$|[\s\)\]）】/])', s)
    if m_jp:
        return m_jp.group(1)

    # カッコ内の米国株ティッカー（例: (VYM), (SPYD)）
    ignore_words = {'NISA', 'ETF', 'REIT', 'SPDR', 'MAXIS', 'SLIM', 'INDEX', 'FUND', 'NEXT', 'FUNDS', 'ISHARES', 'VANGUARD', 'CORE', 'HIGH', 'DIV'}
    m_us_paren = re.findall(r'[\(\[（【]([A-Z]{1,5})[\)\]）】]', s)
    for cand in m_us_paren:
        if cand not in ignore_words:
            return cand

    # 空白区切りの米国ティッカー
    m_us_word = re.findall(r'\b([A-Z]{2,5})\b', s)
    for cand in m_us_word:
        if cand not in ignore_words:
            return cand

    return ""

@st.cache_data(ttl=3600, show_spinner=False)
def search_jp_ticker_by_name(stock_name):
    """銘柄名しか記載がない場合、Yahoo!ファイナンス検索から証券コードを特定する"""
    try:
        clean_q = re.sub(r'[\(\[（【].*?[\)\]）】]', '', str(stock_name)).strip()
        clean_q = clean_q.replace('株式会社', '').strip()
        if not clean_q or len(clean_q) < 2:
            return ""
        url = f"https://finance.yahoo.co.jp/search/?query={urllib.parse.quote(clean_q)}"
        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'})
        with urllib.request.urlopen(req, timeout=4) as res:
            html = res.read().decode('utf-8', errors='ignore')
        m = re.search(r'/quote/([0-9]{3}[0-9A-Z])\.T', html)
        if m:
            return m.group(1)
    except Exception:
        pass
    return ""

@st.cache_data(ttl=3600, show_spinner=False)
def fetch_latest_dividend_yield(ticker, stock_name=""):
    """
    最新の配当利回り(%)を自動調査するハイブリッドエンジン
    1. 日本株・東証ETF(4桁): Yahoo!ファイナンス日本版から最新の会社予想配当利回り(株式分割・増減配反映済)を取得
    2. 米国株・ETF / フォールバック: Yahoo Finance v8 APIから株式分割調整済みの直近1年配当と現在株価を取得して算出
    """
    if ticker == "FUND_NO_DIV":
        return 0.0, "投信(再投資)"

    # ティッカーが空なら銘柄名から検索を試みる
    if not ticker and stock_name:
        if any(k in str(stock_name) for k in ['ファンド', 'インデックス', 'eMAXIS', 'オルカン', '投信']) and 'ETF' not in str(stock_name):
            return 0.0, "投信(再投資)"
        ticker = search_jp_ticker_by_name(stock_name)

    if not ticker:
        return 0.0, "未特定(手動入力可)"

    headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'}

    # --- A. 日本株・東証ETF・REIT（4桁コード）の最新予想配当取得 ---
    if re.match(r'^[0-9]{3}[0-9A-Z]$', ticker):
        try:
            url_jp = f"https://finance.yahoo.co.jp/quote/{ticker}.T"
            req = urllib.request.Request(url_jp, headers=headers)
            with urllib.request.urlopen(req, timeout=5) as res:
                html = res.read().decode('utf-8', errors='ignore')

            # 1) PRELOADED_STATE JSONから最新の会社予想利回り・1株配当・株価を抽出
            m_state = re.search(r'window\.__PRELOADED_STATE__\s*=\s*(\{.*?\});?\s*</script>', html, re.DOTALL)
            if m_state:
                state_str = m_state.group(1)
                # 予想配当利回りの直接抽出
                m_yield = re.search(r'"dividendYield"\s*:\s*\{\s*"raw"\s*:\s*([0-9.]+)', state_str)
                if m_yield:
                    y_val = float(m_yield.group(1))
                    if 0 <= y_val < 30:
                        return round(y_val, 2), f"最新会社予想({ticker}.T)"

            # 2) HTMLテキストから「配当利回り（会社予想）」または「分配金利回り」を抽出
            # 株式分割や増配・減配が発表された場合、Yahoo!ファイナンスのこの項目が即座に最新値へ更新される
            for label in ['配当利回り（会社予想）', '分配金利回り（税込）', '配当利回り', '分配金利回り']:
                idx = html.find(label)
                if idx != -1:
                    snippet = html[idx:idx+600]
                    # 数値 + % のパターンを探す
                    m_pct = re.search(r'([0-9]{1,2}\.[0-9]{1,2})\s*</span[^>]*>\s*<span[^>]*>\s*%|([0-9]{1,2}\.[0-9]{1,2})\s*[%％]', snippet)
                    if m_pct:
                        val_str = m_pct.group(1) or m_pct.group(2)
                        y_val = float(val_str)
                        if 0 < y_val < 30:
                            return round(y_val, 2), f"最新予想({ticker}.T)"
        except Exception:
            pass

    # --- B. Yahoo Finance Chart API (米国株・ETF および 日本株フォールバック) ---
    # 株式分割(splits)と配当履歴(dividends)を同時取得し、分割調整後の正確な直近利回りを算出
    yf_symbol = f"{ticker}.T" if re.match(r'^[0-9]{3}[0-9A-Z]$', ticker) else ticker
    try:
        url_api = f"https://query1.finance.yahoo.com/v8/finance/chart/{urllib.parse.quote(yf_symbol)}?range=1y&interval=1d&events=div,split"
        req = urllib.request.Request(url_api, headers=headers)
        with urllib.request.urlopen(req, timeout=5) as res:
            data = json.loads(res.read().decode('utf-8'))

        result = data.get('chart', {}).get('result', [])
        if result:
            meta = result[0].get('meta', {})
            current_price = float(meta.get('regularMarketPrice', 0.0))
            events = result[0].get('events', {})
            divs = events.get('dividends', {})
            splits = events.get('splits', {})

            if current_price > 0 and divs:
                # 株式分割イベントがある場合、分割前の配当額を分割比率で調整する
                split_list = []
                for _, sp in splits.items():
                    num = float(sp.get('numerator', 1.0))
                    den = float(sp.get('denominator', 1.0))
                    if den > 0 and num != den:
                        split_list.append((int(sp.get('date', 0)), num / den))

                total_div = 0.0
                for _, d_obj in divs.items():
                    d_amt = float(d_obj.get('amount', 0.0))
                    d_date = int(d_obj.get('date', 0))
                    # その配当より後に株式分割が行われていた場合、現在株数基準に補正
                    for sp_date, ratio in split_list:
                        if d_date < sp_date and ratio > 0:
                            d_amt = d_amt / ratio
                    total_div += d_amt

                calc_yield = (total_div / current_price) * 100.0
                if 0 < calc_yield < 35:
                    return round(calc_yield, 2), f"最新実績({yf_symbol})"
            elif current_price > 0 and not divs:
                return 0.0, f"無配({yf_symbol})"
    except Exception:
        pass

    return 0.0, "未取得(手動入力可)"

def enrich_with_latest_dividends(df):
    """DataFrameの全銘柄に対して並列で最新配当利回りを自動調査して付与する"""
    if df.empty:
        return df

    df = df.copy()
    if '銘柄コード' not in df.columns:
        df['銘柄コード'] = ""
    if '配当ソース' not in df.columns:
        df['配当ソース'] = ""

    # 1. 銘柄コード・ティッカーの抽出
    tickers = []
    for _, row in df.iterrows():
        t = extract_ticker_from_text(row.get('銘柄名', ''), row.get('銘柄コード', ''))
        tickers.append(t)
    df['銘柄コード'] = tickers

    # 2. まだ配当利回りが0%の銘柄を対象に並列で最新配当データを取得
    tasks = []
    for idx, row in df.iterrows():
        cur_y = clean_num(row.get('配当利回り(%)', 0.0))
        cur_src = str(row.get('配当ソース', ''))
        if cur_y <= 0 and cur_src == "":
            tasks.append((idx, row['銘柄コード'], row['銘柄名']))

    if tasks:
        def _worker(task):
            idx, t_code, s_name = task
            y_val, src_label = fetch_latest_dividend_yield(t_code, s_name)
            return idx, y_val, src_label

        with ThreadPoolExecutor(max_workers=8) as executor:
            results = list(executor.map(_worker, tasks))

        for idx, y_val, src_label in results:
            df.at[idx, '配当利回り(%)'] = y_val
            df.at[idx, '配当ソース'] = src_label

    # 資産クラスの自動付与
    df['資産クラス'] = [
        classify_asset_type(r['銘柄名'], r['銘柄コード'])
        for _, r in df.iterrows()
    ]
    return df

# ==============================================================================
# 2. 証券会社CSVの自動判別 & マルチブロック解析エンジン
# ==============================================================================

def detect_and_parse_csv(uploaded_file):
    """
    アップロードされたCSVの中身を自動判別（楽天 / SBI / 保存済みCSV）して解析する
    """
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

    # 1) このアプリで保存した配当設定済みCSVかどうかの判定
    if '証券会社' in first_lines and '配当利回り(%)' in first_lines and '評価額' in first_lines:
        try:
            df_saved = pd.read_csv(io.StringIO(text_data))
            if '配当ソース' not in df_saved.columns:
                df_saved['配当ソース'] = '保存データ'
            if '銘柄コード' not in df_saved.columns:
                df_saved['銘柄コード'] = [extract_ticker_from_text(n, '') for n in df_saved['銘柄名']]
            return df_saved
        except Exception:
            pass

    # 2) 楽天証券 vs SBI証券 の自動判別
    fname = uploaded_file.name.lower()
    if '銘柄・ファンド名' in text_data or '時価評価額[円]' in text_data or 'rakuten' in fname or '楽天' in text_data:
        broker_name = '楽天証券'
    elif 'SBI' in text_data or 'sbi' in fname or 'ポートフォリオ一覧' in text_data or '評価金額' in text_data:
        broker_name = 'SBI証券'
    else:
        broker_name = 'SBI証券' if '評価金額' in text_data else '楽天証券'

    lines = text_data.splitlines()
    records = []
    current_account = '特定'
    i = 0
    n = len(lines)
    header_keywords = ['銘柄', 'ファンド', '商品名', '評価額', '評価金額', '時価評価額', '保有数量', '数量', '取得']

    while i < n:
        line = lines[i].strip()
        if not line:
            i += 1
            continue

        acc_detect = normalize_account(line)
        if acc_detect and line.count(',') <= 3:
            current_account = acc_detect

        hit_count = sum(1 for kw in header_keywords if kw in line)
        if hit_count >= 2 and ',' in line:
            block_lines = [line]
            i += 1
            while i < n:
                next_line = lines[i].strip()
                if not next_line:
                    break
                if next_line.count(',') <= 2 and any(k in next_line for k in ['合計', 'トータル', '預り', '口座', '一覧', '■', '◆']):
                    break
                if sum(1 for kw in header_keywords if kw in next_line) >= 2 and ('銘柄' in next_line or 'ファンド' in next_line):
                    break
                block_lines.append(next_line)
                i += 1

            try:
                df_block = pd.read_csv(io.StringIO("\n".join(block_lines)), on_bad_lines='skip')

                col_code = next((c for c in df_block.columns if any(k in str(c) for k in ['銘柄コード', 'ティッカー', 'コード']) and '名' not in str(c)), None)
                col_name = next((c for c in df_block.columns if any(k in str(c) for k in ['銘柄・ファンド名', '銘柄名', 'ファンド名', 'ファンド', '銘柄', '商品名']) and c != col_code), None)
                if not col_name:
                    col_name = next((c for c in df_block.columns if any(k in str(c) for k in ['銘柄', 'ファンド', '商品'])), None)

                col_val = next((c for c in df_block.columns if any(k in str(c) for k in ['時価評価額[円]', '評価額[円]', '評価金額[円]', '時価評価額', '評価金額', '評価額']) and '外貨' not in str(c) and '損益' not in str(c)), None)
                col_pnl = next((c for c in df_block.columns if any(k in str(c) for k in ['評価損益[円]', '評価損益額', '評価損益', 'トータルリターン', '含み損益']) and '率' not in str(c) and '％' not in str(c) and '%' not in str(c)), None)
                col_cost = next((c for c in df_block.columns if any(k in str(c) for k in ['取得総額[円]', '取得総額', '取得金額', '買付金額'])), None)
                col_acc = next((c for c in df_block.columns if any(k in str(c) for k in ['口座区分', '口座', '預り区分'])), None)
                col_div = next((c for c in df_block.columns if any(k in str(c) for k in ['配当利回り', '分配金利回り', '利回り'])), None)

                if col_name and col_val:
                    for _, row in df_block.iterrows():
                        name_str = str(row[col_name]).strip()
                        if not name_str or name_str in ['nan', 'None', '-', '合計', '総合計'] or '合計' in name_str:
                            continue

                        val_num = clean_num(row[col_val])
                        if val_num <= 0:
                            continue

                        pnl_num = clean_num(row[col_pnl]) if col_pnl else 0.0
                        if col_cost and clean_num(row[col_cost]) > 0:
                            cost_num = clean_num(row[col_cost])
                        else:
                            cost_num = max(val_num - pnl_num, 0.0)

                        row_acc = current_account
                        if col_acc:
                            detected = normalize_account(row[col_acc])
                            if detected:
                                row_acc = detected

                        code_str = str(row[col_code]).strip() if col_code and not pd.isna(row[col_code]) else ""
                        if code_str.endswith('.0'):
                            code_str = code_str[:-2]

                        div_yield = clean_num(row[col_div]) if col_div else 0.0

                        records.append({
                            '証券会社': broker_name,
                            '口座区分': row_acc,
                            '銘柄コード': extract_ticker_from_text(name_str, code_str),
                            '銘柄名': name_str,
                            '評価額': val_num,
                            '取得額': cost_num,
                            '評価損益': val_num - cost_num,
                            '配当利回り(%)': div_yield,
                            '配当ソース': 'CSV記載' if div_yield > 0 else ''
                        })
            except Exception:
                pass
        else:
            i += 1

    return pd.DataFrame(records)

# ==============================================================================
# 3. サイドバー：一括ドラッグ＆ドロップ & 最新配当データ更新
# ==============================================================================

with st.sidebar:
    st.header("📂 1. CSV一括ドラッグ＆ドロップ")
    st.caption("楽天証券・SBI証券・保存済みCSVを区別せず、ここにまとめて一気にドロップできます（自動判別）")
    uploaded_files = st.file_uploader(
        "CSVファイルをまとめてドロップ（複数選択可）",
        type=['csv'],
        accept_multiple_files=True
    )

    st.markdown("---")
    st.header("🔄 2. 最新配当データの自動調査")
    st.caption("Yahoo!ファイナンスから株式分割・増配・減配を反映した最新利回りを自動取得します。")
    if st.button("🔄 最新配当データを再取得（キャッシュ更新）", use_container_width=True):
        st.cache_data.clear()
        if "portfolio_df" in st.session_state:
            del st.session_state["portfolio_df"]
        st.rerun()

    st.markdown("---")
    use_demo = st.checkbox("🧪 デモデータで試す（CSVなしで確認）", value=False)

# --- データの読み込みと最新配当の自動付与 ---
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
        # デモデータもあえて配当利回り0%で渡し、自動取得エンジンの実力を確認できるように設定
        demo_data = [
            {'証券会社': 'SBI証券', '口座区分': 'NISA', '銘柄コード': 'FUND_NO_DIV', '銘柄名': 'eMAXIS Slim 全世界株式(オール・カントリー)', '評価額': 4500000, '取得額': 3200000, '評価損益': 1300000, '配当利回り(%)': 0.0, '配当ソース': ''},
            {'証券会社': 'SBI証券', '口座区分': 'NISA', '銘柄コード': 'VYM', '銘柄名': 'バンガード・米国高配当株式ETF (VYM)', '評価額': 2800000, '取得額': 2100000, '評価損益': 700000, '配当利回り(%)': 0.0, '配当ソース': ''},
            {'証券会社': 'SBI証券', '口座区分': '特定', '銘柄コード': '8058', '銘柄名': '三菱商事 (8058)', '評価額': 1800000, '取得額': 1200000, '評価損益': 600000, '配当利回り(%)': 0.0, '配当ソース': ''},
            {'証券会社': 'SBI証券', '口座区分': '特定', '銘柄コード': '8316', '銘柄名': '三井住友フィナンシャルG (8316)', '評価額': 1500000, '取得額': 950000, '評価損益': 550000, '配当利回り(%)': 0.0, '配当ソース': ''},
            {'証券会社': '楽天証券', '口座区分': 'NISA', '銘柄コード': 'FUND_NO_DIV', '銘柄名': '楽天・全米株式インデックス・ファンド', '評価額': 3200000, '取得額': 2400000, '評価損益': 800000, '配当利回り(%)': 0.0, '配当ソース': ''},
            {'証券会社': '楽天証券', '口座区分': '特定', '銘柄コード': 'SPYD', '銘柄名': 'SPDR ポートフォリオS&P 500高配当株式ETF (SPYD)', '評価額': 2200000, '取得額': 1900000, '評価損益': 300000, '配当利回り(%)': 0.0, '配当ソース': ''},
            {'証券会社': '楽天証券', '口座区分': '一般', '銘柄コード': '2914', '銘柄名': '日本たばこ産業 (2914)', '評価額': 1200000, '取得額': 900000, '評価損益': 300000, '配当利回り(%)': 0.0, '配当ソース': ''},
        ]
        df_list.append(pd.DataFrame(demo_data))

    if df_list:
        raw_master = pd.concat(df_list, ignore_index=True)
        for col in ['評価額', '取得額', '評価損益', '配当利回り(%)']:
            if col not in raw_master.columns:
                raw_master[col] = 0.0
            raw_master[col] = raw_master[col].apply(clean_num)
        if '口座区分' not in raw_master.columns:
            raw_master['口座区分'] = '特定'

        with st.spinner("🔍 各銘柄の最新配当データ（株式分割・増減配反映）を自動調査中..."):
            enriched_master = enrich_with_latest_dividends(raw_master)
        st.session_state["portfolio_df"] = enriched_master
    else:
        st.session_state["portfolio_df"] = pd.DataFrame(
            columns=['証券会社', '口座区分', '資産クラス', '銘柄コード', '銘柄名', '評価額', '取得額', '評価損益', '配当利回り(%)', '配当ソース']
        )
    st.session_state["last_file_sig"] = file_sig

df_master = st.session_state["portfolio_df"].copy()

# 年間配当金額（税引前・税引後）の常時計算
if not df_master.empty:
    df_master['年間配当金(税引前)'] = df_master['評価額'] * (df_master['配当利回り(%)'] / 100.0)
    df_master['税率'] = df_master['口座区分'].apply(lambda x: 0.0 if x == 'NISA' else 0.20315)
    df_master['年間配当金(税引後)'] = df_master['年間配当金(税引前)'] * (1.0 - df_master['税率'])
else:
    df_master['年間配当金(税引前)'] = 0.0
    df_master['年間配当金(税引後)'] = 0.0

# 現在の総資産額（円 & 万円）
current_total_valuation = float(df_master['評価額'].sum()) if not df_master.empty else 10000000.0
current_total_cost = float(df_master['取得額'].sum()) if not df_master.empty else 8000000.0
current_total_man = round(current_total_valuation / 10000.0, 1)

# --- タブ構成 ---
tab1, tab2, tab3, tab4, tab5 = st.tabs([
    "📊 1. 総合ダッシュボード",
    "🎁 2. 配当データ分析",
    "📈 3. 積立シミュレーション(万円)",
    "🏖️ 4. サイドFIRE診断(万円)",
    "🧭 5. リバランス & 暴落テスト"
])

# ==============================================================================
# TAB 1: 総合ダッシュボード
# ==============================================================================
with tab1:
    if df_master.empty:
        st.info("👈 左のサイドバーに楽天証券・SBI証券のCSVをまとめてドラッグ＆ドロップするか、「🧪 デモデータで試す」にチェックを入れてください。")
    else:
        total_pnl = current_total_valuation - current_total_cost
        pnl_ratio = (total_pnl / current_total_cost * 100.0) if current_total_cost > 0 else 0.0
        total_div_after = float(df_master['年間配当金(税引後)'].sum())
        total_yield_after = (total_div_after / current_total_valuation * 100.0) if current_total_valuation > 0 else 0.0

        c1, c2, c3, c4 = st.columns(4)
        c1.metric("総資産額（現時点評価額）", f"{current_total_valuation / 10000:,.1f} 万円", f"{current_total_valuation:,.0f} 円")
        c2.metric("投資元本（取得額合計）", f"{current_total_cost / 10000:,.1f} 万円", f"{current_total_cost:,.0f} 円")
        c3.metric("トータル含み損益", f"{total_pnl / 10000:+,.1f} 万円", f"{pnl_ratio:+.2f} %")
        c4.metric("年間手取り配当金（自動算出）", f"{total_div_after / 10000:,.2f} 万円/年", f"実質利回り {total_yield_after:.2f}% (月 {total_div_after/12:,.0f}円)")

        st.markdown("---")
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
            st.subheader("🏦 証券会社 × 口座区分 内訳")
            fig_bar = px.bar(
                df_master.groupby(['証券会社', '口座区分'], as_index=False)['評価額'].sum(),
                x='証券会社', y='評価額', color='口座区分', barmode='group',
                text_auto=',.0f', title="証券会社・口座別の資産残高（円）"
            )
            st.plotly_chart(fig_bar, use_container_width=True)

        st.subheader("📋 全保有銘柄一覧（最新配当利回り 自動反映済）")
        st.caption("※「配当利回り(%)」はYahoo!ファイナンス等から株式分割・最新の増配/減配を反映して自動取得しています。「🎁 2. 配当データ分析」タブで手動微調整も可能です。")
        disp_cols_t1 = ['証券会社', '口座区分', '資産クラス', '銘柄コード', '銘柄名', '評価額', '取得額', '評価損益', '配当利回り(%)', '年間配当金(税引後)', '配当ソース']
        st.dataframe(
            df_master[disp_cols_t1].sort_values('評価額', ascending=False).style.format({
                '評価額': '{:,.0f} 円',
                '取得額': '{:,.0f} 円',
                '評価損益': '{:+,.0f} 円',
                '配当利回り(%)': '{:.2f} %',
                '年間配当金(税引後)': '{:,.0f} 円'
            }),
            use_container_width=True,
            hide_index=True
        )

# ==============================================================================
# TAB 2: 配当データ分析機能
# ==============================================================================
with tab2:
    st.subheader("🎁 条件別 ポートフォリオ＆最新配当データ分析")

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

        with st.expander("✏️ 銘柄ごとの配当利回り(%)を確認・微調整する（クリックで開閉）", expanded=False):
            st.caption("自動取得された最新利回りを手動で変更すると、「📊 1. 総合ダッシュボード」を含むアプリ全体に即座に反映されます。")
            edit_cols = ['証券会社', '口座区分', '銘柄コード', '銘柄名', '評価額', '取得額', '評価損益', '配当利回り(%)', '配当ソース']
            edited_df = st.data_editor(
                df_master[edit_cols],
                column_config={
                    "配当利回り(%)": st.column_config.NumberColumn(
                        "配当利回り(%) ✏️",
                        min_value=0.0, max_value=50.0, step=0.05, format="%.2f %%"
                    ),
                    "評価額": st.column_config.NumberColumn("現時点評価額(円)", format="¥%d"),
                    "取得額": st.column_config.NumberColumn("取得額(円)", format="¥%d"),
                    "評価損益": st.column_config.NumberColumn("評価損益(円)", format="¥%d"),
                },
                disabled=['証券会社', '口座区分', '銘柄名', '評価損益', '配当ソース'],
                use_container_width=True,
                hide_index=True,
                key="dividend_editor"
            )

            # 編集内容をセッションステートにも同期（タブ1の全保有銘柄一覧にも反映）
            if not edited_df['配当利回り(%)'].equals(df_master['配当利回り(%)']):
                st.session_state["portfolio_df"]['配当利回り(%)'] = edited_df['配当利回り(%)']
                st.session_state["portfolio_df"]['銘柄コード'] = edited_df['銘柄コード']
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
            df_filtered['年間配当額(税引前)'] = df_filtered['評価額'] * (df_filtered['配当利回り(%)'] / 100.0)
            df_filtered['税率'] = df_filtered['口座区分'].apply(lambda x: 0.0 if x == 'NISA' else 0.20315)
            df_filtered['年間配当額(税引後)'] = df_filtered['年間配当額(税引前)'] * (1.0 - df_filtered['税率'])

            df_filtered['基準対比利回り(税引前%)'] = np.where(
                df_filtered['基準資産額'] > 0,
                df_filtered['年間配当額(税引前)'] / df_filtered['基準資産額'] * 100.0,
                0.0
            )
            df_filtered['基準対比利回り(税引後%)'] = np.where(
                df_filtered['基準資産額'] > 0,
                df_filtered['年間配当額(税引後)'] / df_filtered['基準資産額'] * 100.0,
                0.0
            )

            sum_target_asset = df_filtered['基準資産額'].sum()
            sum_div_pre = df_filtered['年間配当額(税引前)'].sum()
            sum_div_post = df_filtered['年間配当額(税引後)'].sum()
            monthly_div_post = sum_div_post / 12.0
            portfolio_yield_pre = (sum_div_pre / sum_target_asset * 100.0) if sum_target_asset > 0 else 0.0
            portfolio_yield_post = (sum_div_post / sum_target_asset * 100.0) if sum_target_asset > 0 else 0.0

            st.markdown(f"#### 📊 選択条件での分析結果（基準：{asset_basis}）")
            k1, k2, k3, k4 = st.columns(4)
            k1.metric(f"対象総資産額 ({basis_col})", f"{sum_target_asset / 10000:,.1f} 万円", f"{sum_target_asset:,.0f} 円")
            k2.metric("年間配当金（税引前）", f"{sum_div_pre / 10000:,.2f} 万円", f"利回り {portfolio_yield_pre:.2f} % ({sum_div_pre:,.0f}円)")
            k3.metric("年間配当金（税引後・手取り）", f"{sum_div_post / 10000:,.2f} 万円", f"実質利回り {portfolio_yield_post:.2f} % ({sum_div_post:,.0f}円)")
            k4.metric("月平均の手取り配当金", f"{monthly_div_post / 10000:,.2f} 万円/月", f"{monthly_div_post:,.0f} 円/月")

            # --- 新機能：配当マイルストーン達成メーター ---
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

            g_col1, g_col2 = st.columns(2)
            with g_col1:
                df_div_only = df_filtered[df_filtered['年間配当額(税引後)'] > 0]
                if not df_div_only.empty:
                    fig_div_pie = px.pie(
                        df_div_only, values='年間配当額(税引後)', names='銘柄名',
                        title="銘柄別 手取り配当金 構成比", hole=0.4
                    )
                    fig_div_pie.update_traces(textposition='inside', textinfo='percent+label')
                    st.plotly_chart(fig_div_pie, use_container_width=True)
                else:
                    st.info("選択された条件に配当のある銘柄がありません。")

            with g_col2:
                fig_acc_div = px.bar(
                    df_filtered.groupby(['口座区分', '証券会社'], as_index=False)[['年間配当額(税引後)']].sum(),
                    x='口座区分', y='年間配当額(税引後)', color='証券会社', barmode='group',
                    text_auto=',.0f', title="口座区分 × 証券会社別 年間手取り配当額（円）"
                )
                st.plotly_chart(fig_acc_div, use_container_width=True)

            st.markdown("##### 📋 条件抽出銘柄の配当詳細リスト")
            display_cols = ['証券会社', '口座区分', '銘柄コード', '銘柄名', '基準資産額', '配当利回り(%)', '年間配当額(税引前)', '年間配当額(税引後)', '基準対比利回り(税引前%)', '基準対比利回り(税引後%)', '配当ソース']
            st.dataframe(
                df_filtered[display_cols].sort_values('年間配当額(税引後)', ascending=False).style.format({
                    '基準資産額': '{:,.0f} 円',
                    '配当利回り(%)': '{:.2f} %',
                    '年間配当額(税引前)': '{:,.0f} 円',
                    '年間配当額(税引後)': '{:,.0f} 円',
                    '基準対比利回り(税引前%)': '{:.2f} %',
                    '基準対比利回り(税引後%)': '{:.2f} %',
                }),
                use_container_width=True,
                hide_index=True
            )

# ==============================================================================
# TAB 3: 積み立て想定シミュレーション（すべて「万円」単位 & 〇年目＋西暦表示）
# ==============================================================================
with tab3:
    st.subheader("📈 積立投資 将来資産シミュレーション（1〜30年 / 単位：万円）")

    base_year = datetime.now().year

    with st.container(border=True):
        s_col1, s_col2, s_col3 = st.columns(3)
        with s_col1:
            sim_initial_man = st.number_input(
                "現在の総資産額（万円）",
                min_value=0.0, max_value=100000.0,
                value=float(current_total_man), step=10.0,
                help="CSVから読み取った現在の総資産額（万円）が初期セットされています"
            )
        with s_col2:
            sim_monthly_man = st.number_input(
                "毎月の積立額（万円）",
                min_value=0.0, max_value=1000.0,
                value=10.0, step=1.0
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
            '元本合計(万円)': round(principal_man, 1),
            '運用益合計(万円)': round(profit_man, 1),
            '総資産想定額(万円)': round(balance_man, 1),
            '年間増加額(万円)': round(yearly_gain_man, 1)
        })

    df_sim = pd.DataFrame(sim_rows)

    st.markdown("#### 🎯 10年目・20年目・30年後の到達マイルストーン（単位：万円）")
    m10 = df_sim[df_sim['年数_数値'] == 10].iloc[0]
    m20 = df_sim[df_sim['年数_数値'] == 20].iloc[0]
    m30 = df_sim[df_sim['年数_数値'] == 30].iloc[0]

    mc1, mc2, mc3 = st.columns(3)
    mc1.metric(f"【10年目 / {m10['西暦']}】総資産想定額", f"{m10['総資産想定額(万円)']:,.1f} 万円", f"うち運用益 +{m10['運用益合計(万円)']:,.1f} 万円")
    mc2.metric(f"【20年目 / {m20['西暦']}】総資産想定額", f"{m20['総資産想定額(万円)']:,.1f} 万円", f"うち運用益 +{m20['運用益合計(万円)']:,.1f} 万円")
    mc3.metric(f"【30年目 / {m30['西暦']}】総資産想定額", f"{m30['総資産想定額(万円)']:,.1f} 万円", f"うち運用益 +{m30['運用益合計(万円)']:,.1f} 万円")

    st.markdown("#### 📊 年次別 総資産推移グラフ（元本 ＋ 運用益 / 単位：万円）")
    view_span = st.radio("表示期間の切り替え", ["10年目まで", "20年目まで", "30年目まで（全期間）"], index=2, horizontal=True)
    max_y = 10 if "10" in view_span else (20 if "20" in view_span else 30)
    df_sim_ filtered = df_sim[df_sim['年数_数値'] <= max_y]

    fig_sim = go.Figure()
    fig_sim.add_trace(go.Bar(
        x=df_sim_filtered['年表示(グラフ用)'], y=df_sim_filtered['元本合計(万円)'],
        name='投資元本（初期資産＋積立累計）', marker_color='#3b82f6'
    ))
    fig_sim.add_trace(go.Bar(
        x=df_sim_filtered['年表示(グラフ用)'], y=df_sim_filtered['運用益合計(万円)'],
        name='複利運用益', marker_color='#10b981'
    ))
    fig_sim.update_layout(
        barmode='stack',
        xaxis_title='経過年数（西暦）',
        yaxis_title='金額（万円）',
        yaxis=dict(ticksuffix="万円"),
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
        hovermode="x unified"
    )
    st.plotly_chart(fig_sim, use_container_width=True)

    st.markdown("#### 📋 年次別 総資産想定額 一覧表（〇年目 ＆ 西暦 / 単位：万円）")
    df_sim_table = df_sim_filtered[['経過年数', '西暦', '元本合計(万円)', '運用益合計(万円)', '総資産想定額(万円)', '年間増加額(万円)']]
    st.dataframe(
        df_sim_table.style.format({
            '元本合計(万円)': '{:,.1f} 万円',
            '運用益合計(万円)': '{:,.1f} 万円',
            '総資産想定額(万円)': '{:,.1f} 万円',
            '年間増加額(万円)': '+{:,.1f} 万円'
        }),
        use_container_width=True,
        hide_index=True
    )

# ==============================================================================
# TAB 4: サイドFIRE シミュレーション機能（すべて「万円」単位）
# ==============================================================================
with tab4:
    st.subheader("🏖️ サイドFIRE 実現可能性シミュレーター（単位：万円）")
    st.caption("現在の総資産額・サイドFIRE予定年月・副業収入・年金・生活費をすべて「万円」単位で設定し、資産推移を精緻に検証します。")

    now_dt = datetime.now()
    cur_year, cur_month = now_dt.year, now_dt.month

    with st.container(border=True):
        st.markdown("##### ⚙️ シミュレーション前提条件の入力（単位：万円）")
        f_c1, f_c2, f_c3 = st.columns(3)

        with f_c1:
            st.markdown("**① 現在の資産と運用条件**")
            fire_init_man = st.number_input("現在の総資産額（万円）", min_value=0.0, max_value=100000.0, value=float(current_total_man), step=10.0, key="f_init")
            fire_yield = st.number_input("想定運用利回り（年率 %）", min_value=0.0, max_value=25.0, value=5.0, step=0.1, key="f_yield")
            pre_fire_saving_man = st.number_input("サイドFIRE達成までの毎月積立額（万円）", min_value=0.0, max_value=500.0, value=10.0, step=0.5)
            inflation_rate = st.number_input("想定インフレ率（生活費の上昇率・年率 %）", min_value=0.0, max_value=10.0, value=1.0, step=0.1)

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
            living_cost_man = st.number_input("想定生活費（月額・万円）", min_value=5.0, max_value=200.0, value=25.0, step=0.5)
            side_income_man = st.number_input("副業想定収入（手取り月額・万円）", min_value=0.0, max_value=200.0, value=10.0, step=0.5)
            side_job_end_option = st.selectbox("副業の継続期間", ["年金受給開始まで継続", "サイドFIRE後10年間のみ", "サイドFIRE後15年間のみ", "生涯継続"])
            pension_man = st.number_input("年金予定額（手取り月額・万円）", min_value=0.0, max_value=100.0, value=15.0, step=0.5)

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
                    '年間積立額(万円)': round(y_save, 1),
                    '年間副業収入(万円)': round(y_side, 1),
                    '年間年金収入(万円)': round(y_pen, 1),
                    '年間生活費(万円)': round(y_liv, 1),
                    '年間運用益(万円)': round(y_gain, 1),
                    '年間資産増減(万円)': round(y_save + y_side + y_pen - y_liv + y_gain, 1),
                    '年末資産残高(万円)': round(max(bal, 0.0), 1)
                })
                y_save = y_side = y_pen = y_liv = y_gain = 0.0

        if at_fire is None:
            at_fire = bal
        return pd.DataFrame(records), at_fire, dep_date

    df_fire_sim, asset_at_fire_man, depletion_date = run_fire_simulation(
        fire_init_man, fire_yield, pre_fire_saving_man, inflation_rate,
        fire_year, fire_month, pension_year, pension_month,
        sim_years_total, living_cost_man, side_income_man, side_job_end_option, pension_man
    )

    st.markdown("---")
    st.markdown("#### 🩺 サイドFIRE 実現可能性 判定結果")

    fire_gap_man = max(living_cost_man - side_income_man, 0.0)
    required_fire_man_4pct = fire_gap_man * 12.0 * 25.0

    if depletion_date is None and df_fire_sim.iloc[-1]['年末資産残高(万円)'] >= asset_at_fire_man:
        st.success(f"🎉 **【判定：大成功（余裕でサイドFIRE可能）】** {fire_year}年{fire_month}月にサイドFIREしても資産は一切枯渇せず、運用益が取り崩しを上回り資産が増え続けます！")
    elif depletion_date is None:
        st.info(f"✅ **【判定：サイドFIRE可能】** 資産を取り崩しながらも、今後{sim_years_total}年間（{cur_year + sim_years_total}年まで）資産が枯渇せず維持できます。")
    else:
        st.error(f"⚠️ **【判定：資金枯渇リスクあり】** この条件では **{depletion_date.year}年{depletion_date.month}月頃** に資産が枯渇する見込みです。サイドFIRE時期を遅らせるか、副業収入・生活費の見直しを推奨します。")

    fk1, fk2, fk3, fk4 = st.columns(4)
    fk1.metric(f"サイドFIRE時点({fire_year}/{fire_month})の想定資産", f"{asset_at_fire_man:,.1f} 万円")
    fk2.metric("FIRE直後の毎月不足額(生活費-副業)", f"{fire_gap_man:,.1f} 万円/月")
    fk3.metric("4%ルール上の必要目標資産", f"{required_fire_man_4pct:,.1f} 万円", f"過不足: {asset_at_fire_man - required_fire_man_4pct:+,.1f} 万円")
    fk4.metric(f"{sim_years_total}年後の最終資産残高", f"{df_fire_sim.iloc[-1]['年末資産残高(万円)']:,.1f} 万円")

    # 推移グラフ（万円単位）
    fig_fire = go.Figure()
    fig_fire.add_trace(go.Scatter(
        x=df_fire_sim['西暦_数値'], y=df_fire_sim['年末資産残高(万円)'],
        mode='lines+markers', name='年末資産残高(万円)',
        fill='tozeroy', line=dict(color='#10b981', width=3)
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
        xaxis=dict(ticksuffix="年"),
        yaxis=dict(ticksuffix="万円"),
        hovermode="x unified"
    )
    st.plotly_chart(fig_fire, use_container_width=True)

    # --- 新機能：サイドFIRE 感度分析マトリクス（利回り × 副業収入 早見表） ---
    with st.expander("🔥 サイドFIRE 感度分析マトリクス（利回り × 副業収入別の最終資産早見表）", expanded=True):
        st.caption(f"「もし運用利回りが想定と違ったら？」「副業収入が増減したら？」のシナリオ別に、{sim_years_total}年後の最終資産残高（万円）を比較します。")
        test_yields = [3.0, 4.0, 5.0, 6.0, 7.0]
        test_sides = [max(0.0, side_income_man - 5.0), side_income_man, side_income_man + 5.0, side_income_man + 10.0]
        matrix_data = {}
        for s_val in test_sides:
            col_label = f"副業 月{s_val:.1f}万円"
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
            '年間積立額(万円)': '{:,.1f} 万円',
            '年間副業収入(万円)': '{:,.1f} 万円',
            '年間年金収入(万円)': '{:,.1f} 万円',
            '年間生活費(万円)': '{:,.1f} 万円',
            '年間運用益(万円)': '{:,.1f} 万円',
            '年間資産増減(万円)': '{:+,.1f} 万円',
            '年末資産残高(万円)': '{:,.1f} 万円',
        }),
        use_container_width=True,
        hide_index=True
    )

# ==============================================================================
# TAB 5: アセットアロケーション・リバランス計算機 & 暴落ストレステスト
# ==============================================================================
with tab5:
    st.subheader("🧭 リバランス計算機 & 暴落ストレステスト（単位：万円）")

    col_r1, col_r2 = st.columns(2)

    with col_r1:
        st.markdown("#### ⚖️ 1. アセットクラス別 リバランス計算機")
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
            reb_rows = []
            for c_name in all_classes:
                c_man = cur_map.get(c_name, 0.0)
                c_pct = (c_man / current_total_man * 100.0) if current_total_man > 0 else 0.0
                t_pct = targets[c_name]
                ideal_man = current_total_man * (t_pct / 100.0)
                diff_man = ideal_man - c_man
                action = f"+{diff_man:,.1f} 万円 買増" if diff_man >= 0 else f"{diff_man:,.1f} 万円 売却"
                reb_rows.append({
                    '資産クラス': c_name,
                    '現在評価額': f"{c_man:,.1f} 万円",
                    '現在比率': f"{c_pct:.1f} %",
                    '目標比率': f"{t_pct:.1f} %",
                    'リバランス目安': action
                })
            st.table(pd.DataFrame(reb_rows))

    with col_r2:
        st.markdown("#### 📉 2. 暴落ストレステスト（単位：万円）")
        st.caption("金融危機で株価が下落した際の総資産額・含み損益・4%ルール安全取崩額を試算します。")

        stress_scenarios = [-10, -20, -30, -40, -50]
        stress_rows = []
        cost_man = current_total_cost / 10000.0
        for drop in stress_scenarios:
            stressed_man = current_total_man * (1.0 + drop / 100.0)
            stressed_pnl_man = stressed_man - cost_man
            safe_4pct_man = (stressed_man * 0.04) / 12.0
            stress_rows.append({
                '暴落シナリオ': f"株価 {drop}% 下落",
                '暴落後の総資産額': f"{stressed_man:,.1f} 万円",
                '暴落後のトータル損益': f"{stressed_pnl_man:+,.1f} 万円",
                '4%ルール月間取崩枠': f"{safe_4pct_man:,.2f} 万円/月"
            })
        st.table(pd.DataFrame(stress_rows))
