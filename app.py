import streamlit as st
import pandas as pd
import plotly.express as px
import io
import csv
import unicodedata

# --- ページ設定 ---
st.set_page_config(page_title="資産管理ダッシュボード", layout="wide")
st.title("📊 資産ポートフォリオ分析 & 配当管理")

# --- 設定 ---
FIRE_GOAL = 30000000  # 目標金額

# --- ユーティリティ関数 ---

def normalize_text(text):
    if not isinstance(text, str): return str(text)
    return unicodedata.normalize('NFKC', text).upper()

def clean_currency(x):
    if isinstance(x, (int, float)): return float(x)
    if isinstance(x, str):
        clean_str = x.replace(',', '').replace('円', '').replace('USD', '').replace('%', '').strip()
        try:
            return float(clean_str)
        except ValueError:
            return 0.0
    return 0.0

def detect_encoding(bytes_data):
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
    """万能CSVパーサー"""
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
    """属性推測（セクター含む）"""
    if df.empty: return df
    
    # 1. 国・地域
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
    
    # 2. 資産クラス
    def get_class(row):
        name = normalize_text(row['銘柄名'])
        cat = normalize_text(row.get('種別_raw', ''))
        if "投資信託" in cat or "ファンド" in name: return "投資信託"
        return "個別株"

    # 3. 口座区分
    def get_account(row):
        txt = normalize_text(row.get('口座区分_raw', ''))
        if "旧NISA" in txt: return "旧NISA"
        if "つみたて" in txt: return "新NISA(つみたて)"
        if "成長" in txt: return "新NISA(成長)"
        if "特定" in txt: return "特定口座"
        if "一般" in txt: return "一般口座"
        return "特定口座"

    # 4. 配当利回り（予想）
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

    # 5. セクター（業種）推測
    def get_sector(row):
        name = normalize_text(row['銘柄名'])
        ac = get_class(row)
        
        if ac == "投資信託":
            if any(x in name for x in ["S&P500", "全米", "オルカン", "全世界", "TOPIX", "日経"]): return "インデックス投信"
            if "インド" in name: return "インド株投信"
            if "債券" in name: return "債券ファンド"
            if "ゴールド" in name or "金" in name: return "コモディティ"
            if "REIT" in name: return "REIT"
            return "その他投信"
        
        # 個別株の簡易判定
        if any(x in name for x in ["銀行", "フィナンシャル", "FG", "バンク"]): return "銀行・金融"
        if any(x in name for x in ["商事", "物産", "伊藤忠", "丸紅", "双日"]): return "総合商社"
        if any(x in name for x in ["通信", "ソフトバンク", "KDDI", "NTT"]): return "情報通信"
        if any(x in name for x in ["自動車", "トヨタ", "ホンダ", "日産", "マツダ"]): return "自動車・輸送機"
        if any(x in name for x in ["製薬", "薬品", "ファーマ"]): return "医薬品"
        if any(x in name for x in ["鉄", "スチール"]): return "鉄鋼・素材"
        if any(x in name for x in ["船", "郵船", "商船"]): return "海運"
        if any(x in name for x in ["APPLE", "MICROSOFT", "NVIDIA", "AMAZON", "TESLA", "GOOGLE"]): return "米国ハイテク"
        if any(x in name for x in ["P&G", "COCA", "J&J", "MCDONALD"]): return "米国ディフェンシブ"
        
        return "その他事業"

    df['国・地域'] = df.apply(get_country, axis=1)
    df['資産クラス'] = df.apply(get_class, axis=1)
    df['口座区分'] = df.apply(get_account, axis=1)
    df['予想利回り(%)'] = df.apply(get_yield, axis=1)
    df['セクター'] = df.apply(get_sector, axis=1)
    
    # 計算項目の追加
    df['取得額'] = df['評価額'] - df['評価損益']
    
    # 税引後利益の計算（簡易計算）
    def calc_after_tax(row):
        profit = row['評価損益']
        if profit <= 0: return profit # 損失ならそのまま
        if "NISA" in row['口座区分']: return profit # NISAは非課税
        return profit * 0.79685 # 特定口座は約20%課税
    
    df['含み損益(税引後)'] = df.apply(calc_after_tax, axis=1)
    df['含み損益(税引前)'] = df['評価損益'] # 表示名統一用

    return df

# --- メイン処理 ---

# 1. サイドバー：アップロードと表示設定
with st.sidebar:
    st.header("📂 データ取り込み")
    uploaded_files = st.file_uploader("楽天・SBIのCSVをまとめてアップロード", type=['csv'], accept_multiple_files=True)
    
    st.markdown("---")
    st.header("⚙️ 表示設定")
    
    # 表示する指標の選択
    metric_options = ['評価額', '取得額', '含み損益(税引前)', '含み損益(税引後)']
    selected_metric = st.radio("分析する金額を選択:", metric_options, index=0)
    
    st.caption(f"※現在、すべてのグラフは「{selected_metric}」で表示されています。")

# 2. データ処理とメイン画面
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
        
        # 3. 銘柄選別エディタ（一番上に配置）
        st.subheader("✅ 分析対象の選択 & セクター編集")
        st.caption("チェックを外すと集計から除外されます。セクター（業種）もここで手動修正できます。")
        
        # デフォルトで全てTrueにするための列を追加
        if '分析対象' not in df_all.columns:
            df_all.insert(0, '分析対象', True)
        
        # 編集用データフレーム
        editor_cols = ['分析対象', '銘柄名', 'セクター', '評価額', '評価損益', '口座区分', '証券会社']
        
        edited_df = st.data_editor(
            df_all[editor_cols],
            column_config={
                "分析対象": st.column_config.CheckboxColumn(default=True),
                "評価額": st.column_config.NumberColumn(format="%d"),
                "評価損益": st.column_config.NumberColumn(format="%d"),
            },
            use_container_width=True,
            hide_index=True,
            height=300
        )
        
        # フィルタリング実行
        # 編集されたデータをもとに、元のdf_allをフィルタリング・更新
        target_indices = edited_df[edited_df['分析対象']].index
        # セクターの変更を反映させるために、編集結果をマージする必要があるが、
        # 簡易的にインデックスで紐づけて、表示用dfを作る
        
        # フィルタリング後のデータ
        df_filtered = df_all.iloc[edited_df.index].copy() # 行順序が同じ前提
        df_filtered['分析対象'] = edited_df['分析対象']
        df_filtered['セクター'] = edited_df['セクター'] # セクター修正を反映
        df_filtered = df_filtered[df_filtered['分析対象'] == True] # チェックがついているものだけ
        
        if df_filtered.empty:
            st.warning("分析対象が1つも選択されていません。")
        else:
            # --- 集計 ---
            # 選択された指標（selected_metric）の合計
            total_val = df_filtered[selected_metric].sum()
            
            # 評価額（KPI用）は常に計算
            total_assets = df_filtered['評価額'].sum()
            total_profit = df_filtered['含み損益(税引前)'].sum()
            
            st.markdown("---")
            st.subheader(f"📈 資産サマリー ({selected_metric} ベース)")
            
            c1, c2, c3, c4 = st.columns(4)
            c1.metric(f"合計 {selected_metric}", f"{total_val:,.0f} 円")
            
            # 常に表示しておきたい参考指標
            c2.metric("総資産評価額", f"{total_assets:,.0f} 円") 
            c3.metric("含み益(税引前)", f"{total_profit:,.0f} 円", delta_color="normal")
            c4.metric("Side FIRE 達成率", f"{(total_assets/FIRE_GOAL)*100:.1f} %")
            
            st.progress(min(total_assets / FIRE_GOAL, 1.0))

            st.markdown("---")

            # --- グラフ表示 ---
            # すべてのグラフの値は selected_metric を使用する
            
            # 共通設定関数
            def update_layout_common(fig):
                fig.update_layout(yaxis=dict(tickformat=","), xaxis=dict(tickformat=","))
                fig.update_traces(hovertemplate='%{label}: %{value:,.0f} 円')
                return fig

            tab1, tab2, tab3, tab4 = st.tabs(["セクター別", "国・地域", "資産クラス", "口座区分"])
            
            with tab1:
                st.write(f"### セクター（業種）別構成 - {selected_metric}")
                col_s1, col_s2 = st.columns(2)
                with col_s1:
                    # サンバースト（セクター -> 銘柄）
                    fig_sec = px.sunburst(df_filtered, path=['セクター', '銘柄名'], values=selected_metric, title="セクター詳細")
                    fig_sec.update_traces(textinfo="label+percent entry", hovertemplate='%{label}: %{value:,.0f} 円')
                    st.plotly_chart(fig_sec, use_container_width=True)
                with col_s2:
                    # 円グラフ
                    fig_sec_pie = px.pie(df_filtered, values=selected_metric, names='セクター', title="セクター比率")
                    fig_sec_pie.update_traces(textinfo='percent+label', hovertemplate='%{label}: %{value:,.0f} 円')
                    st.plotly_chart(fig_sec_pie, use_container_width=True)

            with tab2:
                col_a, col_b = st.columns(2)
                with col_a:
                    fig = px.sunburst(df_filtered, path=['国・地域', '銘柄名'], values=selected_metric, title=f"国別・銘柄別 ({selected_metric})")
                    fig.update_traces(textinfo="label+percent entry", hovertemplate='%{label}: %{value:,.0f} 円')
                    st.plotly_chart(fig, use_container_width=True)
                with col_b:
                    fig2 = px.pie(df_filtered, values=selected_metric, names='国・地域', title=f"国別比率 ({selected_metric})")
                    fig2.update_traces(textinfo='percent+label', hovertemplate='%{label}: %{value:,.0f} 円')
                    st.plotly_chart(fig2, use_container_width=True)
                    
            with tab3:
                fig3 = px.bar(df_filtered, x='資産クラス', y=selected_metric, color='国・地域', title=f"資産クラス内訳 ({selected_metric})")
                fig3 = update_layout_common(fig3)
                st.plotly_chart(fig3, use_container_width=True)
                
            with tab4:
                acc_grp = df_filtered.groupby('口座区分')[selected_metric].sum().reset_index()
                fig4 = px.bar(acc_grp, x='口座区分', y=selected_metric, color='口座区分', title=f"口座区分別 ({selected_metric})")
                fig4 = update_layout_common(fig4)
                st.plotly_chart(fig4, use_container_width=True)

            st.markdown("---")
            st.subheader("💰 配当金シミュレーション")
            st.caption("※配当金は「評価額」ベースで計算されます（除外した銘柄は含まれません）")

            # 編集用テーブル
            div_cols = df_filtered[['銘柄名', 'セクター', '評価額', '予想利回り(%)']].copy()
            
            edited_div_df = st.data_editor(
                div_cols,
                column_config={
                    "評価額": st.column_config.NumberColumn(format="%d"),
                    "予想利回り(%)": st.column_config.NumberColumn(format="%.1f %%", min_value=0.0, max_value=20.0, step=0.1)
                },
                use_container_width=True,
                hide_index=True,
                height=300
            )
            
            div_total = (edited_div_df['評価額'] * (edited_div_df['予想利回り(%)'] / 100)).sum()
            avg_yield = (div_total / total_assets * 100) if total_assets > 0 else 0
            
            c_d1, c_d2 = st.columns(2)
            c_d1.metric("年間受取配当（税引前・予想）", f"{div_total:,.0f} 円")
            c_d2.metric("平均利回り", f"{avg_yield:.2f} %")

    else:
        st.error("CSVデータを読み込めませんでした。")

else:
    st.info("左のサイドバーから、楽天証券とSBI証券のCSVファイルをアップロードしてください。")
