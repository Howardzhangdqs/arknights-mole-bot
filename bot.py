#!/usr/bin/env python3
"""明日方舟 Skland「打地鼠」——scrcpy 视频流版（反应 ~0.1s）。

架构：
    scrcpy H.264 流（设备端持续编码，帧龄 ~25-40ms，60fps 上限）
      → 帧即匹配分辨率（1200×540，无需缩放）
      → 模板匹配白名单 + 空场 delta 快速通道
      → scrcpy 控制通道注入触摸（~10-30ms）

    --backend maafw 可回退 MaaFw 控制器（每帧请求 ~650ms，仅应急）。

白名单策略：只点匹配到的好怪；冰爆(冰蓝)/异质(紫)绝不点击。
页面优先级：结算 > 模式 > 干员 > 游戏中。

用法:
    python bot.py                       # USB 设备, scrcpy 后端, 限时 1 局
    python bot.py --mode challenge --loop 5
    python bot.py --serial 100.70.132.60:5555
    python bot.py --backend maafw
"""
from __future__ import annotations

import argparse
import logging
import subprocess
import time
from pathlib import Path

import cv2
import numpy as np

BASE = Path(__file__).resolve().parent
TPL = BASE / "templates"
REF_W, REF_H = 2400, 1080
HALF = 0.5
MATCH_W, MATCH_H = int(REF_W * HALF), int(REF_H * HALF)   # 1200×540

CLICKS = ["drone.png", "mole_alpha.png", "mole_small.png", "cabbage.png", "watermelon.png", "giant.png"]
# 模板类型 → 变体文件（templates/{type}*.png 全部参与匹配，取各类型最佳）
import glob as _glob
CLICK_TYPES = [c[:-4] for c in CLICKS]
# 每模板阈值（键为类型名，不带 .png）：空场/干扰物实测假阳性 ≤0.57，放宽到 0.80 抓冒头早期；
# drone 例外：背景等距图案假阳性 0.86-0.89，必须 ≥0.93
TH_MAP = {"giant": 0.78}          # giant 实战形态差异大，阈值放宽
TH_DEFAULT = 0.80                 # drone 用洞列几何门控防假阳性，阈值与其他一致
# 怪物出没区（棋盘 ROI，归一化 x1,y1,x2,y2）：匹配只在子图进行；上沿 0.12 覆盖 WARNING 时偏上的巨型虫
BOARD = (0.20, 0.07, 0.85, 0.98)
A_REPLAY, A_MODE = "btn_replay.png", {"timed": "mode_timed.png", "challenge": "mode_challenge.png"}
A_OPERATOR, A_CONFIRM = "op_talulah.png", "btn_confirm.png"
TH_ANCHOR, TH_MONSTER = 0.90, 0.85
A_WINDOW_REF = {
    A_REPLAY: (1400, 400, 1750, 660),
    "mode_timed.png": (500, 600, 1000, 830),
    "mode_challenge.png": (1350, 600, 1850, 830),
    A_OPERATOR: (560, 580, 1100, 800),
    A_CONFIRM: (950, 740, 1500, 900),
}
HOLES = [(0.3308, 0.4643), (0.5036, 0.4643), (0.6764, 0.4643),
         (0.3004, 0.6494), (0.5035, 0.6494), (0.7069, 0.6494),
         (0.2724, 0.8341), (0.5036, 0.8341), (0.7348, 0.8341)]
RIM_LO, RIM_HI = np.array([85, 40, 120]), np.array([115, 255, 255])
HOLE_ROI, HOLE_DY, DELTA_TH = 150, -40, 5.0

log = logging.getLogger("mole")


# ============================ 帧源后端 ============================
class ScrcpyBackend:
    """scrcpy 视频流（快帧）+ MaaFw/MaaTouch 点击（~50ms，坐标已验证）。

    注：mysc 的 f_touch 在本机上坐标注入异常（所有点击落在固定点），
    因此触摸不走 scrcpy 控制通道，仅用其视频流。
    """

    name = "scrcpy"

    def __init__(self, adb_path: str, serial: str):
        from adbutils import adb as _adb
        from mysc_core.session import Session
        from mysc_core.video import VideoKwargs
        from mysc_core.control import ControlKwargs
        dev = _adb.device(serial=serial)
        self.latest: np.ndarray | None = None
        self.seq = 0
        self.serial = serial

        def on_frame(_n, frame):
            arr = frame.to_ndarray(format="rgb24")
            self.latest = cv2.cvtColor(arr, cv2.COLOR_RGB2BGR)
            self.seq += 1

        self.session = Session(device=dev,
                               video_kwargs=VideoKwargs(max_fps=60, max_size=1200),
                               control_kwargs=ControlKwargs(),
                               frame_update_callback=on_frame)
        # mysc 连接时会按"检测到的屏幕状态"强制设置，可能误关屏 → 主动亮屏
        if self.session.ca:
            self.session.ca.f_set_screen(True)
        t0 = time.time()
        while self.latest is None and time.time() - t0 < 5:
            time.sleep(0.05)
        if self.latest is None:
            raise SystemExit("scrcpy 流启动失败（5 秒无帧）")
        from mysc_core.utils.keys import EnumAction
        self._EA = EnumAction
        # 常驻 adb shell 池 ×3：多目标点击并行下发（设备端 input tap ~28ms 串行，3 shell 并行）
        import subprocess as _sp
        self._shs = [_sp.Popen(["adb", "-s", serial, "shell"],
                               stdin=_sp.PIPE, stdout=_sp.DEVNULL, stderr=_sp.DEVNULL)
                     for _ in range(3)]
        self._sh_idx = 0
        time.sleep(0.5)
        log.info("scrcpy 流+控制就绪: %s", serial)

    def frame(self, last_seq: int, timeout: float = 0.3):
        t0 = time.time()
        while time.time() - t0 < timeout:
            if self.seq > last_seq:
                break
            time.sleep(0.004)
        return self.latest, self.seq

    def tap(self, x_ref: float, y_ref: float):
        img = self.latest
        if img is None:
            return
        fh, fw = img.shape[:2]
        if fh > fw:
            return
        cmd = f"input tap {int(x_ref)} {int(y_ref)}\n".encode()
        sh = self._shs[self._sh_idx % len(self._shs)]
        self._sh_idx += 1
        try:
            sh.stdin.write(cmd)
            sh.stdin.flush()
        except Exception:
            # 常驻 shell 挂了 → 重建该池成员
            import subprocess as _sp
            try:
                self._shs[self._sh_idx % len(self._shs)] = _sp.Popen(
                    ["adb", "-s", self.serial, "shell"],
                    stdin=_sp.PIPE, stdout=_sp.DEVNULL, stderr=_sp.DEVNULL)
            except Exception:
                pass

    def close(self):
        for sh in self._shs:
            try:
                sh.terminate()
            except Exception:
                pass
        try:
            self.session.disconnect()
        except Exception:
            pass


class MaafwBackend:
    """MaaFw 控制器后端（应急备用，每帧 ~650ms）"""

    name = "maafw"

    def __init__(self, adb_path: str, serial: str):
        from maa.controller import AdbController
        from maa.toolkit import Toolkit
        Toolkit.init_option(str(BASE / "logs"))
        devs = Toolkit.find_adb_devices()
        dev = next((d for d in devs if d.address == serial), None)
        if dev is None:
            raise SystemExit(f"未找到设备 {serial}: {[d.address for d in devs]}")
        self.adb, self.serial = str(dev.adb_path), dev.address
        self.ctrl = AdbController(adb_path=self.adb, address=dev.address,
                                  screencap_methods=dev.screencap_methods,
                                  input_methods=dev.input_methods, config=dev.config)
        if not self.ctrl.post_connection().wait().status.succeeded:
            raise SystemExit("MaaFw 控制器连接失败")
        try:
            self.ctrl.set_screenshot_target_short_side(540)
        except Exception:
            pass
        self._img = None

    def frame(self, last_seq: int, timeout: float = 0.0):
        self.ctrl.post_screencap().wait()
        self._img = self.ctrl.cached_image
        return self._img, (last_seq + 1)

    def tap(self, x_ref: float, y_ref: float):
        img = self._img
        if img is None:
            return
        fh, fw = img.shape[:2]
        if fh > fw:
            return
        try:
            self.ctrl.post_click(int(x_ref * fw / REF_W), int(y_ref * fh / REF_H)).wait()
        except Exception:
            subprocess.run([self.adb, "-s", self.serial, "shell", "input", "tap",
                            str(int(x_ref)), str(int(y_ref))], check=False,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def close(self):
        pass


# ============================ 主逻辑 ============================
class Bot:
    def __init__(self, backend, mode: str, loop: int, record: str | None = None, capture: str | None = None):
        self.bk = backend
        self.mode, self.loop = mode, loop
        self.rounds_done = 0
        self.last_tap = (0.0, 0.0, 0.0)
        self.stop = False
        self._op_tapped = False
        self._tpl_cache: dict[str, np.ndarray] = {}
        self._baseline: np.ndarray | None = None
        self._nomatch_last = 0.0
        self._anchor_streak: dict[str, int] = {}
        self._seq = -1
        self._rot = None
        self._hits_dir = BASE / "logs" / "hits"
        self._hits_dir.mkdir(parents=True, exist_ok=True)
        self._stats = [0.0, 0]
        # 录制：识别结果渲染进帧
        self._rec = None
        self._cap_dir = None
        self._cap_last = 0.0
        self._taps: list[tuple[float, float, float, str]] = []   # (x, y, t, label)
        self._tap_hist: list[tuple[float, float, float]] = []    # 近期点击历史（去重用）
        self._last_img: np.ndarray | None = None
        self._last_acts: list = []
        self._last_cands: list = []
        if capture:
            from pathlib import Path as _P
            self._cap_dir = _P(capture)
            self._cap_dir.mkdir(parents=True, exist_ok=True)
            log.info("采集开启 → %s", capture)
        if record:
            fourcc = cv2.VideoWriter_fourcc(*"mp4v")
            self._rec = cv2.VideoWriter(record, fourcc, 15, (MATCH_W, MATCH_H))
            log.info("录制开启 → %s", record)

    # ---------- 触觉 ----------
    def tap(self, x: float, y: float, force=False, label=""):
        now = time.time()
        # 历史去重：0.85s 内同位置（<60px）不重复点——怪死后动画还会被模板命中，补刀=空点断连击
        if not force:
            for tx, ty, tt in self._tap_hist:
                if (x - tx) ** 2 + (y - ty) ** 2 < 60 ** 2 and now - tt < 0.85:
                    return
        self._tap_hist.append((x, y, now))
        self._tap_hist = [t for t in self._tap_hist if now - t[0 + 2] < 2.0]
        # 拟人化抖动：以目标中心为均值的正态分布（σ=10px，限 ±25px），模拟真人手点
        import random as _r
        jx = max(-25.0, min(25.0, _r.gauss(0, 10)))
        jy = max(-25.0, min(25.0, _r.gauss(0, 10)))
        x, y = x + jx, y + jy
        self._taps.append((x, y, now, label))
        self.bk.tap(x, y)
        self.last_tap = (x, y, now)

    # ---------- 视觉 ----------
    def tpl_path(self, path: str, coarse: bool = False) -> np.ndarray:
        """按文件路径缓存灰度半分辨率模板；coarse=True 再缩半用于粗匹配"""
        key = (path, coarse)
        if key not in self._tpl_cache:
            t = self._tpl_cache.get((path, False))
            if t is None:
                t = cv2.imread(path)
                if t is None:
                    self._tpl_cache[key] = np.zeros((10, 10), np.uint8)
                    return self._tpl_cache[key]
                w = int(t.shape[1] * HALF)
                h = int(t.shape[0] * HALF)
                t = cv2.resize(t, (w, h), interpolation=cv2.INTER_AREA)
                t = cv2.cvtColor(t, cv2.COLOR_BGR2GRAY)
                self._tpl_cache[(path, False)] = t
            if coarse:
                t = cv2.resize(t, None, fx=0.5, fy=0.5, interpolation=cv2.INTER_AREA)
                self._tpl_cache[key] = t
                return t
        return self._tpl_cache[key]

    def frame_match_res(self):
        """最新帧 → 匹配分辨率（1200×540，竖屏自动转横）"""
        img, self._seq = self.bk.frame(self._seq)
        if img is None:
            return None
        h, w = img.shape[:2]
        if h > w:
            code = self._rot or cv2.ROTATE_90_CLOCKWISE
            img = cv2.rotate(img, code)
            h, w = img.shape[:2]
        if w != MATCH_W:
            img = cv2.resize(img, (MATCH_W, int(round(h * MATCH_W / w))), interpolation=cv2.INTER_AREA)
        if img.shape[0] != MATCH_H:
            img = cv2.resize(img, (MATCH_W, MATCH_H))
        self._last_img = img
        return img

    def match(self, img, name):
        tpl = self.tpl_path(str(TPL / name))
        if tpl.ndim == 2 and img.ndim == 3:
            img = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        elif tpl.ndim == 3 and img.ndim == 2:
            img = cv2.cvtColor(img, cv2.COLOR_GRAY2BGR)
        if tpl.shape[0] > img.shape[0] or tpl.shape[1] > img.shape[1]:
            return 0.0, 0, 0
        res = cv2.matchTemplate(img, tpl, cv2.TM_CCOEFF_NORMED)
        _, mx, _, loc = cv2.minMaxLoc(res)
        return mx, (loc[0] + tpl.shape[1] // 2) / HALF, (loc[1] + tpl.shape[0] // 2) / HALF

    def board_sub(self, img):
        """棋盘灰度子图（含每帧缓存）；返回 (sub, sub_small, x1, y1)"""
        if getattr(self, "_bs_seq", None) == self._seq and getattr(self, "_bs", None) is not None:
            return self._bs
        H, W = img.shape[:2]
        x1, y1 = int(BOARD[0] * W), int(BOARD[1] * H)
        x2, y2 = int(BOARD[2] * W), int(BOARD[3] * H)
        sub = img[y1:y2, x1:x2]
        if sub.ndim == 3:
            sub = cv2.cvtColor(sub, cv2.COLOR_BGR2GRAY)
        small = cv2.resize(sub, None, fx=0.5, fy=0.5, interpolation=cv2.INTER_AREA)
        self._bs = (sub, small, x1, y1)
        self._bs_seq = self._seq
        return self._bs

    def _match_sub(self, sub, tpl, x1, y1):
        if tpl.shape[0] > sub.shape[0] or tpl.shape[1] > sub.shape[1]:
            return 0.0, 0, 0
        res = cv2.matchTemplate(sub, tpl, cv2.TM_CCOEFF_NORMED)
        _, mx, _, loc = cv2.minMaxLoc(res)
        return (mx, (loc[0] + tpl.shape[1] // 2 + x1) / HALF,
                (loc[1] + tpl.shape[0] // 2 + y1) / HALF)

    def match_board(self, img, tpl_path: str):
        """棋盘 ROI 内精匹配（全分辨率子图）"""
        sub, _, x1, y1 = self.board_sub(img)
        return self._match_sub(sub, self.tpl_path(tpl_path), x1, y1)

    def match_board_coarse(self, img, tpl_path: str, th=0.70):
        """粗匹配（子图再缩半），低于 th 直接判负；返回 (score, cx, cy)"""
        _, small, x1, y1 = self.board_sub(img)
        tpl = self.tpl_path(tpl_path, coarse=True)
        sc, cx, cy = self._match_sub(small, tpl, x1, y1)
        return (sc, cx, cy) if sc >= th else (0.0, 0, 0)

    def match_anchor(self, img, name, streak=1):
        sc, cx, cy = self.match(img, name)
        ok = sc >= TH_ANCHOR
        if ok:
            x1, y1, x2, y2 = A_WINDOW_REF[name]
            ok = x1 <= cx <= x2 and y1 <= cy <= y2
        if not ok:
            self._anchor_streak[name] = 0
            return 0.0, 0, 0
        n = self._anchor_streak.get(name, 0) + 1
        self._anchor_streak[name] = n
        return (sc, cx, cy) if n >= streak else (0.0, 0, 0)

    def in_game(self, img) -> bool:
        hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
        mask = cv2.inRange(hsv, RIM_LO, RIM_HI)
        n = 0
        for hx, hy in HOLES:
            cx, cy, r = int(hx * MATCH_W), int(hy * MATCH_H + HOLE_DY / 2), 18
            if mask[max(0, cy - r):cy + r, max(0, cx - r):cx + r].mean() > 0.12:
                n += 1
        return n >= 3

    def hole_deltas(self, img):
        acts = []
        if self._baseline is None or self._baseline.shape != img.shape:
            return acts
        for i, (hx, hy) in enumerate(HOLES):
            cx = int(hx * MATCH_W)
            cy = int(hy * MATCH_H + HOLE_DY)
            x1, y1 = max(0, cx - HOLE_ROI), max(0, cy - HOLE_ROI)
            x2 = min(img.shape[1], cx + HOLE_ROI)
            y2 = min(img.shape[0], cy + HOLE_ROI)
            d = float(cv2.absdiff(img[y1:y2, x1:x2], self._baseline[y1:y2, x1:x2]).mean())
            if d > DELTA_TH:
                acts.append((i, d, (x1, y1, x2, y2)))
        return acts

    def save_hit(self, img, name, score, cx, cy, tag="hit"):
        try:
            vis = img.copy()
            tpl = self.tpl_path(str(TPL / name))
            th, tw = tpl.shape[:2]
            x, y = int(cx * HALF - tw / 2), int(cy * HALF - th / 2)
            cv2.rectangle(vis, (x, y), (x + tw, y + th), (0, 0, 255), 2)
            cv2.putText(vis, f"{name[:-4]} {score:.2f}", (x, y - 6),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.55, (0, 0, 255), 2)
            ts = time.strftime("%H%M%S")
            cv2.imwrite(str(self._hits_dir / f"{ts}_{tag}_{name[:-4]}_{score:.2f}.jpg"), vis,
                        [cv2.IMWRITE_JPEG_QUALITY, 85])
        except Exception as e:
            log.debug("save_hit: %s", e)

    def act_on(self, img, score, name, cx, cy):
        self.save_hit(img, name, score, cx, cy)
        log.info("命中 %s (%.3f) → 点 (%d,%d)", name, score, cx, cy)
        if name == "giant.png":
            for _ in range(5):
                self.tap(cx, cy, force=True, label=name[:-4])
                time.sleep(0.04)
        else:
            self.tap(cx, cy, label=name[:-4])

    # ---------- 单帧决策 ----------
    def step(self):
        img = self.frame_match_res()
        if img is None:
            return

        # in_game 门控降频：确认在游戏后每 10 帧复查一次（省 ~5ms/帧）
        self._gskip = getattr(self, "_gskip", 0) + 1
        if not getattr(self, "_was_in", False) or self._gskip >= 10:
            self._was_in = self.in_game(img)
            self._gskip = 0
        if self._was_in:
            # 结算锚点每 8 帧查一次（省时；结算页面是稳态，晚 ~0.5s 发现无碍）
            self._frm = getattr(self, "_frm", 0) + 1
            sc, cx, cy = self.match_anchor(img, A_REPLAY) if self._frm % 8 == 0 else (0.0, 0, 0)
            if sc:
                self._baseline = None
                self.rounds_done += 1
                log.info("第 %d/%d 局结束（结算页）", self.rounds_done, self.loop)
                if self.rounds_done >= self.loop:
                    self.stop = True
                    return
                self.tap(cx, cy, force=True)
                time.sleep(2.0)
                return
            if self._baseline is None:
                self._baseline = img.copy()
                log.info("空场基线已建立")
                return
            # 两级匹配：粗匹配（全部模板，~12ms）筛出候选类型 → 仅对候选精匹配
            self._frm = getattr(self, "_frm", 0) + 1
            cands = []
            sub, _, _, _ = self.board_sub(img)
            for tname in CLICK_TYPES:
                if tname == "giant" and self._frm % 2 == 0:
                    continue
                files = sorted(_glob.glob(str(TPL / f"{tname}*.png")))
                best_t = (0.0, None, 0.0, 0.0)
                for fp in files:
                    sc, cx, cy = self.match_board_coarse(img, fp, th=0.70)
                    if sc > best_t[0]:
                        best_t = (sc, fp, cx, cy)
                if best_t[1] is None:
                    continue                      # 粗匹配全灭，跳过精匹配
                sc, cx, cy = self.match_board(img, best_t[1])
                if sc >= TH_MAP.get(tname, TH_DEFAULT):
                    if tname == "drone":
                        # 实测无人机只在洞口列上方悬停；假阳性出现在洞间空隙。洞列几何门控。
                        hole_xs = [int(hx * REF_W) for hx, _ in HOLES]
                        if min(abs(cx - hx) for hx in hole_xs) > 90:
                            continue
                        cy += 12  # 实测无人机框系统性偏上 ~15px
                    cands.append((sc, tname + ".png", cx, cy))
            cands.sort(reverse=True)
            self._last_cands = cands
            clicked: list[tuple[float, float]] = []
            for sc, name, cx, cy in cands:
                if any((cx - a) ** 2 + (cy - b) ** 2 < 80 ** 2 for a, b in clicked):
                    continue
                clicked.append((cx, cy))
                self.act_on(img, sc, name, cx, cy)
            return

        # 非游戏画面：入场流程
        self._baseline = None
        sc, cx, cy = self.match_anchor(img, A_REPLAY)
        if sc:
            self.rounds_done += 1
            log.info("第 %d/%d 局结束（结算页）", self.rounds_done, self.loop)
            if self.rounds_done >= self.loop:
                self.stop = True
                return
            self.tap(cx, cy, force=True)
            time.sleep(2.0)
            return
        sc, cx, cy = self.match_anchor(img, A_MODE[self.mode], streak=2)
        if sc:
            log.info("模式页 → 点击 %s", self.mode)
            self.tap(cx, cy, force=True)
            time.sleep(2.0)
            return
        sc, cx, cy = self.match_anchor(img, A_OPERATOR, streak=2)
        if sc and not self._op_tapped:
            log.info("干员页 → 选择左干员")
            self.tap(cx, cy, force=True)
            self._op_tapped = True
            time.sleep(0.8)
            return
        sc, cx, cy = self.match_anchor(img, A_CONFIRM, streak=2)
        if sc:
            log.info("干员页 → 确认进入游戏")
            self.tap(cx, cy, force=True)
            self._op_tapped = False
            time.sleep(1.5)
            return

        # WARNING/Boss 全屏会盖住洞口（rim 门控失效走到这里）→ giant 直接匹配+连点
        gpath = str(TPL / "giant.png")
        sc, cx, cy = self.match_board(img, gpath)
        if sc >= 0.78:
            log.info("Boss/WARNING (%.3f) → 连点 (%d,%d)", sc, cx, cy)
            self.save_hit(img, "giant.png", sc, cx, cy)
            for _ in range(6):
                self.tap(cx, cy, force=True, label="boss")
                time.sleep(0.05)

    def render_frame(self, loop_ms: float):
        """把识别结果画到帧上并写视频 + 采集干净帧"""
        if self._rec is None and self._cap_dir is None:
            return
        if self._cap_dir is not None and self._last_img is not None:
            now = time.time()
            if now - self._cap_last > 0.33:
                self._cap_last = now
                ts = time.strftime("%H%M%S") + f"_{int(now*1000)%1000:03d}"
                cv2.imwrite(str(self._cap_dir / f"{ts}.jpg"), self._last_img,
                            [cv2.IMWRITE_JPEG_QUALITY, 85])
        if self._rec is None or self._last_img is None:
            return
        vis = self._last_img.copy()
        now = time.time()
        # 激活洞：黄框
        for i, d, (x1, y1, x2, y2) in self._last_acts:
            cv2.rectangle(vis, (x1, y1), (x2, y2), (0, 255, 255), 1)
            cv2.putText(vis, f"{d:.0f}", (x1 + 4, y1 + 14), cv2.FONT_HERSHEY_SIMPLEX, 0.4, (0, 255, 255), 1)
        # 白名单候选：绿框（超阈值已点）/ 灰框（未达阈值）
        for score, name, cx, cy in self._last_cands:
            color = (0, 255, 0) if score >= TH_MONSTER else (160, 160, 160)
            tpl = self.tpl_path(str(TPL / name))
            tw, th = tpl.shape[1], tpl.shape[0]
            x, y = int(cx * HALF - tw / 2), int(cy * HALF - th / 2)
            cv2.rectangle(vis, (x, y), (x + tw, y + th), color, 2)
            cv2.putText(vis, f"{name[:-4]} {score:.2f}", (x, y - 4),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.45, color, 1)
        # 最近 0.8s 的点击：红圈+十字+标签
        for tx, ty, tt, label in list(self._taps):
            age = now - tt
            if age > 0.8:
                self._taps.remove((tx, ty, tt, label))
                continue
            px, py = int(tx * HALF), int(ty * HALF)
            cv2.circle(vis, (px, py), 22, (0, 0, 255), 2)
            cv2.line(vis, (px - 28, py), (px + 28, py), (0, 0, 255), 1)
            cv2.line(vis, (px, py - 28), (px, py + 28), (0, 0, 255), 1)
            if label:
                cv2.putText(vis, label, (px + 24, py - 8), cv2.FONT_HERSHEY_SIMPLEX, 0.45, (0, 0, 255), 1)
        # 角标
        cv2.putText(vis, f"{loop_ms:.0f}ms f{self._stats[1]}", (8, 16),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 255, 255), 1)
        self._rec.write(vis)

    def run(self):
        t0 = time.time()
        log.info("开始: 后端=%s 模式=%s 局数=%d", self.bk.name, self.mode, self.loop)
        while not self.stop:
            ts = time.time()
            try:
                self.step()
            except KeyboardInterrupt:
                raise
            except Exception as e:
                log.warning("step 异常: %s", e)
                time.sleep(0.2)
            loop_ms = (time.time() - ts) * 1000
            self.render_frame(loop_ms)
            self._stats[0] += loop_ms / 1000
            self._stats[1] += 1
            if self._stats[1] % 200 == 0:
                log.info("循环统计: 平均 %.0f ms/帧", self._stats[0] / self._stats[1] * 1000)
            if time.time() - t0 > 3600:
                log.warning("超 1 小时，退出")
                break
        if self._rec is not None:
            self._rec.release()
        log.info("完成 %d 局（平均 %.0f ms/帧）",
                 self.rounds_done, self._stats[0] / max(self._stats[1], 1) * 1000)


def main():
    ap = argparse.ArgumentParser(description="明日方舟 Skland 打地鼠助手")
    ap.add_argument("--mode", choices=["timed", "challenge"], default="timed")
    ap.add_argument("--loop", type=int, default=1)
    ap.add_argument("--serial", default=None, help="缺省自动选择第一个 USB 设备")
    ap.add_argument("--backend", choices=["scrcpy", "maafw"], default="scrcpy")
    ap.add_argument("--record", default=None, help="录制识别过程到 mp4（如 --record logs/round.mp4）")
    ap.add_argument("--capture", default=None, help="采集干净帧目录（约3fps，用于建语料/标注入池）")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s",
                        datefmt="%H:%M:%S")

    serial = args.serial
    if serial is None:
        out = subprocess.run(["adb", "devices"], capture_output=True, text=True).stdout
        devs = [l.split("\t")[0] for l in out.splitlines()[1:] if "\tdevice" in l]
        usb = [d for d in devs if not d.startswith("100.") and "adb-" not in d]
        if not usb:
            raise SystemExit("未发现 USB 设备（可用 --serial 指定无线地址）")
        serial = usb[0]
        log.info("自动选择设备: %s", serial)

    if args.backend == "scrcpy":
        bk = ScrcpyBackend("adb", serial)
    else:
        bk = MaafwBackend("adb", serial)

    # 保持屏幕常亮（USB 供电期间），退出时恢复
    subprocess.run(["adb", "-s", serial, "shell", "input", "keyevent", "KEYCODE_WAKEUP"],
                   check=False, stdout=subprocess.DEVNULL)
    subprocess.run(["adb", "-s", serial, "shell", "svc", "power", "stayon", "usb"],
                   check=False, stdout=subprocess.DEVNULL)
    log.info("已保持屏幕常亮（退出时恢复）")

    bot = Bot(bk, args.mode, args.loop, record=args.record, capture=args.capture)
    try:
        bot.run()
    except KeyboardInterrupt:
        log.info("手动停止，已完成 %d 局", bot.rounds_done)
    finally:
        subprocess.run(["adb", "-s", serial, "shell", "svc", "power", "stayon", "false"],
                       check=False, stdout=subprocess.DEVNULL)
        bk.close()


if __name__ == "__main__":
    main()
