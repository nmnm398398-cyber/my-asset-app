import streamlit as st
import pandas as pd
import plotly.express as px
import io
import csv
import unicodedata

# --- ページ設定 ---
st.set_page_config(page_title="資産分析Pro", layout="wide")
st.title("📊 資産ポートフォリオ分析 & 配当管理")

# --- 設定 ---
FIRE_GOAL = 30000000  # 目標金額

# --- 関数群 ---

def normalize_text(text):
    """全角・半角を統一し、大文字にする"""
    if not isinstance(text, str):
        return str(text)
    return unicodedata.normalize('NFKC', text).upper()

def clean_currency(x):
    """金額文字列を数値に変換"""
    if isinstance(x, (int, float)):
        return float(x)
    if isinstance(x, str):
        clean_str = x.replace(',', '').replace('円', '').replace('USD', '').replace('%', '').strip()
        try:
            return float(clean_str)
        except ValueError:
            return 0.0
    return 0.0

def detect_encoding(bytes_data):
    """文字コード自動判定"""
    encodings = ['cp932', 'shift_jis', 'utf-8', 'utf-16']
    for enc in encodings:
        try:
            text = bytes_data.decode(enc)
            if "銘柄" in text or "ファンド" in text or "評価" in text or "保有" in text:
                return text
        except:
            continue
    return bytes_data.decode('utf-8', errors='ignore')

def universal_parser(text_data):
    """万能スキャンパーサー"""
    data_rows = []
    lines = text_data.splitlines()
    reader = csv.reader(lines)

    col_maps = {
        'name': ['銘柄', '銘柄名', '銘柄名称', 'ファンド名', '銘柄コード/銘柄名', '銘柄・ファンド名'],
        'value': ['評価額', '評価金額', '時価評価額', '時価評価額[円]', '金額'],
        'profit': ['評価損益', '含み損益', '評価損益[円]', '損益'],
        'account': ['口座', '口座区分', '預り区分', '詳細']
    }

    current_header_map = {}
    current_section = "不明"

    for row in reader:
        if not row: continue
        line_str = "".join(row)

        if ("株式" in line_str or "投資信託" in line_str or "NISA" in line_str) and \
           not any(k in line_str for k in ['銘柄', 'ファンド', '数量', '取得']):
             current_section = line_str.replace('"', '').replace(',', '').replace('合計', '').strip()

        is_header = False
        temp_map = {}
        for idx, col_val in enumerate(row):
            col_val_clean = col_val.replace('"', '').strip()
            temp_map[col_val_clean] = idx
            if any(k in col_val_clean for k in col_maps['name']):
                is_header = True
        
        if is_header:
            current_header_map = temp_map
            continue

        if not current_header_map: continue

        name_idx = next((current_header_map[k] for k in col_maps['name'] if k in current_header_map), None)
        val_idx = next((current_header_map[k] for k in col_maps['value'] if k in current_header_map), None)
        pl_idx = next((current_header_map[k] for k in col_maps['profit'] if k in current_header_map), None)
        acc_idx = next((current_header_map[k] for k in col_maps['account'] if k in current_header_map), None)

        if name_idx is not None and val_idx is not None:
            if len(row) > max(name_idx, val_idx):
                try:
                    name_val = row[name_idx].strip()
                    val_float = clean_currency(row[val_idx])
                    
                    if not name_val or (val_float == 0 and row[val_idx].strip() == ""):
                        continue
                    
                    acc_val = row[acc_idx] if acc_idx is not None and len(row) > acc_idx else current_section
                    
                    item = {
                        '銘柄名': name_val,
                        '評価額': val_float,
                        '評価損益': clean_currency(row[pl_idx]) if pl_idx is not None and len(row) > pl_idx else 0,
                        '口座区分_raw': acc_val,
                        '種別_raw': '投資信託' if '投資信託' in current_section or 'ファンド' in name_val else '株式'
                    }
                    data_rows.append(item)
                except:
                    continue

    df = pd.DataFrame(data_rows)
    if not df.empty:
        if any(k in current_header_map for k in ['銘柄コード・ティッカー', '時価評価額[円]']):
            df['証券会社'] = '楽天証券'
        else:
            df['証券会社'] = 'SBI証券'
            
    return df

def guess_attributes(df):
    """属性推測（国、資産クラス、口座区分、利回り）"""
    if df.empty: return df
    
    def get_country(row):
        name = normalize_text(row['銘柄名'])
        cat = normalize_text(row.get('種別_raw', ''))
        if any(x in name for x in ["日本", "TOPIX", "日経", "JAPAN"]) or "国内" in cat: return "日本"
        if any(x in name for x in ["米国", "S&P", "NASDAQ", "全米", "US", "AMERICA", "VYM", "SPYD"]): return "米国"
        if any(x in name for x in ["インド", "INDIA"]): return "インド"
        if any(x in name for x in ["全世界", "オール・カントリー", "オルカン", "GLOBAL"]): return "全世界"
        if "先進国" in name: return "先進国"
        if "新興国" in name: return "新興国"
        return "その他"
    
    def get_class(row):
        name = normalize_text(row['銘柄名'])
        cat = normalize_text(row.get('種別_raw', ''))
        if "投資信託" in cat or "ファンド" in name: return "投資信託"
        return "個別株"

    def get_account(row):
        txt = normalize_text(row.get('口座区分_raw', ''))
        if "旧NISA" in txt: return "旧NISA"
        if "つみたて" in txt: return "新NISA(つみたて)"
        if "成長" in txt: return "新NISA(成長)"
        if "特定" in txt: return "特定口座"
        if "一般" in txt: return "一般口座"
        return "特定口座"

    def get_yield(row):
        name = normalize_text(row['銘柄名'])
        ac = get_class(row)
        if any(x in name for x in ["高配当", "VYM", "HDV", "SPYD"]): return 3.5
        if any(x in name for x in ["REIT", "リート"]): return 4.0
        if any(x in name for x in ["債券", "AGG", "BND"]): return 2.5
        if "インド" in name or "NASDAQ" in name: return 0.0
        if any(x in name for x in ["S&P500", "SP500", "全米", "VTI"]): return 1.3
        if any(x in name for x in ["全世界", "オルカン"]): return 1.5
        if ac == "個別株": return 2.0
        return 0.0

    df['国・地域'] = df.apply(get_country, axis=1)
    df['資産クラス'] = df.apply(get_class, axis=1)
    df['口座区分'] = df.apply(get_account, axis=1)
    df['予想利回り(%)'] = df.apply(get_yield, axis=1)
    
    return df

# --- メイン処理 ---

with st.sidebar:
    st.header("📂 データ取り込み")
    uploaded_files = st.file_uploader("楽天・SBIのCSVをまとめてアップロード", type=['csv'], accept_multiple_files=True)
    st.caption("※ファイル形式は自動判別します")

if uploaded_files:
    df_list = []
    for file in uploaded_files:
        content = file.getvalue()
        text = detect_encoding(content)
        df_temp = universal_parser(text)
        if not df_temp.empty:
            df_list.append(df_temp)
    
    if df_list:
        df_all = pd.concat(df_list, ignore_index=True)
        df_all = guess_attributes(df_all)
        
        # --- 集計 ---
        total_assets = df_all['評価額'].sum()
        total_profit = df_all['評価損益'].sum()
        
        st.subheader("📈 資産サマリー")
        c1, c2, c3, c4 = st.columns(4)
        # カンマ区切りフォーマット (:,.0f) を適用
        c1.metric("総資産", f"{total_assets:,.0f} 円")
        c2.metric("含み益", f"{total_profit:,.0f} 円", delta_color="normal")
        c3.metric("利益率", f"{(total_profit/total_assets)*100:.1f} %" if total_assets else "0%")
        c4.metric("Side FIRE 達成率", f"{(total_assets/FIRE_GOAL)*100:.1f} %")
        st.progress(min(total_assets / FIRE_GOAL, 1.0))

        st.markdown("---")

        # --- グラフ表示（軸フォーマット設定追加） ---
        tab1, tab2, tab3 = st.tabs(["国・地域", "資産クラス", "口座区分"])
        
        # 共通のチャート設定（ツールチップと軸にカンマを入れる）
        def update_comma_format(fig):
            fig.update_layout(yaxis=dict(tickformat=","), xaxis=dict(tickformat=","))
            fig.update_traces(hovertemplate='%{label}: %{value:,.0f} 円')
            return fig

        with tab1:
            col_a, col_b = st.columns(2)
            with col_a:
                fig = px.sunburst(df_all, path=['国・地域', '銘柄名'], values='評価額', title="国別・銘柄別 構成")
                fig.update_traces(textinfo="label+percent entry", hovertemplate='%{label}: %{value:,.0f} 円')
                st.plotly_chart(fig, use_container_width=True)
            with col_b:
                fig2 = px.pie(df_all, values='評価額', names='国・地域', title="国別比率")
                fig2.update_traces(textinfo='percent+label', hovertemplate='%{label}: %{value:,.0f} 円')
                st.plotly_chart(fig2, use_container_width=True)
                
        with tab2:
            fig3 = px.bar(df_all, x='資産クラス', y='評価額', color='国・地域', title="資産クラス内訳")
            fig3 = update_comma_format(fig3)
            st.plotly_chart(fig3, use_container_width=True)
            
        with tab3:
            acc_grp = df_all.groupby('口座区分')['評価額'].sum().reset_index()
            fig4 = px.bar(acc_grp, x='口座区分', y='評価額', color='口座区分', title="口座区分別残高")
            fig4 = update_comma_format(fig4)
            st.plotly_chart(fig4, use_container_width=True)

        st.markdown("---")
        st.subheader("💰 配当金シミュレーション")

        # 編集用テーブル
        edit_cols = df_all[['銘柄名', '証券会社', '口座区分', '評価額', '予想利回り(%)']].copy()
        
        # テーブル編集（数値フォーマット指定）
        edited_df = st.data_editor(
            edit_cols,
            column_config={
                "評価額": st.column_config.NumberColumn(format="%d"), # 編集モードでカンマを入れると文字列扱いになる恐れがあるため、表示はシンプルに
                "予想利回り(%)": st.column_config.NumberColumn(format="%.1f %%", min_value=0.0, max_value=20.0, step=0.1)
            },
            use_container_width=True,
            hide_index=True,
            height=300
        )
        
        div_total = (edited_df['評価額'] * (edited_df['予想利回り(%)'] / 100)).sum()
        avg_yield = (div_total / total_assets * 100) if total_assets else 0
        
        c_d1, c_d2 = st.columns(2)
        # 配当金もカンマ区切りで表示
        c_d1.metric("年間受取配当（税引前・予想）", f"{div_total:,.0f} 円")
        c_d2.metric("平均利回り", f"{avg_yield:.2f} %")

    else:
        st.error("CSVデータを読み込めませんでした。")

else:
    st.info("左のサイドバーから、楽天証券とSBI証券のCSVファイルをアップロードしてください。")
