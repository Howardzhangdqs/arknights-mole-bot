#!/usr/bin/env python3
"""scrcpy 帧源探针：验证 mysc_core 视频流 + 触摸，测量帧率与帧时延。"""
import sys
import time
from collections import deque

sys.path.insert(0, ".")

from adbutils import AdbDevice, adb as adbutils_adb
from mysc_core.session import Session
from mysc_core.video import VideoKwargs
from mysc_core.control import ControlKwargs, ControlAdapter
from mysc_core.utils.keys import EnumAction


def main(serial: str):
    dev = adbutils_adb.device(serial=serial)
    stamps = deque(maxlen=120)

    def on_frame(n, frame):
        stamps.append(time.time())

    vk = VideoKwargs(max_fps=60, max_size=1200, video_codec=VideoKwargs.EnumVideoCodec.H264)
    ck = ControlKwargs()
    t0 = time.time()
    session = Session(device=dev, video_kwargs=vk, control_kwargs=ck,
                      frame_update_callback=on_frame)
    print(f"Session 建立马图 {time.time()-t0:.2f}s")

    time.sleep(4)
    arr = session.va.get_ndarray("rgb24")
    print("帧 ndarray:", None if arr is None else (arr.shape, arr.dtype))

    if len(stamps) >= 10:
        import numpy as np
        gaps = np.diff(list(stamps)) * 1000
        fps = 1000 / gaps.mean()
        print(f"帧率 ~{fps:.1f} fps, 帧间隔均值 {gaps.mean():.1f}ms, p95 {np.percentile(gaps,95):.1f}ms")

    # 帧时延：新帧到来时刻 vs 事件时刻（用帧计数变化测触摸往返）
    if session.ca:
        ca = session.ca
        w, h = 1200, 540
        if arr is not None:
            h, w = arr.shape[:2]
        for i in range(3):
            t1 = time.time()
            ca.f_touch(EnumAction.DOWN, 600, 300, w, h, touch_id=0)
            ca.f_touch(EnumAction.UP, 600, 300, w, h, touch_id=0)
            print(f"f_touch 下发+抬起往返 {(time.time()-t1)*1000:.1f}ms")
        time.sleep(0.5)

    session.disconnect()
    print("断开完成")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "6590635e")
