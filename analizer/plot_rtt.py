import sys
import re
import matplotlib.pyplot as plt

if len(sys.argv) < 2:
    print("使い方: python3 plot_rtt.py <ログファイル名>")
    sys.exit(1)

log_file = sys.argv[1]
sents = []
rcvds = []

# 1. ログファイルから送信・受信の時間を抽出
with open(log_file, "r") as f:
    for line in f:
        if line.startswith("SENT"):
            m = re.search(r"SENT \(([0-9.]+)s\)", line)
            if m: sents.append(float(m.group(1)))
        elif line.startswith("RCVD"):
            m = re.search(r"RCVD \(([0-9.]+)s\)", line)
            if m: rcvds.append(float(m.group(1)))

matched_sents = set()
times = []
rtts = []

# 2. パケットロス対応のマッチング処理（RCVDから見て一番近い過去のSENTを探す）
for r in rcvds:
    # このRCVDより前で、まだマッチしていないSENTを抽出
    valid_sents = [s for s in sents if s < r and s not in matched_sents]
    if valid_sents:
        # 最も時刻が近い（一番最近の）SENTをペアとする
        best_s = max(valid_sents)
        matched_sents.add(best_s)
        rtt_ms = (r - best_s) * 1000
        times.append(best_s)  # X軸は送信時刻
        rtts.append(rtt_ms)   # Y軸はRTT

# 送信時刻順にソート
sorted_data = sorted(zip(times, rtts))
if sorted_data:
    times, rtts = zip(*sorted_data)
else:
    times, rtts = [], []

# 3. マッチしなかったSENTは「パケットロス」と判定
lost_times = [s for s in sents if s not in matched_sents]

# --- グラフの描画設定 ---
plt.figure(figsize=(12, 6))

# 成功したRTTのグラフ（青い階段グラフ＋ドット）
if times:
    plt.step(times, rtts, where='post', color='#1f77b4', linewidth=1.5, label='RTT (ms)')
    plt.plot(times, rtts, '.', color='#1f77b4', alpha=0.5) 

# パケットロスの描画（グラフ底辺の Y=0 の位置に赤いバツ印を打つ）
if lost_times:
    plt.plot(lost_times, [0]*len(lost_times), 'rx', markersize=8, label='Packet Loss', markeredgewidth=2)

plt.title(f"RTT Step Graph with Packet Loss ({log_file})")
plt.xlabel("Elapsed Time (seconds)")
plt.ylabel("RTT (ms) / Loss Marker")

# バツ印が見切れないようにY軸の下限を少し下げる
plt.ylim(bottom=-5) 

plt.grid(True, linestyle='--', alpha=0.7)
plt.legend(loc='upper right')
plt.tight_layout()

# 画像として保存
output_img = log_file.replace(".log", "_plot.png") if log_file.endswith(".log") else f"{log_file}_plot.png"
plt.savefig(output_img)

# ターミナルに解析結果のサマリーを出力
print(f"[+] グラフを保存しました: {output_img}")
print(f"    - 送信パケット数: {len(sents)}")
print(f"    - 受信パケット数: {len(matched_sents)}")
print(f"    - パケットロス数: {len(lost_times)} ({(len(lost_times)/len(sents)*100 if sents else 0):.2f}%)")
