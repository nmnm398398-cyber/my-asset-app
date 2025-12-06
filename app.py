import streamlit as st
import pandas as pd
import plotly.express as px
import io

# --- 設定エリア ---
FIRE_GOAL = 30000000  # 目標金額（3000万円）

st.set_page_config(page_title="資産管理ダッシュボード", layout="wide")
st.title("💰 資産管理 & Side FIRE ダッシュボード")

# --- 関数定義エリア ---

def clean_currency(x):
    """ '1,234円' や 'USD 10.5' などの文字列を数値(float)に変換する """
    if isinstance(x, str):
        # カンマ、円、USD、%などを除去
        clean_str = x.replace(',', '').replace('円', '').replace('USD', '').replace('%', '').strip()
        try:
            return float(clean_str)
        except ValueError:
            return 0
    return x

def load_csv_smartly(uploaded_file, source_name):
    """
    CSVを賢く読み込む関数
    - 余計なヘッダー行を自動スキップ
    - Shift-JIS / UTF-8 両対応
    - エラー行を無視
    """
    try:
        # 1. まずバイナリとして読み込み、Shift-JISでデコード（失敗したらUTF-8）
        bytes_data = uploaded_file.getvalue()
        try:
            text_data = bytes_data.decode('shift_jis')
        except UnicodeDecodeError:
            text_data = bytes_data.decode('utf-8', errors='ignore')

        # 2. 行ごとに分割して、「銘柄」や「ファンド」という言葉がある行を探す
        lines = text_data.splitlines()
        header_index = 0
        found_header = False
        
        # 検索するキーワード（各社のCSVヘッダーによくある言葉）
        keywords = ['銘柄', 'ファンド', '保有数量', '評価金額', '取得単価']

        for i, line in enumerate(lines):
            # 行の中にキーワードのどれかが含まれていたら、そこをヘッダーとみなす
            if any(k in line for k in keywords):
                header_index = i
                found_header = True
                break
        
        if not found_header:
            st.warning(f"{source_name}: データ表の開始位置が見つかりませんでした。通常の読み込みを試みます。")

        # 3. 見つけたヘッダー位置からPandasで読み込む
        # on_bad_lines='skip' で列数が合わない行（注意書きなど）を無視する
        df = pd.read_csv(
            io.StringIO(text_data), 
            skiprows=header_index, 
            on_bad_lines='skip'
        )

        # 4. 列名の統一処理
        target_cols = {}
        if source_name == '楽天':
            # 楽天のパターン
            target_cols = {
                '銘柄・ファンド名': '銘柄名', 
                'ファンド名': '銘柄名',
                '銘柄名': '銘柄名',
                '評価額': '評価額', 
                '時価評価額': '評価額',
                '評価損益': '評価損益',
                'トータルリターン': '評価損益'
            }
        elif source_name == 'SBI':
            # SBIのパターン
            target_cols = {
                '銘柄名': '銘柄名',
                'ファンド名': '銘柄名',
                '銘柄コード/銘柄名': '銘柄名',
                '評価金額': '評価額',
                '評価額': '評価額', # まれにある
                '時価評価額': '評価額', # まれにある
                '評価損益': '評価損益',
                '含み損益': '評価損益'
            }

        # 存在する列だけ抽出してリネーム
        available_cols = [c for c in target_cols.keys() if c in df.columns]
        
        if not available_cols:
            st.error(f"{source_name}: 必要な列（銘柄名や評価額）が見つかりませんでした。列名: {list(df.columns)}")
            return pd.DataFrame()

        df = df[available_cols].rename(columns=target_cols)
        
        # 重複列がある場合は最初のものを採用（列名マッピングの都合）
        df = df.loc[:, ~df.columns.duplicated()]
        
        df['証券会社'] = f"{source_name}証券"
        return df

    except Exception as e:
        st.error(f"{source_name} CSV読み込みエラー詳細: {e}")
        return pd.DataFrame()

# --- サイドバー：データアップロード ---
with st.sidebar:
    st.header("📂 データ取り込み")
    st.caption("各証券会社の保有商品一覧CSVをアップロードしてください")
    
    file_rakuten = st.file_uploader("楽天証券 CSV", type=['csv'])
    file_sbi = st.file_uploader("SBI証券 CSV", type=['csv'])
    
    st.markdown("---")
    st.info("CSVのヘッダー（銘柄名などが書かれた行）を自動検出し、読み込めない行はスキップします。")

# --- メイン処理 ---
df_list = []

if file_rakuten:
    df_r = load_csv_smartly(file_rakuten, '楽天')
    if not df_r.empty:
        df_list.append(df_r)

if file_sbi:
    df_s = load_csv_smartly(file_sbi, 'SBI')
    if not df_s.empty:
        df_list.append(df_s)

if df_list:
    # データを結合
    df_all = pd.concat(df_list, ignore_index=True)
    
    # 数値変換処理
    cols_to_clean = ['評価額', '評価損益']
    for col in cols_to_clean:
        if col in df_all.columns:
            df_all[col] = df_all[col].apply(clean_currency)
    
    # 欠損値埋め
    df_all = df_all.fillna(0)

    # --- 1. KPI & 目標進捗 ---
    total_assets = df_all['評価額'].sum()
    total_profit = df_all['評価損益'].sum()
    progress = min(total_assets / FIRE_GOAL, 1.0)
    
    st.subheader("資産サマリー")
    col1, col2, col3 = st.columns(3)
    col1.metric("総資産評価額", f"{total_assets:,.0f} 円", delta=None)
    col2.metric("トータル含み益", f"{total_profit:,.0f} 円", delta_color="normal")
    col3.metric("Side FIRE 達成率", f"{progress*100:.1f} %")

    # プログレスバー
    st.write(f"目標: {FIRE_GOAL:,} 円まで、あと {FIRE_GOAL - total_assets:,.0f} 円")
    st.progress(progress)

    st.markdown("---")

    # --- 2. グラフによる可視化 ---
    col_chart1, col_chart2 = st.columns(2)
    
    with col_chart1:
        st.subheader("ポートフォリオ内訳 (銘柄別)")
        if not df_all.empty:
            fig = px.pie(df_all, values='評価額', names='銘柄名', title='銘柄別構成比', hole=0.4)
            fig.update_traces(textposition='inside', textinfo='percent+label')
            st.plotly_chart(fig, use_container_width=True)

    with col_chart2:
        st.subheader("証券会社別 バランス")
        if not df_all.empty:
            fig2 = px.bar(df_all, x='証券会社', y='評価額', color='銘柄名', title='証券会社別 積み上げ')
            st.plotly_chart(fig2, use_container_width=True)

    # --- 3. 詳細データテーブル ---
    st.subheader("保有銘柄 詳細リスト")
    st.dataframe(
        df_all.sort_values('評価額', ascending=False),
        use_container_width=True,
        hide_index=True
    )

else:
    st.info("👈 左側のサイドバーからCSVファイルをアップロードしてください。")
