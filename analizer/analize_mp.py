import json
import sys
from collections import defaultdict

def analyze_qlog(file_path):
    # パスごとのパケット・バイト数集計
    path_stats = defaultdict(lambda: {"sent_pkts": 0, "sent_bytes": 0, "recv_pkts": 0, "recv_bytes": 0})
    # 発見されたフレームタイプのカウント
    frame_counts = defaultdict(int)
    # パス検証やアドレス制御のイベント記録
    mp_events = []

    print(f"=== qlog マルチパス解析開始: {file_path} ===")

    with open(file_path, 'r', encoding='utf-8', errors='ignore') as f:
        for line_num, line in enumerate(f, 1):
            line = line.strip().lstrip('\x1e')
            if not line:
                continue
            
            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                continue

            # qlog イベント構造の統一化 (draft-02 / 独自フォーマットの吸収)
            ev_name = ""
            ev_data = {}
            
            if isinstance(data, dict):
                ev_name = data.get("name", data.get("type", ""))
                ev_data = data.get("data", data)
            elif isinstance(data, list) and len(data) >= 3:
                # [time, category, event_type, data] 形式の場合
                ev_name = data[2]
                ev_data = data[3] if len(data) > 3 else {}

            # パケット送受信イベントのパース
            if "packet_sent" in ev_name or "packet_received" in ev_name:
                is_sent = "packet_sent" in ev_name
                
                # パス識別子 (IPペア または path_id) の取得
                raw_hdr = ev_data.get("header", {})
                size = ev_data.get("raw", {}).get("length", ev_data.get("packet_size", 0))
                
                # アドレス情報またはpath_idからパス名を特定
                path_key = ev_data.get("path_id")
                if path_key is None:
                    src = ev_data.get("src_ip", raw_hdr.get("src_ip", "unknown"))
                    dst = ev_data.get("dst_ip", raw_hdr.get("dst_ip", "unknown"))
                    path_key = f"{src} -> {dst}"

                stats = path_stats[str(path_key)]
                if is_sent:
                    stats["sent_pkts"] += 1
                    stats["sent_bytes"] += size
                else:
                    stats["recv_pkts"] += 1
                    stats["recv_bytes"] += size

                # フレームの検出 (PATH_CHALLENGE / PATH_RESPONSE / MP_ACK 等)
                frames = ev_data.get("frames", [])
                for frame in frames:
                    f_type = frame.get("frame_type", frame.get("type", "unknown"))
                    frame_counts[f_type] += 1
                    
                    if f_type in ["path_challenge", "path_response", "add_address", "remove_address", "mp_ack"]:
                        mp_events.append((line_num, ev_name, f_type, path_key))

            # 明示的なパス状態変更イベント
            elif "path_" in ev_name or "subpath" in ev_name:
                mp_events.append((line_num, ev_name, "path_event", ev_data))

    # ---- 結果の出力 ----
    print("\n【 1. パス別 通信量集計結果 】")
    if not path_stats:
        print("  ※ パケット送受信のパス情報が検出されませんでした。")
    else:
        print(f"{'Path ID / 4-Tuple':<35} | {'送信パケット':<12} | {'送信バイト(MB)':<14} | {'受信パケット':<12} | {'受信バイト(MB)':<14}")
        print("-" * 95)
        for path_id, stat in path_stats.items():
            sent_mb = stat['sent_bytes'] / (1024 * 1024)
            recv_mb = stat['recv_bytes'] / (1024 * 1024)
            print(f"{path_id:<35} | {stat['sent_pkts']:<12} | {sent_mb:<14.2f} | {stat['recv_pkts']:<12} | {recv_mb:<14.2f}")

    print("\n【 2. マルチパス制御フレーム検出数 】")
    mp_frame_found = False
    for f_name, count in frame_counts.items():
        if any(keyword in f_name.lower() for keyword in ["path", "address", "mp_ack"]):
            print(f"  ・ {f_name:<20}: {count} 回")
            mp_frame_found = True
    if not mp_frame_found:
        print("  ※ 特徴的なマルチパス制御フレーム（PATH_CHALLENGE等）は個別カウントされませんでした。")

    print("\n【 3. マルチパス判定結論 】")
    active_paths = len(path_stats)
    if active_paths >= 2:
        print(f"  🟢 SUCCESS: 複数 ({active_paths} 個) の通信パス上でパケットの送受信が確認できました。マルチパス通信が動作しています！")
    elif active_paths == 1:
        print("  🟡 WARNING: 通信は行われていますが、単一のパス（1つのIPペア）のみしか記録されていません。")
    else:
        print("  🔴 FAIL: 有効なパケット送受信ログを解析できませんでした。")

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python3 analyze_mp.py <path_to_qlog_or_sqlog>")
        sys.exit(1)
    analyze_qlog(sys.argv[1])
