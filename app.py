import streamlit as st
import pandas as pd
import numpy as np
import plotly.express as px
import plotly.graph_objects as go
import io
import re
from datetime import datetime
from dateutil.relativedelta import relativedelta

# --- ページ設定 ---
st.set_page_config(page_title="資産管理 & サイドFIRE統合シミュレーター", layout="wide")
st.title("💎 資産管理 & サイドFIRE 統合シミュレーター")

# --- データクレンジング・解析関数 ---

def clean_num(val):
    """文字列から数値(float)へ安全に変換する"""
    if pd.isna(val):
        return 0.0
    if isinstance(val, (int, float, np.number)):
        return float(val)
    s = str(val).replace(',', '').replace('円', '').replace('USD', '').replace('US$', '').replace('%', '').replace('+', '').replace(' ', '').replace(' ', '').strip()
    if s in ['', '-', '--', '---', 'nan', 'None']:
        return 0.0
    # 全角数字・マイナス対応
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

def parse_broker_csv(uploaded_file, broker_name):
    """
    楽天証券・SBI証券の複数ブロック構造CSVを解析し、
    [証券会社, 口座区分, 銘柄名, 評価額, 取得額, 評価損益, 配当利回り] のDataFrameを返す
    """
    bytes_data = uploaded_file.getvalue()
    text_data = ""
    for enc in ['shift_jis', 'cp932', 'utf-8-sig', 'utf-8']:
        try:
            text_data = bytes_data.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    if not text_data:
        text_data = bytes_data.decode('utf-8', errors='ignore')

    lines = text_data.splitlines()
    records = []
    current_account = '特定'  # デフォルト口座
    
    # セクションごとにヘッダーとデータ行を解析
    i = 0
    n = len(lines)
    header_keywords = ['銘柄', 'ファンド', '商品名', '評価額', '評価金額', '時価評価額', '保有数量', '数量']

    while i < n:
        line = lines[i].strip()
        if not line:
            i += 1
            continue

        # セクション見出し行から口座区分を検出（例：「■特定口座」「NISA預り（成長投資枠）」など）
        acc_detect = normalize_account(line)
        if acc_detect and line.count(',') <= 3:
            current_account = acc_detect

        # ヘッダー行の判定
        hit_count = sum(1 for kw in header_keywords if kw in line)
        if hit_count >= 2 and ',' in line:
            # このブロックの終了位置を探す
            block_lines = [line]
            i += 1
            while i < n:
                next_line = lines[i].strip()
                if not next_line:
                    break
                # 次のヘッダーや別セクション見出しが来たらブロック終了
                if next_line.count(',') <= 2 and any(k in next_line for k in ['合計', 'トータル', '預り', '口座', '一覧', '■', '◆']):
                    break
                if sum(1 for kw in header_keywords if kw in next_line) >= 2 and ('銘柄' in next_line or 'ファンド' in next_line):
                    break
                block_lines.append(next_line)
                i += 1

            # ブロックをDataFrame化
            try:
                df_block = pd.read_csv(io.StringIO("\n".join(block_lines)), on_bad_lines='skip')
                
                # 列名の特定
                col_name = next((c for c in df_block.columns if any(k in str(c) for k in ['銘柄・ファンド名', '銘柄名', 'ファンド名', 'ファンド', '銘柄', '商品名']) and 'コード' not in str(c)), None)
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
                        
                        # 取得額の決定（取得額列があればそれを使用、なければ 評価額 - 評価損益 で自動算出）
                        if col_cost and clean_num(row[col_cost]) > 0:
                            cost_num = clean_num(row[col_cost])
                        else:
                            cost_num = max(val_num - pnl_num, 0.0)

                        # 口座区分の決定
                        row_acc = current_account
                        if col_acc:
                            detected = normalize_account(row[col_acc])
                            if detected:
                                row_acc = detected

                        div_yield = clean_num(row[col_div]) if col_div else 0.0

                        records.append({
                            '証券会社': broker_name,
                            '口座区分': row_acc,
                            '銘柄名': name_str,
                            '評価額': val_num,
                            '取得額': cost_num,
                            '評価損益': val_num - cost_num,
                            '配当利回り(%)': div_yield
                        })
            except Exception:
                pass
        else:
            i += 1

    return pd.DataFrame(records)

# --- サイドバー：CSVアップロード & サンプルデータ ---
with st.sidebar:
    st.header("📂 1. データ取り込み")
    file_rakuten = st.file_uploader("楽天証券 CSV", type=['csv'])
    file_sbi = st.file_uploader("SBI証券 CSV", type=['csv'])
    
    st.markdown("---")
    st.header("⚙️ 2. 配当設定データの読込")
    st.caption("過去にこのアプリで保存した配当設定付CSVがあれば読み込めます")
    file_saved = st.file_uploader("保存済みデータ CSV (任意)", type=['csv'])

    st.markdown("---")
    use_demo = st.checkbox("🧪 デモデータで試す（CSVなしで確認）", value=False)

# --- データの統合処理 ---
df_list = []

if file_saved:
    try:
        df_saved = pd.read_csv(file_saved)
        df_list.append(df_saved)
    except Exception as e:
        st.sidebar.error(f"保存データの読込エラー: {e}")

if file_rakuten:
    df_r = parse_broker_csv(file_rakuten, '楽天証券')
    if not df_r.empty:
        df_list.append(df_r)
    else:
        st.sidebar.warning("楽天証券CSVから有効な明細を検出できませんでした。")

if file_sbi:
    df_s = parse_broker_csv(file_sbi, 'SBI証券')
    if not df_s.empty:
        df_list.append(df_s)
    else:
        st.sidebar.warning("SBI証券CSVから有効な明細を検出できませんでした。")

if use_demo and not df_list:
    demo_data = [
        {'証券会社': 'SBI証券', '口座区分': 'NISA', '銘柄名': 'eMAXIS Slim 全世界株式(オール・カントリー)', '評価額': 4500000, '取得額': 3200000, '評価損益': 1300000, '配当利回り(%)': 0.0},
        {'証券会社': 'SBI証券', '口座区分': 'NISA', '銘柄名': 'バンガード・米国高配当株式ETF (VYM)', '評価額': 2800000, '取得額': 2100000, '評価損益': 700000, '配当利回り(%)': 3.1},
        {'証券会社': 'SBI証券', '口座区分': '特定', '銘柄名': '三菱商事 (8058)', '評価額': 1800000, '取得額': 1200000, '評価損益': 600000, '配当利回り(%)': 3.8},
        {'証券会社': 'SBI証券', '口座区分': '特定', '銘柄名': '三井住友フィナンシャルG (8316)', '評価額': 1500000, '取得額': 950000, '評価損益': 550000, '配当利回り(%)': 4.2},
        {'証券会社': '楽天証券', '口座区分': 'NISA', '銘柄名': '楽天・全米株式インデックス・ファンド', '評価額': 3200000, '取得額': 2400000, '評価損益': 800000, '配当利回り(%)': 0.0},
        {'証券会社': '楽天証券', '口座区分': '特定', '銘柄名': 'SPDR ポートフォリオS&P 500高配当株式ETF (SPYD)', '評価額': 2200000, '取得額': 1900000, '評価損益': 300000, '配当利回り(%)': 4.5},
        {'証券会社': '楽天証券', '口座区分': '一般', '銘柄名': '日本たばこ産業 (2914)', '評価額': 1200000, '取得額': 900000, '評価損益': 300000, '配当利回り(%)': 4.6},
    ]
    df_list.append(pd.DataFrame(demo_data))

if df_list:
    df_master = pd.concat(df_list, ignore_index=True)
    for col in ['評価額', '取得額', '評価損益', '配当利回り(%)']:
        if col not in df_master.columns:
            df_master[col] = 0.0
        df_master[col] = df_master[col].apply(clean_num)
    if '口座区分' not in df_master.columns:
        df_master['口座区分'] = '特定'
else:
    df_master = pd.DataFrame(columns=['証券会社', '口座区分', '銘柄名', '評価額', '取得額', '評価損益', '配当利回り(%)'])

# 現在の総資産額（シミュレーションの初期値に連動）
current_total_valuation = float(df_master['評価額'].sum()) if not df_master.empty else 10000000.0
current_total_cost = float(df_master['取得額'].sum()) if not df_master.empty else 8000000.0

# --- タブ構成 ---
tab1, tab2, tab3, tab4, tab5 = st.tabs([
    "📊 1. 総合ダッシュボード",
    "🎁 2. 配当データ分析",
    "📈 3. 積立シミュレーション",
    "🏖️ 4. サイドFIRE診断",
    "⚡ 5. 暴落ストレステスト & 4%ルール"
])

# ==============================================================================
# TAB 1: 総合ダッシュボード
# ==============================================================================
with tab1:
    if df_master.empty:
        st.info("👈 左のサイドバーから楽天証券・SBI証券のCSVをアップロードするか、「🧪 デモデータで試す」にチェックを入れてください。")
    else:
        total_pnl = current_total_valuation - current_total_cost
        pnl_ratio = (total_pnl / current_total_cost * 100) if current_total_cost > 0 else 0.0

        c1, c2, c3, c4 = st.columns(4)
        c1.metric("総資産額（現時点評価額）", f"{current_total_valuation:,.0f} 円")
        c2.metric("投資元本（取得額合計）", f"{current_total_cost:,.0f} 円")
        c3.metric("トータル含み損益", f"{total_pnl:+,.0f} 円", f"{pnl_ratio:+.2f} %")
        c4.metric("保有銘柄数", f"{len(df_master)} 銘柄")

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
                text_auto=',.0f', title="証券会社・口座別の資産残高"
            )
            st.plotly_chart(fig_bar, use_container_width=True)

        st.subheader("📋 全保有銘柄一覧")
        st.dataframe(df_master.sort_values('評価額', ascending=False), use_container_width=True, hide_index=True)

# ==============================================================================
# TAB 2: 配当データ分析機能
# ==============================================================================
with tab2:
    st.subheader("🎁 条件別 ポートフォリオ＆配当データ分析")
    
    if df_master.empty:
        st.warning("分析するデータがありません。サイドバーからCSVをアップロードするか、デモデータを有効にしてください。")
    else:
        # --- 1. 抽出・計算条件の選択パネル ---
        with st.container(border=True):
            st.markdown("##### 🔍 分析条件の選択")
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

        # --- 2. 配当利回りの入力・編集テーブル ---
        with st.expander("✏️ 銘柄ごとの配当利回り(%)を入力・調整する（クリックで開閉）", expanded=True):
            st.caption("※CSVに配当利回りが含まれていない場合、下の表の「配当利回り(%)」列をダブルクリックして直接入力できます。")
            
            bulk_col1, bulk_col2 = st.columns([1, 3])
            with bulk_col1:
                default_yield = st.number_input("未設定(0%)銘柄への一括仮利回り(%)", min_value=0.0, max_value=20.0, value=0.0, step=0.1)
                if st.button("0%の銘柄に一括適用"):
                    df_master.loc[df_master['配当利回り(%)'] == 0, '配当利回り(%)'] = default_yield

            edited_df = st.data_editor(
                df_master,
                column_config={
                    "配当利回り(%)": st.column_config.NumberColumn(
                        "配当利回り(%) ✏️",
                        help="時価に対する年間配当利回り(%)を入力してください",
                        min_value=0.0,
                        max_value=50.0,
                        step=0.1,
                        format="%.2f %%"
                    ),
                    "評価額": st.column_config.NumberColumn("現時点評価額(円)", format="¥%d"),
                    "取得額": st.column_config.NumberColumn("取得額(円)", format="¥%d"),
                    "評価損益": st.column_config.NumberColumn("評価損益(円)", format="¥%d"),
                },
                disabled=['証券会社', '口座区分', '銘柄名', '評価損益'],
                use_container_width=True,
                hide_index=True,
                key="dividend_editor"
            )

            # 編集済みデータのダウンロード保存機能
            csv_save = edited_df.to_csv(index=False).encode('utf-8-sig')
            st.download_button(
                label="💾 入力した配当利回りデータをCSVで保存（次回からサイドバーで読込可能）",
                data=csv_save,
                file_name="my_portfolio_with_dividend.csv",
                mime="text/csv"
            )

        # --- 3. フィルタ適用と配当計算 ---
        df_filtered = edited_df[
            (edited_df['証券会社'].isin(selected_brokers)) &
            (edited_df['口座区分'].isin(selected_accounts))
        ].copy()

        if df_filtered.empty:
            st.warning("選択された条件に一致する銘柄がありません。")
        else:
            # 年間配当額（税引前）の算出
            # ※入力された利回りを「選択した基準額（評価額 or 取得額）」に掛けるか、時価に対する配当額からYOCを出すか選択可能に
            df_filtered['基準資産額'] = df_filtered[basis_col]
            df_filtered['年間配当額(税引前)'] = df_filtered['評価額'] * (df_filtered['配当利回り(%)'] / 100.0)
            
            # 税引後配当額（NISAは100%、特定・一般は20.315%課税で79.685%）
            df_filtered['税率'] = df_filtered['口座区分'].apply(lambda x: 0.0 if x == 'NISA' else 0.20315)
            df_filtered['年間配当額(税引後)'] = df_filtered['年間配当額(税引前)'] * (1.0 - df_filtered['税率'])
            
            # 選択基準（評価額 or 取得額）に対する実質利回り
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

            # --- 4. 分析サマリーKPI ---
            sum_target_asset = df_filtered['基準資産額'].sum()
            sum_div_pre = df_filtered['年間配当額(税引前)'].sum()
            sum_div_post = df_filtered['年間配当額(税引後)'].sum()
            portfolio_yield_pre = (sum_div_pre / sum_target_asset * 100.0) if sum_target_asset > 0 else 0.0
            portfolio_yield_post = (sum_div_post / sum_target_asset * 100.0) if sum_target_asset > 0 else 0.0

            st.markdown("---")
            st.markdown(f"#### 📊 選択条件での分析結果（基準：{asset_basis}）")
            
            k1, k2, k3, k4 = st.columns(4)
            k1.metric(f"対象総資産額 ({basis_col})", f"{sum_target_asset:,.0f} 円")
            k2.metric("年間配当金（税引前）", f"{sum_div_pre:,.0f} 円", f"利回り {portfolio_yield_pre:.2f} %")
            k3.metric("年間配当金（税引後・手取り）", f"{sum_div_post:,.0f} 円", f"実質利回り {portfolio_yield_post:.2f} %")
            k4.metric("月平均の手取り配当金", f"{sum_div_post / 12.0:,.0f} 円/月")

            # --- 5. グラフ可視化 ---
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
                    st.info("配当利回り(%)が0%より大きい銘柄を入力すると、ここに配当構成比グラフが表示されます。")

            with g_col2:
                fig_acc_div = px.bar(
                    df_filtered.groupby(['証券会社', '口座区分'], as_index=False)[['基準資産額', '年間配当額(税引後)']].sum(),
                    x='口座区分', y='年間配当額(税引後)', color='証券会社', barmode='group',
                    text_auto=',.0f', title="口座区分 × 証券会社別 年間手取り配当額"
                )
                st.plotly_chart(fig_acc_div, use_container_width=True)

            st.markdown("##### 📋 条件抽出銘柄の配当詳細リスト")
            display_cols = ['証券会社', '口座区分', '銘柄名', '基準資産額', '年間配当額(税引前)', '年間配当額(税引後)', '基準対比利回り(税引前%)', '基準対比利回り(税引後%)']
            st.dataframe(
                df_filtered[display_cols].sort_values('年間配当額(税引後)', ascending=False).style.format({
                    '基準資産額': '{:,.0f} 円',
                    '年間配当額(税引前)': '{:,.0f} 円',
                    '年間配当額(税引後)': '{:,.0f} 円',
                    '基準対比利回り(税引前%)': '{:.2f} %',
                    '基準対比利回り(税引後%)': '{:.2f} %',
                }),
                use_container_width=True,
                hide_index=True
            )

# ==============================================================================
# TAB 3: 積み立て想定シミュレーション（10年・20年・30年）
# ==============================================================================
with tab3:
    st.subheader("📈 積立投資 将来資産シミュレーション（1〜30年）")
    
    with st.container(border=True):
        s_col1, s_col2, s_col3 = st.columns(3)
        with s_col1:
            sim_initial = st.number_input(
                "現在の総資産額（初期投資額・円）",
                min_value=0, max_value=1000000000,
                value=int(current_total_valuation), step=100000,
                help="CSVから読み取った総資産額が自動セットされています（自由に変更可能）"
            )
        with s_col2:
            sim_monthly = st.number_input(
                "毎月の積立額（円）",
                min_value=0, max_value=10000000,
                value=100000, step=10000
            )
        with s_col3:
            sim_rate = st.number_input(
                "想定運用利回り（年率 %）",
                min_value=0.0, max_value=30.0,
                value=5.0, step=0.1
            )

    # 月次複利で1年目〜30年目まで計算
    monthly_rate = (sim_rate / 100.0) / 12.0
    sim_rows = []
    balance = float(sim_initial)
    principal = float(sim_initial)

    for year in range(1, 31):
        prev_balance = balance
        for _ in range(12):
            balance = balance * (1.0 + monthly_rate) + sim_monthly
            principal += sim_monthly
        profit = balance - principal
        yearly_gain = balance - prev_balance
        sim_rows.append({
            '経過年数': f"{year}年目",
            '年数_数値': year,
            '元本合計(円)': round(principal),
            '運用益合計(円)': round(profit),
            '総資産想定額(円)': round(balance),
            '年間増加額(円)': round(yearly_gain)
        })

    df_sim = pd.DataFrame(sim_rows)

    # 10年・20年・30年 マイルストーン表示
    st.markdown("#### 🎯 10年・20年・30年後の到達マイルストーン")
    m10 = df_sim[df_sim['年数_数値'] == 10].iloc[0]
    m20 = df_sim[df_sim['年数_数値'] == 20].iloc[0]
    m30 = df_sim[df_sim['年数_数値'] == 30].iloc[0]

    mc1, mc2, mc3 = st.columns(3)
    mc1.metric("【10年後】総資産想定額", f"{m10['総資産想定額(円)']:,.0f} 円", f"うち運用益 +{m10['運用益合計(円)']:,.0f} 円")
    mc2.metric("【20年後】総資産想定額", f"{m20['総資産想定額(円)']:,.0f} 円", f"うち運用益 +{m20['運用益合計(円)']:,.0f} 円")
    mc3.metric("【30年後】総資産想定額", f"{m30['総資産想定額(円)']:,.0f} 円", f"うち運用益 +{m30['運用益合計(円)']:,.0f} 円")

    # 積み上げ棒グラフ（元本 vs 運用益）
    st.markdown("#### 📊 年次別 総資産推移グラフ（元本 ＋ 運用益）")
    fig_sim = go.Figure()
    fig_sim.add_trace(go.Bar(
        x=df_sim['経過年数'], y=df_sim['元本合計(円)'],
        name='投資元本（初期資産＋積立累計）', marker_color='#3b82f6'
    ))
    fig_sim.add_trace(go.Bar(
        x=df_sim['経過年数'], y=df_sim['運用益合計(円)'],
        name='複利運用益', marker_color='#10b981'
    ))
    fig_sim.update_layout(
        barmode='stack',
        xaxis_title='経過年数',
        yaxis_title='金額（円）',
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
        hovermode="x unified"
    )
    st.plotly_chart(fig_sim, use_container_width=True)

    # 年次別シミュレーション表（10年/20年/30年切り替え可能）
    st.markdown("#### 📋 年次別 総資産想定額 一覧表")
    view_span = st.radio("表示期間の切り替え", ["10年目まで", "20年目まで", "30年目まで（全期間）"], index=2, horizontal=True)
    max_y = 10 if "10" in view_span else (20 if "20" in view_span else 30)
    
    df_sim_view = df_sim[df_sim['年数_数値'] <= max_y].drop(columns=['年数_数値'])
    st.dataframe(
        df_sim_view.style.format({
            '元本合計(円)': '{:,.0f} 円',
            '運用益合計(円)': '{:,.0f} 円',
            '総資産想定額(円)': '{:,.0f} 円',
            '年間増加額(円)': '+{:,.0f} 円'
        }),
        use_container_width=True,
        hide_index=True
    )

# ==============================================================================
# TAB 4: サイドFIRE シミュレーション機能
# ==============================================================================
with tab4:
    st.subheader("🏖️ サイドFIRE 実現可能性シミュレーター")
    st.caption("現在の資産・サイドFIRE予定年月・副業収入・年金・生活費から、資産が枯渇せずに維持できるかを精緻にシミュレーションします。")

    now_dt = datetime.now()
    cur_year, cur_month = now_dt.year, now_dt.month

    with st.container(border=True):
        st.markdown("##### ⚙️ シミュレーション前提条件の入力")
        f_c1, f_c2, f_c3 = st.columns(3)

        with f_c1:
            st.markdown("**① 現在の資産と運用条件**")
            fire_init_asset = st.number_input("現在の総資産額（円）", min_value=0, max_value=1000000000, value=int(current_total_valuation), step=100000, key="f_init")
            fire_yield = st.number_input("想定運用利回り（年率 %）", min_value=0.0, max_value=25.0, value=5.0, step=0.1, key="f_yield")
            pre_fire_saving = st.number_input("サイドFIRE達成までの毎月積立額（円）", min_value=0, max_value=5000000, value=100000, step=10000)
            inflation_rate = st.number_input("想定インフレ率（生活費の上昇率・年率 %）", min_value=0.0, max_value=10.0, value=1.0, step=0.1)

        with f_c2:
            st.markdown("**② サイドFIRE・年金スケジュール**")
            sc1, sc2 = st.columns(2)
            fire_year = sc1.number_input("サイドFIRE予定（年）", min_value=cur_year, max_value=cur_year + 50, value=cur_year + 5)
            fire_month = sc2.number_input("サイドFIRE予定（月）", min_value=1, max_value=12, value=4)
            
            pc1, pc2 = st.columns(2)
            pension_year = pc1.number_input("年金受給開始（年）", min_value=cur_year, max_value=cur_year + 70, value=cur_year + 20)
            pension_month = pc2.number_input("年金受給開始（月）", min_value=1, max_value=12, value=4)
            
            sim_years_total = st.slider("シミュレーション検証期間（今後何年間を検証するか）", min_value=20, max_value=60, value=45)

        with f_c3:
            st.markdown("**③ サイドFIRE後の月次収支**")
            living_cost_monthly = st.number_input("想定生活費（月額・円）", min_value=50000, max_value=2000000, value=250000, step=10000)
            side_income_monthly = st.number_input("副業想定収入（手取り月額・円）", min_value=0, max_value=2000000, value=100000, step=10000)
            side_job_end_Option = st.selectbox("副業の継続期間", ["年金受給開始まで継続", "サイドFIRE後10年間のみ", "サイドFIRE後15年間のみ", "生涯継続"])
            pension_monthly = st.number_input("年金予定額（手取り月額・円）", min_value=0, max_value=1000000, value=150000, step=10000)

    # --- 月次キャッシュフローシミュレーション計算 ---
    start_date = datetime(cur_year, cur_month, 1)
    fire_date = datetime(int(fire_year), int(fire_month), 1)
    pension_date = datetime(int(pension_year), int(pension_month), 1)

    m_return = (fire_yield / 100.0) / 12.0
    m_inflation = (inflation_rate / 100.0) / 12.0

    asset_bal = float(fire_init_asset)
    asset_at_fire = float(fire_init_asset) if fire_date <= start_date else None
    depletion_date = None

    yearly_records = []
    total_months = sim_years_total * 12

    y_saving = 0.0
    y_side_inc = 0.0
    y_pension_inc = 0.0
    y_living_exp = 0.0
    y_inv_gain = 0.0

    for m in range(total_months):
        curr_dt = start_date + relativedelta(months=m)
        is_fired = curr_dt >= fire_date
        is_pension = curr_dt >= pension_date

        if is_fired and asset_at_fire is None:
            asset_at_fire = asset_bal

        # インフレ考慮後の生活費
        adj_living_cost = living_cost_monthly * ((1.0 + m_inflation) ** m)

        if not is_fired:
            # サイドFIRE前（資産形成期）
            inv_gain = asset_bal * m_return
            asset_bal = asset_bal + inv_gain + pre_fire_saving
            y_saving += pre_fire_saving
            y_inv_gain += inv_gain
        else:
            # サイドFIRE後
            # 副業収入の判定
            months_since_fire = (curr_dt.year - fire_date.year) * 12 + (curr_dt.month - fire_date.month)
            active_side_job = True
            if side_job_end_Option == "年金受給開始まで継続" and is_pension:
                active_side_job = False
            elif side_job_end_Option == "サイドFIRE後10年間のみ" and months_since_fire >= 120:
                active_side_job = False
            elif side_job_end_Option == "サイドFIRE後15年間のみ" and months_since_fire >= 180:
                active_side_job = False

            cur_side = side_income_monthly if active_side_job else 0.0
            cur_pen = pension_monthly if is_pension else 0.0

            net_cashflow = cur_side + cur_pen - adj_living_cost  # マイナスなら資産取り崩し
            inv_gain = asset_bal * m_return if asset_bal > 0 else 0.0
            asset_bal = asset_bal + inv_gain + net_cashflow

            if asset_bal <= 0 and depletion_date is None:
                depletion_date = curr_dt

            y_side_inc += cur_side
            y_pension_inc += cur_pen
            y_living_exp += adj_living_cost
            y_inv_gain += inv_gain

        # 12ヶ月ごと（1年単位）に集計記録
        if (m + 1) % 12 == 0:
            year_num = (m + 1) // 12
            status_str = "積立期" if not is_fired else ("年金＋サイドFIRE" if is_pension else "サイドFIRE期")
            yearly_records.append({
                '西暦': f"{curr_dt.year}年",
                '経過年': f"{year_num}年後",
                'ステータス': status_str,
                '年間積立額': round(y_saving),
                '年間副業収入': round(y_side_inc),
                '年間年金収入': round(y_pension_inc),
                '年間生活費(インフレ込)': round(y_living_exp),
                '年間運用益': round(y_inv_gain),
                '年間資産増減': round(y_saving + y_side_inc + y_pension_inc - y_living_exp + y_inv_gain),
                '年末資産残高': round(max(asset_bal, 0))
            })
            y_saving = y_side_inc = y_pension_inc = y_living_exp = y_inv_gain = 0.0

    df_fire_sim = pd.DataFrame(yearly_records)
    if asset_at_fire is None:
        asset_at_fire = asset_bal

    # --- 診断結果の表示 ---
    st.markdown("---")
    st.markdown("#### 🩺 サイドFIRE 実現可能性 判定結果")

    fire_gap_monthly = max(living_cost_monthly - side_income_monthly, 0)
    required_fire_asset_4pct = fire_gap_monthly * 12 * 25  # 不足生活費の25年分（4%ルール基準）

    if depletion_date is None and df_fire_sim.iloc[-1]['年末資産残高'] >= asset_at_fire:
        st.success(f"🎉 **【判定：大成功（余裕でサイドFIRE可能）】** {fire_year}年{fire_month}月にサイドFIREしても資産は一切枯渇せず、運用益が取り崩しを上回り資産が増え続けます！")
    elif depletion_date is None:
        st.info(f"✅ **【判定：サイドFIRE可能】** 資産を取り崩しながらも、今後{sim_years_total}年間（{cur_year + sim_years_total}年まで）資産が枯渇せず維持できます。")
    else:
        st.error(f"⚠️ **【判定：資金枯渇リスクあり】** この条件では **{depletion_date.year}年{depletion_date.month}月頃** に資産が枯渇する見込みです。サイドFIRE時期を遅らせるか、副業収入・生活費の見直しを推奨します。")

    fk1, fk2, fk3, fk4 = st.columns(4)
    fk1.metric(f"サイドFIRE時点({fire_year}/{fire_month})の想定資産", f"{asset_at_fire:,.0f} 円")
    fk2.metric("FIRE直後の毎月不足額(生活費-副業)", f"{fire_gap_monthly:,.0f} 円/月")
    fk3.metric("4%ルール上の必要目標資産", f"{required_fire_asset_4pct:,.0f} 円", f"過不足: {asset_at_fire - required_fire_asset_4pct:+,.0f} 円")
    fk4.metric(f"{sim_years_total}年後の最終資産残高", f"{df_fire_sim.iloc[-1]['年末資産残高']:,.0f} 円")

    # 推移グラフ
    fig_fire = go.Figure()
    fig_fire.add_trace(go.Scatter(
        x=df_fire_sim['西暦'], y=df_fire_sim['年末資産残高'],
        mode='lines+markers', name='年末資産残高',
        fill='tozeroy', line=dict(color='#10b981', width=3)
    ))
    fig_fire.add_vline(x=f"{fire_year}年", line_dash="dash", line_color="orange", annotation_text="サイドFIRE開始")
    if pension_year <= cur_year + sim_years_total:
        fig_fire.add_vline(x=f"{pension_year}年", line_dash="dash", line_color="blue", annotation_text="年金受給開始")
    fig_fire.update_layout(title="サイドFIRE後の長期資産残高推移", xaxis_title="年", yaxis_title="資産残高（円）", hovermode="x unified")
    st.plotly_chart(fig_fire, use_container_width=True)

    st.markdown("##### 📋 年次別 キャッシュフロー＆資産推移表")
    st.dataframe(
        df_fire_sim.style.format({
            '年間積立額': '{:,.0f} 円',
            '年間副業収入': '{:,.0f} 円',
            '年間年金収入': '{:,.0f} 円',
            '年間生活費(インフレ込)': '{:,.0f} 円',
            '年間運用益': '{:,.0f} 円',
            '年間資産増減': '{:+,.0f} 円',
            '年末資産残高': '{:,.0f} 円',
        }),
        use_container_width=True,
        hide_index=True
    )

# ==============================================================================
# TAB 5: 暴落ストレステスト & 4%ルール生活費カバー率診断（追加プロ機能）
# ==============================================================================
with tab5:
    st.subheader("⚡ プロ機能：暴落ストレステスト & 4%ルール診断")
    
    col_p1, col_p2 = st.columns(2)
    
    with col_p1:
        st.markdown("#### 📉 1. 暴落ストレステスト（金融危機シミュレーション）")
        st.caption("リーマンショックやコロナショック級の暴落が起きた際、総資産と含み損益がどうなるかを試算します。")
        
        stress_scenarios = [-10, -20, -30, -40, -50]
        stress_rows = []
        for drop in stress_scenarios:
            stressed_val = current_total_valuation * (1.0 + drop / 100.0)
            stressed_pnl = stressed_val - current_total_cost
            safe_4pct_monthly = (stressed_val * 0.04) / 12.0
            stress_rows.append({
                '暴落シナリオ': f"株価 {drop}% 下落",
                '暴落後の総資産額': round(stressed_val),
                '暴落後のトータル損益': round(stressed_pnl),
                '4%ルールでの月間取崩可能額': round(safe_4pct_monthly)
            })
        df_stress = pd.DataFrame(stress_rows)
        st.dataframe(
            df_stress.style.format({
                '暴落後の総資産額': '{:,.0f} 円',
                '暴落後のトータル損益': '{:+,.0f} 円',
                '4%ルールでの月間取崩可能額': '{:,.0f} 円/月'
            }),
            use_container_width=True,
            hide_index=True
        )

    with col_p2:
        st.markdown("#### 🛡️ 2. 4%ルール（トリニティ・スタディ）生活費カバー率")
        st.caption("現在の資産を年4%で取り崩した場合、想定生活費の何%をカバーできているかを診断します。")
        
        target_living = st.number_input("現在の月間生活費（円）", min_value=50000, max_value=2000000, value=250000, step=10000, key="p_living")
        monthly_4pct = (current_total_valuation * 0.04) / 12.0
        coverage_rate = (monthly_4pct / target_living * 100.0) if target_living > 0 else 0.0
        
        st.metric("現在の4%ルール安全取崩額（月額）", f"{monthly_4pct:,.0f} 円 / 月", f"年間 {current_total_valuation * 0.04:,.0f} 円")
        st.write(f"**生活費カバー率: {coverage_rate:.1f} %** （完全FIRE達成＝100%、サイドFIRE目安＝50%）")
        st.progress(min(coverage_rate / 100.0, 1.0))
        
        if coverage_rate >= 100:
            st.success("🎉 現在の資産だけで、4%ルールによる完全FIRE（生活費100%カバー）が可能な水準です！")
        elif coverage_rate >= 50:
            st.info("👍 生活費の半分以上を資産所得でカバーできており、サイドFIRE射程圏内に入っています！")
        else:
            need_more = (target_living * 12 * 25 * 0.5) - current_total_valuation
            st.write(f"💡 サイドFIRE目安（生活費の50%カバー）まで、あと **{max(need_more, 0):,.0f} 円** です。")
