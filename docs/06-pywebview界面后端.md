# 06 · pywebview 界面后端

目标：理解 `app.py` 如何把 `dewatermark.py` 的函数变成前端可调用的 API，同时保证处理大视频时窗口不卡死。

---

## 1. 三层架构

```text
ui/index.html  ──window.pywebview.api.xxx()──▶  app.py 的 Api 类  ──▶  dewatermark.py
       ▲                                             │
       └──────────────── poll() 轮询 ◀───────────────┘（后台线程写 state）
```

- 前端只发命令、拿状态，不做重活；
- `Api` 持有文件列表、日志、处理状态和窗口引用；
- FFmpeg/OpenCV 全部在 `Api._run()` 起的后台线程里执行。

---

## 2. 为什么 `window` 必须私有

这是踩过的最坑的一个 bug：

```python
class Api:
    def __init__(self):
        self._lock = threading.RLock()
        self._window: Optional[webview.Window] = None
```

早期写的是 `self.window = window`。pywebview 的 JS 桥会把 `Api` 的**公共属性递归反射**给 JavaScript，而 `Window` 对象内部又引用了 WebView2 的原生控件，反射一层层下去直接把界面卡死。

教训：**暴露给 JS 的 API 对象，只放纯数据和方法，窗口/线程/定时器这类对象一律用下划线前缀藏起来**。

启动时：

```python
window = webview.create_window("视频去水印", url=str(ui), js_api=api, ...)
api._window = window
window.events.shown += api.setup_file_drop
webview.start(debug="--debug" in sys.argv)
```

---

## 3. 数据模型：文件、日志、状态

```python
self.files: List[Dict[str, Any]] = []
self.by_path: Dict[str, Dict[str, Any]] = {}
self.logs: List[Dict[str, str]] = []
self.state = {
    "running": False, "percent": 0, "current": "", "done": 0, "total": 0,
    "message": "", "elapsed": 0, "eta": 0, "finished": False,
}
```

三个容器各司其职：

- `files` 是有序列表，`by_path` 是 `os.path.normcase(abspath)` 索引的字典，用来快速去重；
- `logs` 最多保留 500 条，超出删前 200 条——避免长时间运行内存无限涨；
- `state` 是单一进度状态，前端每次 `poll()` 拿走一份副本。

文件条目字段很全：`path/name/size/meta/status/box/box_text/thumb/out/error/note/w/h/conf`。前端卡片就是照这个结构渲染的。

---

## 4. 导入文件的三条路

| 方法 | 来源 | 实现要点 |
|---|---|---|
| `pick_files()` | 「添加视频」按钮 | `window.create_file_dialog(OPEN_DIALOG, allow_multiple=True, file_types=...)` |
| `pick_folder()` | 「文件夹」按钮 | `FOLDER_DIALOG` + `os.walk()` 递归收集 |
| `add_paths(paths)` | 剪贴板 Ctrl+V（及历史拖放代码） | 接收本地路径数组 |

三者都汇聚到 `_register(path)`：

```python
p = os.path.abspath(path)
if Path(p).suffix.lower() not in VIDEO_EXTS:
    return None
k = self._key(p)
if k in self.by_path:
    return None
entry = {...}
vi = DW.probe(p)
entry.update(w=..., h=..., fps=..., duration=..., dur_text=...)
```

统一在这里做格式过滤、去重、元数据探测，避免每个入口各写一遍。

### 4.1 剪贴板读取的 64 位坑

`clipboard_files()` 用 ctypes 直接读 `CF_HDROP`：

```python
u.GetClipboardData.argtypes = [wintypes.UINT]
u.GetClipboardData.restype = wintypes.HANDLE   # 64 位下必须是 HANDLE，否则句柄被截断
```

早期没设 `restype`，返回指针被按 32 位 int 截断，`DragQueryFileW` 拿到无效句柄，剪贴板导入永远为空。这类问题不写日志根本查不出来，所以 `_diag()` 会把每个关键节点写到 `%TEMP%\dewm_ui_diag.log`。

---

## 5. 定位与缩略图

```python
def detect(self, path, corner="br", pad=2):
    det = DW.detect_watermark(e["path"], corner=corner)
    x, y, w, h = DW.clamp_box((x-pad, y-pad, w+pad*2, h+pad*2), e["w"], e["h"])
    e["box"] = [x, y, w, h]
    e["conf"] = round(det.confidence, 3)
    e["thumb"] = self._thumb(e["path"], e["box"])
```

`_thumb()` 不只是截图，它做了几件贴心的事：

1. 用 `cv2.copyMakeBorder(..., BORDER_REPLICATE)` 四周镜像补边，这样水印在画面角落时，裁剪窗口还能把它放在**正中间**；
2. 绿框标出即将修复的区域；
3. `cv2.imencode('.jpg')` + base64 转成 data URL，前端直接 `style.backgroundImage` 用；
4. 只缩小不放大，保证点开放大时仍然清晰。

置信度低于 50% 时写 warn 日志，提示「建议点缩略图确认」——算法的不确定性在这里传达到人。

`set_box(path, box)` 则让用户在放大浮层里直接改 `x/y/宽/高`，改完重新生成缩略图，状态从 `pending` 变 `ready`。

---

## 6. 处理流程：后台线程 + 状态机

### 6.1 `start(opts)`：只负责校验和派发

```python
targets = [f for f in self.files if f["status"] not in ("done",)]
if not targets: return {"ok": False, "note": "没有待处理的文件"}
...
self._thread = threading.Thread(target=self._run, args=(targets, opts), daemon=True)
self._thread.start()
```

`start()` 本身立刻返回，前端不用等。校验包括：引擎是否可用、文件元数据是否齐全、输出目录是否可创建、inpaint 是否有 OpenCV。

### 6.2 `_run()`：逐文件状态推进

每个文件的状态流转：

```text
pending → detecting → running → done
                          ↓
                        failed / skipped
```

关键代码：

```python
e["status"] = "detecting"
self.state.update(current=e["name"], message="定位水印…", percent=...)
if not e["box"]:
    det = DW.detect_watermark(...)
    ...
e["status"] = "running"

def cb(frac):
    overall = (done + frac) / total * 100
    self.state.update(percent=int(overall), message=f"处理中 {int(frac*100)}%")

DW.process_delogo(task, crf, preset, keep_audio, on_progress=cb)
e["status"] = "done"
```

- 如果卡片已经有 `box`（用户手动改过或提前定位过），**跳过重复定位**；
- 进度回调把「单文件百分比」换算成「整批百分比」，前端一条进度条就能表示全局；
- 每个文件结束后更新 `done / elapsed / eta`，最后写 `finished=True`。

### 6.3 完成提示为什么只弹一次

后端 state 里 `finished=True` 会一直保持到下一次 start。前端每 320ms 轮询一次，如果在 `finished` 分支里直接 `toast()`，就会一直弹「完成 4 个」——这就是用户反馈过的 bug。

解决方案是前后端配合：

```python
# 前端 start()
if r.ok: runSeq += 1

# 前端 poll()
if (!st.running && st.finished) {
    if (toastSeq !== runSeq) {
        toastSeq = runSeq;
        toast(st.message || "处理完成", "ok");
    }
}
```

每次开始处理给一个递增序号，每个序号只提示一次。本题表面是 UI 问题，根因是「轮询模型的幂等性」没设计好。

---

## 7. 轮询接口 `poll()`

```python
def poll(self, since=0):
    with self._lock:
        files = [self._public(f) for f in self.files]
        logs = self.logs[since:] if since < len(self.logs) else []
        st = dict(self.state)
    return {"files": files, "logs": logs, "log_total": len(self.logs), "state": st}
```

- `since` 是日志游标，前端只拿增量，避免每 320ms 重复渲染全部日志；
- 返回的文件字典经过 `_public()` 白名单过滤，不把内部对象泄漏给 JS；
- `RLock` 保护读写——后台线程写状态，UI 线程读状态，没有锁会出现半更新数据。

---

## 8. 拖放：尝试过，但产品上放弃

`app.py` 里保留了完整的 `setup_file_drop()` / `_install_drop()` 实现，包括：

- `form.BeginInvoke(Action(...))` 把 WinForms 控件操作切回 UI 线程；
- `obj.DragEnter += handler`（**必须**用 `+=`，pythonnet 的 `add_DragEnter(pyfunc)` 会静默失败）；
- 处理 `DragOver` 并设置 `e.Effect`，否则 Windows 不认为你接受放置；
- `ctrl.AllowExternalDrop = False` 让 WebView2 把拖放让出来。

但即便事件都绑上了，在不同 WebView2 / 系统版本下仍无法稳定拿到真实路径。与其留一个「有时能拖有时没反应」的功能，不如明确不支持：README 和界面都写明用按钮或 Ctrl+V，前端 `dragenter/dragover/drop` 只做 `preventDefault()` 防止页面被导航走。

这是产品判断，不是技术偷懒：**一个不稳定的功能比没有这个功能更伤体验**。

---

## 9. 小练习

1. 在 `Api.log()` 里加一个文件落盘，把每次处理完整日志保存成 `logs/时间戳.log`。
2. 给 `poll()` 加 `since_file` 参数，只返回状态变化过的文件，减少前端 diff 开销。
3. 把 `_run()` 里的串行 for 改成 `ThreadPoolExecutor`，并想想 `percent` 回调会出什么问题（提示：闭包里的 `done` 和 `total`）。
4. 阅读 `%TEMP%\dewm_ui_diag.log`，找出一次启动过程中的全部关键事件。

下一步：[07 前端界面实现](./07-前端界面实现.md) —— 看这些状态如何变成一张张卡片和进度条。
