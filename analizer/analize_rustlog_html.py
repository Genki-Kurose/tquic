import sys
import re
import datetime
import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots
from collections import defaultdict

def parse_quic_logs(log_file_path):
    # データ構造を「コネクションID -> パケットタイプ -> パケット番号 -> (送信時間, IP)」に変更
    # これによりマルチパス環境下でも正確なパケットの照合が可能になります
    sent_packets = defaultdict(lambda: defaultdict(dict)) 
    current_recv_type = {} # conn_id -> packet_type (ACKフレーム解析時の文脈用)
    
    rtt_data = defaultdict(list)     # { ip_address: [(time, rtt_ms)] }
    tput_data = defaultdict(list)    # { ip_address: [(time, bytes)] }

    print(f"--- 1. ログの抽出を開始: {log_file_path} ---")

    try:
        with open(log_file_path, 'r', encoding='utf-8') as f:
            for line in f:
                time_match = re.match(r'^\[(.*?)Z', line)
                if not time_match:
                    continue
                
                dt_str = time_match.group(1)
                timestamp = datetime.datetime.fromisoformat(dt_str).timestamp()

                # 1. パケット送信の記録
                # 例: SERVER-090... sent packet OneRTT ... pn=0 ... sent_size=143 ... remote=206.83.109.245...
                send_match = re.search(r'SERVER-([0-9a-fA-F]+)\s+sent packet\s+(\w+).*?pn=(\d+).*?sent_size=(\d+).*?remote=([\d\.]+)', line)
                if send_match:
                    conn_id, pkt_type, pn, size, ip = send_match.groups()
                    # どのコネクションの、どの暗号化フェーズの、何番のパケットかを正確に記録
                    sent_packets[conn_id][pkt_type][int(pn)] = (timestamp, ip)
                    tput_data[ip].append({'time': timestamp, 'bytes': int(size)})
                    continue

                # 2. 受信パケットの種類をトラッキング (直後に来るACKフレームの紐付け用)
                # 例: SERVER-090... recv packet OneRTT ...
                recv_pkt_match = re.search(r'SERVER-([0-9a-fA-F]+)\s+recv packet\s+(\w+)', line)
                if recv_pkt_match:
                    conn_id, pkt_type = recv_pkt_match.groups()
                    current_recv_type[conn_id] = pkt_type

                # 3. パケット受信時のデータサイズ記録 (RXスループット用)
                recv_bytes_match = re.search(r'SERVER recv packet (\d+) bytes PacketInfo \{ src: ([\d\.]+)', line)
                if recv_bytes_match:
                    size, ip = recv_bytes_match.groups()
                    tput_data[ip].append({'time': timestamp, 'bytes': int(size)})
                    continue

                # 4. ACK（受信確認）の記録
                # 例: SERVER-090... recv frame ACK delay=106 ranges=[0..0]
                if 'recv frame ACK' in line:
                    ack_match = re.search(r'SERVER-([0-9a-fA-F]+).*?ranges=\[(\d+)\.\.(\d+)\]', line)
                    if ack_match:
                        conn_id, start_pn, end_pn = ack_match.groups()
                        # 直前に受信したパケットのフェーズを取得（デフォルトはデータ通信のOneRTT）
                        pkt_type = current_recv_type.get(conn_id, "OneRTT")
                        
                        for pn in range(int(start_pn), int(end_pn) + 1):
                            if pn in sent_packets[conn_id][pkt_type]:
                                # 送信時の記録を取り出して削除 (pop)
                                send_time, send_ip = sent_packets[conn_id][pkt_type].pop(pn)
                                rtt_ms = (timestamp - send_time) * 1000.0
                                
                                # 異常な値（負の値や10秒以上の極端な誤検知）をフィルタリング
                                if 0 <= rtt_ms < 10000:
                                    rtt_data[send_ip].append({'time': timestamp, 'rtt_ms': rtt_ms})
                                    
    except FileNotFoundError:
        print(f"エラー: ファイル '{log_file_path}' が見つかりませんでした。")
        sys.exit(1)
                                
    print(f"抽出完了: RTTデータ {sum(len(v) for v in rtt_data.values())} 件, スループットデータ {sum(len(v) for v in tput_data.values())} 件")
    return rtt_data, tput_data

def generate_interactive_html(rtt_data, tput_data, output_file):
    print(f"--- 2. HTMLファイルを出力中: {output_file} ---")
    
    min_time = float('inf')
    for data in rtt_data.values():
        if data: min_time = min(min_time, min(d['time'] for d in data))
    for data in tput_data.values():
        if data: min_time = min(min_time, min(d['time'] for d in data))

    fig = make_subplots(rows=2, cols=1, shared_xaxes=True, 
                        subplot_titles=("RTT (ms)", "Total Throughput (Mbps)"),
                        vertical_spacing=0.1)

    colors = ['#1f77b4', '#ff7f0e', '#2ca02c', '#d62728', '#9467bd', '#8c564b']
    ip_color_map = {}
    
    def get_color(ip):
        if ip not in ip_color_map:
            ip_color_map[ip] = colors[len(ip_color_map) % len(colors)]
        return ip_color_map[ip]

    for ip, data in rtt_data.items():
        if not data: continue
        df_rtt = pd.DataFrame(data)
        df_rtt['time'] = df_rtt['time'] - min_time
        
        fig.add_trace(go.Scatter(x=df_rtt['time'], y=df_rtt['rtt_ms'], 
                                 mode='lines+markers', name=f'RTT ({ip})',
                                 line=dict(color=get_color(ip), shape='hv'),
                                 marker=dict(size=4)),
                      row=1, col=1)

    for ip, data in tput_data.items():
        if not data: continue
        df_tput = pd.DataFrame(data)
        df_tput['time'] = df_tput['time'] - min_time
        
        df_tput['time_bin'] = df_tput['time'].round(1)
        df_grouped = df_tput.groupby('time_bin')['bytes'].sum().reset_index()
        df_grouped['mbps'] = (df_grouped['bytes'] * 8) / 1_000_000 / 0.1 
        
        fig.add_trace(go.Scatter(x=df_grouped['time_bin'], y=df_grouped['mbps'], 
                                 mode='lines', name=f'Throughput ({ip})',
                                 line=dict(color=get_color(ip), shape='hv')),
                      row=2, col=1)

    fig.update_layout(height=700, title_text="tquic Multipath Performance Analysis",
                      hovermode="x unified",
                      template='plotly_white')
    
    fig.update_xaxes(title_text="Time (seconds)", row=2, col=1)
    fig.write_html(output_file)
    print(f"✅ 処理完了: グラフを '{output_file}' に出力しました。")

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("使い方: python analyze_rust_log.py <入力ログファイル>")
        sys.exit(1)
        
    input_file = sys.argv[1]
    output_file = input_file.rsplit('.', 1)[0] + "_analysis.html"
    
    rtt, tput = parse_quic_logs(input_file)
    if not rtt and not tput:
        print("⚠️ 警告: グラフ化するデータが1件も見つかりませんでした。")
    else:
        generate_interactive_html(rtt, tput, output_file)