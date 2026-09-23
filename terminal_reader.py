#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
terminal_reader.py — 伪装终端小说阅读器  v1.0

在真实终端（cmd.exe / PowerShell / Windows Terminal）里直接运行，把小说正文
伪装成"正在跑的命令输出"，例如：

    C:\\Users\\Administrator\\Documents> type novel\\book.txt | more +1280
    [2024-05-12 14:03:21] [INFO] 萧炎站在悬崖边上，望着远处的山峰。
    D:\\proj\\src\\engine.cpp:142: 风从谷底吹上来，卷起阵阵烟尘。
    [ 47%] Building CXX object 他握紧了拳头，指甲嵌进掌心。
    [INFO] cache hit ratio 0.93
    [INFO] written 312 records to out\\ch03.part

设计目标（v1.0 硬性约束）：
  * 纯标准库，零第三方依赖
  * 单线程，绝不使用 threading / queue —— 按键永远优先于打字动画，从根上杜绝"按键没反应/卡死"
  * 中文一律走 WriteConsoleW 输出，规避 GBK 代码页乱码
  * 退出时恢复原始屏幕、标题、字体、光标（备用屏缓冲）

伪装细节：
  * 11 种伪装样式（cmd_type / path_lineno / log_ts / build_out / pip_inst / py_trace /
    tail_n / git_stat / docker_step / test_case / http_access），每 2 页轮换一种主导样式；
    页内再抽查几行加同类前缀，一页之内风格统一（混四五种反而像刻意伪造）
  * 每页正文之间夹 2~3 条假命令（按每 4 行正文一条配比，短页自动减），
    紧跟命令的正文自然读成它的"输出"；页内另插 3~6 条噪声行，散布在各处而非堆在页尾
  * 正文按中文排版缩进：段首多缩进两个全角空格（＋基础半角缩进），续行只留基础缩进；
    空段落顶格 —— 整页里出现"缩进的空白"反而假
  * 伪装部分按终端配色上色，且**一律用加亮色**：提示符亮绿、命令亮青、参数亮黄、
    错误亮红、警告亮黄、成功亮绿、[INFO] 青、[DEBUG] 亮品红、路径青、行号亮黄、
    时间戳亮蓝、进度亮青（CONFIG['term_color'] 可关）。
    刻意不用暗灰(90)和亮白：终端默认前景色本就是浅灰白，这两种在深色背景上
    根本看不出颜色，"有配色"就白配了

用法：
    python terminal_reader.py 小说.txt          # 直接打开
    python terminal_reader.py                   # 打开书架
    python terminal_reader.py --selftest        # 非交互自检（不开界面）

打包版（PyInstaller 单文件 exe，用法完全相同，可直接把 txt 拖到 exe 上）：
    terminal_reader.exe 小说.txt
    双击 exe 则进书架；--selftest / --preview 等参数同样可用

按键：
    空格 / n / j            下一页（打字中按下则立即显示整页）
    p / k                   上一页
    b / l                   添加书签 / 打开书签列表
    t                       章节目录（打开后 j/k 选章节，回车进入，Esc 返回）
    v                       切换输出速度（fast / normal / slow）
    f                       书架 / 最近打开
    s                       关键字搜索
    g                       跳到百分比（如 35%）
    c                       命令模式
    o                       导入其它 txt（进界面后 1 输路径 / 2 浏览文件夹）
    + / -                   字号增减
    r                       重新随机当前页伪装
    ? / h                   帮助
    q / Esc / Ctrl+C        退出（恢复原终端画面）

每个功能都有一个单字节字母键，不依赖方向键 / PgUp / Home 这类扩展键；
扩展键照样能用，只是不再是非它不可（方向键在不同终端上报的字节差异很大）。
"""

import os
import re
import sys
import json
import time
import random
import argparse
import unicodedata

IS_WINDOWS = os.name == 'nt'

# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------

CONFIG = {
    # 伪装
    'skins': ['cmd_type', 'path_lineno', 'log_ts', 'build_out',
              'pip_inst', 'py_trace', 'tail_n', 'git_stat',
              'docker_step', 'test_case', 'http_access'],
    'skin_switch_every': 2,        # 每 N 页换一次主导皮肤（轮着来，周期性随机重复）
    'prefix_every': (2, 5),        # 页内每 2~5 行随机抽 1 行加前缀
    'noise_per_page': (3, 6),      # 每页插入 3~6 条无正文噪声行
    'cmd_per_page': (2, 3),        # 每页正文之间再插 2~3 条假命令（"命令→输出"成组出现）
    'page_overhead': 12,           # 每页预留行数（页首假命令+页内插命令+页尾收束+噪声+余量）
    # 排版：正文不顶格，整体缩进成"被程序打印出来的文本块"，段首再多缩进一点
    'body_indent': 2,              # 正文相对左边距缩进的半角空格数
    'para_indent': 2,              # 段落首行额外缩进的全角空格数（1 个占 2 列）
    # 打字机
    'speed': 'fast',               # 输出速度档：fast / normal / slow（按 v 切换）
    'typing_cps': 600,             # 每秒输出字符数（apply_speed 会按档位覆盖）
    'typing_jitter': 0.45,         # 节奏抖动比例
    'cmd_typing_cps': 120,         # 假命令行的打字速度（装成有人在敲键盘）
    'line_pause': 0.012,           # 行间停顿秒
    'instant_pause': 0.018,        # 瞬时行之间的停顿（装成程序在跑）
    'max_chars_per_frame': 24,     # 每帧最多补几个字符（太小会让打字速度被帧率卡住）
    # 外观
    'font_face': 'Consolas',
    'font_size': 14,
    'color': True,                 # 使用 ANSI 颜色渲染日志级别（不支持时自动关闭）
    'term_color': True,            # 伪装部分按终端配色（提示符/路径/级别各用老终端经典取色）
    # 伪装身份
    'user': 'Administrator',
    'fake_cwd': r'C:\Users\Administrator\Documents',
    'fake_title': r'C:\Windows\System32\cmd.exe',
    'fake_title_ps': 'Windows PowerShell',
    # 存储
    'state_file': os.path.join(os.path.expanduser('~'), '.terminal_reader_state.json'),
    # 文件浏览器把哪些扩展名当"文本类"（只用来打 [TXT] 标记，不过滤、不挡其它文件）
    'text_exts': ('.txt', '.text', '.md', '.log', '.srt', '.csv', '.json', '.ini', '.cfg', '.epub'),
}

STATE_VERSION = 1

# 输出速度档。真实终端是"整行刷出来"的，所以 fast/normal 档下正文按行瞬现，
# 只有 slow 档才逐字打字（保留原来的文艺观感）。假命令行在所有档位都保留敲键盘感。
SPEEDS = {
    'fast':   {'body_mode': 'instant', 'typing_cps': 600, 'cmd_typing_cps': 120,
               'line_pause': 0.012, 'instant_pause': 0.018, 'max_chars_per_frame': 24},
    'normal': {'body_mode': 'instant', 'typing_cps': 300, 'cmd_typing_cps': 70,
               'line_pause': 0.030, 'instant_pause': 0.045, 'max_chars_per_frame': 12},
    'slow':   {'body_mode': 'typed', 'typing_cps': 90, 'cmd_typing_cps': 26,
               'line_pause': 0.060, 'instant_pause': 0.100, 'max_chars_per_frame': 6},
}
SPEED_ORDER = ['fast', 'normal', 'slow']


def apply_speed(name):
    """切到某个速度档（不认识的档位退回 fast），返回生效的档名。"""
    if name not in SPEEDS:
        name = 'fast'
    CONFIG['speed'] = name
    for k, v in SPEEDS[name].items():
        if k != 'body_mode':
            CONFIG[k] = v
    return name


def body_mode():
    """当前档位下正文行的输出方式。"""
    return SPEEDS.get(CONFIG['speed'], SPEEDS['fast'])['body_mode']


apply_speed(CONFIG['speed'])

HELP_TEXT = """可用按键（伪装成一套程序参数说明）：
  空格/n/j       下一段输出        p/k        上一段输出
  t              列出章节(目录)    f          已处理文件列表(书架)
  s              检索关键字        g          按百分比定位
  b              打点(加书签)      l          打点列表
  c              进入命令模式      o          导入其它 txt
  r              重新生成当前输出  v          切换输出速度(fast/normal/slow)
  + / -          调整字号          ?/h        本帮助
  q              结束进程(退出并还原终端)

列表里（目录/书架/书签/搜索结果/帮助）：
  j/k 移动，空格或 n 向下翻页，p 向上翻页，g/e 跳到首/尾
  Enter 进入/确认当前高亮项    Esc 或 q 返回阅读
  d              删除当前高亮项：书架里 = 把这本书从列表移除（只删记录，
                 磁盘上的 txt 文件和已打的点都不动）；书签列表里 = 删掉该打点

导入界面（阅读中按 o 进入）：
  1 或 p         手工输入 txt 的路径（支持中文/带引号/拖拽进来的路径）
  2 或 f         列文件夹翻找：j/k 选择，Enter 进目录或直接导入文件，
                 Backspace 回上级，w 跳到别处（可直接输盘符如 D:\），Esc 取消

以上每个功能都有单字节字母键，任何终端都发得对；
方向键、PgUp/PgDn、Home/End 同样可用，只是不再是非它不可。"""


# ---------------------------------------------------------------------------
# 文本工具：ANSI 剥离、东亚宽度、折行
# ---------------------------------------------------------------------------

ANSI_RE = re.compile(r'\x1b\[[0-9;]*[A-Za-z]')


def strip_ansi(s):
    return ANSI_RE.sub('', s)


def char_width(ch):
    """单字符显示宽度：全角 2 列，组合符 0 列，其余 1 列。"""
    if unicodedata.combining(ch):
        return 0
    return 2 if unicodedata.east_asian_width(ch) in ('W', 'F') else 1


def display_width(s):
    """字符串显示宽度（忽略 ANSI 转义）。"""
    return sum(char_width(c) for c in strip_ansi(s))


# ---------------------------------------------------------------------------
# 终端配色
# 取的是老 conhost / Windows Terminal 的默认配色：提示符亮绿、路径青、参数黄、
# 时间戳灰、成功绿、警告黄、报错红。正文本身不上色，保持终端默认前景色 ——
# 花哨的是伪装行，正文反而干净，混在一起才像日志里夹着内容。
# ---------------------------------------------------------------------------

# 配色原则：**一律用加亮色**。终端默认前景色本身就是浅灰白，暗灰(90)和亮白(97)
# 在深色背景上几乎分辨不出来 —— 伪装看着像"没有颜色"就等于白伪装了。
TERM_COLORS = {
    'prompt': '1;32',    # 提示符：亮绿
    'cmd': '1;36',       # 命令本体：亮青
    'arg': '1;33',       # 参数/数值：亮黄
    'dim': '94',         # 时间戳/次要输出：亮蓝
    'info': '36',        # [INFO]：青
    'debug': '95',       # [DEBUG]：亮品红
    'warn': '1;33',      # [WARN]：亮黄
    'error': '1;31',     # [ERROR]/Traceback：亮红
    'ok': '1;32',        # 成功/收尾统计：亮绿
    'path': '36',        # 路径：青
    'lineno': '1;33',    # 行号：亮黄
    'pct': '1;36',       # 进度百分比：亮青
    'hash': '1;33',      # 提交号：亮黄
    'accent': '1;35',    # 强调：亮品红
}

# 同一组颜色在"不支持 ANSI"的老 conhost 上的等价 TextAttribute
# （低 4 位是前景色的 BGR 位，0x08 是加亮位；这里同样一个灰都不用）
WIN_ATTRS = {
    'prompt': 0x0A, 'cmd': 0x0B, 'arg': 0x0E, 'dim': 0x09, 'info': 0x03,
    'debug': 0x0D, 'warn': 0x0E, 'error': 0x0C, 'ok': 0x0A, 'path': 0x0B,
    'lineno': 0x0E, 'pct': 0x0B, 'hash': 0x0E, 'accent': 0x0D,
}
DEFAULT_ATTR = 0x07


def wrap_color(text, name):
    """给一段文字套上终端配色；关掉配色或名字为空时原样返回。"""
    code = TERM_COLORS.get(name) if name else None
    if not code or not CONFIG['term_color'] or not CONFIG['color']:
        return text
    return '\x1b[%sm%s\x1b[0m' % (code, text)


def indent_widths():
    """返回 (段首行缩进宽度, 续行缩进宽度, 段首缩进文本, 续行缩进文本)。"""
    bi = max(0, int(CONFIG['body_indent']))
    pi = max(0, int(CONFIG['para_indent']))
    cont = ' ' * bi
    first = cont + '　' * pi
    return display_width(first), display_width(cont), first, cont


def wrap_para(text, width, first_indent=0, cont_indent=0):
    """
    折行成物理行，且段首行/续行用不同的缩进预算。
    返回的物理行**不含缩进本身** —— 缩进由渲染层加，这样分页和渲染共用同一份宽度预算，
    缩进永远不会把某行挤到折行、把分页算错（伪装前缀当初就是踩着这个坑过来的）。
    """
    w0 = max(8, width - first_indent)
    w1 = max(8, width - cont_indent)
    if display_width(text) <= w0:
        return [text]
    lines, cur, w, limit = [], [], 0, w0
    for ch in text:
        cw = char_width(ch)
        if cur and w + cw > limit:
            lines.append(''.join(cur))
            cur, w, limit = [], 0, w1
        cur.append(ch)
        w += cw
    if cur:
        lines.append(''.join(cur))
    return lines


def wrap_text(text, width):
    """按显示宽度硬折行为若干物理行。中文可任意断行，符合终端直觉。"""
    if width < 8:
        width = 8
    if display_width(text) <= width:
        return [text]
    lines, cur, w = [], [], 0
    for ch in text:
        cw = char_width(ch)
        if cur and w + cw > width:
            lines.append(''.join(cur))
            cur, w = [], 0
        cur.append(ch)
        w += cw
    if cur:
        lines.append(''.join(cur))
    return lines


def truncate_width(s, width, tail='…'):
    """按宽度截断（用于目录/列表行）。"""
    if display_width(s) <= width:
        return s
    out, w = [], 0
    limit = max(1, width - display_width(tail))
    for ch in strip_ansi(s):
        cw = char_width(ch)
        if w + cw > limit:
            break
        out.append(ch)
        w += cw
    return ''.join(out) + tail


# ---------------------------------------------------------------------------
# 编码探测与读取
# ---------------------------------------------------------------------------

def detect_encoding(path):
    """返回 (encoding, text)。BOM 优先，其次 utf-8，再退回 gbk/big5/latin-1。"""
    with open(path, 'rb') as fh:
        raw = fh.read()
    if raw.startswith(b'\xef\xbb\xbf'):
        return 'utf-8-sig', raw.decode('utf-8-sig', 'replace')
    if raw.startswith(b'\xff\xfe') or raw.startswith(b'\xfe\xff'):
        return 'utf-16', raw.decode('utf-16', 'replace')
    for enc in ('utf-8', 'gbk', 'big5'):
        try:
            return enc, raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return 'utf-8', raw.decode('utf-8', 'replace')


def normalize_paras(text):
    """把原始文本切成段落列表：统一换行、去行尾空白、压缩连续空行（最多 1 空行）。"""
    text = text.replace('\r\n', '\n').replace('\r', '\n').replace('\u3000', '　')
    raw_lines = text.split('\n')
    paras, blank = [], 0
    for ln in raw_lines:
        ln = ln.rstrip()
        if ln.strip() == '':
            blank += 1
            if blank == 1 and paras:
                paras.append('')
            continue
        blank = 0
        paras.append(ln.rstrip())
    while paras and paras[-1] == '':
        paras.pop()
    return paras


# ---------------------------------------------------------------------------
# 章节切分
# ---------------------------------------------------------------------------

CHAPTER_PATTERNS = [
    re.compile(r'^\s*第\s*[0-9一二三四五六七八九十百千万零两]{1,8}\s*[章回节卷篇集部]'),
    re.compile(r'^\s*Chapter\s+[0-9IVXLivxl]+'),
    re.compile(r'^\s*【第[^】]{1,30}】'),
    re.compile(r'^\s*#{1,3}\s*第[^#]{1,30}'),
    re.compile(r'^\s*(序章|楔子|引子|前言|后记|尾声|终章|番外)[\s：:].{0,30}$'),
    re.compile(r'^\s*(序章|楔子|引子|尾声|终章|番外)\s*$'),
]


def split_chapters(paras):
    """把段落列表切成章节：[{'title':..,'start':段落下标,'end':..}]。"""
    marks = []
    for i, p in enumerate(paras):
        s = p.strip()
        if not s or len(s) > 60:      # 章节标题一般不会很长
            continue
        for pat in CHAPTER_PATTERNS:
            if pat.match(s):
                marks.append((i, s))
                break
    if not marks:
        return [{'title': '全文', 'start': 0, 'end': len(paras)}]
    chapters = []
    if marks[0][0] > 0:
        chapters.append({'title': '开头', 'start': 0, 'end': marks[0][0]})
    for idx, (pos, title) in enumerate(marks):
        end = marks[idx + 1][0] if idx + 1 < len(marks) else len(paras)
        chapters.append({'title': title, 'start': pos, 'end': end})
    return chapters


# ---------------------------------------------------------------------------
# 书
# ---------------------------------------------------------------------------

class Book:
    def __init__(self, path, paras, encoding, chapters):
        self.path = path
        self.paras = paras
        self.encoding = encoding
        self.chapters = chapters

    @property
    def title(self):
        return os.path.splitext(os.path.basename(self.path))[0]

    @property
    def total(self):
        return len(self.paras)

    def chapter_of(self, para_idx):
        """返回段落所属章节下标。"""
        for i, ch in enumerate(self.chapters):
            if ch['start'] <= para_idx < ch['end']:
                return i
        return len(self.chapters) - 1

    @staticmethod
    def load(path):
        if not os.path.isfile(path):
            raise FileNotFoundError(path)
        enc, text = detect_encoding(path)
        paras = normalize_paras(text)
        if not paras:
            raise ValueError('文件为空或无法解析出文本')
        return Book(os.path.abspath(path), paras, enc, split_chapters(paras))


# ---------------------------------------------------------------------------
# 状态存储：进度 / 书签 / 书架 / 设置（原子写）
# ---------------------------------------------------------------------------

class Store:
    def __init__(self, path):
        self.path = path
        self.data = {'version': STATE_VERSION, 'shelf': {}, 'bookmarks': {}, 'settings': {}}
        self._load()

    def _load(self):
        try:
            with open(self.path, 'r', encoding='utf-8') as fh:
                d = json.load(fh)
            if isinstance(d, dict):
                self.data.update(d)
        except (OSError, ValueError):
            pass                                  # 状态文件损坏/不存在：静默用空状态，不阻塞阅读
        for key in ('shelf', 'bookmarks', 'settings'):
            if not isinstance(self.data.get(key), dict):
                self.data[key] = {}

    def save(self):
        tmp = self.path + '.tmp'
        try:
            with open(tmp, 'w', encoding='utf-8') as fh:
                json.dump(self.data, fh, ensure_ascii=False, indent=1)
            os.replace(tmp, self.path)             # 原子替换，避免写坏
        except OSError:
            pass

    # -- 进度 --
    def get_progress(self, path):
        return self.data['shelf'].get(os.path.abspath(path))

    def put_progress(self, book, para_idx):
        key = os.path.abspath(book.path)
        ch = book.chapter_of(para_idx)
        self.data['shelf'][key] = {
            'title': book.title,
            'para': para_idx,
            'percent': round(para_idx / max(1, book.total) * 100, 2),
            'chapter': ch,
            'chapter_title': book.chapters[ch]['title'],
            'updated': int(time.time()),
        }

    def shelf(self, limit=20):
        items = sorted(self.data['shelf'].items(), key=lambda kv: kv[1].get('updated', 0), reverse=True)
        return items[:limit]

    def del_book(self, path):
        """把一本从书架(已处理文件列表)里移除，返回是否真的删到了。

        只删**记录** —— shelf 里的进度条目没了，磁盘上的 txt 文件一动不动；
        该书已打的点(bookmarks)保留，重新导入同一路径后打点还在。
        """
        return self.data['shelf'].pop(os.path.abspath(path), None) is not None

    # -- 书签 --
    def bookmarks(self, path):
        return self.data['bookmarks'].setdefault(os.path.abspath(path), [])

    def add_bookmark(self, book, para_idx, label):
        bms = self.bookmarks(book.path)
        for b in bms:
            if abs(b['para'] - para_idx) <= 2:
                return False                        # 已有邻近书签
        bms.append({'para': para_idx, 'label': label, 'ts': int(time.time())})
        bms.sort(key=lambda b: b['para'])
        return True

    def del_bookmark(self, book, index):
        bms = self.bookmarks(book.path)
        if 0 <= index < len(bms):
            bms.pop(index)
            return True
        return False

    # -- 设置 --
    def get_setting(self, key, default):
        return self.data['settings'].get(key, default)

    def set_setting(self, key, value):
        self.data['settings'][key] = value


# ---------------------------------------------------------------------------
# 伪装引擎
# ---------------------------------------------------------------------------

FAKE_FILES = [
    r'engine.cpp', r'renderer.cpp', r'net_client.cpp', r'parser.cpp', r'pool.cpp',
    r'audio_mix.cpp', r'scene_loader.cpp', r'asset_pack.cpp', r'main.cpp', r'utils.cpp',
    r'loader.py', r'schema.py', r'worker.py', r'cli.py', r'dataset.py',
]
FAKE_DIRS = [r'D:\proj\src', r'D:\proj\build\obj', r'E:\work\core\src', r'D:\proj\third_party',
             r'D:\proj\venv\Lib\site-packages', r'D:\proj\src\core']

FAKE_COMMANDS = [
    r'type novel\{name}.txt | more +{off}',
    r'python -m tools.dump --file novel\{name}.txt --chunk {chunk} --offset {off}',
    r'findstr /n "." novel\{name}.txt | more +{off}',
    r'logtail -n 60 --follow --grep "chunk={chunk}"',
    r'sed -n "{off},{off2}p" novel\{name}.txt',
    r'python scripts\trace_chunk.py --id {chunk} --window {off}',
    r'git diff --stat HEAD~{chunk} -- src',
    r'ninja -C build obj\core{chunk}.obj',
    r'pip install -r requirements.txt --progress-bar off',
    r'python -m pytest tests -k batch{chunk} --tb=short',
    r'powershell -c "Get-Content out\\part{chunk}.log -Tail 80"',
    r'cargo build --release --message-format short',
]

# 每种伪装样式配套的"页首指令"，让整页的伪装看起来是同一件事在做
SKIN_COMMANDS = {
    'pip_inst': [
        r'pip install -r reqs\c{chunk}.txt --no-cache-dir',
        r'python -m pip install --upgrade {name}-loader',
    ],
    'py_trace': [
        r'python -m app.batch --idx {chunk} --offset {off}',
        r'python -X faulthandler scripts\replay.py --part {chunk}',
    ],
    'tail_n': [
        r'powershell -c "Get-Content logs\app-{chunk}.log -Tail 120"',
        r'logtail -n 120 --file logs\worker-{chunk}.log',
    ],
    'git_stat': [
        r'git show --stat --oneline HEAD~{chunk}',
        r'git diff --numstat src\core -- {off}',
    ],
}

PKG_NAMES = ['lxml', 'numpy', 'regex', 'aiohttp', 'pyyaml', 'rich', 'click', 'pillow',
             'chardet', 'msgpack', 'protobuf', 'uvloop', 'orjson', 'markupsafe']
MOD_NAMES = ['loader', 'schema', 'worker', 'pool', 'parser', 'net_client', 'asset_pack',
             'renderer', 'scene_loader', 'audio_mix', 'cli', 'utils']

NOISE_LINES = [
    '[INFO] cache hit ratio 0.{r:02d}',
    '[INFO] pool 4 workers idle, queue {q}',
    '[DEBUG] heap 41.{r}MB / 128MB',
    'warning: unused variable \'tmp\' [-Wunused-variable]',
    '[INFO] sha1 {hex} ok',
    '[INFO] flushed {n} records (avg 0.0{s}ms)',
    '0x7ff8a2c1{hex} -> 0x00000001400012a0',
    '[INFO] retry 0/{q} on socket reset',
    '[INFO] tls handshake ok in 31ms',
    '[DEBUG] gc pause 1.{r}ms',
    'note: candidate function not viable: no known conversion',
    '[INFO] loading locale zh_CN.UTF-8',
    '[INFO] mmap 128MB ok',
    '  Using cached {pkg}-{d}.{d}.{d}-py3-none-any.whl ({d}.{d} MB)',
    'Requirement already satisfied: {pkg} in d:\\proj\\venv\\lib\\site-packages ({d}.{d}.{d})',
    '  File "d:\\proj\\src\\{mod}.py", line {ln}, in <module>',
    'ImportError: DLL load failed while importing _core: 找不到指定的模块。',
    'INFO:     application startup complete.',
    'INFO:     127.0.0.1:{port} - "GET /api/v1/items HTTP/1.1" 200 OK',
    '{hex}  src\\core\\{mod}.cpp  | {n} +++++++++---------',
    'create mode 100644 src\\gen\\{mod}.h',
    '[INFO] checkpoint {chunk} saved to runs\\exp{d}\\last.pt',
    '[WARN] loss plateau for {n} steps, reducing lr to 0.000{d}',
    'Writing objects: 100% ({n}/{n}), {d}.{d} MiB | {d}.{d} MiB/s, done.',
    # --- 编译 / 构建（缩进的续行是真实构建日志最显眼的特征）---
    '  [{r}%] Building CXX object src\\core\\CMakeFiles\\core.dir\\{mod}.cpp.obj',
    '  [{r}%] Linking CXX shared library bin\\core.dll',
    '  instantiated from \'Result<T> process<T>(const T &)\' here',
    'In file included from src\\core\\{mod}.cpp:{ln}:',
    'note: see declaration of \'{mod}\'',
    'src\\core\\{mod}.cpp({ln}): warning C4267: "return": 从"size_t"转换到"int"，可能丢失数据',
    'src\\core\\{mod}.cpp({ln}): error C2065: "g_cache": 未声明的标识符',
    '  ../../../src/core/{mod}.cpp:{ln}: undefined reference to `Ctx::flush()\'',
    # --- 测试 ---
    'tests\\test_{mod}.py::test_batch_{chunk} PASSED                        [{r}%]',
    '  -------- 已收集 {n} 个用例 --------',
    '==================== {n} passed, {d} warnings in {d}.{d}s ====================',
    'FAILED tests\\test_{mod}.py::test_chunk_{chunk} - AssertionError: assert 0 == {d}',
    # --- 依赖 / 打包 ---
    '  Downloading {pkg}-{d}.{d}.{d}-cp312-cp312-win_amd64.whl ({d}.{d} MB)',
    '  Preparing metadata (setup.py) ... done',
    'Building wheel for {pkg} (pyproject.toml) ... done',
    'Successfully built {pkg}',
    'Created wheel for {pkg}: filename={pkg}-{d}.{d}.{d}-py3-none-any.whl size={n}',
    # --- 容器 ---
    'Step {chunk}/{n} : RUN python -m build --wheel',
    ' ---> Running in {hex}',
    ' ---> {hex}',
    'Removing intermediate container {hex}',
    # --- 服务 / 网络 ---
    '127.0.0.1:{port} - - "{d}/{d}/{d} 12:00:0{d}" "GET /static/app.js HTTP/1.1" 304 -',
    'DEBUG:asyncio:Using proactor: IocpProactor',
    'INFO:     {port} - "POST /api/v1/chunk HTTP/1.1" 201 Created',
    # --- 进程 / 内存 ---
    '[DEBUG] allocated {n} objects, {d} freed, generation 1',
    '[INFO] working set {n}.{d} MB, private {n}.{d} MB',
    '  GC {d} collections, {d}.{d}ms total',
    # --- Windows / cmd 自己的输出 ---
    '系统找不到指定的文件。',
    '系统找不到指定的路径。',
    '已复制         1 个文件。',
    '      0 个文件已复制。',
    'Microsoft Windows [版本 10.0.19045.4291]',
    'Copyright (c) Microsoft Corporation. 保留所有权利。',
    # --- 杂项 ---
    '  at <anonymous> (d:\\proj\\src\\{mod}.js:{ln}:{d})',
    'elapsed {d}.{d}s, cpu {d}%, mem {n}MB',
    '  -- 已完成 {chunk}/{n}，剩余约 {d} 秒',
]

CMD_COLORS = {'ERROR': '1;31', 'WARN': '1;33', 'WARNING': '1;33', 'OK': '1;32', 'DONE': '1;32'}
DEFAULT_INFO_COLOR = '36'


def skin_names():
    return list(CONFIG['skins'])


def _pct():
    return '[ %2d%%]' % random.randint(3, 97)


def _hex(n):
    return '%0*x' % (n, random.randint(0, 16 ** n - 1))


def noise_line():
    return random.choice(NOISE_LINES).format(
        r=random.randint(10, 99), q=random.randint(0, 6), d=random.randint(0, 9),
        n=random.randint(20, 400), hex=_hex(6), s=random.randint(1, 9),
        pkg=random.choice(PKG_NAMES), mod=random.choice(MOD_NAMES),
        ln=random.randint(10, 900), port=random.randint(1024, 9000),
        chunk=random.randint(1, 120))


NOISE_ERR_RE = re.compile(r'(error C\d|ERROR|FAILED|Traceback|AssertionError'
                          r'|undefined reference|ImportError|错误)')
NOISE_WARN_RE = re.compile(r'(warning|WARN|警告|note:)')
NOISE_OK_RE = re.compile(r'(PASSED|passed|Successfully|已复制|done\.|Build finished)')


def noise_color(text):
    """按内容取终端配色，模仿真实日志的级别着色。
    噪声行整体压暗（dim），只让级别本身跳出来 —— 正文保持默认前景色反而更醒目。"""
    t = text.strip()
    if NOISE_ERR_RE.search(t):
        return 'error'
    if NOISE_WARN_RE.search(t):
        return 'warn'
    if NOISE_OK_RE.search(t):
        return 'ok'
    if t.startswith('[INFO]'):
        return 'info'
    if re.match(r'^\[\s*\d+%\]', t) or re.match(r'^Step \d+/\d+', t) or t.startswith('  ['):
        return 'pct'
    return 'dim'


def noise_item():
    """返回 (文本, 终端配色名)。"""
    text = noise_line()
    return text, noise_color(text)


class Skin:
    """一种伪装样式。produce() 返回一组渲染行。"""

    def __init__(self, name):
        self.name = name

    # ---- 页首假命令（块式伪装） ----
    def head(self, book, page_index, para_idx):
        tpl = random.choice(SKIN_COMMANDS.get(self.name) or FAKE_COMMANDS)
        # 偏移量跟着阅读进度走，看起来就像程序真的在一段段往下处理数据
        off = max(1, para_idx + 1)
        cmd = tpl.format(
            name=book.title, chunk=page_index + 1,
            off=off, off2=off + random.randint(6, 40),
        )
        prompt = random.choice([CONFIG['fake_cwd'] + '>', 'PS ' + CONFIG['fake_cwd'] + '>'])
        # 提示符亮绿、命令体亮白：老终端里敲命令就是这个样子。
        # prefix_len 取提示符长度：渲染层靠它把"提示符"和"命令"分成两段上色。
        return {'text': prompt + ' ' + cmd, 'mode': 'typed', 'kind': 'cmd',
                'prefix_len': len(prompt) + 1, 'deco_len': len(prompt) + 1,
                'pre_color': 'prompt', 'color': 'cmd'}

    # ---- 行前缀（抽查行伪装） ----
    def prefix(self, width):
        """返回 (行首前缀文本, 终端配色名)。只在整段独占一个物理行时才加。"""
        if self.name == 'path_lineno':
            d = random.choice(FAKE_DIRS)
            return ('%s\\%s:%d: ' % (d, random.choice(FAKE_FILES), random.randint(20, 900)),
                    'path')
        if self.name == 'log_ts':
            lvl = random.choice(['INFO', 'INFO', 'DEBUG', 'WARN'])
            ts = time.strftime('%Y-%m-%d %H:%M:') + '%02d' % random.randint(0, 59)
            return ('[%s] [%s] ' % (ts, lvl),
                    {'INFO': 'info', 'DEBUG': 'dim', 'WARN': 'warn'}.get(lvl, 'dim'))
        if self.name == 'build_out':
            return ('%s ' % _pct(), 'pct')
        if self.name == 'pip_inst':
            pkg = random.choice(PKG_NAMES)
            ver = '%d.%d.%d' % (random.randint(0, 5), random.randint(0, 20), random.randint(0, 9))
            return random.choice([
                ('Collecting %s==%s  ' % (pkg, ver), 'cmd'),
                ('  Using cached %s-%s-py3-none-any.whl  ' % (pkg, ver), 'dim'),
                ('Requirement already satisfied: %s  ' % pkg, 'dim'),
            ])
        if self.name == 'py_trace':
            return random.choice([
                ('  File "d:\\proj\\src\\%s.py", line %d, in <module>  '
                 % (random.choice(MOD_NAMES), random.randint(10, 900)), 'lineno'),
                ('    return self._dispatch(%s)  ' % random.choice(MOD_NAMES), 'dim'),
                ('Traceback (most recent call last):  ', 'error'),
            ])
        if self.name == 'tail_n':
            return random.choice([
                ('==> logs\\app-%s.log <==  ' % time.strftime('%m%d'), 'path'),
                ('==> logs\\worker-%02d.log <==  ' % random.randint(1, 24), 'path'),
                ('%s: ' % time.strftime('%H:%M:%S'), 'dim'),
            ])
        if self.name == 'git_stat':
            return random.choice([
                ('%s ' % _hex(7), 'hash'),
                ('modified:   src\\core\\%s.cpp  ' % random.choice(MOD_NAMES), 'warn'),
                ('%d +%s -%d  src\\%s.cpp  '
                 % (random.randint(1, 40), '+' * random.randint(1, 6), random.randint(0, 9),
                    random.choice(MOD_NAMES)), 'ok'),
            ])
        if self.name == 'docker_step':
            return random.choice([
                ('Step %d/%d : RUN pip install -r requirements.txt  '
                 % (random.randint(2, 30), random.randint(30, 60)), 'pct'),
                (' ---> Running in %s  ' % _hex(6), 'dim'),
                (' ---> %s  ' % _hex(6), 'hash'),
            ])
        if self.name == 'test_case':
            return random.choice([
                ('tests\\test_%s.py::test_%s_%d PASSED  '
                 % (random.choice(MOD_NAMES), random.choice(MOD_NAMES),
                    random.randint(1, 90)), 'ok'),
                ('tests\\test_%s.py::test_%s_%d FAILED  '
                 % (random.choice(MOD_NAMES), random.choice(MOD_NAMES),
                    random.randint(1, 90)), 'error'),
                ('  %s [ %2d%%]  ' % ('-' * random.randint(4, 10), random.randint(3, 97)), 'dim'),
            ])
        if self.name == 'http_access':
            return random.choice([
                ('127.0.0.1:%d "GET /api/%s" 200 %d  '
                 % (random.randint(1024, 9000), random.choice(MOD_NAMES),
                    random.randint(120, 9000)), 'info'),
                ('127.0.0.1:%d "POST /chunk" 201 %d  '
                 % (random.randint(1024, 9000), random.randint(1, 900)), 'ok'),
                ('[%s] %d bytes sent  ' % (time.strftime('%H:%M:%S'),
                                           random.randint(100, 9000)), 'dim'),
            ])
        return '', None        # cmd_type 不在行内加前缀

    def tail(self, book, page_index):
        text = random.choice([
            '[INFO] written %d records to out\\ch%02d.part' % (random.randint(80, 400), random.randint(1, 40)),
            '[INFO] done in 0.%03ds' % random.randint(60, 990),
            '[INFO] %d files changed, %d insertions(+), %d deletions(-)'
            % (random.randint(1, 6), random.randint(4, 300), random.randint(0, 20)),
            'Build finished at %s (took %dms)' % (time.strftime('%H:%M:%S'), random.randint(90, 900)),
            'Successfully installed %s-%d.%d.%d'
            % (random.choice(PKG_NAMES), random.randint(0, 5), random.randint(0, 20), random.randint(0, 9)),
            '[INFO] %d passed, %d skipped in %.2fs' % (random.randint(4, 60), random.randint(0, 3),
                                                       random.uniform(0.4, 9.9)),
            'Copied %d file(s), %d bytes' % (random.randint(1, 9), random.randint(1000, 90000)),
            '[INFO] flushed %d blocks, sync overhead 0.%03ds' % (random.randint(10, 90),
                                                                 random.randint(10, 900)),
            '实时保护: 已扫描 %d 个文件，未发现威胁。' % random.randint(20, 900),
        ])
        return {
            'text': text,
            'mode': 'instant', 'kind': 'info', 'prefix_len': 0, 'deco_len': 0,
            'pre_color': None, 'color': noise_color(text),
        }


def colorize(text, kind):
    if not CONFIG['color']:
        return text
    m = re.search(r'\[(ERROR|WARN|WARNING|OK|DONE|INFO|DEBUG)\]', text)
    code = None
    if m:
        code = CMD_COLORS.get(m.group(1))
        if code is None and m.group(1) == 'INFO' and kind == 'noise':
            code = DEFAULT_INFO_COLOR
    if code:
        return '\x1b[%sm%s\x1b[0m' % (code, text)
    return text


def build_page(book, page_paras, page_index, width, skin_cycle):
    """
    把一页的段落渲染成 [{text, mode, kind, prefix_len, deco_len, pre_color, color}, ...]。
    page_paras: [(para_idx, [物理行...]), ...]
    绝不改写正文内容：只在行首追加伪装前缀、在正文前加缩进。
    正文起点恒等于 deco_len（伪装前缀 + 缩进），正文本身零损伤。
    """
    # 周期性随机换主导皮肤
    names = skin_names()
    skin = Skin(names[(page_index // max(1, CONFIG['skin_switch_every']) + skin_cycle) % len(names)])
    # 页内抽查行统一用主导皮肤的样式：真实日志一页之内风格是一致的，
    # 一页里混四五种前缀反而像刻意伪造。主导皮肤是 cmd_type 时页内就不加前缀。
    inline = skin if skin.name != 'cmd_type' else None
    _, _, first_ind, cont_ind = indent_widths()

    out = []
    out.append(skin.head(book, page_index, page_paras[0][0] if page_paras else 0))

    # 页内插命令的落点：只能落在"某个段落的第一行"之前。
    # 插在折行段落中间会把一句话劈成两半，一眼就假；第 0 行前面已经有页首命令了，
    # 所以从第 1 段开始才是候选。真实终端本来就是一屏好几条命令带着各自的输出。
    heads, acc = [], 0
    for i, (_, phys) in enumerate(page_paras):
        if i:
            heads.append(acc)
        acc += len(phys)
    clo, chi = CONFIG['cmd_per_page']
    # 命令与正文按比例配：每 4 行正文才夹一条命令。只按 cmd_per_page 抽的话，
    # 段落少的短页会出现"5 条命令配 4 行字"，那是命令刷屏，不像在读小说。
    n_body = sum(len(phys) for _, phys in page_paras)
    per_cmd = max(1, n_body // 4)
    n_cmd = min(random.randint(clo, chi), len(heads), per_cmd if n_body >= 3 else 0)
    cmd_at = set(random.sample(heads, n_cmd)) if n_cmd else set()

    # 噪声行配额：散布在各处（紧跟命令最像"命令的即时输出"），而不是整齐堆在页尾
    noise_left = random.randint(*CONFIG['noise_per_page'])

    def push_noise():
        nonlocal noise_left
        nt, nc = noise_item()
        out.append({'text': nt, 'mode': 'instant', 'kind': 'noise',
                    'prefix_len': 0, 'deco_len': 0,
                    'pre_color': None, 'color': nc})
        noise_left -= 1

    # 页内抽查行：先定好"第几行加前缀"，而不是顺序数到第 N 行 ——
    # 顺序数在一页不到 8 行时永远轮不到，整页看不到任何行内伪装（真机上就是这样）。
    # 候选只取"单物理行且非空"的段落：折行段落和空行的落点等于白抽。
    cand = []
    li = -1
    for _, phys in page_paras:
        for k in range(len(phys)):
            li += 1
            if k == 0 and phys[k].strip():
                cand.append(li)
    plo, phi = CONFIG['prefix_every']
    per = max(1, random.randint(plo, phi))          # 平均每 per 个候选行抽 1 行
    want = 0 if not cand else min(4, max(1, len(cand) // per))
    slack = len(cand)

    body_lines = -1
    for para_idx, phys in page_paras:
        # 前缀只贴在"段落的第一个物理行"前面：不管段落折不折行，都绝不会插进
        # 一句话的中间；折行段落的续行不带前缀，正是真实日志折行的样子。
        for li, text in enumerate(phys):
            body_lines += 1
            if body_lines in cmd_at:
                # 再下一条命令：紧跟其后的正文自然读成它的"输出"
                out.append(skin.head(book, page_index, para_idx))
                if noise_left > 0 and random.random() < 0.7:
                    push_noise()
            elif li == 0 and noise_left > 0 and random.random() < 0.15:
                push_noise()
            # 段首行多缩进一点，续行只留基础缩进：正文读起来才是"一段一段"的
            # 空段落保持顶格空行：整页里出现"缩进的空白"反而显得假
            ind = (first_ind if li == 0 else cont_ind) if text.strip() else ''
            pref, pcolor = '', None
            if li == 0 and text.strip() and want > 0 and inline is not None:
                slack = max(0, slack - 1)
                # want/slack 概率撒点：剩余候选不够了就必须落，保证本页一定抽得中；
                # 装不下的行不消耗配额，顺延给后面的短行
                if slack < want or random.random() < want / float(slack + 1):
                    # 同一皮肤的前缀长度差别很大（git_stat 从 8 列的短哈希到 31 列的
                    # "modified: ..." 都有），装不下就再要一条；最多试 6 次
                    for _ in range(6):
                        p, pc = inline.prefix(width)
                        if p and display_width(p) + display_width(ind) + display_width(text) <= width:
                            pref, pcolor = p, pc
                            want -= 1
                            break
            deco = pref + ind
            out.append({
                'text': deco + text,
                'mode': body_mode(),
                'kind': 'body' if not pref else 'annotated',
                'prefix_len': len(pref),
                'deco_len': len(deco),
                'pre_color': pcolor,
                'color': None,          # 正文用终端默认前景色
            })

    # 剩下没撒出去的噪声行收在页尾
    while noise_left > 0:
        push_noise()

    out.append(skin.tail(book, page_index))
    return out


def render_line(line):
    """把一行渲染成带 ANSI 配色的文本（不含换行）：伪装前缀 / 缩进 / 正文各配各色。
    预览/自检走这里；真实输出走 WinConsole.write_seg —— 老 conhost 没有 VT 时
    它要改走 SetConsoleTextAttribute 分段写，那条路是这条路的等价替身。"""
    text = line['text']
    pl = min(len(text), line.get('prefix_len', 0))
    d = min(len(text), line.get('deco_len', 0))
    return (wrap_color(text[:pl], line.get('pre_color'))
            + wrap_color(text[pl:d], None)
            + wrap_color(text[d:], line.get('color')))


# ---------------------------------------------------------------------------
# 分页
# ---------------------------------------------------------------------------

class Paginator:
    """
    以"原文段落"为最小单位分页。
    进度/搜索/书签都记段落下标，屏幕宽度变化时只需重算物理行，进度不丢。
    """

    def __init__(self, book, width, height):
        self.book = book
        self.width = width
        self.height = height
        self.reflow(width, height)

    def reflow(self, width, height):
        """高度变化只重算分页；只有宽度变化才重新折行（全文字折行是这里最贵的一步）。"""
        self.height = height
        # cap = 可见行数 - 伪装开销(页首假命令+页尾收束+噪声) - 1
        # 末尾那个 -1 是必须的：写满最后一行的换行会触发终端滚屏，把页首挤掉
        self.cap = max(3, height - CONFIG['page_overhead'] - 1)
        if not getattr(self, 'wrapped', None) or width != self.width:
            self.width = width
            if width:
                # 缩进也占宽度，折行必须按缩进后的可用宽度算，否则渲染时会挤到折行
                fw, cw, _, _ = indent_widths()
                self.wrapped = [wrap_para(p, width, fw, cw) for p in self.book.paras]
            else:
                self.wrapped = [[p] for p in self.book.paras]
        self.pages = []                              # [(开始段落, 结束段落)]
        start, used = 0, 0
        for i, phys in enumerate(self.wrapped):
            n = len(phys)
            if used and used + n > self.cap:
                self.pages.append((start, i))
                start, used = i, 0
            used += n
            if used >= self.cap:                     # 单段就超长时也要强制断页
                self.pages.append((start, i + 1))
                start, used = i + 1, 0
        if start < len(self.wrapped) or not self.pages:
            self.pages.append((start, len(self.wrapped)))

    def page_of(self, para_idx):
        lo, hi = 0, len(self.pages) - 1
        while lo < hi:
            mid = (lo + hi) // 2
            if para_idx < self.pages[mid][0]:
                hi = mid - 1
            elif para_idx >= self.pages[mid][1]:
                lo = mid + 1
            else:
                return mid
        return max(0, min(lo, len(self.pages) - 1))

    def page_paras(self, index):
        a, b = self.pages[index]
        return [(i, self.wrapped[i]) for i in range(a, b)]

    def total_pages(self):
        return len(self.pages)

    def find(self, needle, from_para):
        """从 from_para 往后搜索，返回匹配的段落下标列表。"""
        needle = needle.lower()
        hits = []
        for i in range(max(0, from_para), self.book.total):
            if needle in self.book.paras[i].lower():
                hits.append(i)
                if len(hits) >= 200:
                    break
        return hits


# ---------------------------------------------------------------------------
# Windows 控制台（ctypes）
# ---------------------------------------------------------------------------

if IS_WINDOWS:
    import ctypes
    from ctypes import wintypes

    class COORD(ctypes.Structure):
        _fields_ = [('X', ctypes.c_short), ('Y', ctypes.c_short)]

    class SMALL_RECT(ctypes.Structure):
        _fields_ = [('Left', ctypes.c_short), ('Top', ctypes.c_short),
                    ('Right', ctypes.c_short), ('Bottom', ctypes.c_short)]

    class CONSOLE_SCREEN_BUFFER_INFO(ctypes.Structure):
        _fields_ = [('dwSize', COORD), ('dwCursorPosition', COORD),
                    ('wAttributes', ctypes.c_ushort), ('srWindow', SMALL_RECT),
                    ('dwMaximumWindowSize', COORD)]

    class CONSOLE_FONT_INFOEX(ctypes.Structure):
        _fields_ = [('cbSize', ctypes.c_ulong), ('nFont', ctypes.c_ulong),
                    ('dwFontSize', COORD), ('FontFamily', ctypes.c_uint),
                    ('FontWeight', ctypes.c_uint), ('FaceName', ctypes.c_wchar * 32)]

    class CONSOLE_CURSOR_INFO(ctypes.Structure):
        _fields_ = [('dwSize', ctypes.c_ulong), ('bVisible', wintypes.BOOL)]


# VT 输入序列的终结符 → 键名。若终端以 VT 输入模式上报按键（Windows Terminal /
# 新版 conhost），方向键是 \x1b[A、PgUp 是 \x1b[5~；只认 \x1b 前缀会把它们误判成 Esc。
#
# 关键：**不能拿精确字符串去查表**。ConPTY 经常把修饰符参数一并送出，实际收到的是
# \x1b[1;1A / \x1b[5;1~ / \x1b[1;5C 这类形式（甚至 \x1b[1;2;3A 多段参数），精确
# 字典会把它们全部漏成 'special' —— 现象就是"进了目录按方向键/PgUp 毫无反应"。
# 所以按「终结符 + 第一个数字参数」宽容解析。
VT_FINAL_KEYS = {
    'A': 'up', 'B': 'down', 'C': 'right', 'D': 'left',
    'H': 'home', 'F': 'end', 'Z': 'shift-tab',
}
VT_TILDE_KEYS = {
    '1': 'home', '2': 'ins', '3': 'del', '4': 'end',
    '5': 'pgup', '6': 'pgdn', '7': 'home', '8': 'end',
}


def vt_key(seq):
    """把 \\x1b 之后的字节序列翻译成键名。空序列 = 真正的 Esc。

    \\x1b[A → up；\\x1bOA → up；\\x1b[1;1A / \\x1b[1;5C → up / right（修饰符忽略）；
    \\x1b[5~ / \\x1b[5;1~ → pgup；\\x1b[3~ → del。认不出来返回 'special'。
    """
    if not seq:
        return 'esc'
    if seq[0] == 'O':                        # SS3 形式：\x1bOA / \x1bO1;2A ...
        body = seq[1:]
        if not body:
            return 'special'
        final = body[-1]
        return VT_FINAL_KEYS.get(final, 'special') if final.isupper() else 'special'
    if seq[0] != '[':
        return 'special'
    body = seq[1:]
    if not body or body[0] in '?><=':
        return 'special'                     # \x1b[?...c 之类是终端响应，不是按键
    final = body[-1]
    first = body[:-1].split(';')[0]
    if final == '~':
        return VT_TILDE_KEYS.get(first, 'special')
    if final.isupper():                      # 按键序列的终结符一定是大写字母
        return VT_FINAL_KEYS.get(final, 'special')
    return 'special'


def _hex_bytes(s):
    """把读到的原始字节转成可打印十六进制，用于面板诊断行（不打印控制字符）。"""
    return ' '.join('%02X' % (ord(c) & 0xFF) for c in s)


# 传统输入模式（老式 conhost）下，方向键/PgUp 等由 msvcrt 上报成 \x00 或 \xe0
# 前缀 + 一个扫描码字符，映射关系如下。
EXT_SEQ_KEYS = {
    'H': 'up', 'P': 'down', 'I': 'pgup', 'Q': 'pgdn',
    'G': 'home', 'O': 'end', 'S': 'del', 'R': 'ins',
    'K': 'left', 'M': 'right',
}

# 扩展键第二字节 / VT 序列后续字节的最长等待窗口（秒）。
# 只在真的读到 \x00、\xe0、\x1b 时才等待，不影响正常打字节奏。
KEY_EXT_WAIT = 0.060


def ext_key(code):
    """把 \x00/\xe0 之后的扫描码翻译成键名。空 = 前缀本身，未知 = 'special'。"""
    if not code:
        return 'special'
    return EXT_SEQ_KEYS.get(code.upper(), 'special')


def _peek_more(msvcrt, want=1, timeout=KEY_EXT_WAIT, stop_early=False):
    """在 timeout 秒内尽量凑齐 want 个后续字节（拿不满也把已拿到的返回）。

    读到 \\x00/\\xe0 后立刻 kbhit() 经常是 False —— 扫描码第二字节还没进队列，
    于是方向键/PgUp 被判成 'special' 丢掉：面板里表现为"按方向键光标不动、按
    PgUp 也不翻页，但面板又不退出"。所以这里必须轮询等待，不能一问就走。
    """
    buf = ''
    t0 = time.time()
    while len(buf) < want and time.time() - t0 < timeout:
        if msvcrt.kbhit():
            c = msvcrt.getwch()
            buf += c
            if stop_early and (c.isalpha() or c == '~'):
                break                # ANSI 序列的终结符到了
        else:
            time.sleep(0.001)
    return buf


class WinConsole:
    """封装控制台读写、备用屏、标题、字体。非 Windows 或失败时优雅降级。"""

    STD_OUT = -11
    STD_IN = -10
    VT_PROCESSING = 0x0004
    ATTR_REVERSE = 0x4000            # COMMON_LVB_REVERSE_VIDEO：无 ANSI 时的反显高亮
    _attr = DEFAULT_ATTR             # 无 ANSI 时当前生效的前景色（用于去重）

    def __init__(self):
        self.ok = False
        self.real = False
        self.vt = False
        self.alt = False
        self.cols, self.rows = 80, 25
        self._orig_title = None
        self._orig_font = None
        self._orig_out_mode = None
        if not IS_WINDOWS:
            self._fallback_size()
            return
        k = ctypes.windll.kernel32
        self.k = k
        k.GetStdHandle.restype = wintypes.HANDLE
        k.GetStdHandle.argtypes = [ctypes.c_int]
        self.hout = k.GetStdHandle(self.STD_OUT)
        k.WriteConsoleW.argtypes = [wintypes.HANDLE, wintypes.LPCWSTR, wintypes.DWORD,
                                    ctypes.POINTER(wintypes.DWORD), wintypes.LPVOID]
        k.WriteConsoleW.restype = wintypes.BOOL
        k.GetConsoleScreenBufferInfo.argtypes = [wintypes.HANDLE,
                                                 ctypes.POINTER(CONSOLE_SCREEN_BUFFER_INFO)]
        k.SetConsoleMode.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        k.GetConsoleMode.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        k.SetConsoleTitleW.argtypes = [wintypes.LPCWSTR]
        k.GetConsoleTitleW.argtypes = [wintypes.LPWSTR, wintypes.DWORD]
        # 无 VT 时的降级 API（老 conhost 也支持）
        k.FillConsoleOutputCharacterW.argtypes = [wintypes.HANDLE, ctypes.c_wchar, wintypes.DWORD,
                                                  COORD, ctypes.POINTER(wintypes.DWORD)]
        k.FillConsoleOutputAttribute.argtypes = [wintypes.HANDLE, wintypes.WORD, wintypes.DWORD,
                                                 COORD, ctypes.POINTER(wintypes.DWORD)]
        k.SetConsoleCursorPosition.argtypes = [wintypes.HANDLE, COORD]
        k.GetConsoleCursorInfo.argtypes = [wintypes.HANDLE, ctypes.POINTER(CONSOLE_CURSOR_INFO)]
        k.SetConsoleCursorInfo.argtypes = [wintypes.HANDLE, ctypes.POINTER(CONSOLE_CURSOR_INFO)]
        k.SetConsoleTextAttribute.argtypes = [wintypes.HANDLE, wintypes.WORD]
        self.ok = True
        self.size()
        self._enable_vt()
        self._orig_title = self.get_title()

    # ---- 尺寸 / 标题 / 字体 ----
    def size(self):
        if not self.ok:
            return self.cols, self.rows
        info = CONSOLE_SCREEN_BUFFER_INFO()
        if self.k.GetConsoleScreenBufferInfo(self.hout, ctypes.byref(info)):
            self.real = True
            w = info.srWindow
            self.cols = max(20, w.Right - w.Left + 1)
            self.rows = max(6, w.Bottom - w.Top + 1)
        return self.cols, self.rows

    def get_title(self):
        if not self.ok:
            return ''
        buf = ctypes.create_unicode_buffer(512)
        self.k.GetConsoleTitleW(buf, 512)
        return buf.value

    def set_title(self, text):
        if self.ok:
            self.k.SetConsoleTitleW(text)

    def get_font(self):
        if not self.ok:
            return None
        info = CONSOLE_FONT_INFOEX()
        info.cbSize = ctypes.sizeof(info)
        if self.k.GetCurrentConsoleFontEx(self.hout, False, ctypes.byref(info)):
            return info
        return None

    def set_font(self, size=None, face=None):
        info = self.get_font()
        if info is None:
            return False
        if size:
            info.dwFontSize.Y = max(6, min(48, int(size)))
            info.dwFontSize.X = 0
        if face:
            info.FaceName = face[:31]
        return bool(self.k.SetCurrentConsoleFontEx(self.hout, False, ctypes.byref(info)))

    # ---- 输出 ----
    def _enable_vt(self):
        mode = wintypes.DWORD()
        if not self.k.GetConsoleMode(self.hout, ctypes.byref(mode)):
            return
        self._orig_out_mode = mode.value
        self.vt = bool(self.k.SetConsoleMode(self.hout, mode.value | self.VT_PROCESSING))

    def write(self, text):
        """写 Unicode 到控制台；中文靠 WriteConsoleW 保证不乱码。"""
        if not text:
            return
        # 换行统一成 CRLF：VT 模式下 conhost 的 '\n' 只下移不回车，会写成阶梯状乱屏
        if '\n' in text:
            text = text.replace('\r\n', '\n').replace('\n', '\r\n')
        if not self.ok:
            self._stdout_write(text)
            return
        CHUNK = 4000
        for i in range(0, len(text), CHUNK):
            part = text[i:i + CHUNK]
            written = wintypes.DWORD(0)
            okw = self.k.WriteConsoleW(self.hout, part, len(part), ctypes.byref(written), None)
            if not okw and not written.value:
                # 输出被重定向到文件/管道时 WriteConsoleW 不可用 → 永久降级到 stdout
                self.ok = False
                self._stdout_write(text)
                return

    def write_reverse(self, text):
        """写一行反显高亮。VT 下用 ANSI；没有 ANSI 时退回老 API 的 COMMON_LVB_REVERSE_VIDEO，
        否则光标行的移动在视觉上完全看不出来（会被误认为"按键没反应"）。"""
        if self.vt:
            self.write('\x1b[7m' + text + '\x1b[0m\n')
            return
        if not self.ok:
            self.write(text + '\n')
            return
        self.k.SetConsoleTextAttribute(self.hout, ATTR_REVERSE | 7)
        self.write(text + '\n')
        self.k.SetConsoleTextAttribute(self.hout, 7)

    def set_attr(self, name):
        """没有 ANSI 时用老 API 直接切前景色。
        内部做去重：打字机是逐字写的，不去重会每秒狂调上千次 SetConsoleTextAttribute。"""
        if not self.ok or self.vt:
            return
        v = WIN_ATTRS.get(name, DEFAULT_ATTR) if name else DEFAULT_ATTR
        if self._attr != v:
            self.k.SetConsoleTextAttribute(self.hout, v)
            self._attr = v

    def write_seg(self, text, color=None):
        """写一段带终端配色的文字（不含换行，换行由调用方补）。"""
        if not text:
            return
        if self.vt or not self.ok:
            self.write(wrap_color(text, color))
        else:
            self.set_attr(color)
            self.write(text)
            self.set_attr(None)

    def _stdout_write(self, text):
        try:
            sys.stdout.write(text)
            sys.stdout.flush()
        except Exception:
            pass

    def flush(self):
        if not self.ok:
            try:
                sys.stdout.flush()
            except Exception:
                pass

    def hide_cursor(self):
        self.set_cursor_visible(False)

    def show_cursor(self):
        self.set_cursor_visible(True)

    def set_cursor_visible(self, visible):
        if self.vt:
            self.write('\x1b[?25h' if visible else '\x1b[?25l')
            return
        if not self.ok:
            return
        info = CONSOLE_CURSOR_INFO()
        if self.k.GetConsoleCursorInfo(self.hout, ctypes.byref(info)):
            info.bVisible = bool(visible)
            self.k.SetConsoleCursorInfo(self.hout, ctypes.byref(info))

    def __norm_clear(self):
        """老 API 清屏：没有 ANSI/VT 时的降级方案。"""
        if not self.ok:
            return False
        cells = self.cols * self.rows
        written = wintypes.DWORD(0)
        origin = COORD(0, 0)
        b = bool(self.k.FillConsoleOutputCharacterW(
            self.hout, ctypes.c_wchar(' '), cells, origin, ctypes.byref(written)))
        self.k.FillConsoleOutputAttribute(self.hout, 7, cells, origin, ctypes.byref(written))
        self.k.SetConsoleCursorPosition(self.hout, origin)
        return b

    def home(self):
        if self.vt:
            self.write('\x1b[2J\x1b[H')
        elif not self.__norm_clear():
            self.write('\n' * self.rows)

    def enter_alt(self):
        if self.vt:
            self.write('\x1b[?1049h\x1b[2J\x1b[H')
            self.alt = True

    def leave_alt(self):
        if self.alt:
            self.write('\x1b[?1049l')
            self.alt = False

    def restore(self):
        """恢复标题、字号、光标、屏幕。任何异常都不能阻止退出。"""
        try:
            self.show_cursor()
            self.leave_alt()
            if self.ok and self._orig_title is not None:
                self.set_title(self._orig_title)
            if self.ok and self._orig_out_mode is not None:
                self.k.SetConsoleMode(self.hout, self._orig_out_mode)
        except Exception:
            pass

    # ---- 输入 ----
    def read_key(self):
        """非阻塞读一个按键，返回规范化名字（无按键返回 None）。"""
        if not IS_WINDOWS:
            return None
        import msvcrt
        if not msvcrt.kbhit():
            return None
        ch = msvcrt.getwch()
        self.last_raw = ''                       # 只在认不出来时才填，供面板诊断行显示
        if ch in ('\x00', '\xe0'):
            code = _peek_more(msvcrt)            # 扫描码第二字节常晚到，必须轮询
            if not code:
                self.last_raw = _hex_bytes(ch)
                return 'ext-lost'                # 前缀到了、扫描码没到（本 bug 的现场）
            key = ext_key(code)
            if key == 'special':
                self.last_raw = _hex_bytes(ch + code)
            return key
        if ch == '\x1b':
            # Esc 与 VT 输入序列共用 \x1b 前缀：若终端以 VT 输入模式上报按键
            # （Windows Terminal / 新版 conhost），方向键会发 \x1b[A、PgUp 发 \x1b[5~
            # —— 见到 \x1b 就立刻返回 'esc' 会让面板"按方向键/PgUp 就自己退出"。
            # 这里同样最多等 KEY_EXT_WAIT，拿不到后续字节才判定为真 Esc。
            seq = _peek_more(msvcrt, want=8, stop_early=True)
            if not seq:
                return 'esc'                     # 确实没有后续字节 → 真 Esc
            key = vt_key(seq)
            if key == 'special':
                self.last_raw = _hex_bytes(ch + seq)
            return key
        if ch == '\r':
            return 'enter'
        if ch in ('\x08', '\x7f'):
            return 'backspace'
        if ch == ' ':
            return 'space'
        if ch == '\t':
            return 'tab'
        if ch and ord(ch) < 32:                  # 其它控制键 → 统一的 ctrl-x 名字
            return 'ctrl-' + chr(ord(ch) + 96)   # \x03 -> ctrl-c, \x14 -> ctrl-t
        return ch                                # 字母保留大小写，由上层归一化

    def input_line(self, prompt):
        """阻塞式读一行。用内置 input() 以保留输入法（中文搜索/路径必须）。"""
        self.show_cursor()
        try:
            return input(prompt)
        except (EOFError, KeyboardInterrupt):
            return None
        finally:
            self.hide_cursor()


def _fallback_size():
    try:
        sz = os.get_terminal_size()
        return sz.columns, sz.lines
    except OSError:
        return 80, 25


if not IS_WINDOWS:
    WinConsole.size = lambda self: _fallback_size()


# ---------------------------------------------------------------------------
# 阅读器
# ---------------------------------------------------------------------------

class Reader:
    def __init__(self, console, store, book=None, start_para=0):
        self.c = console
        self.store = store
        self.book = None
        self.pag = None
        self.para = start_para
        self.page = 0
        self.running = True
        self.skin_cycle = random.randint(0, 20)
        self.render_lines = []
        self.tw = {'i': 0, 'ci': 0, 'next': 0.0, 'done': True}
        self.message = ''
        self._shelf_muted = set()     # 刚被从书架删掉的路径：本次运行不再自动写回
        apply_speed(self.store.get_setting('speed', CONFIG['speed']))   # 沿用上次选的速度档
        if book:
            self.attach(book, start_para)

    # ---- 加载 ----
    def attach(self, book, para_idx=0):
        self.book = book
        cols, rows = self.c.size()
        self.pag = Paginator(book, cols - 1, rows)
        self.para = max(0, min(para_idx, book.total - 1))
        self.page = self.pag.page_of(self.para)
        self.banner()

    def banner(self):
        """开书时的一小段假构建输出，让开场也像在跑程序。"""
        rows, cols = self.c.rows, self.c.cols
        self.c.home()
        head = '%s> %s --load "%s"' % (CONFIG['fake_cwd'], random.choice(
            ['python -m tools.loader', 'loader.exe', 'python tools\\pipeline.py']), self.book.title)
        self.c.write(colorize(head, 'info') + '\n')
        for i in range(random.randint(2, 4)):
            self.c.write(colorize(noise_line(), 'noise') + '\n')
            self.c.flush()
            time.sleep(0.05)
        self.c.write(colorize('[INFO] parsed %d records, %d chunks, encoding %s'
                              % (self.book.total, len(self.book.chapters), self.book.encoding), 'info') + '\n')
        self.c.write(colorize('[INFO] resume at offset %d (%.1f%%)'
                              % (self.para, self.para / max(1, self.book.total) * 100), 'info') + '\n')
        time.sleep(0.25)
        self.draw(fresh=True)

    # ---- 渲染 ----
    def build(self):
        cols, rows = self.c.size()
        if cols - 1 != self.pag.width or rows != self.pag.height:
            self.pag.reflow(cols - 1, rows)              # 终端被缩放：重排但保住阅读进度
            self.page = self.pag.page_of(self.para)
        self.page = max(0, min(self.page, self.pag.total_pages() - 1))
        self.para = self.pag.pages[self.page][0]
        self.render_lines = build_page(self.book, self.pag.page_paras(self.page),
                                       self.page, cols - 1, self.skin_cycle)

    def draw(self, fresh=False):
        self.build()
        if fresh:
            self.c.home()
        self.tw = {'i': 0, 'ci': 0, 'next': 0.0, 'done': False}

    def emit(self, line):
        """输出一整行：伪装前缀 / 缩进 / 正文三段各自配色。
        走 write_seg 而不是直接拼 ANSI —— 老 conhost 上没有 VT 时它会退回
        SetConsoleTextAttribute 分段写，颜色照样出得来。"""
        text = line['text']
        pl = min(len(text), line.get('prefix_len', 0))
        n = min(len(text), line.get('deco_len', 0))
        if pl:
            self.c.write_seg(text[:pl], line.get('pre_color'))
        if n > pl:
            # 缩进属于正文的排版，不着伪装色（否则整页左边一竖条颜色很扎眼）
            self.c.write_seg(text[pl:n], None)
        self.c.write_seg(text[n:], line.get('color'))
        self.c.write('\n')

    def flush_page(self):
        """立即输出整页（跳过打字动画）。"""
        while self.tw['i'] < len(self.render_lines):
            self.emit(self.render_lines[self.tw['i']])
            self.tw['i'] += 1
        self.tw['done'] = True
        self.c.flush()
        self.save_progress()

    # ---- 打字机（每帧调用，绝不阻塞按键） ----
    def tick(self, now):
        if self.tw['done']:
            return
        lines = self.render_lines
        if self.tw['i'] >= len(lines):
            self.tw['done'] = True
            self.save_progress()
            return
        if now < self.tw['next']:
            return
        line = lines[self.tw['i']]
        text = line['text']
        n = min(len(text), line.get('deco_len', 0))
        pl = min(n, line.get('prefix_len', 0))
        if line['mode'] == 'instant':
            self.emit(line)
            self.c.flush()
            self.tw['i'] += 1
            self.tw['next'] = now + CONFIG['instant_pause'] * random.uniform(0.4, 1.4)
            return
        cps = CONFIG['cmd_typing_cps'] if line['kind'] == 'cmd' else CONFIG['typing_cps']
        step = max(0.004, (1.0 / cps) * random.uniform(1 - CONFIG['typing_jitter'], 1 + CONFIG['typing_jitter']))
        # 一帧最多补几个字符，既保证跟手又不至于整页瞬现
        chars = max(1, int((now - self.tw['next']) / step) + 1)
        chars = min(chars, CONFIG['max_chars_per_frame'])
        # 行首装饰（提示符/伪装前缀/缩进）一次性写出，只有正文逐字打 ——
        # 逐字打缩进空格既慢又假，而且真实终端里提示符也不是一个个蹦出来的
        if self.tw['ci'] == 0 and n:
            if pl:
                self.c.write_seg(text[:pl], line.get('pre_color'))
            if n > pl:
                self.c.write_seg(text[pl:n], None)
        body = text[n:]
        seg = body[self.tw['ci']:self.tw['ci'] + chars]
        if seg:
            self.c.write_seg(seg, line.get('color'))
            self.tw['ci'] += chars
            self.c.flush()
        self.tw['next'] = now + step
        if self.tw['ci'] >= len(body):
            self.c.write('\n')
            self.c.flush()
            self.tw['i'] += 1
            self.tw['ci'] = 0
            self.tw['next'] = now + (CONFIG['line_pause'] * random.uniform(0.5, 1.8))

    # ---- 进度 ----
    def save_progress(self):
        if self.book:
            # 这本书刚被从书架里删掉：别让随后的自动落盘又把它写回去
            if os.path.abspath(self.book.path) in self._shelf_muted:
                return
            self.store.put_progress(self.book, self.pag.pages[self.page][0])
            self.store.save()

    # ---- 导航 ----
    def go_page(self, delta):
        target = self.page + delta
        if 0 <= target < self.pag.total_pages():
            self.page = target
            self.para = self.pag.pages[self.page][0]
            self.draw(fresh=True)
            return True
        return False

    def go_para(self, para_idx):
        self.para = max(0, min(para_idx, self.book.total - 1))
        self.page = self.pag.page_of(self.para)
        self.draw(fresh=True)

    # ---- 交互面板（目录 / 书架 / 书签 / 搜索 / 帮助） ----
    def panel(self, title, items, current=0, allow_delete=False, readonly=False,
              hotkeys=None):
        """
        全屏滚动列表：目录/书架/书签/搜索结果/帮助/文件浏览器共用。
        items: [(显示文本, payload)]。返回 (payload, 'pick') / (序号, '__del__') / (None, None)。
        超出一屏时可用 ↑↓ / PgUp / PgDn / Home / End 滚动 —— 面板不再被截成"最后一屏"。
        纯选择式交互：没有输入框，↑↓ 移动 + 回车进入，Esc/q 返回（其它键一律不退出）。
        hotkeys: 额外认的键名（如文件浏览器的 'backspace'），命中时返回 (键名, '__key__')。
        面板只负责画自己，返回后由调用方重绘阅读页。
        """
        if not items:
            return None, None

        def _bye(payload, act):
            self.c.hide_cursor()
            return payload, act

        self.flush_page()                # 面板打开前先输出完当前页（并落盘进度）
        self.c.hide_cursor()             # 列表不需要光标，免得像在等输入
        self.c.size()
        cur = max(0, min(int(current or 0), len(items) - 1))
        first = 0
        note = ''                        # 上一个没被识别的按键，画在提示行下面
        while True:
            rows = max(8, int(getattr(self.c, 'rows', 30)))
            view = max(3, rows - 5)          # 标题 + 状态 + 提示 + 余量
            if cur < first:
                first = cur
            elif cur >= first + view:
                first = cur - view + 1
            first = max(0, min(first, max(0, len(items) - view)))

            visible = min(len(items), first + view) - first
            hi = CONFIG['color']
            self.c.home()
            self.c.write(colorize('[INFO] %s' % title, 'info') + '\n')
            for i in range(first, first + visible):
                line = '%s %3d  %s' % ('>' if i == cur else ' ', i + 1,
                                       truncate_width(items[i][0], max(20, self.c.cols - 8)))
                if i == cur and hi:
                    self.c.write_reverse(line)
                else:
                    self.c.write(line + '\n')
            self.c.write('\n' * (view - visible))      # 补齐，抹掉上一屏残影
            if readonly:
                hint = '[INFO] %d/%d   j/k 滚动   Space/n 翻页   p 回翻   Esc/q 返回' \
                       % (cur + 1, len(items))
            else:
                hint = '[INFO] %d/%d   j/k 选择   Space/n 翻页   p 回翻   Enter 确认   Esc/q 返回' \
                       % (cur + 1, len(items))
                if allow_delete:
                    hint += '   d 删除'
            self.c.write(colorize(hint, 'noise') + '\n')
            # 这一行平时是占位空行；只有上一个键没被识别时才写诊断，方便定位
            # "按了方向键/PgUp 却没反应"到底是没读到、还是读到了解析不出来。
            if note:
                self.c.write(colorize(truncate_width(note, max(20, self.c.cols - 1)),
                                      'info') + '\n')
            else:
                self.c.write('\n')
            self.c.flush()

            key = self.wait_key()
            if key in ('special', 'ext-lost'):
                raw = getattr(self.c, 'last_raw', '') or '(空)'
                note = '[!] %s 按键未识别, 原始字节: %s' % (key, raw)
            else:
                note = ''
            if key in ('esc', 'q'):
                return _bye(None, None)          # 只有 Esc / q 退出，其余键绝不触发返回
            if readonly and key in ('enter', 'right'):
                return _bye(None, None)
            if key in ('up', 'k'):
                cur = max(0, cur - 1)
            elif key in ('down', 'j', 'space'):
                cur = min(len(items) - 1, cur + 1)
            elif key in ('pgup', 'p'):
                cur = max(0, cur - view)
            elif key in ('pgdn', 'tab', 'n'):
                cur = min(len(items) - 1, cur + view)
            elif key in ('home', 'g'):
                cur = 0
            elif key in ('end', 'e'):
                cur = len(items) - 1
            elif key == 'enter':
                return _bye(items[cur][1], 'pick')   # 回车 = 进入当前高亮项
            elif key == 'd' and allow_delete:
                return _bye(cur, '__del__')      # 返回 (要删的序号, '__del__')
            elif hotkeys and key in hotkeys:
                return _bye(key, '__key__')      # 返回 (键名, '__key__')，由调用方自己处理

    def show_help(self):
        items = [(ln if ln.strip() else ' ', None) for ln in HELP_TEXT.split('\n')]
        self.panel('usage: reader.exe [options] <file>', items, readonly=True)
        self.draw(fresh=True)

    def cycle_speed(self):
        cur = CONFIG['speed']
        nxt = SPEED_ORDER[(SPEED_ORDER.index(cur) + 1) % len(SPEED_ORDER)] \
            if cur in SPEED_ORDER else 'fast'
        apply_speed(nxt)
        self.store.set_setting('speed', nxt)
        self.store.save()
        self.toast('[INFO] output speed -> %s' % nxt)

    def wait_key(self):
        while True:
            k = self.c.read_key()
            if k is not None:
                if isinstance(k, str) and len(k) == 1 and k.isalpha():
                    return k.lower()             # 面板内字母不分大小写
                return k
            time.sleep(0.01)

    def chapter_menu(self):
        items = [('%s   (%.1f%%)' % (c['title'], c['start'] / max(1, self.book.total) * 100), c['start'])
                 for c in self.book.chapters]
        cur = self.book.chapter_of(self.para)
        payload, act = self.panel('chapters 共 %d 个' % len(items), items, current=cur)
        if act == 'pick':
            self.skin_cycle = random.randint(0, 20)
            self.go_para(payload)
        else:
            self.draw(fresh=True)

    def shelf_menu(self):
        items = []
        for path, meta in self.store.shelf(30):
            mark = '*' if (self.book and os.path.abspath(path) == self.book.path) else ' '
            items.append(('%s%s  [%s]  %.1f%%  %s' % (
                mark, meta.get('title', '?'), meta.get('chapter_title', '')[:16],
                meta.get('percent', 0), path), path))
        if not items:
            self.flush_page()
            self.c.write('\n' + colorize('[WARN] 书架为空，用 %s 小说.txt 打开一本' % RUN_NAME, 'info') + '\n')
            self.c.flush()
            self.wait_key()
            return
        payload, act = self.panel('工作区文件（书架）', items, allow_delete=True)
        if act == 'pick':
            self.open_path(payload)
        elif act == '__del__':
            # panel 回传的是行号，这里换成真正的路径再删
            target = items[payload][1]
            self.store.del_book(target)
            self.store.save()
            now_reading = bool(self.book and os.path.abspath(target) == self.book.path)
            if now_reading:
                # 删的就是正在读的这本：本次运行别让它被自动落盘又写回列表
                self._shelf_muted.add(os.path.abspath(target))
            self.toast('[INFO] 已从书架移除: %s（txt 文件本身没动%s）'
                       % (os.path.basename(target), '，目前仍在读它' if now_reading else ''))
            self.shelf_menu()                # 删完立刻重开，列表当场刷新
        else:
            self.draw(fresh=True)

    def bookmark_menu(self):
        bms = self.store.bookmarks(self.book.path)
        if not bms:
            self.flush_page()
            self.c.write('\n' + colorize('[INFO] 当前文件没有打点记录，按 b 添加', 'info') + '\n')
            self.c.flush()
            self.wait_key()
            return
        items = [('%.1f%%  %s' % (b['para'] / max(1, self.book.total) * 100, b['label']), i)
                 for i, b in enumerate(bms)]
        payload, act = self.panel('断点/书签', items, allow_delete=True)
        if act == 'pick':
            self.go_para(bms[payload]['para'])
        elif act == '__del__':
            self.store.del_bookmark(self.book, payload)
            self.store.save()
            self.bookmark_menu()             # 删除后立刻重开，列表当场刷新
        else:
            self.draw(fresh=True)

    def cmd_search(self):
        term = self.c.input_line('\nsearch> ')
        if not term:
            return
        self.flush_page()
        hits = self.pag.find(term, 0)
        if not hits:
            self.c.write(colorize('[WARN] no match for "%s"' % term, 'info') + '\n')
            self.c.flush()
            self.wait_key()
            return
        # 从当前位置之后找第一条，方便连续搜索
        nxt = next((h for h in hits if h > self.para), hits[0])
        items = []
        for h in hits[:80]:
            items.append(('%.1f%%  %s' % (h / max(1, self.book.total) * 100,
                                          truncate_width(self.book.paras[h], 60)), h))
        start_idx = next((i for i, it in enumerate(items) if it[1] == nxt), 0)
        payload, act = self.panel('grep "%s" 命中 %d 处' % (term, len(hits)), items, current=start_idx)
        if act == 'pick':
            self.go_para(payload)
        else:
            self.draw(fresh=True)

    def cmd_goto(self):
        s = self.c.input_line('\ngoto (百分比 0-100 / 或行号) > ')
        if not s:
            return
        s = s.strip().rstrip('%')
        try:
            v = float(s)
        except ValueError:
            return
        idx = int(v / 100 * self.book.total) if v <= 100 else int(v)
        self.go_para(idx)

    # ---- 导入 txt：① 手输路径  ② 浏览文件夹翻找 ----
    def start_dir(self):
        """浏览器的起始目录：当前书所在目录 → 当前工作目录。"""
        path = getattr(self.book, 'path', None) if self.book is not None else None
        if path:
            d = os.path.dirname(os.path.abspath(path))
            if os.path.isdir(d):
                return d
        return os.getcwd()

    def import_menu(self):
        """
        导入界面（o 键落点）：只认两个键 —— 1/p 手输路径，2/f 浏览文件夹，Esc/q 返回。
        两个入口最后都汇到 open_path()，成功与否由它统一报错。
        """
        self.flush_page()
        self.c.hide_cursor()
        note = ''
        while True:
            self.c.size()
            rows = max(6, int(getattr(self.c, 'rows', 30)))
            self.c.home()
            self.c.write(colorize('[INFO] import: 导入 txt', 'info') + '\n')
            self.c.write('  1  输入路径导入     (p)\n')
            self.c.write('  2  浏览文件夹导入   (f)\n')
            self.c.write('\n' * max(0, rows - 6))
            self.c.write(colorize('[INFO] 1/p 输入路径   2/f 浏览文件夹   Esc/q 返回', 'noise') + '\n')
            if note:
                self.c.write(colorize(truncate_width(note, max(20, self.c.cols - 1)),
                                      'info') + '\n')
            else:
                self.c.write('\n')
            self.c.flush()
            key = self.wait_key()
            if key in ('special', 'ext-lost'):
                raw = getattr(self.c, 'last_raw', '') or '(空)'
                note = '[!] %s 按键未识别, 原始字节: %s' % (key, raw)
            else:
                note = ''
            if key in ('esc', 'q'):
                return
            if key in ('1', 'p'):
                p = self.c.input_line('\nfile> ')
                if p and p.strip():
                    self.open_path(p.strip().strip('"'))
                    return
            elif key in ('2', 'f'):
                p = self.file_browser(self.start_dir())
                if p:
                    self.open_path(p)
                    return

    def file_browser(self, start=None):
        """
        文件夹浏览：列表交给通用 panel（j/k 选择、Space/n 翻屏、p 回翻、g/e 首尾、
        Enter 进目录或导入文件），本函数只负责在目录之间走来走去。
        额外热键：Backspace/h 回上级，w 跳到别处（可直接输盘符，如 D:\\）。
        返回选中文件的绝对路径；用户取消返回 None。
        """
        curdir = os.path.abspath(start or os.getcwd())
        while True:
            if not os.path.isdir(curdir):
                up = os.path.dirname(curdir)
                curdir = up if os.path.isdir(up) else os.getcwd()
            try:
                names = os.listdir(curdir)
            except OSError as exc:
                self.toast('[ERROR] 无法列出 %s: %s' % (curdir, exc))
                return None
            parent = os.path.dirname(curdir.rstrip('\\/'))
            if parent == curdir:
                parent = ''                      # 已在盘符根，没有上级
            items = []
            if parent:
                items.append(('[..] 上级目录', ('dir', parent)))
            dirs, files = [], []
            for n in sorted(names, key=lambda s: s.lower()):
                if n.startswith('.'):
                    continue
                full = os.path.join(curdir, n)
                if os.path.isdir(full):
                    dirs.append(('[DIR] %s' % n, ('dir', full)))
                elif os.path.isfile(full):
                    ext = os.path.splitext(n)[1].lower()
                    tag = '[TXT]' if ext in CONFIG['text_exts'] else '[   ]'
                    try:
                        size = '%7.1fK' % (os.path.getsize(full) / 1024.0)
                    except OSError:
                        size = ' ' * 8
                    files.append(('%s %s %s' % (tag, size, n), ('file', full)))
            items += dirs + files                # 目录永远排在文件前面
            if not items:
                items.append(('(空目录: Backspace 回上级)', ('dir', parent or curdir)))
            title = 'browse %s   (Backspace 上级, w 跳转)' % curdir
            payload, act = self.panel(truncate_width(title, max(20, self.c.cols - 1)),
                                      items, hotkeys=('backspace', 'h', 'w'))
            if act == '__key__':
                if payload in ('backspace', 'h'):
                    if parent:
                        curdir = parent
                    continue
                d = self.c.input_line('\ndir> ')     # w：跨盘符跳转，或直接粘文件路径
                if d and d.strip():
                    d = os.path.abspath(os.path.expanduser(d.strip().strip('"')))
                    if os.path.isfile(d):
                        return d
                    if os.path.isdir(d):
                        curdir = d
                    else:
                        self.toast('[ERROR] 目录不存在: %s' % d)
                continue
            if act != 'pick':
                return None
            kind, val = payload
            if kind == 'file':
                return val
            curdir = val                         # 进子目录 / 回上级，继续列

    def cmd_open(self):
        p = self.c.input_line('\nfile> ')
        if p:
            self.open_path(p.strip('"').strip())

    def cmd_mode(self):
        raw = self.c.input_line('\n%s> ' % CONFIG['fake_cwd'])
        if not raw:
            return
        cmd = raw.strip()
        low = cmd.lower()
        if low in ('q', 'quit', 'exit'):
            self.running = False
        elif low in ('ls', 'dir'):
            self.shelf_menu()
        elif low in ('open', 'import'):
            self.import_menu()               # 裸 open/import：进导入界面（带路径才直接开）
        elif low.startswith('open ') or low.startswith('type '):
            self.open_path(cmd.split(' ', 1)[1].strip('"'))
        elif low.startswith('g ') or low.startswith('goto '):
            self.go_para_from_text(cmd.split(' ', 1)[1])
        elif low in ('cls', 'clear'):
            self.draw(fresh=True)
        elif low.startswith('skin'):
            self.skin_cycle = random.randint(0, 20)
            self.draw(fresh=True)
        elif low in ('help', '?'):
            self.show_help()
        else:
            self.flush_page()
            self.c.write(colorize("'%s' is not recognized as an internal command." % cmd, 'error') + '\n')
            self.c.flush()
            self.wait_key()

    def go_para_from_text(self, s):
        s = s.strip().rstrip('%')
        try:
            v = float(s)
        except ValueError:
            return
        self.go_para(int(v / 100 * self.book.total) if v <= 100 else int(v))

    def open_path(self, path):
        path = os.path.abspath(os.path.expanduser(path))
        self._shelf_muted.discard(path)      # 重新导入同一本书 -> 解除静音，正常回到书架
        if not os.path.isfile(path):
            self.flush_page()
            self.c.write(colorize('[ERROR] 找不到文件: %s' % path, 'error') + '\n')
            self.c.flush()
            self.wait_key()
            return
        try:
            book = Book.load(path)
        except Exception as exc:
            self.flush_page()
            self.c.write(colorize('[ERROR] 打开失败: %s' % exc, 'error') + '\n')
            self.c.flush()
            self.wait_key()
            return
        prog = self.store.get_progress(path)
        self.skin_cycle = random.randint(0, 20)
        self.attach(book, int(prog.get('para', 0)) if prog else 0)

    # ---- 主循环 ----
    def run(self):
        if not self.render_lines:          # 已 attach 过的书不复述开场横幅
            self.banner()
        while self.running:
            now = time.perf_counter()
            key = self.c.read_key()          # 1) 按键永远优先
            if key is not None:
                self.on_key(key)
            self.tick(now)                   # 2) 再推进打字动画
            time.sleep(0.004)

    def on_key(self, key):
        # 字母统一按小写比较；大写（Shift+字母）单独用 upper 记住，供"书签列表"这类次级功能使用
        upper = bool(isinstance(key, str) and len(key) == 1 and key.isupper())
        if isinstance(key, str) and len(key) == 1 and key.isalpha():
            key = key.lower()
        if not self.tw['done'] and key in ('space', 'pgdn', 'j', 'down', 'enter'):
            self.flush_page()                # 打字中按翻页键 → 先立即显示整页
            return
        if key in ('space', 'pgdn', 'j', 'down'):
            self.go_page(1)
        elif key in ('pgup', 'k', 'up'):
            self.go_page(-1)
        elif key == 'right':
            self.flush_page()
        elif key in ('esc', 'ctrl-c', 'q'):
            self.running = False
        elif key in ('t', 'ctrl-t'):         # 目录：裸 t 触发（Ctrl+T 兼容保留）
            self.chapter_menu()
        elif key == 'f':
            self.shelf_menu()
        elif key == 's':
            self.cmd_search()
        elif key == 'g':
            self.cmd_goto()
        elif key == 'l' or (key == 'b' and upper):
            self.bookmark_menu()             # l = 打点列表（Shift+B 兼容保留）
        elif key == 'b':
            label = self.book.chapters[self.book.chapter_of(self.para)]['title']
            ok = self.store.add_bookmark(self.book, self.pag.pages[self.page][0], label)
            self.store.save()
            self.toast('[INFO] 打点%s: %s' % ('已存在' if not ok else '成功', label))
        elif key in (':', 'c'):
            self.cmd_mode()
        elif key in ('o', 'i'):
            self.import_menu()               # 导入界面：1/p 输路径、2/f 浏览文件夹
        elif key in ('+', '='):
            self.zoom(+2)
        elif key in ('-', '_'):
            self.zoom(-2)
        elif key == 'r':
            self.skin_cycle = random.randint(0, 20)
            self.draw(fresh=True)
        elif key == 'v':
            self.cycle_speed()
        elif key in ('?', 'h'):
            self.show_help()

    def toast(self, text):
        self.c.write('\n' + colorize(text, 'info') + '\n')
        self.c.flush()

    def zoom(self, delta):
        size = int(self.store.get_setting('font_size', CONFIG['font_size'])) + delta
        size = max(8, min(32, size))
        self.store.set_setting('font_size', size)
        self.store.save()
        okfont = self.c.set_font(size=size, face=CONFIG['font_face'])
        time.sleep(0.05)
        self.draw(fresh=True)
        if not okfont:
            self.toast('[提示] 已记住字号 %d，但当前终端不允许程序改字号'
                       '（Windows Terminal 会忽略该设置）—— 可在终端里按 Ctrl+滚轮 调整。' % size)


# ---------------------------------------------------------------------------
# 自检（非交互，用于验证全链路）
# ---------------------------------------------------------------------------

SAMPLE = """第1章 陨落的天才

萧炎站在悬崖边上，望着远处的山峰，风从谷底吹上来，卷起阵阵烟尘。他握紧了拳头，指甲深深嵌进掌心。
三年之前，他还是这片大陆上最耀眼的天才。而如今，斗之气三段，被所有人当作笑柄。
"Do you really think you can make it?" 他低声问自己。

第2章 纳兰嫣然

纳兰嫣然来了。她穿着白色的衣裙，站在萧家的大门前，神色淡漠，如同在看一个陌生人。
萧炎抬起头，迎着所有人的目光，一字一句地说：三十年河东，三十年河西，莫欺少年穷。
众人哗然。有人冷笑，有人摇头，只有萧战站在角落里，握紧了拳头。

第3章 药老

戒指里住着一个老人。他说自己叫药尘，曾是这片大陆上最顶尖的炼药师。
"小子，拜我为师，我教你炼药。"老人的声音在萧炎脑海中响起。
萧炎愣了很久，然后跪了下来。
"""


class FakeConsole:
    """无界面控制台替身：排队的按键喂给主循环，输出全部收集，用于自动验证交互流程。"""

    def __init__(self, keys=(), inputs=(), cols=80, rows=30):
        self.keys = list(keys)
        self.inputs = list(inputs)
        self.out = []
        self.cols, self.rows = cols, rows
        self.vt = False
        self.ok = True
        self.real = True

    def size(self):
        return self.cols, self.rows

    def write(self, text):
        self.out.append(text)

    def write_reverse(self, text):
        self.write('\x1b[7m' + text + '\x1b[0m\n')

    def write_seg(self, text, color=None):
        # 替身恒有"ANSI"，自检里就能直接断言配色真的套上去了
        self.write(wrap_color(text, color))

    def set_attr(self, name):
        pass

    def flush(self):
        pass

    def home(self):
        self.out.append('\n<<CLEAR>>\n')

    def hide_cursor(self):
        pass

    def show_cursor(self):
        pass

    def set_cursor_visible(self, visible):
        pass

    def set_font(self, **kw):
        return True

    def get_title(self):
        return 'fake'

    def set_title(self, text):
        pass

    def enter_alt(self):
        pass

    def leave_alt(self):
        pass

    def restore(self):
        pass

    def read_key(self):
        if self.keys:
            return self.keys.pop(0)
        return 'esc'                      # 键耗尽即退出，避免模拟里死循环

    def input_line(self, prompt=''):
        return self.inputs.pop(0) if self.inputs else ''

    def text(self):
        return ''.join(self.out)


def selftest():
    import tempfile
    ok = True

    def check(name, cond, extra=''):
        nonlocal ok
        print('  [%s] %s %s' % ('PASS' if cond else 'FAIL', name, extra))
        ok = ok and bool(cond)

    print('1) 编码探测')
    for enc in ('utf-8', 'gbk', 'utf-16'):
        with tempfile.NamedTemporaryFile('wb', suffix='.txt', delete=False) as fh:
            fh.write(SAMPLE.encode(enc))
            p = fh.name
        got_enc, text = detect_encoding(p)
        check(enc, '第1章' in text and '药尘' in text, '-> %s' % got_enc)
        os.unlink(p)

    print('2) 段落归一化 + 章节切分')
    paras = normalize_paras(SAMPLE)
    chapters = split_chapters(paras)
    check('段落数 > 10', len(paras) > 10, '-> %d 段' % len(paras))
    check('切出 3 章', len(chapters) == 3, '-> %d 章: %s' % (len(chapters), [c['title'] for c in chapters]))

    big = '\n'.join([SAMPLE] * 8)
    big_paras = normalize_paras(big)
    book = Book('测试书.txt', big_paras, 'utf-8', split_chapters(big_paras))
    pag = Paginator(book, 78, 30)

    print('3) 分页（多页场景）')
    total_phys = sum(len(l) for l in pag.wrapped)
    covered = sum(len(pag.wrapped[i]) for a, b in pag.pages for i in range(a, b))
    check('分页覆盖全部物理行', total_phys == covered,
          '-> 覆盖 %d/%d 物理行' % (covered, total_phys))
    check('确实分了多页', pag.total_pages() >= 5, '-> %d 页, 每页上限 %d 行' % (pag.total_pages(), pag.cap))
    check('每页都有内容', all(b > a for a, b in pag.pages))
    idxs = [pag.page_of(i) for i in range(book.total)]
    check('page_of 单调不减', idxs == sorted(idxs))

    print('4) 伪装渲染：多样式 + 周期性 + 正文零损伤')
    cats, lossless, header_ok, max_lines = set(), True, True, 0
    for pi in range(pag.total_pages()):
        lines = build_page(book, pag.page_paras(pi), pi, 78, 0)
        max_lines = max(max_lines, len(lines))
        body = ''.join(l['text'][l.get('deco_len', 0):] for l in lines
                       if l['kind'] in ('body', 'annotated'))
        a, b = pag.pages[pi]
        if body != ''.join(book.paras[i] for i in range(a, b)):
            lossless = False
        if lines[0]['kind'] != 'cmd' or lines[-1]['kind'] != 'info':
            header_ok = False
        for l in lines:
            if l['kind'] == 'cmd':
                cats.add('cmd_type')
            elif l['kind'] == 'annotated':
                t = strip_ansi(l['text'])
                if re.match(r'^\[ *\d+%\] ', t):
                    cats.add('build_out')
                elif re.match(r'^\[\d{4}-\d\d-\d\d ', t):
                    cats.add('log_ts')
                elif re.match(r'^[A-Za-z]:\\', t):
                    cats.add('path_lineno')
    check('正文零损伤（伪装只加前缀）', lossless)
    check('每页首尾都像在跑程序', header_ok)
    check('真实书页上也出现了多种伪装', len(cats) >= 3, '-> %s' % sorted(cats))

    # 书的页数不一定够轮遍皮肤，用同一个书页渲染多轮，验证全部伪装样式都会出现
    cats_all = set(cats)
    rounds = CONFIG['skin_switch_every'] * len(skin_names()) + 2
    for pi in range(rounds):
        for l in build_page(book, pag.page_paras(0), pi, 78, 0):
            t = strip_ansi(l['text'])
            if l['kind'] == 'cmd':
                cats_all.add('cmd_type')
            elif t.startswith('[ ') and '%]' in t[:8]:
                cats_all.add('build_out')
            elif re.match(r'^\[\d{4}-\d\d-\d\d ', t):
                cats_all.add('log_ts')
            elif re.match(r'^[A-Za-z]:\\', t):
                cats_all.add('path_lineno')
            elif t.startswith(('Collecting ', '  Using cached ', 'Requirement already satisfied:')):
                cats_all.add('pip_inst')
            elif re.match(r'^(  File "|    return |Traceback )', t):
                cats_all.add('py_trace')
            elif re.match(r'^(==> |\d\d:\d\d:\d\d: )', t):
                cats_all.add('tail_n')
            elif re.match(r'^(modified:   |[0-9a-f]{7} |\d+ \+)', t):
                cats_all.add('git_stat')
            elif re.match(r'^Step \d+/\d+ :', t) or t.startswith(' ---> '):
                cats_all.add('docker_step')
            elif re.match(r'^tests\\test_\w+\.py::test_\w+_\d+ (PASSED|FAILED)', t) \
                    or re.match(r'^\s*-{4,} \[\s*\d+%\]', t):
                cats_all.add('test_case')
            elif re.match(r'^127\.0\.0\.1:\d+ "', t) or re.match(r'^\[\d\d:\d\d:\d\d\] \d+ bytes', t):
                cats_all.add('http_access')
    check('轮流渲染 %d 页后 %d 种伪装全部出现过' % (rounds, len(skin_names())),
          len(cats_all) >= len(skin_names()), '-> %s' % sorted(cats_all))
    check('每页不溢出屏幕（不会滚屏挤掉页首）', max_lines <= 30 - 1,
          '-> 最多 %d 行 / 可见 %d 行' % (max_lines, 30))
    check('无任何正文行被伪装加宽到折行',
          all(display_width(l['text']) <= 78 for pi in range(pag.total_pages())
              for l in build_page(book, pag.page_paras(pi), pi, 78, 0)
              if l['kind'] in ('body', 'annotated')))

    skins_seen = set()
    for pi in range(CONFIG['skin_switch_every'] * len(skin_names()) * 2):
        names = skin_names()
        skins_seen.add(names[(pi // CONFIG['skin_switch_every']) % len(names)])
    check('周期性换肤轮遍所有皮肤', len(skins_seen) == len(skin_names()),
          '-> %s' % sorted(skins_seen))

    # 每种伪装都必须能独立产出行前缀/页首/页尾（缺一个就会在真实阅读时暴露成空白行）
    skin_ok, skin_bad = True, []
    for name in skin_names():
        try:
            sk = Skin(name)
            p, pc = sk.prefix(78)
            if name != 'cmd_type' and not p:
                raise ValueError('前缀为空')
            if p and not pc:
                raise ValueError('前缀没有配色')
            if display_width(p) >= 78:
                raise ValueError('前缀本身超过屏幕宽度')
            if sk.head(book, 3, 40)['kind'] != 'cmd' or sk.tail(book, 3)['kind'] != 'info':
                raise ValueError('head/tail 类型不对')
        except Exception as exc:
            skin_ok = False
            skin_bad.append('%s:%s' % (name, exc))
    check('全部 %d 种伪装都能产出前缀/页首/页尾' % len(skin_names()), skin_ok,
          '-> %s' % (skin_bad or '、'.join(skin_names())))

    print('4c) 缩进与终端配色（伪装行顶格、正文缩进、各有配色）')
    fwid, cwid, first_ind, cont_ind = indent_widths()
    check('段首行比续行多缩进', fwid > cwid > 0, '-> 段首 %d 列 / 续行 %d 列' % (fwid, cwid))

    ind_ok, ind_bad, color_ok, color_bad = True, '', True, ''
    for pi in range(pag.total_pages()):
        for l in build_page(book, pag.page_paras(pi), pi, 78, 0):
            if l['kind'] in ('body', 'annotated'):
                if not l['text'].strip():      # 空段落刻意保持顶格
                    continue
                deco = l['text'][:l.get('deco_len', 0)]
                # 行首装饰 = 伪装前缀 + 缩进，缩进一定是行尾那一截
                if not (deco.endswith(first_ind) or deco.endswith(cont_ind)):
                    ind_ok, ind_bad = False, l['text'][:40]
                elif l.get('prefix_len') and not l.get('pre_color'):
                    color_ok, color_bad = False, l['text'][:40]
            elif l['kind'] in ('noise', 'info'):
                if l.get('deco_len'):
                    ind_ok, ind_bad = False, l['text'][:40]
                if not l.get('color'):
                    color_ok, color_bad = False, l['text'][:40]
    check('正文行一律带缩进、伪装行一律顶格', ind_ok, '-> %s' % ind_bad)
    check('伪装行与行首前缀都带终端配色', color_ok, '-> %s' % color_bad)

    head = build_page(book, pag.page_paras(0), 0, 78, 0)[0]
    check('页首假命令是"提示符 + 命令"两段配色',
          head['kind'] == 'cmd' and head.get('pre_color') == 'prompt'
          and head.get('color') == 'cmd' and 0 < head.get('deco_len', 0) < len(head['text']),
          '-> %s' % head['text'][:40])

    lines0 = build_page(book, pag.page_paras(0), 0, 78, 0)
    fc = FakeConsole()
    for l in lines0:
        t = l['text']
        pl = min(len(t), l.get('prefix_len', 0))
        d = min(len(t), l.get('deco_len', 0))
        fc.write_seg(t[:pl], l.get('pre_color'))
        fc.write_seg(t[pl:d], None)
        fc.write_seg(t[d:], l.get('color'))
    dump = ''.join(fc.out)
    check('渲染路径真的会吐出 ANSI 配色', '\x1b[' in dump and '\x1b[0m' in dump,
          '-> 转义序列 %d 个' % dump.count('\x1b['))
    check('配色不改变可见文本（剥掉 ANSI 与原始行逐字一致）',
          strip_ansi(dump) == ''.join(l['text'] for l in lines0))
    CONFIG['term_color'] = False
    check('关掉伪装配色后不再吐 ANSI', wrap_color('boom', 'error') == 'boom')
    CONFIG['term_color'] = True

    print('4b) 输出速度档（真实终端是整行刷出来，不是逐字打）')
    check('fast 档正文按行瞬现', apply_speed('fast') == 'fast' and body_mode() == 'instant'
          and CONFIG['line_pause'] < 0.02,
          '-> body_mode=%s line_pause=%.3f' % (body_mode(), CONFIG['line_pause']))
    check('slow 档保留逐字打字', apply_speed('slow') == 'slow' and body_mode() == 'typed'
          and CONFIG['typing_cps'] < 200,
          '-> body_mode=%s typing_cps=%s' % (body_mode(), CONFIG['typing_cps']))
    slow_lines = build_page(book, pag.page_paras(0), 0, 78, 0)
    check('slow 档正文行标记为 typed',
          any(l['mode'] == 'typed' and l['kind'] in ('body', 'annotated') for l in slow_lines))
    check('非法档位退回 fast', apply_speed('nonsense') == 'fast')
    fast_lines = build_page(book, pag.page_paras(0), 0, 78, 0)
    check('fast 档正文行标记为 instant',
          any(l['mode'] == 'instant' and l['kind'] in ('body', 'annotated') for l in fast_lines))
    check('运行时的每帧字符上限已放开（否则打字被帧率卡住）',
          CONFIG['max_chars_per_frame'] >= 12, '-> %d' % CONFIG['max_chars_per_frame'])


    print('5) 存储（进度/书签/书架，原子写）')
    sf = os.path.join(tempfile.gettempdir(), '_tr_selftest_state.json')
    if os.path.exists(sf):
        os.unlink(sf)
    st = Store(sf)
    st.put_progress(book, 7)
    st.add_bookmark(book, 7, '第1章')
    st.add_bookmark(book, 7, '重复应被拒绝')
    st.save()
    st2 = Store(sf)
    check('进度写入', st2.get_progress(book.path)['para'] == 7)
    check('书签去重', len(st2.bookmarks(book.path)) == 1)
    check('书架非空', len(st2.shelf()) == 1)
    check('删书架条目会同时清掉它的进度记录',
          st2.del_book(book.path) is True and len(st2.shelf()) == 0
          and st2.get_progress(book.path) is None)
    check('删书架条目不动打点', len(st2.bookmarks(book.path)) == 1)
    check('删不存在的条目返回 False', st2.del_book(book.path) is False)
    os.unlink(sf)

    print('6) 搜索')
    hits = pag.find('萧炎', 0)
    check('搜索命中', len(hits) >= 2, '-> %d 处' % len(hits))

    print('7) 交互流程模拟（按键必须有反应，全程不得卡死）')
    sf2 = os.path.join(tempfile.gettempdir(), '_tr_selftest_state2.json')
    if os.path.exists(sf2):
        os.unlink(sf2)
    st3 = Store(sf2)
    old_cps = (CONFIG['typing_cps'], CONFIG['cmd_typing_cps'])
    CONFIG['typing_cps'] = CONFIG['cmd_typing_cps'] = 1000000   # 模拟里让打字机瞬间跑完
    try:
        # A. 连翻数页：每页正文必须真的被输出（旧版就是死在这一步）
        fc = FakeConsole(['space', 'space', 'space', 'space', 'space', 'esc'])
        rd = Reader(fc, st3, book, 0)
        rd.run()
        txt = fc.text()
        shown = 0
        for pi in range(3):
            a, b = rd.pag.pages[pi]
            if all(book.paras[i][:12] in txt for i in range(a, b) if book.paras[i].strip()):
                shown += 1
        check('按键有反应：连翻 3 页正文全部输出', shown == 3, '-> %d/3 页' % shown)
        check('翻页后进度已落盘', st3.get_progress(book.path) is not None)
        check('输出里带伪装头尾', txt.count('[INFO]') >= 2)

        # A2. 书架界面里删除记录（真实文件 + 独立 store，避开 run()/落盘对断言的干扰）
        sh_dir = tempfile.mkdtemp(prefix='_tr_shelf_')
        sh_path = os.path.join(sh_dir, '书架测试.txt')
        with open(sh_path, 'w', encoding='utf-8') as fh:
            fh.write(SAMPLE)
        bk2 = Book.load(sh_path)
        sf3 = os.path.join(sh_dir, 'state.json')
        st4 = Store(sf3)
        st4.put_progress(bk2, 7)
        st4.add_bookmark(bk2, 7, '第1章')
        st4.save()
        before = len(st4.shelf())
        fc = FakeConsole(['d', 'esc'])
        rd = Reader(fc, st4, bk2, 0)
        rd.shelf_menu()
        check('书架里按 d 能删掉一条记录',
              before == 1 and len(st4.shelf()) == 0,
              '-> %d 条 -> %d 条' % (before, len(st4.shelf())))
        check('删除后给出提示, 且写明 txt 文件没动',
              '已从书架移除' in fc.text() and 'txt 文件本身没动' in fc.text())
        check('删书架不动打点、不删磁盘文件',
              len(st4.bookmarks(bk2.path)) == 1 and os.path.exists(bk2.path))
        check('键耗尽(=Esc)退出书架界面不报错', rd.running is not False)
        try:
            os.unlink(sh_path)
            os.unlink(sf3)
            os.rmdir(sh_dir)
        except OSError:
            pass

        # A3. 阅读中按 f 能打开书架（删除按钮就在这个界面里）
        fc = FakeConsole(['f', 'esc', 'esc'])
        rd = Reader(fc, st3, book, 0)
        rd.run()
        check('按 f 打开书架面板, 且提示行带 d 删除',
              '工作区文件' in fc.text() and 'd 删除' in fc.text())

        # B. 首页上翻不越界、不异常
        fc = FakeConsole(['pgup', 'pgup', 'esc'])
        rd = Reader(fc, st3, book, 0)
        rd.run()
        check('首页上翻不越界', rd.page == 0)

        # C. 目录跳转到一个靠后的章节（避免因第 2 章恰好在第 0 页而假通过）
        cidx = min(10, len(book.chapters) - 1)
        target = book.chapters[cidx]['start']
        fc = FakeConsole(['t'] + ['down'] * cidx + ['enter', 'esc'])
        rd = Reader(fc, st3, book, 0)
        rd.run()
        a, b = rd.pag.pages[rd.page]
        check('目录 ↑↓ 选章节 + 回车跳转生效', a <= target < b,
              '-> 第 %d 章起点 %d 落在页 %d 的 [%d,%d)' % (cidx + 1, target, rd.page, a, b))

        # C2. 触发方式：裸 t 打开目录（旧的 Ctrl+T 已按用户要求改回 t）
        fc = FakeConsole(['t', 'esc', 'esc'])
        rd = Reader(fc, st3, book, 0)
        rd.run()
        check('裸 t 打开目录面板', 'chapters' in fc.text())
        check('目录里不再有 select> 输入框', 'select>' not in fc.text())

        # C3. 面板可用 vi 键操作：j 下移 / k 上移 / q 返回
        fc = FakeConsole(['t', 'j', 'j', 'k', 'q', 'esc'])
        rd = Reader(fc, st3, book, 0)
        rd.run()
        check('面板接受 j/k 与 q 返回', 'chapters' in fc.text() and rd.running is False)

        # D. 书签：添加 + 去重 + 列表跳转
        st3.data['bookmarks'].pop(book.path, None)
        fc = FakeConsole(['b', 'b', 'esc'])
        rd = Reader(fc, st3, book, 0)
        rd.run()
        check('书签添加且去重', len(st3.bookmarks(book.path)) == 1,
              '-> %d 条' % len(st3.bookmarks(book.path)))
        fc = FakeConsole(['B', 'enter', 'esc'])
        rd = Reader(fc, st3, book, 0)
        rd.run()
        check('书签列表可跳转', rd.para == st3.bookmarks(book.path)[0]['para'])
        fc = FakeConsole(['B', 'd', 'esc', 'esc'])
        rd = Reader(fc, st3, book, 0)
        rd.run()
        check('书签列表按 d 真能删除（旧版返回的 __del__ 没人接）',
              len(st3.bookmarks(book.path)) == 0,
              '-> 剩 %d 条' % len(st3.bookmarks(book.path)))

        # E. 搜索 → 面板默认高亮"当前位置之后的第一条命中" → 回车跳到它所在的页
        #    （para 是"当前页首段"，跳转一个段落后 para 会对齐到该页首段，这是设计）
        needle = book.paras[-1].strip()[:10]
        hits_e = pag.find(needle, 0)
        fc = FakeConsole(['s', 'enter', 'esc'], inputs=[needle])
        rd = Reader(fc, st3, book, book.total - 2)
        p0 = rd.page
        start_para = rd.para
        rd.run()
        hit = next((h for h in hits_e if h > start_para), hits_e[0])
        a, b = rd.pag.pages[rd.page]
        check('搜索选中后落到含命中的页，且页首段对齐',
              a <= hit < b and rd.page == rd.pag.page_of(hit) and rd.para == a,
              '-> 原段落 %d(页 %d) → 段落 %d(页 %d) [%d,%d)，期望命中 %d，命中表 %s'
              % (start_para, p0, rd.para, rd.page, a, b, hit, hits_e[:8]))

        # E2. 真·跳转：造一本命中只出现在书末的书，从第 0 页搜起，必须离开第 0 页
        uniq = 'ZZZ 独特命中标记 ZZZ'
        bp2 = list(big_paras) + [uniq]
        book2 = Book('测试书2.txt', bp2, 'utf-8', split_chapters(bp2))
        fc = FakeConsole(['s', 'enter', 'esc'], inputs=['独特命中标记'])
        rd2 = Reader(fc, st3, book2, 0)
        p0 = rd2.page
        rd2.run()
        a, b = rd2.pag.pages[rd2.page]
        check('搜索真把读者从第 0 页拉到书末命中处',
              rd2.page != p0 and a <= len(bp2) - 1 < b,
              '-> 原页 %d → 页 %d [%d,%d)，命中段落 %d' % (p0, rd2.page, a, b, len(bp2) - 1))

        # F. 按百分比跳转
        fc = FakeConsole(['g', 'esc'], inputs=['50'])
        rd = Reader(fc, st3, book, 0)
        rd.run()
        check('按百分比跳转', 0.3 * book.total <= rd.para <= 0.7 * book.total,
              '-> 第 %d/%d 段' % (rd.para, book.total))

        # G. 命令模式
        fc = FakeConsole([':', 'esc'], inputs=['ls'])
        rd = Reader(fc, st3, book, 0)
        rd.run()
        check('命令模式可用', len(fc.text()) > 0)

        # H. 调字号
        fc = FakeConsole(['+', 'esc'])
        rd = Reader(fc, st3, book, 0)
        rd.run()
        check('字号可调并持久化', st3.get_setting('font_size', None) is not None,
              '-> %s' % st3.get_setting('font_size', None))

        # I. Esc 立即退出，不阻塞
        fc = FakeConsole(['esc'])
        rd = Reader(fc, st3, book, 0)
        t0 = time.perf_counter()
        rd.run()
        check('Esc 立即退出（不阻塞）', (not rd.running) and time.perf_counter() - t0 < 1.0)

        # J. 列表面板必须能滚动 —— 旧行为是"一次性刷完，只剩最后一屏"
        big = [('item-%02d' % i, i) for i in range(60)]
        fc = FakeConsole(['end', 'enter'])
        rd = Reader(fc, st3, book, 0)
        payload, act = rd.panel('selftest list', big, current=0)
        check('列表超一屏：End 滚到末项并选中', act == 'pick' and payload == 59,
              '-> act=%s payload=%s' % (act, payload))
        last_screen = fc.text().split('\n<<CLEAR>>\n')[-1]
        check('列表只占一屏（末项在屏、首项已滚出）',
              'item-59' in last_screen and 'item-00' not in last_screen,
              '-> 末屏 %d 字符' % len(last_screen))

        fc = FakeConsole(['down', 'down', 'enter'])
        rd = Reader(fc, st3, book, 0)
        payload, act = rd.panel('selftest list', big)
        check('列表 ↓ 移动后 Enter 选中第 3 项', act == 'pick' and payload == 2,
              '-> payload=%s' % payload)

        fc = FakeConsole(['pgdn', 'pgdn', 'enter'])
        rd = Reader(fc, st3, book, 0)
        payload, act = rd.panel('selftest list', big, current=0)
        check('列表 PgDn 翻页后仍能整屏重绘',
              act == 'pick' and payload == 50, '-> payload=%s' % payload)

        fc = FakeConsole(['esc'])
        rd = Reader(fc, st3, book, 0)
        payload, act = rd.panel('selftest list', big)
        check('列表 Esc 取消不误选', act is None and payload is None,
              '-> act=%s payload=%s' % (act, payload))

        fc = FakeConsole(['1', 'down', 'enter'])
        rd = Reader(fc, st3, book, 0)
        payload, act = rd.panel('selftest list', big)
        check('列表里数字键不再直接跳转（select> 输入框已去除）',
              act == 'pick' and payload == 1, '-> payload=%s' % payload)

        # PgUp 连按必须只翻页、绝不退出面板（用户报的"按两下 PgUp 就回到阅读界面"）
        fc = FakeConsole(['pgup', 'pgup', 'down', 'enter'])
        rd = Reader(fc, st3, book, 0)
        payload, act = rd.panel('selftest list', big, current=30)
        check('面板里连续 PgUp 只翻页不退出',
              act == 'pick' and payload == 1, '-> act=%s payload=%s' % (act, payload))

        # 字母键兜底：终端对方向键/翻页键的上报方式千奇百怪（精确序列 / 带修饰符
        # 序列 / 老式扫描码），必须有一组纯 ASCII 单字节操作键也能选章节。
        fc = FakeConsole(['n', 'enter'])
        rd = Reader(fc, st3, book, 0)
        payload, act = rd.panel('selftest list', big)
        check('面板 n 键翻一屏（不依赖终端的翻页键上报）',
              act == 'pick' and payload == 25, '-> payload=%s' % payload)

        fc = FakeConsole(['p', 'down', 'enter'])
        rd = Reader(fc, st3, book, 0)
        payload, act = rd.panel('selftest list', big, current=0)
        check('面板 p 键回翻一屏且不越界',
              act == 'pick' and payload == 1, '-> payload=%s' % payload)

        fc = FakeConsole(['g', 'enter'])
        rd = Reader(fc, st3, book, 0)
        payload, act = rd.panel('selftest list', big, current=30)
        check('面板 g 键跳到首项（字母兜底 Home）',
              act == 'pick' and payload == 0, '-> payload=%s' % payload)

        fc = FakeConsole(['e', 'enter'])
        rd = Reader(fc, st3, book, 0)
        payload, act = rd.panel('selftest list', big, current=0)
        check('面板 e 键跳到末项（字母兜底 End）',
              act == 'pick' and payload == 59, '-> payload=%s' % payload)

        fc = FakeConsole(['q'])
        rd = Reader(fc, st3, book, 0)
        rd.on_key('q')
        check('阅读页 q 键退出（字母兜底 Esc）',
              rd.running is False, '-> running=%s' % rd.running)

        check('VT 输入序列解析（方向键/PgUp 不再被误判成 Esc）',
              vt_key('[A') == 'up' and vt_key('[B') == 'down'
              and vt_key('[5~') == 'pgup' and vt_key('[6~') == 'pgdn'
              and vt_key('OA') == 'up' and vt_key('[3~') == 'del'
              and vt_key('') == 'esc' and vt_key('[99~') == 'special')

        # ConPTY / Windows Terminal 会把修饰符参数一并送出：实测方向键常是
        # ESC [ 1 ; 1 A、PgUp 是 ESC [ 5 ; 1 ~。用精确字典查表会把这些全漏成
        # 'special' —— 用户实测现场：目录里按方向键/PgUp 显示"按键未识别"。
        check('带修饰符参数的 VT 序列也能解析（ConPTY 会发 ESC [ 1 ; 1 A）',
              vt_key('[1;1A') == 'up' and vt_key('[1;1B') == 'down'
              and vt_key('[1;5C') == 'right' and vt_key('[1;5D') == 'left'
              and vt_key('[5;1~') == 'pgup' and vt_key('[6;1~') == 'pgdn'
              and vt_key('[3;2~') == 'del' and vt_key('[1;2;3A') == 'up'
              and vt_key('[1;1H') == 'home' and vt_key('[1;1F') == 'end'
              and vt_key('O1;2A') == 'up'
              and vt_key('[?1;2c') == 'special' and vt_key('[1;1R') == 'special')

        check('\\x00/\\xe0 扫描码映射',
              ext_key('H') == 'up' and ext_key('P') == 'down'
              and ext_key('I') == 'pgup' and ext_key('Q') == 'pgdn'
              and ext_key('K') == 'left' and ext_key('M') == 'right'
              and ext_key('h') == 'up' and ext_key('') == 'special'
              and ext_key('?') == 'special')

        # 扩展键第二字节竞态回归（用户实测：目录里方向键光标不动、PgUp 不翻页，
        # 但面板也不退出 —— 旧实现拿到 \x00/\xe0 后立刻 kbhit()，扫描码还没进队
        # 列就被判成 'special' 丢掉）。这里用假 msvcrt 复现"第二字节迟到"。
        class _FakeMsvcrt:
            """kbhit/getwch 的最小替身：取走一个字节后先假装后面暂时没字节。"""

            def __init__(self, data, lag):
                self.q = list(data)
                self.lag = lag
                self.cool = 0
                self.taken = False

            def kbhit(self):
                if not self.q:
                    return False
                if self.taken and self.cool > 0:
                    self.cool -= 1
                    return False
                return True

            def getwch(self):
                c = self.q.pop(0)
                self.taken = True
                self.cool = self.lag
                return c

        import sys as _sys
        _real_msvcrt = _sys.modules.get('msvcrt')
        probe = WinConsole.__new__(WinConsole)   # 只调 read_key，不碰真实控制台
        ext_ok = bool(IS_WINDOWS)
        detail = []
        for _data, _want in (('\xe0H', 'up'), ('\x00I', 'pgup'), ('\xe0P', 'down'),
                             ('\xe0K', 'left'), ('\xe0', 'ext-lost')):
            _sys.modules['msvcrt'] = _FakeMsvcrt(_data, lag=5)
            try:
                _got = probe.read_key()
            finally:
                if _real_msvcrt is None:
                    _sys.modules.pop('msvcrt', None)
                else:
                    _sys.modules['msvcrt'] = _real_msvcrt
            if _got != _want:
                ext_ok = False
                detail.append('%r -> %r(期望 %r)' % (_data, _got, _want))
        check('扩展键第二字节迟到也能解析（方向键/PgUp 不再被丢成 special）',
              ext_ok, '; '.join(detail))

        # K. 帮助也走同一个滚动面板
        fc = FakeConsole(['?', 'down', 'down', 'esc', 'esc'])
        rd = Reader(fc, st3, book, 0)
        rd.run()
        check('帮助面板可滚动且不卡死', len(fc.text()) > 0)

        # L. 速度档切换键
        apply_speed('fast')
        fc = FakeConsole(['v', 'v', 'v', 'esc'])
        rd = Reader(fc, st3, book, 0)
        rd.run()
        check('v 键循环速度档并落盘', st3.get_setting('speed', None) is not None,
              '-> %s' % st3.get_setting('speed', None))

        # M. 导入界面：o → 1 手输路径 / o → 2 浏览文件夹（进子目录、回上级、选中文件导入）
        imp_dir = tempfile.mkdtemp(prefix='_tr_import_')
        sub_dir = os.path.join(imp_dir, 'sub')
        os.mkdir(sub_dir)
        another = os.path.join(imp_dir, 'aaa_other.txt')
        target = os.path.join(imp_dir, '导入目标.txt')
        for p in (another, target):
            with open(p, 'w', encoding='utf-8') as fh:
                fh.write('第1章 导入\n' + '\n'.join('导入测试段落 %d' % i for i in range(40)))

        fc = FakeConsole(['o', '1'], inputs=[target, 'esc'], cols=120)
        rd = Reader(fc, st3, book, 0)
        rd.run()
        check('o → 1 手输路径能导入新 txt',
              os.path.normcase(rd.book.path) == os.path.normcase(os.path.abspath(target)),
              '-> %s' % rd.book.path)

        # 当前书放在 imp_dir 里 → 浏览器应从它所在目录起步；
        # aaa_other.txt 按名字排在前面，所以第 3 个文件位正好是"导入目标.txt"
        fc = FakeConsole(['o', '2', 'down', 'enter', 'backspace', 'down', 'down', 'down',
                          'enter', 'esc'], cols=120)
        rd = Reader(fc, st3, Book.load(another), 0)
        rd.run()
        dump = fc.text()
        check('o → 2 浏览器列出起始目录（目录/文件/上级都有名字）',
              ('browse %s' % imp_dir) in dump and '[TXT]' in dump
              and '[DIR] sub' in dump and '[..] 上级目录' in dump)
        check('浏览器能进子目录、Backspace 回上级、回车导入选中的文件',
              ('browse %s' % sub_dir) in dump
              and dump.count('browse %s' % imp_dir) >= 2
              and os.path.normcase(rd.book.path) == os.path.normcase(os.path.abspath(target)),
              '-> 标题出现次数 %d，最终书 %s'
              % (dump.count('browse %s' % imp_dir), rd.book.path))

        # M2. 浏览器里取消（Esc）不能把当前书搞丢
        fc = FakeConsole(['o', '2', 'esc', 'esc'], cols=120)
        rd = Reader(fc, st3, Book.load(target), 0)
        before = rd.book.path
        rd.run()
        check('浏览器里 Esc 取消后仍留在原书', rd.book.path == before, '-> %s' % rd.book.path)

        # M3. 导入界面里 Esc 取消，同样留在原书
        fc = FakeConsole(['o', 'esc', 'esc'], cols=120)
        rd = Reader(fc, st3, Book.load(target), 0)
        rd.run()
        check('导入界面 Esc 取消后仍留在原书', rd.book.path == before)

        try:
            for p in (another, target):
                os.unlink(p)
            os.rmdir(sub_dir)
            os.rmdir(imp_dir)
        except OSError:
            pass
    finally:
        CONFIG['typing_cps'], CONFIG['cmd_typing_cps'] = old_cps
        apply_speed('fast')
        if os.path.exists(sf2):
            os.unlink(sf2)

    print('\n%s' % ('全部通过 ✅' if ok else '存在失败 ❌'))
    return 0 if ok else 1


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------

def preview(path, n_pages, width, height):
    """把伪装渲染结果用普通 print 输出，检查排版观感（不进入交互、不改终端）。"""
    import tempfile
    if path and os.path.isfile(path):
        book = Book.load(os.path.abspath(path))
    else:
        fp = os.path.join(tempfile.gettempdir(), '_tr_preview.txt')
        with open(fp, 'w', encoding='utf-8') as fh:
            fh.write(SAMPLE)
        book = Book.load(fp)
    # 管道里 isatty() 恒为假，核对配色时用 TR_COLOR=1 强制打开（ANSI 会原样打出来）
    CONFIG['color'] = sys.stdout.isatty() or bool(os.environ.get('TR_COLOR'))
    pag = Paginator(book, width, height)
    print('《%s》 编码 %s | %d 段 | %d 章 | 屏幕 %dx%d → %d 页（每页正文上限 %d 物理行）'
          % (book.title, book.encoding, book.total, len(book.chapters),
             width, height, pag.total_pages(), pag.cap))
    for pi in range(min(n_pages, pag.total_pages())):
        lines = build_page(book, pag.page_paras(pi), pi, width, 0)
        print('\n' + '─' * 24 + ' 第 %d/%d 页 ' % (pi + 1, pag.total_pages()) + '─' * 24)
        for l in lines:
            print(render_line(l))
    return 0


def probe():
    """报告控制台能力与伪装 API 是否可用（不改终端状态）。"""
    c = WinConsole()
    cols, rows = c.size()
    font = c.get_font()
    print('Windows 平台        : %s' % IS_WINDOWS)
    print('WinConsole 初始化    : %s' % c.ok)
    print('VT/ANSI 支持        : %s  （备用屏缓冲、清屏、隐藏光标依赖它）' % c.vt)
    print('可见区域            : %d 列 x %d 行' % (cols, rows))
    print('stdout 是终端        : %s' % sys.stdout.isatty())
    print('stdout 编码          : %s' % sys.stdout.encoding)
    print('当前窗口标题        : %r' % c.get_title())
    if font is not None:
        print('当前字体            : %s %d pt' % (font.FaceName, font.dwFontSize.Y))
    if c.ok:
        ok1 = c.set_font(size=16, face='Consolas')
        print('试设 16pt Consolas   : %s' % ok1)
        if font is not None:
            c.set_font(size=max(6, font.dwFontSize.Y), face=font.FaceName or 'Consolas')
            print('字体已还原为原始值')
    print('结论：%s' % ('可在本终端完整运行伪装模式' if (c.ok and c.vt)
                       else ('可用但无 ANSI（伪装与备用屏会降级）' if c.ok else '无法使用控制台 API')))
    return 0



def keytest():
    """交互式按键诊断：把每个按键的原始码原样打印出来，用于排查"按键没反应"。
    在真实 cmd 窗口里运行，按 Esc 退出。"""
    print('=' * 60)
    print(' terminal_reader --keytest   按任意键查看程序收到了什么；Esc 退出')
    print('=' * 60)
    if not IS_WINDOWS:
        print('非 Windows 平台，无需测试')
        return 0
    import msvcrt
    k = ctypes.windll.kernel32
    hin = k.GetStdHandle(-10)                     # STD_INPUT_HANDLE
    mode_in = wintypes.DWORD()
    ok_in = bool(k.GetConsoleMode(hin, ctypes.byref(mode_in)))
    c = WinConsole()
    print(' stdin  是真实控制台 : %s' % ok_in)
    print(' stdout 是真实控制台 : %s' % getattr(c, 'real', False))
    print(' VT/ANSI 支持        : %s' % c.vt)
    print(' 窗口尺寸            : %d x %d' % c.size())
    print('-' * 60)
    print(' 请依次按： ↑  ↓  PgUp  PgDn  Home  End  Esc')
    print(' 每按一个键，下面会打印程序"收到"的原始码。')
    print('-' * 60)
    n = 0
    while True:
        if msvcrt.kbhit():
            ch = msvcrt.getwch()
            n += 1
            print('[%02d] 主字节 U+%04X  %s' % (n, ord(ch), ascii(ch)))
            if ch in ('\x00', '\xe0'):
                d = ''
                for _ in range(80):               # 扩展键第二字节可能晚到，最多等 80ms
                    if msvcrt.kbhit():
                        d = msvcrt.getwch()
                        break
                    time.sleep(0.001)
                if d:
                    print('     扩展第二字节 U+%04X  %s' % (ord(d), ascii(d)))
                else:
                    print('     !! 扩展第二字节没有等到 —— 方向键/PgUp 会被判为未知键而失效')
            if ch == '\x1b' or ch == '\x03':
                break
        time.sleep(0.005)
    print('-' * 60)
    print(' 结束。把上面全部输出复制回来即可定位。')
    return 0


# exe(打包后) 与脚本两种形态下, 给用户的"怎么再跑一次"提示
RUN_NAME = (os.path.basename(sys.executable) if getattr(sys, 'frozen', False)
            else 'python terminal_reader.py')


def _harden_stdio():
    """重定向/管道输出时把不可编码字符降级, 避免 UnicodeEncodeError 打断运行。

    真控制台下 Python 走 Unicode API 写屏, 不受影响; 但 `xxx.exe --selftest > log.txt`
    或经管道输出时会退回 cp936, 此时结语里的 ✅/❌ 会直接抛错 —— 保留 encoding,
    只把不可编码的字符降级成 '?', 让程序照常跑完并留下完整输出。
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors='replace')
        except Exception:
            pass


def main(argv=None):
    _harden_stdio()
    ap = argparse.ArgumentParser(
        prog='terminal_reader', description='伪装终端小说阅读器 v1.0')
    ap.add_argument('path', nargs='?', help='txt 小说路径（省略则打开书架）')
    ap.add_argument('--selftest', action='store_true', help='非交互自检')
    ap.add_argument('--probe', action='store_true', help='探测终端能力（ANSI/备用屏/字体）')
    ap.add_argument('--keytest', action='store_true', help='交互式按键诊断（排查按键没反应）')
    ap.add_argument('--preview', type=int, nargs='?', const=3, default=0,
                    metavar='N', help='用普通输出预览 N 页伪装效果（默认 3，不进入交互）')
    ap.add_argument('--size', default='100x30', help='预览用尺寸，如 100x30')
    ap.add_argument('--font-size', type=int, help='启动字号')
    ap.add_argument('--speed', choices=SPEED_ORDER, help='输出速度档：fast(默认)/normal/slow')
    ap.add_argument('--no-color', action='store_true', help='关闭 ANSI 颜色')
    ap.add_argument('--real-title', action='store_true', help='不伪装窗口标题')
    args = ap.parse_args(argv)

    if args.selftest:
        return selftest()
    if args.probe:
        return probe()
    if args.keytest:
        return keytest()
    if args.preview:
        try:
            w, h = (int(v) for v in args.size.lower().split('x'))
        except ValueError:
            w, h = 100, 30
        return preview(args.path, args.preview, w, h)

    store = Store(CONFIG['state_file'])
    CONFIG['font_size'] = int(store.get_setting('font_size', CONFIG['font_size']))
    apply_speed(args.speed or store.get_setting('speed', CONFIG['speed']))
    if args.font_size:
        CONFIG['font_size'] = args.font_size
    if args.no_color or not sys.stdout.isatty():
        CONFIG['color'] = False

    console = WinConsole()
    if not console.ok:
        print('此版本需要在 Windows 控制台中运行。')
        return 2
    if not console.real:
        print('提示：当前输出不是真正的控制台窗口（stdout 被重定向，或不是 cmd / PowerShell）。')
        print('      交互模式请直接在 cmd 或 PowerShell 窗口里运行本程序；')
        print('      只想看渲染效果的话用:  %s --preview 2 --size 100x32' % RUN_NAME)
        return 2

    # 进入伪装现场
    if console.vt:
        console.enter_alt()
    console.hide_cursor()
    if not args.real_title:
        console.set_title(CONFIG['fake_title'])
    console.set_font(size=CONFIG['font_size'], face=CONFIG['font_face'])

    reader = None
    try:
        book, start = None, 0
        if args.path:
            book = Book.load(os.path.abspath(args.path))
            prog = store.get_progress(book.path)
            start = int(prog.get('para', 0)) if prog else 0
        reader = Reader(console, store, book, start)
        if book is None:
            reader.shelf_menu()
            if reader.book is None:
                console.restore()
                print('未选择文件。用法: %s 小说.txt' % RUN_NAME)
                return 1
        reader.run()
    except KeyboardInterrupt:
        pass
    except Exception as exc:
        console.restore()
        import traceback
        traceback.print_exc()
        print('\n[ERROR] %s' % exc)
        return 1
    finally:
        try:
            if reader and reader.book:
                reader.save_progress()
        except Exception:
            pass
        console.restore()
    return 0


if __name__ == '__main__':
    sys.exit(main())
