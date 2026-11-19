# 03 · 视频探测与 FFmpeg 调用

目标：掌握引擎的「输入输出层」——怎么读视频元数据、怎么驱动 FFmpeg、怎么拿到进度、怎么保留音轨。这一层是定位和修复的共同基础。

---

## 1. 元数据载体：`VideoInfo`

```python
@dataclass
class VideoInfo:
    path: str
    width: int
    height: int
    fps: float
    duration: float
    audio_codec: Optional[str] = None
    has_audio: bool = False
    video_codec: str = ""
    bitrate: int = 0
```

后面所有决策都依赖它：水印框坐标要按 `width/height` 裁边界，进度条要按 `duration` 算百分比，inpaint 管道要按 `fps` 喂帧，音轨要不要复制看 `has_audio/audio_codec`。

---

## 2. `probe()`：优先 ffprobe，兜底 OpenCV

```python
out = subprocess.run(
    [FFPROBE, "-v", "error", "-print_format", "json", "-show_format", "-show_streams", path],
    capture_output=True, text=True, encoding="utf-8", errors="replace",
    creationflags=NO_WINDOW,
).stdout
data = json.loads(out or "{}")
```

要点：

- `-print_format json` 让输出结构化，比解析人类可读文本稳得多；
- `-show_format` 给总时长/码率，`-show_streams` 给每条流的宽高、编码、帧率；
- `encoding="utf-8", errors="replace"` 配合文件开头对 stdout/stderr 的 `reconfigure()`，解决中文路径/日志乱码；
- `creationflags=NO_WINDOW` 是 Windows 下用 `pythonw.exe` 启动 GUI 时**必须**的，否则每跑一次 ffprobe 弹一次黑窗。

### 帧率的坑

```python
fr = st.get("avg_frame_rate") or st.get("r_frame_rate") or "25/1"
```

早期版本只取 `r_frame_rate`，遇到可变帧率（VFR）素材会拿到不真实的值，导致 inpaint 输出音画不同步。现在优先 `avg_frame_rate`，并在 `0/0` 时回退到 `r_frame_rate`，最后兜底 25fps。

### 没有 ffprobe 时

`_probe_by_cv2()` 用 `cv2.VideoCapture` 读 `FRAME_WIDTH/HEIGHT/FPS/FRAME_COUNT`，用 `帧数 / fps` 估算时长。精度差一些，但工具仍能跑。

---

## 3. `run_ffmpeg_with_progress()`：驱动与进度

```python
cmd = [FFMPEG, "-hide_banner", "-loglevel", "error", "-y"] + args \
      + ["-progress", "pipe:1", "-nostats"]
proc = subprocess.Popen(cmd, stdout=PIPE, stderr=PIPE, text=True,
                        encoding="utf-8", errors="replace", bufsize=1,
                        creationflags=NO_WINDOW)
```

设计要点：

1. **`-progress pipe:1`**：让 FFmpeg 把进度写成 `key=value` 行到 stdout，而不是往 stderr 刷一屏 `\r` 覆盖文本。解析起来干净得多。
2. 只认 `out_time_us=` / `out_time_ms=`，换算成秒后除以总时长得到 `0~1` 的 `frac`。
3. 两种消费方式：
   - 命令行：每涨 5% 打印一次 `\r     去水印  45% `，不刷屏；
   - GUI：传 `on_progress` 回调，界面自己换算成总进度。
4. 遇到 `progress=end` 就 break，然后读 stderr、`wait()`、检查 `returncode`，非 0 就抛出带错误摘要的异常。

```python
if on_progress is not None:
    on_progress(frac)
elif SHOW_PROGRESS:
    pct = int(frac * 100)
    if pct >= last + 5:
        print(f"\r      {label} {pct:3d}% ", end="", flush=True)
```

> 注意 `bufsize=1`：行缓冲，保证 `for line in proc.stdout` 能及时拿到新行；否则进度条会一顿一顿的。

---

## 4. 编码参数：画质与兼容性

```python
def _encoder_args(crf, preset, extra=None):
    return ["-c:v", "libx264", "-crf", str(crf), "-preset", preset,
            "-pix_fmt", "yuv420p"] + (extra or [])
```

- `libx264`：兼容性最好的 H.264 编码器；
- `-crf 18`：视觉近无损，默认档；数值越小越清晰；
- `-preset medium`：速度与压缩率的平衡点；
- `-pix_fmt yuv420p`：保证手机、浏览器、剪映等都能正确解码（很多播放器不支持 yuv444p）；
- 输出 mp4/mov/m4v 时额外加 `-movflags +faststart`，把 moov atom 挪到文件头，方便边下边播。

---

## 5. 音轨策略：能复制就复制

```python
COPYABLE_AUDIO = {"aac", "mp3", "alac", "ac3", "eac3", "mp2"}

def _audio_args(info, keep_audio):
    if not keep_audio or not info.has_audio:
        return ["-an"]
    if (info.audio_codec or "") in COPYABLE_AUDIO:
        return ["-c:a", "copy"]
    return ["-c:a", "aac", "-b:a", "192k"]
```

三层逻辑：

1. 用户不要音轨，或原片没音轨 → `-an`；
2. 编码在白名单里 → `-c:a copy`，**零损失、零耗时**；
3. 其它编码（如 pcm、flac）→ 转 AAC 192kbps，保证 mp4 容器能装。

这也是为什么 `probe()` 要记 `audio_codec`——它直接决定音轨处理路径。

---

## 6. delogo 的 FFmpeg 命令长什么样

`process_delogo()` 组装出的命令大致是：

```bash
ffmpeg -hide_banner -loglevel error -y \
  -i video.mp4 \
  -vf "delogo=x=576:y=1226:w=129:h=41" \
  -c:v libx264 -crf 18 -preset medium -pix_fmt yuv420p \
  -movflags +faststart \
  -c:a copy \
  video_dewm.mp4
```

`build_delogo()` 支持多个框，用逗号串起多个滤镜；`clamp_box()` 会把坐标限制在画面内，避免滤镜参数越界导致 FFmpeg 直接失败。

---

## 7. inpaint 的管道命令（预告）

第 05 篇会详细讲，这里先看骨架：

```bash
ffmpeg -f rawvideo -pix_fmt bgr24 -s 720x1280 -r 30.000000 -i pipe:0 \
       -i video.mp4 -map 0:v:0 -map 1:a:0 -c:a copy \
       -c:v libx264 -crf 18 -preset medium -pix_fmt yuv420p out.mp4
```

- 第一个输入是 Python 逐帧写进 stdin 的 BGR 原始帧；
- 第二个输入是原片，只为了拿音轨；
- `-map` 明确「视频取第 0 个输入，音频取第 1 个输入」，避免选错流。

---

## 8. 小练习

1. 找一个 VFR 视频，对比 `ffprobe -show_streams` 里的 `r_frame_rate` 和 `avg_frame_rate`，理解为什么要用后者。
2. 把一个视频的音轨转成 flac 再跑工具，观察日志里是否走了 AAC 重编码分支。
3. 给 `run_ffmpeg_with_progress()` 加一个 `on_stderr` 回调，把 FFmpeg 的警告也转发到界面日志。
4. 思考：如果没有 ffprobe，`duration=0`，进度条会怎样？（提示：看 `frac` 计算处的 `if duration > 0`）

下一步：[04 自动定位算法核心](./04-自动定位算法核心.md) —— 进入这个项目最有意思的部分。
