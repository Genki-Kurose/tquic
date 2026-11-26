// Copyright (c) 2023 The TQUIC Authors.
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     http://www.apache.org/licenses/LICENSE-2.0
//
// Unless required by applicable law or agreed to in writing, software
// distributed under the License is distributed on an "AS IS" BASIS,
// WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
// See the License for the specific language governing permissions and
// limitations under the License.

use log::*;
use std::time::{Duration, Instant};

use crate::connection::path::PathMap;
use crate::connection::space::BufferType;
use crate::connection::space::PacketNumSpaceMap;
use crate::connection::space::SentPacket;
use crate::connection::stream::StreamMap;
use crate::frame::Frame;
use crate::multipath_scheduler::MultipathScheduler;
use crate::Error;
use crate::MultipathConfig;
use crate::Result;

/// LeoCCの状態を管理する列挙型
#[derive(Debug, PartialEq, Clone, Copy)]
enum LeoccState {
    Stable,    // 安定状態（Path 0 のみを使用）
    Redundant, // 接続再構成中（バイキャストモード）
}

/// LeoccScheduler implements a path selection algorithm inspired by the LEOCC paper.
/// It dynamically detects connection reconfiguration via Response Interval (RI) anomalies,
/// and ends the redundant mode only when the packet flow has stably recovered.
pub struct LeoccScheduler {
    state: LeoccState,
    last_rx_time: Option<Instant>,
    last_ri: Duration,
    recovery_acks: u32,

    // LeoCCに基づく設定可能な閾値パラメータ
    outage_ri_threshold: Duration, // RIがこれを越えたらOutage（再構成）とみなす
    flow_recovery_count: u32,      // 安定化を確認するための連続ACK数
}

impl LeoccScheduler {
    pub fn new(_conf: &MultipathConfig) -> LeoccScheduler {
        LeoccScheduler {
            eprintln!("[Leocc] Scheduler initialized!");
            state: LeoccState::Stable,
            last_rx_time: None,
            last_ri: Duration::from_millis(0),
            recovery_acks: 0,
            
            // パラメータの設定
            outage_ri_threshold: Duration::from_millis(200),
            flow_recovery_count: 10, // 10回連続で安定したRIを観測したら回復とみなす
        }
    }
}

impl MultipathScheduler for LeoccScheduler {
    /// Select a validated path with sufficient congestion window.
    fn on_select(
        &mut self,
        paths: &mut PathMap,
        _spaces: &mut PacketNumSpaceMap,
        _streams: &mut StreamMap,
    ) -> Result<usize> {
        let now = Instant::now();
        let mut trigger_redundant = false;

        // 1. 接続再構成の検知 (開始トリガー)
        if self.state == LeoccState::Stable {
            // (A) Response Interval (RI) の監視による Outage 検知
            if let Some(rx_time) = self.last_rx_time {
                let current_ri = now.saturating_duration_since(rx_time);
                
                // 修正: 一定時間（例: 400ms）以上無応答なら last_ri に関係なく Outage とみなす
                let hard_outage_limit = Duration::from_millis(400);
                let dynamic_trigger = current_ri > self.outage_ri_threshold
                    && current_ri > self.last_ri.saturating_mul(2);

                if dynamic_trigger || current_ri > hard_outage_limit {
                    eprintln!(
                        "[Leocc] Outage detected (current_ri: {:?}, last_ri: {:?}). Switching to Redundant.",
                        current_ri, self.last_ri
                    );
                    trigger_redundant = true;
                }
            }
        }

        // 状態遷移の実行
        if trigger_redundant {
            self.state = LeoccState::Redundant;
            self.recovery_acks = 0; // 回復確認用のカウンタをリセット
        }

        // 利用可能なパスを探索
        let mut path0_ready = false;
        let mut path1_ready = false;
        let mut any_ready_pid = None;

        for (pid, path) in paths.iter_mut() {
            if !path.active() || !path.recovery.can_send() {
                continue;
            }
            if any_ready_pid.is_none() {
                any_ready_pid = Some(pid);
            }
            if pid == 0 {
                path0_ready = true;
            } else if pid == 1 {
                path1_ready = true;
            }
        }

        // 3. 状態に応じたパスの選択
        if self.state == LeoccState::Redundant {
            // Redundantモード: on_sent で他方へ複製されるため、準備ができているパスを返す
            if path1_ready {
                return Ok(1);
            }
            if path0_ready {
                return Ok(0);
            }
        } else {
            // Stableモード: 基本的に Path 0 のみを使用
            if path0_ready {
                return Ok(0);
            }
        }

        // フォールバック: 指定パスが使えない場合は空いているパスを返す
        match any_ready_pid {
            Some(pid) => Ok(pid),
            None => Err(Error::Done),
        }
    }

    /// Try to reinject the sent packet to other available paths (Redundant behavior).
    fn on_sent(
        &mut self,
        packet: &SentPacket,
        _now: Instant,
        path_id: usize,
        paths: &mut PathMap,
        spaces: &mut PacketNumSpaceMap,
        _streams: &mut StreamMap,
    ) {
        // Redundantモード中のみパケットを複製する
        if self.state != LeoccState::Redundant {
            return;
        }

        if packet.buffer_flags.has_buffered() {
            return;
        }

        // 他のアクティブなパスにフレームをインジェクト
        for (pid, path) in paths.iter() {
            if pid == path_id || !path.active() {
                continue;
            }
            let space = match spaces.get_mut(path.space_id) {
                Some(space) => space,
                None => return,
            };
            for frame in &packet.frames {
                if let Frame::Stream { .. } = frame {
                    debug!("LeoccScheduler: inject {:?} on path {:?}", frame, pid);
                    space.buffered.push_back(frame.clone(), BufferType::High);
                }
            }
        }
    }

    /// TQUICのパケット受信ループから呼び出されるフック
    fn on_packet_received(&mut self, pid: usize, time: Instant) {
        if pid == 0 {
            if self.last_rx_time.is_none() {
                eprintln!("[Leocc] First packet received on path 0. Monitoring started.");
            }

            if let Some(last) = self.last_rx_time {
                let current_ri = time.saturating_duration_since(last);
                self.last_ri = current_ri;

                // 2. パケットフロー回復の検知 (終了トリガー)
                if self.state == LeoccState::Redundant {
                    if current_ri <= self.outage_ri_threshold {
                        self.recovery_acks += 1;
                        
                        if self.recovery_acks >= self.flow_recovery_count {
                            eprintln!(
                                "[Leocc] Flow recovered ({} stable ACKs). Switching to Stable.",
                                self.recovery_acks
                            );
                            self.state = LeoccState::Stable;
                            self.recovery_acks = 0;
                        }
                    } else {
                        self.recovery_acks = 0;
                    }
                }
            }
            self.last_rx_time = Some(time);
        }
    }
}