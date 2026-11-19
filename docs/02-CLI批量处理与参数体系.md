# 02 · CLI 批量处理与参数体系

目标：理解 `dewatermark.py` 的 `main()` 如何把「一堆路径和选项」变成「一批可执行的任务」，并学会安全地批量处理。

---

## 1. 从命令行到任务列表

`main()` 的流程可以看成五段流水线：

```text
argparse 解析
   ↓
collect_inputs()        找文件（目录展开、格式过滤、去重）
   ↓
probe() + detect()      逐个视频读信息、定水印框
   ↓
Task(src, dst, info, boxes)
   ↓
ThreadPoolExecutor 或串行循环 → process_delogo / process_inpaint
```

### 1.1 参数解析

`main()` 用 `argparse` 定义了全部开关。几个值得注意的设计：

- `inputs` 是 `nargs="*"`，缺省为当前目录 `"."`；
- `--box` 用 `action="append"`，所以可以重复传，每个都用 `parse_box()` 解析成 `(x, y, w, h)`；
- `--mode` 限定 `delogo|inpaint`，`--corner` 限定六个位置；
- `SHOW_PROGRESS` 是全局变量，只在「单文件且非 quiet」时打开，避免并发时进度字符互相覆盖。

```python
ap.add_argument("--jobs", type=int, default=1, help="并发处理文件数（默认 1）")
ap.add_argument("--recursive", action="store_true", help="目录输入时递归子目录")
ap.add_argument("--overwrite", action="store_true", help="覆盖已存在的输出")
```

### 1.2 文件收集：`collect_inputs()`

```python
def collect_inputs(inputs, recursive, include_exts):
    files = []
    for raw in inputs:
        p = Path(raw)
        if p.is_dir():
            it = p.rglob("*") if recursive else p.glob("*")
            for f in sorted(it):
                if f.is_file() and f.suffix.lower() in include_exts and not f.name.startswith("."):
                    files.append(str(f))
        elif p.is_file():
            ...
```

三个细节：

1. 支持的扩展名集中在 `VIDEO_EXTS`（mp4 / mov / mkv / avi / flv / webm / m4v / ts / wmv / mpg / mpeg / 3gp）；
2. 以 `.` 开头的隐藏文件被跳过；
3. 最后用 `os.path.normcase(os.path.abspath(f))` 做**去重保序**，避免同一文件被传两次处理两遍。

### 1.3 任务构造

每个视频都会经历：

```python
info = probe(src)                     # 分辨率、fps、时长、音轨
det  = detect_watermark(src, ...)     # 水印框 + 置信度
boxes = [...]                         # 手动框 / 自动框 / 兜底比例框
dst = make_output_path(src, outdir, suffix)
tasks.append(Task(src, dst, info, boxes))
```

框的来源优先级：

1. `--box`：手动指定，完全跳过自动定位；
2. `--no-auto`：按比例猜一个右下角框（宽 19%、高 3.2%），用于已知位置的批量场景；
3. 默认：调用自动定位；失败或置信度不足时，打印告警并退回同一个比例框。

这一步体现了很重要的一条工程原则：**自动算法失败时要有确定的兜底**，而不是让整批任务崩掉。

---

## 2. 输出路径规则

```python
def make_output_path(src, outdir, suffix):
    name = f"{Path(src).stem}{suffix}.mp4"
    if outdir:
        d = Path(outdir); d.mkdir(parents=True, exist_ok=True)
        return str(d / name)
    return str(Path(src).with_name(name))
```

- 默认 suffix 是 `_dewm`：`video.mp4` → `video_dewm.mp4`，与原片同目录；
- 指定 `-o out` 时统一放进 `out/`；
- 如果目标已存在且没有 `--overwrite`，该任务直接跳过，不会重复编码。

> 注意：两个不同目录下的同名视频，如果都输出到同一个 `-o` 目录，会互相覆盖。批量合并素材时建议保持 `--suffix` 含来源信息，或分目录输出。

---

## 3. 参数速查表

| 参数 | 默认值 | 作用 |
|---|---|---|
| `inputs` | `.` | 视频文件或目录，可多个 |
| `-o, --output-dir` | 同目录 | 输出目录 |
| `--suffix` | `_dewm` | 输出文件名后缀 |
| `--mode` | `delogo` | 修复引擎；`inpaint` 可配 `--inpaint-algo telea\|ns` |
| `--corner` | `br` | 水印位置：`br/bl/tr/tl/bottom/top` |
| `--box` | 无 | 手动框 `x,y,w,h`，可重复 |
| `--no-auto` | 关 | 不做自动定位，用比例框 |
| `--pad` | `2` | 自动定位框向外扩的像素 |
| `--crf` | `18` | x264 画质，越小越清晰 |
| `--preset` | `medium` | x264 编码预设 |
| `--jobs` | `1` | 并发文件数 |
| `--recursive` | 关 | 目录输入时递归 |
| `--overwrite` | 关 | 覆盖已有输出 |
| `--no-audio` | 关 | 丢弃音轨 |
| `--dry-run` | 关 | 只定位不处理 |
| `--preview` | 关 | 生成绿框预览 PNG |
| `--samples` | `32` | 自动定位抽样帧数 |
| `-q, --quiet` | 关 | 少输出 |

---

## 4. 四个典型场景

### 4.1 批量 + 递归 + 并发

```bash
python dewatermark.py "D:/素材" -o "D:/成品" --recursive --jobs 2
```

`--jobs > 1` 时用 `ThreadPoolExecutor`，每个 worker 独立调用 FFmpeg 子进程，适合 CPU 核心较多的机器。注意并发会放大内存占用，4K 素材建议 `--jobs 1` 或 `2`。

### 4.2 先预览整批定位结果

```bash
python dewatermark.py "D:/素材" --recursive --dry-run --preview
```

每生成一个 `*.preview.png` 就打印路径。这是批量处理前最值得做的一次「体检」：如果某几个视频框歪了，单独用 `--box` 处理它们。

### 4.3 水印位置固定在非右下角

```bash
python dewatermark.py "D:/素材" --corner tr --recursive
```

或者完全不依赖定位，直接给一个统一框（适合同批次模板化视频）：

```bash
python dewatermark.py "D:/素材" --no-auto --box 30,40,160,48 --recursive
```

### 4.4 追求更高画质 / 更小体积

```bash
python dewatermark.py video.mp4 --crf 16 --preset slow
python dewatermark.py video.mp4 --crf 23 --preset veryfast
```

CRF 每降 4~6，码率大致翻倍；`preset` 越慢压缩率越高但耗时越长。

---

## 5. 批处理循环里发生了什么

```python
def worker(task):
    if args.mode == "inpaint":
        process_inpaint(task, crf, preset, not no_audio, algo=...)
    else:
        process_delogo(task, crf, preset, not no_audio)
```

- 单文件模式：逐个打印 `[i/n]`、耗时、输出大小，进度百分比实时刷新；
- 并发模式：`as_completed()` 汇总，每个完成项打印成功或失败，最后统计 `成功 N，失败 M，总耗时`；
- 任何单个文件失败只记错误，不会中断整批——这是批处理工具该有的韧性。

---

## 6. 小练习

1. 找 3 个视频放在同一目录，先 `--dry-run --preview`，打开三张 PNG 对比定位效果。
2. 故意传一个不存在的路径和一个非视频文件，观察 `collect_inputs()` 的跳过提示。
3. 用 `--jobs 2` 处理同一批文件，比较总耗时与 CPU 占用。
4. 给 `main()` 加一个 `--suffix` 之外的 `--keep-name` 选项，让输出文件名不带后缀（提示：改 `make_output_path`）。

下一步：[03 视频探测与 FFmpeg 调用](./03-视频探测与FFmpeg调用.md) —— 搞清楚进度、音轨、编码这些「幕后参数」从哪来。
