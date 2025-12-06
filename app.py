import streamlit as st
import pandas as pd
import plotly.express as px

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

def load_rakuten(file):
    """楽天証券のCSV読み込み & 整形"""
    try:
        # 楽天はShift-JISが多い。ヘッダー行は通常0行目
        df = pd.read_csv(file, encoding='shift_jis')
        
        # 必要な列が存在するか確認（あくまで一例です。実際のCSVに合わせて調整が必要な場合があります）
        # 一般的な列名: '銘柄・ファンド名', '保有数量', '平均取得価額', '現在値', '評価額', '評価損益', '通貨'
        target_cols = {'銘柄・ファンド名': '銘柄名', '評価額': '評価額', '評価損益': '評価損益'}
        
        # 存在する列だけ抽出してリネーム
        available_cols = [c for c in target_cols.keys() if c in df.columns]
        df = df[available_cols].rename(columns=target_cols)
        
        df['証券会社'] = '楽天証券'
        return df
    except Exception as e:
        st.error(f"楽天CSVの読み込みエラー: {e}")
        return pd.DataFrame()

def load_sbi(file):
    """SBI証券のCSV読み込み & 整形"""
    try:
        # SBIはCSVの1行目に余計なヘッダーがある場合が多いので skiprows=0 で様子見しつつ、
        # うまくいかない場合は skiprows=1 などを試す必要があります。
        # ここでは一般的なパターンで記述します。
        df = pd.read_csv(file, encoding='shift_jis')
        
        # SBIの列名パターン（保有証券一覧などにより異なる）
        # '銘柄コード', '銘柄名', '保有株数', '現在値', '評価金額', '評価損益'
        target_cols = {'銘柄名': '銘柄名', '評価金額': '評価額', '評価損益': '評価損益'}
        
        available_cols = [c for c in target_cols.keys() if c in df.columns]
        df = df[available_cols].rename(columns=target_cols)
        
        df['証券会社'] = 'SBI証券'
        return df
    except Exception as e:
        st.error(f"SBI CSVの読み込みエラー: {e}")
        return pd.DataFrame()

# --- サイドバー：データアップロード ---
with st.sidebar:
    st.header("📂 データ取り込み")
    st.caption("各証券会社の保有商品一覧CSVをアップロードしてください")
    
    file_rakuten = st.file_uploader("楽天証券 CSV", type=['csv'])
    file_sbi = st.file_uploader("SBI証券 CSV", type=['csv'])
    
    st.markdown("---")
    st.caption("※CSVの形式が読み込めない場合は、列名を確認してコードを微修正する必要があります。")

# --- メイン処理 ---
df_list = []

if file_rakuten:
    df_r = load_rakuten(file_rakuten)
    if not df_r.empty:
        df_list.append(df_r)

if file_sbi:
    df_s = load_sbi(file_sbi)
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
        # 金額が小さいものは「その他」にまとめる処理を入れると見やすいですが、まずは全表示
        fig = px.pie(df_all, values='評価額', names='銘柄名', title='銘柄別構成比', hole=0.4)
        fig.update_traces(textposition='inside', textinfo='percent+label')
        st.plotly_chart(fig, use_container_width=True)

    with col_chart2:
        st.subheader("証券会社別 バランス")
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
    st.write("手順：")
    st.write("1. 楽天証券/SBI証券にログイン")
    st.write("2. 「保有商品一覧」などのページからCSVをダウンロード")
    st.write("3. このアプリにドラッグ＆ドロップ")