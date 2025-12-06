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
        # カンマ、円、USD、%などを除去
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
    elif "つみたて" in txt: # 新NISAつみたて投資枠
        return "新NISA(つみたて)"
    elif "成長" in txt: # 新NISA成長投資枠
        return "新NISA(成長)"
    elif "NISA" in txt: # その他のNISA表記（文脈によるが一旦新NISA扱い）
        return "新NISA(不明)"
    elif "特定" in txt:
        return "特定口座"
    elif "一般" in txt:
        return "一般口座"
    else:
        return "特定口座" # デフォルト

def default_yield(row):
    """銘柄名から予想配当利回りを推測（全角半角対応版）"""
    name = normalize_text(row['銘柄名'])
    asset_class = row['資産クラス']
    
    # キーワードによる推定（数値は概算）
    if "高配当" in name or "VYM" in name or "HDV" in name or "SPYD" in name:
        return 3.5
    elif "REIT" in name or "リート" in name:
        return 4.0
    elif "債券" in name or "AGG" in name or "BND" in name:
        return 2.5
    elif "インド" in name:
        return 0.0 # インド株投信は配当が出ないことが多い
    elif "NASDAQ" in name or "ナスダック" in name:
        return 0.5
    elif "S&P500" in name or "SP500" in name or "全米" in name or "VTI" in name:
        return 1.3
    elif "全世界" in name or "オルカン" in name or "オール・カントリー" in name:
        return 1.5
    elif "TOPIX" in name or "日経" in name:
        return 1.8
    elif asset_class == '個別株':
        # 個別株で判定不能なものは一旦2%と置く
        return 2.0
    
    return 0.0

# --- CSV読み込みロジック ---

def load_rakuten_advanced(text_data):
    """楽天証券のCSV解析"""
    lines = text_data.splitlines()
    header_row_index = -1
    
    for i, line in enumerate(lines):
        if '"種別"' in line and '"銘柄"' in line:
            header_row_index = i
            break
    
    if header_row_index == -1:
        return pd.DataFrame()

    df = pd.read_csv(io.StringIO(text_data), skiprows=header_row_index)

    rename_map = {
        '銘柄': '銘柄名', '銘柄・ファンド名': '銘柄名',
        '保有数量': '保有数',
        '平均取得価額': '取得単価',
        '時価評価額[円]': '評価額',
        '評価損益[円]': '評価損益',
        '種別': '種別_raw',
        '口座': '口座区分_raw'
    }
    
    available_cols = [c for c in rename_map.keys() if c in df.columns]
    df = df[available_cols].rename(columns=rename_map)
    
    df['証券会社'] = '楽天証券'
    # 楽天の口座区分文字列を正規化して判定
    df['口座区分'] = df['口座区分_raw'].apply(guess_account_type)
    
    return df

def load_sbi_advanced(text_data):
    """SBI証券のCSV解析（修正版：ブロック構造対応強化）"""
    data_rows = []
    lines = text_data.splitlines()
    reader = csv.reader(lines)

    current_section = "不明"
    header_map = {}
    
    for row in reader:
        if not row: continue
        line_str = ",".join(row)

        if "合計" in line_str:
            continue
        
        # 1. セクション（口座・商品種別）の判定
        if ("株式" in line_str or "投資信託" in line_str) and \
           ("銘柄" not in line_str and "ファンド名" not in line_str):
                current_section = line_str.replace('"', '').replace(',', '')
                continue

        # 2. ヘッダー行の判定
        if "銘柄" in line_str or "ファンド名" in line_str:
            header_map = {col: idx for idx, col in enumerate(row)}
            continue

        # 3. データ行の解析
        # ※ここを修正：0番目の列（ファンド名など）を確実に拾えるようにロジック変更
        name_idx = None
        for k in ['銘柄名称', 'ファンド名', '銘柄コード/銘柄名']:
            if k in header_map:
                name_idx = header_map[k]
                break
                
        val_idx = None
        for k in ['評価額', '評価金額']:
             if k in header_map:
                 val_idx = header_map[k]
                 break
                 
        pl_idx = None
        for k in ['評価損益', '含み損益']:
            if k in header_map:
                pl_idx = header_map[k]
                break
        
        # 必要な列インデックスが見つかっており、行の長さが十分にある場合
        if name_idx is not None and val_idx is not None and len(row) > max(name_idx, val_idx):
            try:
                val_check = clean_currency(row[val_idx])
                # 明らかにデータ行でないもの（0円かつゴミデータ）を除外
                if val_check == 0 and "円" not in str(row[val_idx]) and str(row[val_idx]) != "0":
                   pass
                else:
                    item = {}
                    item['銘柄名'] = row[name_idx]
                    item['評価額'] = val_check
                    item['評価損益'] = clean_currency(row[pl_idx]) if pl_idx is not None else 0
                    item['証券会社'] = 'SBI証券'
                    
                    # セクション情報から属性を付与
                    item['種別_raw'] = '投資信託' if '投資信託' in current_section else '株式'
                    item['口座区分_raw'] = current_section
                    item['口座区分'] = guess_account_type(current_section)
                    
                    data_rows.append(item)
            except:
                continue

    return pd.DataFrame(data_rows)

def process_files(uploaded_files):
    df_list = []
    for file in uploaded_files:
        bytes_data = file.getvalue()
        try:
            text_data = bytes_data.decode('shift_jis')
        except:
            text_data = bytes_data.decode('utf-8', errors='ignore')
            
        if "楽天" in text_data or ("種別" in text_data and "保有数量" in text_data):
            df = load_rakuten_advanced(text_data)
            df_list.append(df)
        elif "保有証券一覧" in text_data or "評価損益合計" in text_data:
            df = load_sbi_advanced(text_data)
            df_list.append(df)
        else:
            # 判別不能時のフォールバック
            df = load_rakuten_advanced(text_data)
            if df.empty:
                df = load_sbi_advanced(text_data)
            if not df.empty:
                df_list.append(df)

    if not df_list:
        return pd.DataFrame()
        
    df_all = pd.concat(df_list, ignore_index=True)
    df_all['評価額'] = df_all['評価額'].apply(clean_currency)
    df_all['評価損益'] = df_all['評価損益'].apply(clean_currency)
    df_all = df_all.fillna(0)
    
    # 分析用タグ付け
    df_all['資産クラス'] = df_all.apply(guess_asset_class, axis=1)
    df_all['国・地域'] = df_all.apply(lambda x: guess_country(x['銘柄名'], x['種別_raw']), axis=1)
    
    return df_all

# --- メイン画面構築 ---

with st.sidebar:
    st.header("📂 データ取り込み")
    uploaded_files = st.file_uploader(
        "楽天・SBIのCSVをまとめてアップロード", 
        type=['csv'], 
        accept_multiple_files=True
    )
    st.caption("※CSVは自動判別されます")

if uploaded_files:
    df_all = process_files(uploaded_files)
    
    if not df_all.empty:
        # --- 1. サマリーKPI ---
        total_assets = df_all['評価額'].sum()
        total_profit = df_all['評価損益'].sum()
        
        st.subheader("📈 資産サマリー")
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("総資産評価額", f"{total_assets:,.0f} 円")
        c2.metric("含み益", f"{total_profit:,.0f} 円", delta_color="normal")
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
                fig_sun = px.sunburst(
                    df_all, path=['国・地域', '銘柄名'], values='評価額',
                    title="国別・銘柄別 構成比"
                )
                st.plotly_chart(fig_sun, use_container_width=True)
            with col_b:
                fig_pie_country = px.pie(df_all, values='評価額', names='国・地域', title='国・地域 比率')
                st.plotly_chart(fig_pie_country, use_container_width=True)

        with tab2:
            fig_bar_class = px.bar(
                df_all, x='資産クラス', y='評価額', color='国・地域', 
                title='資産クラス × 国別構成'
            )
            st.plotly_chart(fig_bar_class, use_container_width=True)

        with tab3:
            # 口座区分ごとの集計（新・旧NISAの区別確認用）
            account_sum = df_all.groupby('口座区分')['評価額'].sum().reset_index()
            fig_bar_account = px.bar(
                account_sum, x='口座区分', y='評価額', color='口座区分',
                title='口座区分ごとの資産状況（新・旧NISA区別）'
            )
            st.plotly_chart(fig_bar_account, use_container_width=True)

        st.markdown("---")

        # --- 3. 配当金シミュレーション ---
        st.subheader("💰 配当金シミュレーション")
        st.info("💡 銘柄名から利回りを自動推測しました。実態と異なる場合は表の数値を修正してください。")

        # デフォルト利回りを適用（ここで自動推測）
        df_dividend = df_all[['銘柄名', '口座区分', '評価額']].copy()
        # 計算用に全データを用いて推測
        df_dividend['予想利回り(%)'] = df_all.apply(default_yield, axis=1)

        # 編集可能なデータフレームを表示
        edited_df = st.data_editor(
            df_dividend,
            column_config={
                "評価額": st.column_config.NumberColumn(format="%d 円"),
                "予想利回り(%)": st.column_config.NumberColumn(min_value=0.0, max_value=20.0, step=0.1, format="%.1f %%")
            },
            hide_index=True,
            use_container_width=True,
            height=400,
            key="dividend_editor" # キーを指定してリロード時の挙動を安定化
        )

        # 計算結果の表示
        edited_df['年間受取額(予想)'] = edited_df['評価額'] * (edited_df['予想利回り(%)'] / 100)
        total_div = edited_df['年間受取額(予想)'].sum()
        total_yield_avg = (total_div / total_assets * 100) if total_assets > 0 else 0

        c_d1, c_d2 = st.columns(2)
        c_d1.metric("年間受取配当金（税引前・予想）", f"{total_div:,.0f} 円")
        c_d2.metric("ポートフォリオ平均利回り", f"{total_yield_avg:.2f} %")

    else:
        st.error("データの読み込みに失敗しました。")
else:
    st.info("CSVファイルをアップロードしてください。")
