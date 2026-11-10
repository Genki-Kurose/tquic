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

/// LeoccScheduler implements a path selection algorithm based on the LEOCC paper.
/// It maintains a stable mode (using only Path 0) and switches to a redundant mode
/// (using Path 0 and Path 1) when an outage is detected on the primary path.
pub struct LeoccScheduler {
    last_rx_time: Option<Instant>,
    reconfig_until: Option<Instant>,
    outage_threshold: Duration,
    redundant_duration: Duration,
}

impl LeoccScheduler {
    pub fn new(_conf: &MultipathConfig) -> LeoccScheduler {
        LeoccScheduler {
            last_rx_time: None,
            reconfig_until: None,
            outage_threshold: Duration::from_millis(45),
            redundant_duration: Duration::from_millis(150),
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
        let mut is_reconfiguring = false;

        // 1. 応答間隔 (RI) の外れ値検知
        if let Some(rx_time) = self.last_rx_time {
            if now.duration_since(rx_time) > self.outage_threshold {
                self.reconfig_until = Some(now + self.redundant_duration);
            }
        }

        // 2. Redundantモードの維持判定
        if let Some(until) = self.reconfig_until {
            if now <= until {
                is_reconfiguring = true;
            } else {
                self.reconfig_until = None;
            }
        }

        // 利用可能なパスを探索
        let mut path0_ready = false;
        let mut path1_ready = false;
        let mut any_ready_pid = None;

        for (pid, path) in paths.iter_mut() {
            // パスがアクティブでない、または送信枠（cwnd）がない場合はスキップ[cite: 27]
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
        if is_reconfiguring {
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
        now: Instant,
        path_id: usize,
        paths: &mut PathMap,
        spaces: &mut PacketNumSpaceMap,
        _streams: &mut StreamMap,
    ) {
        // Redundantモード中のみパケットを複製する
        let mut is_reconfiguring = false;
        if let Some(until) = self.reconfig_until {
            if now <= until {
                is_reconfiguring = true;
            }
        }

        if !is_reconfiguring {
            return;
        }

        if packet.buffer_flags.has_buffered() {
            return;
        }

        // RedundantSchedulerの実装に倣い、他のアクティブなパスにフレームをインジェクト[cite: 28]
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
    fn on_packet_received(&mut self, pid: usize, time: std::time::Instant) {
        // 元々独自メソッドにあったロジックをここに記述する
        if pid == 0 {
            self.last_rx_time = Some(time);
        }
    }
}