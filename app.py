import streamlit as st
import pandas as pd
import plotly.express as px
import io
import csv

# --- 設定エリア ---
FIRE_GOAL = 30000000  # 目標金額（3000万円）

st.set_page_config(page_title="資産管理ダッシュボード", layout="wide")
st.title("💰 資産管理 & Side FIRE ダッシュボード")

# --- 関数定義エリア ---

def clean_currency(x):
    """ '1,234円' や 'USD 10.5' などの文字列を数値(float)に変換する """
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

def load_rakuten_advanced(uploaded_file):
    """楽天証券の複雑なCSV（ヘッダーが途中にある）を読み込む"""
    try:
        # バイナリ読込 -> テキスト変換
        bytes_data = uploaded_file.getvalue()
        try:
            text_data = bytes_data.decode('shift_jis')
        except UnicodeDecodeError:
            text_data = bytes_data.decode('utf-8', errors='ignore')

        # 行ごとに分解して、「種別」と「銘柄」が含まれる行（本表のヘッダー）を探す
        lines = text_data.splitlines()
        header_row_index = -1
        
        for i, line in enumerate(lines):
            # 楽天のCSVの特徴的なヘッダー列を探す
            if '"種別"' in line and '"銘柄"' in line:
                header_row_index = i
                break
        
        if header_row_index == -1:
            st.error("楽天CSV: 商品詳細データの開始位置が見つかりませんでした。")
            return pd.DataFrame()

        # ヘッダー行以降を読み込む
        df = pd.read_csv(io.StringIO(text_data), skiprows=header_row_index)

        # 必要な列を抽出・リネーム
        # 楽天CSVの列名: "銘柄" (または "銘柄・ファンド名"), "時価評価額[円]", "評価損益[円]"
        # 提示データに合わせてマッピング
        rename_map = {
            '銘柄': '銘柄名',
            '銘柄・ファンド名': '銘柄名', # 場合によって変わる可能性対応
            '保有数量': '保有株数',
            '時価評価額[円]': '評価額',
            '評価損益[円]': '評価損益'
        }
        
        # 実際に存在する列だけでフィルタリング
        available_cols = [c for c in rename_map.keys() if c in df.columns]
        df = df[available_cols].rename(columns=rename_map)
        
        df['証券会社'] = '楽天証券'
        return df

    except Exception as e:
        st.error(f"楽天読み込みエラー: {e}")
        return pd.DataFrame()

def load_sbi_advanced(uploaded_file):
    """SBI証券の多段CSV（複数の表が結合されている）を読み込む"""
    try:
        bytes_data = uploaded_file.getvalue()
        try:
            text_data = bytes_data.decode('shift_jis')
        except UnicodeDecodeError:
            text_data = bytes_data.decode('utf-8', errors='ignore')

        # --- 特殊解析ロジック ---
        # SBIのCSVは「株式ブロック」と「投資信託ブロック」で列構成が違うため
        # 行ごとに走査してデータを拾い集める
        
        data_rows = []
        lines = text_data.splitlines()
        reader = csv.reader(lines) # CSV形式としてパース（引用符処理など）

        current_type = None # 'stock' or 'fund'
        header_map = {}     # そのブロックの {列名: インデックス}
        
        for row in reader:
            if not row: continue # 空行スキップ
            
            # 行の内容を文字列結合して判定しやすくする
            line_str = ",".join(row)

            # 1. ブロックの切り替わり判定（ヘッダー行を見つける）
            if "銘柄コード" in row and "銘柄名称" in row:
                current_type = 'stock'
                header_map = {col_name: idx for idx, col_name in enumerate(row)}
                continue
            elif "ファンド名" in row and "保有口数" in row:
                current_type = 'fund'
                header_map = {col_name: idx for idx, col_name in enumerate(row)}
                continue
            elif "合計" in line_str: # 合計行はスキップ
                current_type = None
                continue

            # 2. データ行の読み取り
            if current_type and len(row) > 3: # ある程度列がある場合のみ
                try:
                    item = {}
                    # 共通項目：銘柄名
                    if current_type == 'stock':
                        name_idx = header_map.get('銘柄名称')
                        val_idx = header_map.get('評価額')
                        pl_idx = header_map.get('評価損益')
                    else: # fund
                        name_idx = header_map.get('ファンド名')
                        val_idx = header_map.get('評価額')
                        pl_idx = header_map.get('評価損益')

                    # 必須データが取れる場合のみ追加
                    if name_idx is not None and val_idx is not None:
                        item['銘柄名'] = row[name_idx]
                        item['評価額'] = clean_currency(row[val_idx])
                        item['評価損益'] = clean_currency(row[pl_idx]) if pl_idx is not None else 0
                        item['証券会社'] = 'SBI証券'
                        data_rows.append(item)
                except Exception:
                    continue # パース失敗行は無視

        df = pd.DataFrame(data_rows)
        return df

    except Exception as e:
        st.error(f"SBI読み込みエラー: {e}")
        return pd.DataFrame()

# --- サイドバー：データアップロード ---
with st.sidebar:
    st.header("📂 データ取り込み")
    st.caption("各証券会社のCSVをアップロードしてください")
    
    file_rakuten = st.file_uploader("楽天証券 CSV", type=['csv'])
    file_sbi = st.file_uploader("SBI証券 CSV", type=['csv'])

# --- メイン処理 ---
df_list = []

if file_rakuten:
    df_r = load_rakuten_advanced(file_rakuten)
    if not df_r.empty:
        df_list.append(df_r)

if file_sbi:
    df_s = load_sbi_advanced(file_sbi)
    if not df_s.empty:
        df_list.append(df_s)

if df_list:
    # データを結合
    df_all = pd.concat(df_list, ignore_index=True)
    
    # 数値の最終クリーニング（念のため）
    df_all['評価額'] = df_all['評価額'].apply(clean_currency)
    df_all['評価損益'] = df_all['評価損益'].apply(clean_currency)
    
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
            # 円グラフ：上位15銘柄を表示し、それ以外は「その他」にまとめる
            df_pie = df_all.groupby('銘柄名')['評価額'].sum().reset_index()
            df_pie = df_pie.sort_values('評価額', ascending=False)
            
            if len(df_pie) > 15:
                top15 = df_pie.head(15)
                other_val = df_pie.iloc[15:]['評価額'].sum()
                others = pd.DataFrame([{'銘柄名': 'その他', '評価額': other_val}])
                df_pie = pd.concat([top15, others])
            
            fig = px.pie(df_pie, values='評価額', names='銘柄名', title='銘柄別構成比', hole=0.4)
            fig.update_traces(textposition='inside', textinfo='percent+label')
            st.plotly_chart(fig, use_container_width=True)

    with col_chart2:
        st.subheader("証券会社別 バランス")
        if not df_all.empty:
            fig2 = px.bar(df_all, x='証券会社', y='評価額', color='銘柄名', title='証券会社別 積み上げ')
            fig2.update_layout(showlegend=False) # 凡例が多すぎると見づらいので消す
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
