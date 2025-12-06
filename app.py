import streamlit as st
import pandas as pd
import plotly.express as px
import io
import csv
import unicodedata

# --- ページ設定 ---
st.set_page_config(page_title="資産分析Pro", layout="wide")
st.title("📊 資産ポートフォリオ分析 & 配当管理")

# --- 定数・設定 ---
FIRE_GOAL = 30000000  # 目標金額

# --- ユーティリティ関数 ---

def normalize_text(text):
    """全角・半角を統一して正規化する"""
    if not isinstance(text, str):
        return str(text)
    return unicodedata.normalize('NFKC', text).upper()

def clean_currency(x):
    """数値を綺麗にする（円やカンマを除去）"""
    if isinstance(x, (int, float)):
        return float(x)
    if isinstance(x, str):
        clean_str = x.replace(',', '').replace('円', '').replace('USD', '').replace('%', '').strip()
        try:
            return float(clean_str)
        except ValueError:
            return 0.0
    return 0.0

def guess_country(name, category):
    """銘柄名から国・地域を推測する"""
    name = normalize_text(name)
    category = normalize_text(category)
    
    if "日本" in name or "TOPIX" in name or "日経" in name or "JAPAN" in name or "国内" in category:
        return "日本"
    elif "米国" in name or "S&P" in name or "NASDAQ" in name or "全米" in name or "US" in name or "AMERICA" in name:
        return "米国"
    elif "インド" in name or "INDIA" in name:
        return "インド"
    elif "全世界" in name or "オール・カントリー" in name or "オルカン" in name or "GLOBAL" in name:
        return "全世界"
    elif "先進国" in name:
        return "先進国"
    elif "新興国" in name:
        return "新興国"
    else:
        return "その他"

def guess_asset_class(row):
    """資産クラス（個別株/投資信託など）を判定"""
    ctype = normalize_text(row.get('種別_raw', ''))
    name = normalize_text(row.get('銘柄名', ''))
    
    if '投資信託' in ctype or 'ファンド' in name:
        return '投資信託'
    elif '株式' in ctype:
        return '個別株'
    else:
        return '投資信託' if 'ファンド' in name else '個別株'

def guess_account_type(txt):
    """口座区分（新NISA/旧NISA/特定）を明確に区別"""
    txt = normalize_text(txt)
    
    if "旧NISA" in txt:
        return "旧NISA"
    elif "つみたて" in txt:
        return "新NISA(つみたて)"
    elif "成長" in txt:
        return "新NISA(成長)"
    elif "NISA" in txt:
        return "新NISA(不明)"
    elif "特定" in txt:
        return "特定口座"
    elif "一般" in txt:
        return "一般口座"
    else:
        return "特定口座"

def default_yield(row):
    """銘柄名から予想配当利回りを推測"""
    name = normalize_text(row['銘柄名'])
    asset_class = row['資産クラス']
    
    if "高配当" in name or "VYM" in name or "HDV" in name or "SPYD" in name:
        return 3.5
    elif "REIT" in name or "リート" in name:
        return 4.0
    elif "債券" in name or "AGG" in name or "BND" in name:
        return 2.5
    elif "インド" in name:
        return 0.0
    elif "NASDAQ" in name or "ナスダック" in name:
        return 0.5
    elif "S&P500" in name or "SP500" in name or "全米" in name or "VTI" in name:
        return 1.3
    elif "全世界" in name or "オルカン" in name or "オール・カントリー" in name:
        return 1.5
    elif "TOPIX" in name or "日経" in name:
        return 1.8
    elif asset_class == '個別株':
        return 2.0
    return 0.0

# --- CSV読み込みロジック (強化版) ---

def load_rakuten_parser(text_data):
    """楽天証券パーサー"""
    try:
        lines = text_data.splitlines()
        header_idx = -1
        # 楽天は「種別」と「銘柄」がある行がヘッダー
        for i, line in enumerate(lines):
            if '"種別"' in line and '"銘柄"' in line:
                header_idx = i
                break
        
        if header_idx == -1: return pd.DataFrame()

        df = pd.read_csv(io.StringIO(text_data), skiprows=header_idx)
        rename_map = {
            '銘柄': '銘柄名', '銘柄・ファンド名': '銘柄名',
            '保有数量': '保有数', '時価評価額[円]': '評価額',
            '評価損益[円]': '評価損益', '種別': '種別_raw', '口座': '口座区分_raw'
        }
        available = [c for c in rename_map.keys() if c in df.columns]
        df = df[available].rename(columns=rename_map)
        df['証券会社'] = '楽天証券'
        df['口座区分'] = df['口座区分_raw'].apply(guess_account_type)
        return df
    except:
        return pd.DataFrame()

def load_sbi_parser_greedy(text_data):
    """SBI証券 強力スキャンパーサー"""
    data_rows = []
    lines = text_data.splitlines()
    reader = csv.reader(lines) # CSVとして正しくパース

    current_section = "不明"
    header_map = {}
    
    # 全行を走査
    for row in reader:
        if not row: continue
        line_str = ",".join(row)

        # 1. セクション名の更新（行に「株式」や「投資信託」があり、ヘッダーではない場合）
        if ("株式" in line_str or "投資信託" in line_str) and "銘柄" not in line_str and "ファンド名" not in line_str:
            current_section = line_str.replace('"', '').replace(',', '').strip()
            continue

        # 2. ヘッダー行の検出（「銘柄」または「ファンド名」があり、かつ「評価」がある行）
        if ("銘柄" in line_str or "ファンド名" in line_str) and ("評価" in line_str or "含み損益" in line_str):
            header_map = {col: idx for idx, col in enumerate(row)}
            continue

        # 3. データ行の取り込み（ヘッダーが見つかった後の行で、必要な列がある場合）
        if not header_map: continue

        # 列の位置を探す
        name_col = next((k for k in ['銘柄名称', 'ファンド名', '銘柄コード/銘柄名'] if k in header_map), None)
        val_col = next((k for k in ['評価額', '評価金額'] if k in header_map), None)
        pl_col = next((k for k in ['評価損益', '含み損益'] if k in header_map), None)

        if name_col and val_col:
            name_idx = header_map[name_col]
            val_idx = header_map[val_col]
            
            # 行の長さが足りているか
            if len(row) > max(name_idx, val_idx):
                try:
                    raw_val = row[val_idx]
                    val_float = clean_currency(raw_val)
                    
                    # 0円かつゴミデータの場合はスキップ、0円でも意味があるなら採用
                    if val_float == 0 and "0" not in str(raw_val):
                        pass
                    else:
                        item = {
                            '銘柄名': row[name_idx],
                            '評価額': val_float,
                            '評価損益': clean_currency(row[header_map[pl_col]]) if pl_col else 0,
                            '証券会社': 'SBI証券',
                            '種別_raw': '投資信託' if '投資信託' in current_section else '株式',
                            '口座区分_raw': current_section,
                            '口座区分': guess_account_type(current_section)
                        }
                        data_rows.append(item)
                except:
                    continue

    return pd.DataFrame(data_rows)

def process_files_robust(uploaded_files):
    df_list = []
    for file in uploaded_files:
        bytes_data = file.getvalue()
        # エンコード判定（cp932を優先）
        text_data = ""
        try:
            text_data = bytes_data.decode('cp932')
        except:
            try:
                text_data = bytes_data.decode('shift_jis')
            except:
                text_data = bytes_data.decode('utf-8', errors='ignore')
        
        # まず楽天として解析を試みる
        df_r = load_rakuten_parser(text_data)
        if not df_r.empty and len(df_r) > 0:
            df_list.append(df_r)
        else:
            # 楽天でなければSBIとして解析
            df_s = load_sbi_parser_greedy(text_data)
            if not df_s.empty:
                df_list.append(df_s)

    if not df_list: return pd.DataFrame()
    
    df_all = pd.concat(df_list, ignore_index=True)
    df_all = df_all.fillna(0)
    
    # 分析用タグ付け
    df_all['資産クラス'] = df_all.apply(guess_asset_class, axis=1)
    df_all['国・地域'] = df_all.apply(lambda x: guess_country(x['銘柄名'], x['種別_raw']), axis=1)
    
    return df_all

# --- メイン画面構築 ---

with st.sidebar:
    st.header("📂 データ取り込み")
    uploaded_files = st.file_uploader("楽天・SBIのCSVをまとめてアップロード", type=['csv'], accept_multiple_files=True)
    st.caption("※CSVは自動判別されます")

if uploaded_files:
    df_all = process_files_robust(uploaded_files)
    
    if not df_all.empty:
        # --- 1. サマリーKPI (万円単位) ---
        total_assets = df_all['評価額'].sum()
        total_profit = df_all['評価損益'].sum()
        
        # 単位変換（万円）
        total_assets_man = total_assets / 10000
        total_profit_man = total_profit / 10000
        fire_goal_man = FIRE_GOAL / 10000
        
        st.subheader("📈 資産サマリー")
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("総資産", f"{total_assets_man:,.0f} 万円")
        c2.metric("含み益", f"{total_profit_man:,.0f} 万円", delta_color="normal")
        c3.metric("利益率", f"{(total_profit/total_assets)*100:.1f} %")
        c4.metric("Side FIRE 達成率", f"{(total_assets/FIRE_GOAL)*100:.1f} %")
        st.progress(min(total_assets / FIRE_GOAL, 1.0))

        st.markdown("---")

        # --- 2. ポートフォリオ詳細分析 ---
        st.subheader("📊 ポートフォリオ分析")
        
        tab1, tab2, tab3 = st.tabs(["国・地域別", "資産クラス別", "口座区分別"])
        
        with tab1:
            col_a, col_b = st.columns([1, 1])
            with col_a:
                fig_sun = px.sunburst(df_all, path=['国・地域', '銘柄名'], values='評価額', title="国別・銘柄別 構成比")
                st.plotly_chart(fig_sun, use_container_width=True)
            with col_b:
                fig_pie = px.pie(df_all, values='評価額', names='国・地域', title='国・地域 比率')
                st.plotly_chart(fig_pie, use_container_width=True)

        with tab2:
            fig_bar_class = px.bar(df_all, x='資産クラス', y='評価額', color='国・地域', title='資産クラス構成')
            # 軸の単位も万円にするハックはないが、ツールチップは見やすい
            st.plotly_chart(fig_bar_class, use_container_width=True)

        with tab3:
            account_sum = df_all.groupby('口座区分')['評価額'].sum().reset_index()
            fig_bar_acc = px.bar(account_sum, x='口座区分', y='評価額', color='口座区分', title='口座区分別 資産状況')
            st.plotly_chart(fig_bar_acc, use_container_width=True)

        st.markdown("---")

        # --- 3. 配当金シミュレーション ---
        st.subheader("💰 配当金シミュレーション")
        
        # 編集用データ
        df_div = df_all[['銘柄名', '口座区分', '評価額']].copy()
        df_div['予想利回り(%)'] = df_all.apply(default_yield, axis=1)

        edited_df = st.data_editor(
            df_div,
            column_config={
                "評価額": st.column_config.NumberColumn(format="%d 円"),
                "予想利回り(%)": st.column_config.NumberColumn(format="%.1f %%", min_value=0.0, max_value=20.0, step=0.1)
            },
            hide_index=True,
            use_container_width=True,
            height=400
        )

        # 計算（万円単位で表示）
        yearly_div = (edited_df['評価額'] * (edited_df['予想利回り(%)'] / 100)).sum()
        yearly_div_man = yearly_div / 10000
        yield_avg = (yearly_div / total_assets * 100) if total_assets > 0 else 0

        c_d1, c_d2 = st.columns(2)
        c_d1.metric("年間受取配当金（税引前・予想）", f"{yearly_div_man:,.1f} 万円")
        c_d2.metric("ポートフォリオ平均利回り", f"{yield_avg:.2f} %")

        # デバッグ用：データ読み込み状況の確認
        with st.expander("データ読み込み詳細（数が合わない場合はここを確認）"):
            st.dataframe(df_all[['証券会社', '銘柄名', '評価額', '口座区分']])

    else:
        st.error("データの読み込みに失敗しました。CSVファイルが空か、形式が未対応です。")
else:
    st.info("CSVファイルをアップロードしてください。")
