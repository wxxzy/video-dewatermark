#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
video-dewatermark · 图形界面（pywebview）

前端：ui/index.html   后端：本文件 Api 类 + dewatermark.py 引擎
启动：python app.py            正常模式
      python app.py --debug    打开开发者工具
"""

from __future__ import annotations

import base64
import json
import os
import sys
import threading
import time
import traceback
from pathlib import Path
from typing import Any, Dict, List, Optional

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))

import webview  # noqa: E402

IMPORT_ERROR = ""
try:
    import dewatermark as DW
except BaseException as e:  # noqa: BLE001  ffmpeg 缺失时会 SystemExit
    DW = None
    IMPORT_ERROR = str(e) or e.__class__.__name__

VIDEO_EXTS = {".mp4", ".mov", ".mkv", ".avi", ".flv", ".webm", ".m4v", ".ts", ".wmv", ".mpg", ".mpeg", ".3gp"}


def _fmt_size(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{int(n)} B"
        n /= 1024
    return f"{n:.1f} GB"


def _fmt_dur(s: float) -> str:
    s = int(max(0, s))
    return f"{s // 60}:{s % 60:02d}" if s < 3600 else f"{s // 3600}:{(s % 3600) // 60:02d}:{s % 60:02d}"


class Api:
    """暴露给前端的接口，方法名即 window.pywebview.api.xxx()"""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self.files: List[Dict[str, Any]] = []
        self.by_path: Dict[str, Dict[str, Any]] = {}
        self.logs: List[Dict[str, str]] = []
        self.state: Dict[str, Any] = {
            "running": False, "percent": 0, "current": "", "done": 0, "total": 0,
            "message": "", "elapsed": 0, "eta": 0, "finished": False,
        }
        self.output_dir = ""
        self._window: Optional[webview.Window] = None
        self._thread: Optional[threading.Thread] = None
        self._drop_timer = None
        self._drop_ready = False
        self._t0 = 0.0
        self._started_at = 0.0

    # ------------------------------------------------------------------ 基础
    def _diag(self, msg: str) -> None:
        """把关键事件写到临时目录的日志，方便出问题时排查（不影响界面）。"""
        try:
            import tempfile
            f = Path(tempfile.gettempdir()) / "dewm_ui_diag.log"
            with open(f, "a", encoding="utf-8") as fh:
                fh.write(f"{time.strftime('%H:%M:%S')} {msg}\n")
        except Exception:
            pass

    def log(self, text: str, level: str = "info") -> None:
        with self._lock:
            self.logs.append({"t": time.strftime("%H:%M:%S"), "text": text, "level": level})
            if len(self.logs) > 500:
                del self.logs[:200]

    def _key(self, p: str) -> str:
        return os.path.normcase(os.path.abspath(p))

    def boot(self) -> Dict[str, Any]:
        """前端启动时调用，返回环境信息。"""
        info = {
            "ffmpeg": getattr(DW, "FFMPEG", "") if DW else "",
            "has_cv": bool(getattr(DW, "HAVE_CV", False)) if DW else False,
            "error": IMPORT_ERROR,
            "video_exts": sorted(VIDEO_EXTS),
            "version": "1.1",
        }
        if DW:
            self.log(f"引擎就绪 · ffmpeg: {os.path.basename(DW.FFMPEG)}", "ok")
            if not DW.HAVE_CV:
                self.log("未检测到 opencv-python，自动定位不可用", "warn")
        else:
            self.log(f"初始化失败：{IMPORT_ERROR}", "error")
        return info

    # ------------------------------------------------------------------ 加文件
    def _register(self, path: str) -> Optional[Dict[str, Any]]:
        p = os.path.abspath(path)
        if Path(p).suffix.lower() not in VIDEO_EXTS:
            return None
        k = self._key(p)
        if k in self.by_path:
            return None
        try:
            st = os.path.getsize(p)
        except OSError:
            return None
        entry: Dict[str, Any] = {
            "path": p, "name": os.path.basename(p), "size": st, "size_text": _fmt_size(st),
            "w": 0, "h": 0, "fps": 0.0, "duration": 0.0, "dur_text": "",
            "status": "pending", "box": None, "box_text": "", "thumb": "", "out": "",
            "error": "", "preview": "", "note": "", "conf": 0.0,
        }
        try:
            vi = DW.probe(p)
            entry.update(w=vi.width, h=vi.height, fps=round(vi.fps, 2), duration=vi.duration,
                         dur_text=_fmt_dur(vi.duration))
            entry["meta"] = f"{vi.width}×{vi.height} · {_fmt_dur(vi.duration)} · {_fmt_size(st)}"
        except Exception as e:  # noqa: BLE001
            entry["meta"] = _fmt_size(st)
            entry["error"] = f"读取失败: {e}"
            entry["status"] = "failed"
        with self._lock:
            self.files.append(entry)
            self.by_path[k] = entry
        return entry

    def pick_files(self) -> List[Dict[str, Any]]:
        if not self._window:
            return []
        exts = ";".join(f"*{e}" for e in sorted(VIDEO_EXTS))
        res = self._window.create_file_dialog(
            webview.OPEN_DIALOG, allow_multiple=True,
            file_types=(f"视频文件 ({exts})", "所有文件 (*.*)"),
        )
        added = []
        if res:
            for f in res:
                e = self._register(f)
                if e:
                    added.append(e)
        if added:
            self.log(f"添加 {len(added)} 个视频")
        return added

    def pick_folder(self) -> List[Dict[str, Any]]:
        if not self._window:
            return []
        res = self._window.create_file_dialog(webview.FOLDER_DIALOG, allow_multiple=False)
        if not res:
            return []
        root = res[0]
        added = []
        for dirpath, _dirnames, filenames in os.walk(root):
            for fn in filenames:
                if Path(fn).suffix.lower() in VIDEO_EXTS:
                    e = self._register(os.path.join(dirpath, fn))
                    if e:
                        added.append(e)
        self.log(f"从文件夹添加 {len(added)} 个视频" if added else "该文件夹下没有视频", "ok" if added else "warn")
        return added

    def add_paths(self, paths: List[str]) -> List[Dict[str, Any]]:
        """支持拖放（若前端能拿到本地路径）。"""
        added: List[Dict[str, Any]] = []
        for p in paths or []:
            if os.path.isdir(p):
                for dirpath, _d, filenames in os.walk(p):
                    for fn in filenames:
                        if Path(fn).suffix.lower() in VIDEO_EXTS:
                            e = self._register(os.path.join(dirpath, fn))
                            if e:
                                added.append(e)
            else:
                e = self._register(p)
                if e:
                    added.append(e)
        return added

    def remove_file(self, path: str) -> bool:
        with self._lock:
            k = self._key(path)
            if k in self.by_path:
                self.files.remove(self.by_path.pop(k))
                return True
        return False

    def clear_files(self) -> bool:
        if self.state["running"]:
            return False
        with self._lock:
            self.files.clear()
            self.by_path.clear()
        return True

    # ------------------------------------------------------------------ 定位
    def detect(self, path: str, corner: str = "br", pad: int = 2) -> Dict[str, Any]:
        e = self.by_path.get(self._key(path))
        if not e:
            return {"ok": False, "note": "文件不在列表中"}
        if not DW or not DW.HAVE_CV:
            return {"ok": False, "note": "缺少 opencv-python，无法自动定位"}
        t0 = time.time()
        try:
            det = DW.detect_watermark(e["path"], corner=corner)
        except Exception as ex:  # noqa: BLE001
            e["status"] = "failed"
            e["error"] = f"定位异常: {ex}"
            return {"ok": False, "note": e["error"]}
        cost = time.time() - t0
        if not det.ok:
            e["note"] = det.note
            e["status"] = "ready" if e["status"] == "pending" else e["status"]
            self.log(f"{e['name']} 自动定位失败（{det.note}），将按默认角落框处理", "warn")
            return {"ok": False, "note": det.note, "entry": self._public(e)}
        x, y, w, h = det.box
        x, y, w, h = DW.clamp_box((x - pad, y - pad, w + pad * 2, h + pad * 2), e["w"], e["h"])
        e["box"] = [x, y, w, h]
        e["box_text"] = f"{x}, {y}, {w}×{h}"
        e["conf"] = round(det.confidence, 3)
        e["status"] = "ready" if e["status"] in ("pending", "ready") else e["status"]
        e["thumb"] = self._thumb(e["path"], e["box"])
        if det.confidence < 0.5:
            self.log(f"{e['name']} 定位完成 → {x},{y},{w}×{h}，但置信度只有 {det.confidence:.0%}，"
                     f"建议点缩略图确认", "warn")
        else:
            self.log(f"{e['name']} 定位完成 → x={x} y={y} w={w} h={h}（置信度 {det.confidence:.0%}，{cost:.1f}s）", "ok")
        return {"ok": True, "box": e["box"], "conf": e["conf"], "entry": self._public(e)}

    def set_box(self, path: str, box: List[int]) -> Dict[str, Any]:
        e = self.by_path.get(self._key(path))
        if not e:
            return {"ok": False}
        try:
            x, y, w, h = (int(v) for v in box)
        except Exception:
            return {"ok": False, "note": "格式应为 4 个整数"}
        x, y, w, h = DW.clamp_box((x, y, w, h), e["w"], e["h"])
        e["box"] = [x, y, w, h]
        e["box_text"] = f"{x}, {y}, {w}×{h}"
        e["thumb"] = self._thumb(e["path"], e["box"])
        if e["status"] == "pending":
            e["status"] = "ready"
        return {"ok": True, "entry": self._public(e)}

    def _thumb(self, path: str, box: Optional[List[int]]) -> str:
        """生成定位预览小图（围绕水印框裁剪 + 绿框标注），返回 data URL。"""
        try:
            import cv2
            cap = cv2.VideoCapture(path)
            ok, frame = cap.read()
            cap.release()
            if not ok:
                return ""
            H, W = frame.shape[:2]
            if box:
                x, y, w, h = box
                cw = max(int(w * 4.0), 480)
                ch = max(int(cw * 0.62), 240)
                # 先四周镜像补边，保证水印框始终位于预览图正中（水印常在画面角落）
                pad = cw
                big = cv2.copyMakeBorder(frame, pad, pad, pad, pad, cv2.BORDER_REPLICATE)
                cx, cy = pad + x + w // 2, pad + y + h // 2
                x0, y0 = cx - cw // 2, cy - ch // 2
                crop = big[y0:y0 + ch, x0:x0 + cw].copy()
                cv2.rectangle(crop, (pad + x - x0, pad + y - y0),
                              (pad + x + w - x0, pad + y + h - y0), (60, 220, 120), 2)
            else:
                scale = 320.0 / W
                crop = cv2.resize(frame, (320, max(1, int(H * scale))), interpolation=cv2.INTER_AREA)
            # 只缩小不放大，保证放大预览时依然清晰
            if crop.shape[1] > 720:
                scale = 720.0 / crop.shape[1]
                crop = cv2.resize(crop, (720, max(1, int(crop.shape[0] * scale))), interpolation=cv2.INTER_AREA)
            ok2, buf = cv2.imencode(".jpg", crop, [int(cv2.IMWRITE_JPEG_QUALITY), 84])
            if not ok2:
                return ""
            return "data:image/jpeg;base64," + base64.b64encode(buf.tobytes()).decode("ascii")
        except Exception:
            return ""

    # ------------------------------------------------------------------ 处理
    def start(self, opts: Dict[str, Any]) -> Dict[str, Any]:
        if self.state["running"]:
            return {"ok": False, "note": "正在处理中"}
        if not DW:
            return {"ok": False, "note": IMPORT_ERROR or "引擎不可用"}
        targets = [f for f in self.files if f["status"] not in ("done",)]
        if not targets:
            return {"ok": False, "note": "没有待处理的文件"}
        missing = [f for f in targets if not f["w"]]
        if missing:
            return {"ok": False, "note": f"{missing[0]['name']} 无法读取画面信息"}

        mode = opts.get("mode", "delogo")
        if mode == "inpaint" and not DW.HAVE_CV:
            return {"ok": False, "note": "inpaint 引擎需要 opencv-python"}

        outdir = (opts.get("outdir") or "").strip()
        if outdir:
            try:
                os.makedirs(outdir, exist_ok=True)
            except OSError as e:
                return {"ok": False, "note": f"输出目录不可用: {e}"}
        self.output_dir = outdir

        with self._lock:
            self.state.update(running=True, percent=0, current="", done=0, total=len(targets),
                              message="准备中…", elapsed=0, eta=0, finished=False)
            self._started_at = time.time()
            for f in targets:
                if f["status"] != "done":
                    f["status"] = "pending"
                    f["error"] = ""

        self._thread = threading.Thread(target=self._run, args=(targets, opts), daemon=True)
        self._thread.start()
        self.log(f"开始处理 {len(targets)} 个文件 · 引擎 {mode}", "ok")
        return {"ok": True, "total": len(targets)}

    def _run(self, targets: List[Dict[str, Any]], opts: Dict[str, Any]) -> None:
        mode = opts.get("mode", "delogo")
        crf = int(opts.get("crf", 18))
        preset = opts.get("preset", "medium")
        keep_audio = bool(opts.get("keep_audio", True))
        overwrite = bool(opts.get("overwrite", True))
        corner = opts.get("corner", "br")
        pad = int(opts.get("pad", 2))
        algo = opts.get("inpaint_algo", "telea")
        suffix = opts.get("suffix", "_dewm") or "_dewm"
        outdir = (opts.get("outdir") or "").strip()
        total = len(targets)
        t_start = time.time()
        done = 0

        for idx, e in enumerate(targets):
            if e["status"] == "done":
                done += 1
                continue
            try:
                e["status"] = "detecting"
                self.state.update(current=e["name"], message="定位水印…",
                                  percent=int(done / total * 100))
                if not e["box"]:
                    det = DW.detect_watermark(e["path"], corner=corner)
                    if det.ok:
                        x, y, w, h = det.box
                        x, y, w, h = DW.clamp_box((x - pad, y - pad, w + pad * 2, h + pad * 2), e["w"], e["h"])
                        e["box"] = [x, y, w, h]
                        e["box_text"] = f"{x}, {y}, {w}×{h}"
                        e["conf"] = round(det.confidence, 3)
                        e["thumb"] = self._thumb(e["path"], e["box"])
                        lvl = "ok" if det.confidence >= 0.5 else "warn"
                        self.log(f"{e['name']} 定位 → {e['box_text']}（置信度 {det.confidence:.0%}）", lvl)
                    else:
                        w_ = int(e["w"] * 0.19)
                        h_ = max(12, int(e["h"] * 0.032))
                        e["box"] = DW.clamp_box(
                            (e["w"] - w_ - int(e["w"] * 0.02), e["h"] - h_ - int(e["h"] * 0.017), w_, h_),
                            e["w"], e["h"])
                        e["box_text"] = f"{e['box'][0]}, {e['box'][1]}, {e['box'][2]}×{e['box'][3]}"
                        self.log(f"{e['name']} 自动定位失败（{det.note}），用默认角落框", "warn")

                # 输出路径
                src = Path(e["path"])
                dst = (Path(outdir) / f"{src.stem}{suffix}.mp4") if outdir else src.with_name(f"{src.stem}{suffix}.mp4")
                dst.parent.mkdir(parents=True, exist_ok=True)
                if dst.exists() and not overwrite:
                    e["status"] = "skipped"
                    e["note"] = "输出已存在"
                    self.log(f"{e['name']} 输出已存在，跳过", "warn")
                    done += 1
                    continue

                vi = DW.VideoInfo(path=e["path"], width=e["w"], height=e["h"], fps=e["fps"],
                                  duration=e["duration"])
                # 补充音轨信息
                try:
                    full = DW.probe(e["path"])
                    vi.has_audio, vi.audio_codec = full.has_audio, full.audio_codec
                    vi.duration = full.duration or vi.duration
                except Exception:
                    pass
                task = DW.Task(src=e["path"], dst=str(dst), info=vi, boxes=[tuple(e["box"])])

                e["status"] = "running"
                self.state.update(message=f"处理中 0%", percent=int(done / total * 100))

                def cb(frac: float, _idx=idx, _e=e, _done=done) -> None:
                    overall = (_done + max(0.0, min(1.0, frac))) / total * 100
                    self.state.update(percent=int(overall), message=f"处理中 {int(frac * 100)}%")

                t1 = time.time()
                if mode == "inpaint":
                    DW.process_inpaint(task, crf, preset, keep_audio, algo=algo, on_progress=cb)
                else:
                    DW.process_delogo(task, crf, preset, keep_audio, on_progress=cb)
                e["status"] = "done"
                e["out"] = str(dst)
                e["out_size"] = _fmt_size(os.path.getsize(dst)) if os.path.exists(dst) else ""
                e["note"] = f"{time.time() - t1:.1f}s · {e['out_size']}"
                self.log(f"完成 {e['name']} → {os.path.basename(str(dst))}（{e['note']}）", "ok")
            except Exception as ex:  # noqa: BLE001
                e["status"] = "failed"
                e["error"] = str(ex)[:300]
                self.log(f"失败 {e['name']}: {e['error']}", "error")
                if os.environ.get("DEWM_DEBUG"):
                    traceback.print_exc()
            finally:
                done += 1
                elapsed = time.time() - t_start
                self.state.update(done=done, percent=int(done / total * 100), elapsed=elapsed,
                                  eta=(elapsed / done * (total - done)) if done else 0)

        ok_n = sum(1 for f in targets if f["status"] == "done")
        bad_n = sum(1 for f in targets if f["status"] == "failed")
        self.state.update(running=False, finished=True, percent=100 if ok_n else self.state["percent"],
                          current="", message=f"完成 {ok_n} 个" + (f"，失败 {bad_n} 个" if bad_n else ""),
                          elapsed=time.time() - t_start)
        self.log(f"全部结束：成功 {ok_n}，失败 {bad_n}", "ok" if not bad_n else "warn")

    # ------------------------------------------------------------------ 轮询
    def _public(self, e: Dict[str, Any]) -> Dict[str, Any]:
        return {k: e.get(k) for k in
                ("path", "name", "size_text", "meta", "status", "box", "box_text", "thumb",
                 "out", "error", "note", "w", "h", "conf")}

    def poll(self, since: int = 0) -> Dict[str, Any]:
        with self._lock:
            files = [self._public(f) for f in self.files]
            logs = self.logs[since:] if since < len(self.logs) else []
            st = dict(self.state)
        st["output_dir"] = self.output_dir
        return {"files": files, "logs": logs, "log_total": len(self.logs), "state": st}


    # ------------------------------------------------------------------ 系统拖放
    def setup_file_drop(self, *_args) -> None:
        """
        Windows 下接管「把文件/文件夹拖进窗口」。
        WebView2 默认自己吞掉外部拖放，而且出于安全不给 JS 本地路径；
        这里关掉它的 AllowExternalDrop，让拖放事件落到 WinForms 窗体上，从 DataObject 取真实路径回传给前端。

        注意：WinForms 控件只能在 UI 线程操作，而 shown 回调不在 UI 线程，
        所以真正的工作必须 BeginInvoke 到 UI 线程再做。
        """
        try:
            win = self._window
            form = win.native if win else None
            if form is None:
                return
            from System import Action

            form.BeginInvoke(Action(lambda: self._install_drop(win, form)))
        except Exception as e:  # noqa: BLE001
            self.log(f"未能启用拖放（继续用「添加视频」按钮即可）：{e}", "warn")

    def _install_drop(self, win, form) -> None:
        """在 UI 线程执行：关掉 WebView2 自带拖放 + 把窗体/控件都注册成拖放目标。"""
        from System import Action
        from System.Windows.Forms import DragDropEffects, DataFormats

        def bind_events(obj) -> bool:
            """
            必须用 += 语法绑定：pythonnet 里 getattr(obj,"add_DragEnter")(pyfunc) 不接受
            Python 函数（需要已转换的委托），会静默失败 —— 之前拖放没反应就是踩了这个。
            """
            try:
                obj.DragEnter += on_enter
                obj.DragOver += on_over
                obj.DragLeave += on_leave
                obj.DragDrop += on_drop
                return True
            except Exception as ex:  # noqa: BLE001
                self._diag(f"绑定事件失败 {type(obj).__name__}: {ex}")
                return False

        try:
            form.AllowDrop = True
            ctrl = None
            for c in form.Controls:
                try:
                    if "WebView2" in c.GetType().Name:
                        ctrl = c
                except Exception:
                    continue

            no_eff = getattr(DragDropEffects, "None")

            def has_files(e) -> bool:
                try:
                    return bool(e.Data.GetDataPresent(DataFormats.FileDrop))
                except Exception:
                    return False

            def want_effect(e):
                """必须是源允许的效果之一，否则 OLE 会拒收这次放置。"""
                try:
                    allowed = e.AllowedEffect
                    if allowed and (allowed & DragDropEffects.Copy):
                        return DragDropEffects.Copy
                    return allowed if allowed else DragDropEffects.Copy
                except Exception:
                    return DragDropEffects.Copy

            def on_enter(sender, e):
                ok_drop = has_files(e)
                try:
                    e.Effect = want_effect(e) if ok_drop else no_eff
                except Exception:
                    pass
                if ok_drop:
                    self._diag("DragEnter: 检测到文件")

            over_state = {"logged": False}

            def on_over(sender, e):
                # 关键：WinForms 的 DragOver 返回值就是给 Windows 的"是否允许放置"，
                # 不处理的话松手不会触发 DragDrop（表现就是拖进去没反应）
                ok_drop = has_files(e)
                try:
                    e.Effect = want_effect(e) if ok_drop else no_eff
                except Exception:
                    pass
                if ok_drop and not over_state["logged"]:
                    over_state["logged"] = True
                    self._diag("DragOver 已接受（允许放置）")

            def on_leave(sender, e):
                self._diag("DragLeave")

            def on_drop(sender, e):
                self._diag("DragDrop 触发")
                try:
                    if not has_files(e):
                        return
                    paths = [str(p) for p in e.Data.GetData(DataFormats.FileDrop)]
                    self._diag(f"Drop 路径 {paths}")
                    if not paths:
                        return
                    js = "window.__dewDrop(" + json.dumps(paths, ensure_ascii=False) + ")"
                    threading.Thread(target=lambda: win.evaluate_js(js), daemon=True).start()
                except Exception as ex:  # noqa: BLE001
                    self._diag(f"Drop 处理失败 {ex}")
                    self.log(f"拖放处理失败: {ex}", "warn")

            # 窗体 -> 事件落到顶层窗口时走这里
            bound_form = bind_events(form)
            self._on_drop, self._on_drag_enter = on_drop, on_enter

            # WebView2 控件 -> 光标落在子窗口时走这里（AllowExternalDrop 关掉后由我们接）
            bound_ctrl = False
            if ctrl is not None:
                try:
                    ctrl.AllowDrop = True
                except Exception:
                    pass
                bound_ctrl = bind_events(ctrl)

            self._diag(f"绑定结果 form={bound_form} ctrl={bound_ctrl}")

            attempts = {"n": 0}

            def apply_external_drop():
                attempts["n"] += 1
                try:
                    ctrl.AllowExternalDrop = False
                except Exception as ex:  # noqa: BLE001
                    if attempts["n"] >= 12:
                        self._diag(f"AllowExternalDrop 失败 {type(ex).__name__}")
                        self._diag("AllowExternalDrop 设置失败（拖放不可用，改用按钮/Ctrl+V 导入）")
                        return
                    th = threading.Timer(0.4, lambda: form.BeginInvoke(Action(apply_external_drop)))
                    th.daemon = True
                    th.start()
                    return
                self._drop_ready = True
                self._diag(f"AllowExternalDrop=False 成功；事件绑定 form={bound_form} ctrl={bound_ctrl}")

            if ctrl is not None:
                apply_external_drop()
        except Exception as e:  # noqa: BLE001
            self._diag(f"安装拖放失败 {e}")

    # ------------------------------------------------------------------ 其它
    def clipboard_files(self) -> List[str]:
        """读剪贴板里的文件（资源管理器 Ctrl+C 复制的就是 CF_HDROP），供 Ctrl+V 导入。"""
        try:
            import ctypes
            from ctypes import wintypes

            u = ctypes.WinDLL("user32", use_last_error=True)
            sh = ctypes.WinDLL("shell32", use_last_error=True)

            u.OpenClipboard.argtypes = [wintypes.HWND]
            u.OpenClipboard.restype = wintypes.BOOL
            u.GetClipboardData.argtypes = [wintypes.UINT]
            u.GetClipboardData.restype = wintypes.HANDLE   # 64 位下必须是 HANDLE，否则句柄被截断
            u.CloseClipboard.argtypes = []
            sh.DragQueryFileW.argtypes = [wintypes.HANDLE, wintypes.UINT, wintypes.LPWSTR, wintypes.UINT]
            sh.DragQueryFileW.restype = wintypes.UINT

            CF_HDROP = 15
            if not u.OpenClipboard(None):
                self._diag(f"OpenClipboard 失败 err={ctypes.get_last_error()}")
                return []
            try:
                h = u.GetClipboardData(CF_HDROP)
                if not h:
                    self._diag("剪贴板里没有文件")
                    return []
                cnt = sh.DragQueryFileW(h, 0xFFFFFFFF, None, 0)
                out = []
                for i in range(cnt):
                    ln = sh.DragQueryFileW(h, i, None, 0)
                    buf = ctypes.create_unicode_buffer(ln + 1)
                    sh.DragQueryFileW(h, i, buf, ln + 1)
                    if buf.value:
                        out.append(buf.value)
                self._diag(f"剪贴板文件 {len(out)} 个")
                return out
            finally:
                u.CloseClipboard()
        except Exception as e:  # noqa: BLE001
            self._diag(f"读剪贴板失败 {e}")
            return []

    def pick_outdir(self) -> str:
        if not self._window:
            return ""
        res = self._window.create_file_dialog(webview.FOLDER_DIALOG, allow_multiple=False)
        return res[0] if res else ""

    def open_path(self, path: str) -> bool:
        try:
            if path and os.path.exists(path):
                os.startfile(path)  # noqa: S606
                return True
        except Exception:
            pass
        return False

    def reveal(self, path: str) -> bool:
        try:
            if path and os.path.exists(path):
                import subprocess
                subprocess.Popen(["explorer", f"/select,{os.path.normpath(path)}"],
                                 creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                return True
        except Exception:
            pass
        return False


def main() -> None:
    api = Api()
    ui = BASE / "ui" / "index.html"
    window = webview.create_window(
        "视频去水印",
        url=str(ui),
        js_api=api,
        width=1200,
        height=820,
        min_size=(1000, 700),
        background_color="#0b0d12",
    )
    api._window = window
    window.events.shown += api.setup_file_drop
    debug = "--debug" in sys.argv
    webview.start(debug=debug)


if __name__ == "__main__":
    main()
