import sys
import json
import csv
import os
import pandas as pd
import numpy as np
import plotly.graph_objects as go
from plotly.subplots import make_subplots
from scipy.signal import find_peaks

THRESHOLD_COEF = 3.0
RESAMPLE_RULE = '50ms'
RESAMPLE_SEC = 0.05  # 50ms = 0.05sec

def extract_to_temp_csv(input_qlog, temp_metrics, temp_tx, temp_rx):
    """qlogからメトリクスとパケット送受信データをそれぞれ抽出する"""
    target_columns = [
        'time_sec', 'min_rtt', 'smoothed_rtt', 'latest_rtt', 
        'bytes_in_flight', 'congestion_window', 'pacing_rate'
    ]
    
    print("--- 1. ログの抽出を開始 ---")
    line_count = 0
    
    with open(input_qlog, 'r', encoding='utf-8') as f_in, \
         open(temp_metrics, 'w', newline='', encoding='utf-8') as f_m, \
         open(temp_tx, 'w', newline='', encoding='utf-8') as f_tx, \
         open(temp_rx, 'w', newline='', encoding='utf-8') as f_rx:
        
        writer_m = csv.DictWriter(f_m, fieldnames=target_columns, extrasaction='ignore')
        writer_m.writeheader()
        
        writer_tx = csv.DictWriter(f_tx, fieldnames=['time_sec', 'bytes'])
        writer_tx.writeheader()
        
        writer_rx = csv.DictWriter(f_rx, fieldnames=['time_sec', 'bytes'])
        writer_rx.writeheader()
        
        for line in f_in:
            line_count += 1
            if line_count % 5000000 == 0:
                print(f"  ... {line_count} 行読み込み完了")
                
            line = line.strip()
            if not line:
                continue
            if line.startswith('\x1e'):
                line = line[1:]
                
            try:
                record = json.loads(line)
                name = record.get("name")
                time_ms = record.get("time", 0.0)
                time_sec = time_ms / 1000.0
                data = record.get("data", {})
                
                # 複数のqlogフォーマットのイベント名に対応
                if name in ["quic:recovery_metrics_updated", "recovery:metrics_updated"]:
                    data["time_sec"] = time_sec
                    writer_m.writerow(data)
                
                elif name in ["transport:packet_sent", "quic:packet_sent"]:
                    # IETF標準とドラフト版の両方のフォーマットに対応
                    size = data.get("raw", {}).get("length") or data.get("header", {}).get("packet_size") or 0
                    if size > 0:
                        writer_tx.writerow({'time_sec': time_sec, 'bytes': size})
                        
                elif name in ["transport:packet_received", "quic:packet_received"]:
                    size = data.get("raw", {}).get("length") or data.get("header", {}).get("packet_size") or 0
                    if size > 0:
                        writer_rx.writerow({'time_sec': time_sec, 'bytes': size})
                        
            except json.JSONDecodeError:
                continue
                
    print(f"抽出完了: 全 {line_count} 行を処理しました。")

def calculate_frequency_info(event_times):
    count = len(event_times)
    if count > 1:
        intervals = np.diff(event_times)
        avg_interval = np.mean(intervals)
        return count, avg_interval, f"{count}回 (平均{avg_interval:.2f}s毎)"
    elif count == 1:
        return count, 0.0, f"1回のみ"
    else:
        return 0, 0.0, "0回"

def plot_series(fig, times_sec, values, display_name, is_rtt):
    """波形とピーク/ドロップを描画する共通関数"""
    fig.add_trace(
        go.Scattergl(x=times_sec, y=values, mode='lines', name=f'{display_name}', visible=True),
        secondary_y=is_rtt
    )

    mean_val = np.mean(values)
    std_val = np.std(values)
    if std_val == 0:
        return

    peak_threshold = mean_val + THRESHOLD_COEF * std_val
    drop_threshold = THRESHOLD_COEF * std_val 

    # ピーク
    peaks, _ = find_peaks(values, height=peak_threshold, distance=20)
    if len(peaks) > 0:
        p_times, p_vals = times_sec[peaks], values[peaks]
        _, _, p_label = calculate_frequency_info(p_times)
        
        hover_texts = [f"<b>{display_name} Peak #{i+1}</b><br>時刻: {p_times[i]:.2f}s<br>値: {p_vals[i]:.2f}" for i in range(len(p_times))]
        fig.add_trace(
            go.Scattergl(
                x=p_times, y=p_vals, mode='markers', name=f'{display_name} Peaks [{p_label}]',
                marker=dict(symbol='triangle-up', size=10, color='rgba(255, 0, 0, 0.8)'),
                text=hover_texts, hoverinfo='text', visible='legendonly'
            ), secondary_y=is_rtt
        )
        print(f"  • {display_name} ピーク: {p_label}")

    # ドロップ
    valleys, _ = find_peaks(-values, prominence=drop_threshold, distance=20)
    if len(valleys) > 0:
        v_times, v_vals = times_sec[valleys], values[valleys]
        _, _, v_label = calculate_frequency_info(v_times)
        
        hover_texts = [f"<b>{display_name} Drop #{i+1}</b><br>時刻: {v_times[i]:.2f}s<br>値: {v_vals[i]:.2f}" for i in range(len(v_times))]
        fig.add_trace(
            go.Scattergl(
                x=v_times, y=v_vals, mode='markers', name=f'{display_name} Drops [{v_label}]',
                marker=dict(symbol='triangle-down', size=10, color='rgba(0, 0, 255, 0.8)'),
                text=hover_texts, hoverinfo='text', visible='legendonly'
            ), secondary_y=is_rtt
        )
        print(f"  • {display_name} ドロップ: {v_label}")

def process_throughput(csv_file, name_label, fig):
    """送受信パケットからMbpsを算出して描画する"""
    if not os.path.exists(csv_file) or os.path.getsize(csv_file) < 50: # ヘッダのみ対策
        return
        
    df = pd.read_csv(csv_file)
    if df.empty: return
    
    df.index = pd.to_timedelta(df['time_sec'], unit='s')
    # 50msで合計バイト数を算出
    ts_bytes = df['bytes'].resample(RESAMPLE_RULE).sum().fillna(0)
    
    # Mbpsに変換: (bytes * 8) / seconds / 1,000,000
    ts_mbps = (ts_bytes * 8) / RESAMPLE_SEC / 1_000_000
    
    times_sec = ts_mbps.index.total_seconds().values
    values = ts_mbps.values
    
    plot_series(fig, times_sec, values, name_label, is_rtt=False)

def analyze_and_plot(input_qlog, output_html):
    base_name = input_qlog.rsplit('.', 1)[0]
    temp_m = base_name + "_metrics.csv"
    temp_tx = base_name + "_tx.csv"
    temp_rx = base_name + "_rx.csv"
    
    extract_to_temp_csv(input_qlog, temp_m, temp_tx, temp_rx)
    
    print("\n--- 2. データの解析と描画を開始 (50ms間隔) ---")
    fig = make_subplots(specs=[[{"secondary_y": True}]])
    print("\n【 検出結果サマリー 】")

    # 1. スループットの解析と描画
    process_throughput(temp_tx, "TX Throughput (Mbps)", fig)
    process_throughput(temp_rx, "RX Throughput (Mbps)", fig)

    # 2. 既存メトリクスの解析と描画
    metrics_to_plot = [
        'min_rtt', 'smoothed_rtt', 'latest_rtt', 
        'bytes_in_flight', 'congestion_window', 'pacing_rate'
    ]

    for col in metrics_to_plot:
        try:
            df_col = pd.read_csv(temp_m, usecols=['time_sec', col]).dropna()
        except ValueError:
            continue
            
        if df_col.empty: continue

        df_ts = pd.DataFrame({col: df_col[col].values}, index=pd.to_timedelta(df_col['time_sec'], unit='s'))
        df_ts = df_ts[~df_ts.index.duplicated(keep='last')]
        
        ts_resampled = df_ts[col].resample(RESAMPLE_RULE).ffill().dropna()
        
        plot_series(fig, ts_resampled.index.total_seconds().values, ts_resampled.values, col, is_rtt=('rtt' in col.lower()))

    fig.update_layout(
        title='QUIC Analysis (Throughput & Metrics)',
        xaxis_title='Time [Seconds]',
        hovermode='x unified',
        template='plotly_white',
        dragmode='pan',
        height=800,
        legend=dict(title="Metrics (Click to toggle)", yanchor="top", y=0.99, xanchor="left", x=1.02)
    )

    fig.update_yaxes(title_text="Bytes / Mbps / Rate", secondary_y=False)
    fig.update_yaxes(title_text="RTT [ms]", secondary_y=True)

    print("\n--- 3. HTMLファイルを出力中 ---")
    fig.write_html(output_html, config={'scrollZoom': True, 'displaylogo': False})
    print(f"✅ 処理完了: グラフを '{output_html}' に出力しました。")
    
    # 一時ファイルのクリーンアップ
    for tmp in [temp_m, temp_tx, temp_rx]:
        if os.path.exists(tmp): os.remove(tmp)

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("使い方: python analyze_qlog.py <入力qlogファイル>")
        sys.exit(1)
        
    input_file = sys.argv[1]
    output_file = input_file.rsplit('.', 1)[0] + "_metrics_analysis.html"
    analyze_and_plot(input_file, output_file)
