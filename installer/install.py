#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ZCode Token 状态条 — 安装器（app.asar 补丁 + 运行时部署）.

原理：在 ZCode Electron 应用 app.asar 的入口文件末尾追加一行
    ;import("<运行时目录>/pump.cjs").then(...)
动态 import asar 之外的常驻目录泵模块（重建 asar 头部与完整性校验数据）。

用法:
  python install.py                 # 安装/更新（定位 asar → 部署运行时 → 打补丁）
  python install.py --status        # 查看安装状态
  python install.py --remove        # 卸载（剥离注入行，恢复原入口）
  python install.py --asar <path>   # 手动指定 app.asar
  python install.py --runtime <dir> # 手动指定运行时目录（默认 ~/.zcode/ztu-statusbar）
  python install.py --finish-pending # 应用待重启替换的 .tmp（客户端退出后运行）

首次安装自动备份 app.asar → app.asar.ztu.bak；--remove 只剥离注入行。
"""

import argparse
import hashlib
import json
import os
import re
import shutil
import struct
import subprocess
import sys
import tempfile
from pathlib import Path

ALIGN = 4
BLOCK = 4194304
DEFAULT_RUNTIME = Path.home() / ".zcode" / "ztu-statusbar"
HERE = Path(__file__).parent.resolve()
DAEMON = HERE.parent / "daemon"
ENGINE = HERE.parent / "engine" / "usage_engine.py"
INJECT_RE = re.compile(
    rb'\n;import\("[^"]*pump\.cjs"\)\.then\(\(\) => null, '
    rb'\(e\) => console\.error\("\[ztu\] load failed", e\)\);')

ASAR = None      # set_target() 设置
BAK = TMP = TMP_META = None
RUNTIME = DEFAULT_RUNTIME


def L(zh, en):
    return zh


# ---------------------------------------------------------------- 定位

def default_asar_candidates():
    cands = []
    if sys.platform == "win32":
        local = os.environ.get("LOCALAPPDATA")
        if local:
            cands += [Path(local) / "Programs" / "zcode" / "resources" / "app.asar",
                      Path(local) / "Programs" / "ZCode" / "resources" / "app.asar"]
        for d in ("E", "D", "C"):
            cands.append(Path(d + ":/") / "zTool" / "zCode" / "resources" / "app.asar")
            cands.append(Path(d + ":/") / "Program Files" / "zcode" / "resources" / "app.asar")
    elif sys.platform == "darwin":
        cands.append(Path("/Applications/ZCode.app/Contents/Resources/app.asar"))
    else:
        cands += [Path("/opt/ZCode/resources/app.asar"),
                  Path("/usr/lib/zcode/resources/app.asar")]
    return cands


def set_target(asar_path):
    global ASAR, BAK, TMP, TMP_META
    ASAR = Path(asar_path)
    BAK = ASAR.with_name("app.asar.ztu.bak")
    TMP = ASAR.with_name("app.asar.ztu.tmp")
    TMP_META = TMP.with_name(TMP.name + ".json")


def zcode_exe_for(asar_path):
    root = asar_path.parent.parent
    # macOS: <App>.app/Contents/Resources/app.asar → 可执行文件在 Contents/MacOS/ 下
    for name in ("ZCode.exe", "MacOS/ZCode", "zcode"):
        p = root / name
        if p.is_file():
            return p
    return None


# ---------------------------------------------------------------- asar 读写

def read_header(f):
    f.seek(0)
    a, b, c, d = struct.unpack("<4I", f.read(16))
    if a != 4:
        raise ValueError(f"unexpected pickle prefix {a}")
    header = json.loads(f.read(d))
    return header, 8 + b


def iter_files(node, path=""):
    for name, ch in node.get("files", {}).items():
        p = f"{path}/{name}"
        if "files" in ch:
            yield from iter_files(ch, p)
        else:
            yield p, ch


def compute_integrity(data: bytes):
    return {
        "algorithm": "SHA256",
        "hash": hashlib.sha256(data).hexdigest(),
        "blockSize": BLOCK,
        "blocks": [hashlib.sha256(data[i:i + BLOCK]).hexdigest()
                   for i in range(0, len(data), BLOCK)],
    }


def repack(src_path: Path, modify: dict, dst_path: Path):
    """重建 asar。modify: {asar内路径: 新内容bytes}。流式拷贝未改动文件。"""
    with open(src_path, "rb") as src:
        header, base = read_header(src)
        header = json.loads(json.dumps(header))
        nodes = dict(iter_files(header))
        missing = set(modify) - set(nodes)
        if missing:
            raise ValueError(f"paths not in asar: {missing}")
        data_parts = []
        offset = 0
        for p, node in nodes.items():
            if node.get("unpacked"):
                node["offset"] = "0"
                continue
            if p in modify:
                data = modify[p]
                node["size"] = len(data)
                node["integrity"] = compute_integrity(data)
            else:
                src.seek(base + int(node["offset"]))
                data = src.read(node["size"])
            node["offset"] = str(offset)
            data_parts.append(data)
            offset += len(data)
        payload = b"".join(data_parts)
        header_bytes = json.dumps(header, ensure_ascii=False,
                                  separators=(",", ":")).encode("utf-8")
        d = len(header_bytes)
        c = (d + 4 + ALIGN - 1) // ALIGN * ALIGN
        b_total = c + 4
        with open(dst_path, "wb") as out:
            out.write(struct.pack("<4I", 4, b_total, c, d))
            out.write(header_bytes)
            out.write(b"\0" * (c - d - 4))
            if out.tell() != 8 + b_total:
                raise AssertionError("header pad mismatch")
            out.write(payload)
    return dst_path


def entry_name_of(asar_path: Path) -> str:
    """从 asar 内 package.json 的 main 字段推入口文件。"""
    with open(asar_path, "rb") as f:
        header, base = read_header(f)
        nodes = dict(iter_files(header))
        node = nodes.get("/package.json")
        if node:
            f.seek(base + int(node["offset"]))
            pkg = json.loads(f.read(node["size"]))
            main = pkg.get("main")
            if main:
                p = "/" + main.lstrip("./")
                if p in nodes:
                    return p.lstrip("/")
        for cand in ("out/main/index.js", "main.js", "index.js"):
            if "/" + cand in nodes:
                return cand
    raise ValueError("entry file not found in asar")


def entry_bytes_of(asar_path: Path, entry: str) -> bytes:
    with open(asar_path, "rb") as f:
        header, base = read_header(f)
        nodes = dict(iter_files(header))
        node = nodes["/" + entry]
        f.seek(base + int(node["offset"]))
        return f.read(node["size"])


def syntax_check(asar_path: Path, entry: str) -> bool:
    """用 ZCode 自带的 Electron 当 node 对入口做 --check（尽力而为，失败不挡安装）。"""
    exe = zcode_exe_for(asar_path)
    if not exe:
        print(L("  [check] 未找到 ZCode 可执行文件，跳过语法检查",
                "  syntax check skipped"))
        return True
    src = entry_bytes_of(asar_path, entry)
    env = dict(os.environ, ELECTRON_RUN_AS_NODE="1")
    fd, name = tempfile.mkstemp(prefix=".ztu-entry-check.", suffix=".js")
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(src)
        r = subprocess.run([str(exe), "--check", name], capture_output=True,
                           text=True, encoding="utf-8", errors="replace", env=env,
                           timeout=60)
        if r.returncode == 0:
            print(L("  [check] 入口语法检查通过", "  entry syntax OK"))
            return True
        print(L(f"  [check] 语法检查失败: {r.stderr.strip()[:300]}",
                f"  syntax check FAILED: {r.stderr.strip()[:300]}"))
        return False
    except Exception as e:
        print(L(f"  [check] 语法检查无法执行（{e}），跳过", f"  syntax check skipped ({e})"))
        return True
    finally:
        try:
            os.unlink(name)
        except OSError:
            pass


# ---------------------------------------------------------------- 运行时部署

RUNTIME_FILES = [
    (DAEMON / "pump.cjs", "pump.cjs"),
    (DAEMON / "bar.js", "bar.js"),
    (DAEMON / "config.example.json", "config.example.json"),
    (ENGINE, "usage_engine.py"),
]


def deploy_runtime():
    RUNTIME.mkdir(parents=True, exist_ok=True)
    for src, name in RUNTIME_FILES:
        dst = RUNTIME / name
        data = src.read_bytes()
        if dst.exists() and dst.read_bytes() == data:
            continue
        dst.write_bytes(data)
        print(L(f"  [deploy] {name}（{len(data)} 字节）",
                f"  deployed {name} ({len(data)} bytes)"))
    cfg = RUNTIME / "config.json"
    if not cfg.exists():
        cfg.write_text('{\n  "python_path": "",\n  "rate_ms": 1500,\n  "debounce_ms": 300\n}\n',
                       encoding="utf-8")
        print(L("  [deploy] 已生成默认 config.json（可配置 python_path 等）",
                "  default config.json created"))
    print(L(f"  [deploy] 运行时目录 {RUNTIME}", f"  runtime dir {RUNTIME}"))


# ---------------------------------------------------------------- 待替换收尾

def finish_pending():
    if not TMP.exists():
        print(L("无待替换文件", "no pending tmp"))
        return True
    meta = {}
    try:
        meta = json.loads(TMP_META.read_text(encoding="utf-8"))
    except Exception:
        pass
    try:
        os.replace(TMP, ASAR)
        TMP_META.unlink(missing_ok=True)
        print(L("待替换 .tmp 已生效", "pending tmp applied"))
        return True
    except OSError as e:
        print(L(f"替换仍失败（客户端未完全退出？）: {e}", f"replace failed: {e}"))
        return False


# ---------------------------------------------------------------- 安装/卸载

def inject_line_for(pump_path: Path) -> bytes:
    url = pump_path.as_uri().replace("'", "%27")
    return ('\n;import("%s").then(() => null, (e) => console.error('
            '"[ztu] load failed", e));' % url).encode("utf-8")


def do_install():
    if TMP.exists():
        print(L("  发现待替换 .tmp（上次 asar 被占用），先尝试收尾", "  pending tmp found; finishing first"))
        if not finish_pending():
            print(L("  仍被占用：请完全退出 ZCode 后重跑本命令", "  still locked; fully quit ZCode and re-run"))
            return False
    deploy_runtime()
    entry = entry_name_of(ASAR)
    print(L(f"  入口: {entry}", f"  entry: {entry}"))
    src_bytes = entry_bytes_of(ASAR, entry)
    if INJECT_RE.search(src_bytes):
        print(L("  注入行已存在（本次仅更新运行时文件）",
                "  already injected (runtime files refreshed)"))
        return True
    line = inject_line_for(RUNTIME / "pump.cjs")
    new_entry = src_bytes + line
    if not BAK.exists():
        shutil.copy2(ASAR, BAK)
        print(L(f"  已备份原 asar → {BAK.name}", f"  backup -> {BAK.name}"))
    repack(ASAR, {"/" + entry: new_entry}, TMP)
    ok = syntax_check(TMP, entry)
    if not ok:
        TMP.unlink(missing_ok=True)
        print(L("  已放弃写入（语法检查未过，原 asar 未动）",
                "  aborted (syntax check failed; asar untouched)"))
        return False
    try:
        os.replace(TMP, ASAR)
        print(L("  ✔ 注入完成（重启 ZCode 后生效）",
                "  injected (restart ZCode to take effect)"))
    except OSError:
        TMP_META.write_text(json.dumps({"source": str(ASAR)}, ensure_ascii=False),
                            encoding="utf-8")
        print(L("  asar 被占用：已写入 .tmp，请完全退出 ZCode 后重跑 "
                "python install.py --finish-pending",
                "  asar locked: tmp written; fully quit ZCode then run --finish-pending"))
    return True


def do_remove():
    entry = entry_name_of(ASAR)
    src_bytes = entry_bytes_of(ASAR, entry)
    new_entry = INJECT_RE.sub(b"", src_bytes)
    if new_entry == src_bytes:
        print(L("  未发现注入行，无需卸载", "  no inject line found"))
        return True
    repack(ASAR, {"/" + entry: new_entry}, TMP)
    try:
        os.replace(TMP, ASAR)
        print(L("  ✔ 已剥离注入行（重启 ZCode 后生效；备份保留于 " + BAK.name + "）",
                "  removed (restart to take effect)"))
    except OSError:
        TMP_META.write_text("{}", encoding="utf-8")
        print(L("  asar 被占用：已写 .tmp，退出 ZCode 后重跑 --finish-pending",
                "  asar locked; quit ZCode then run --finish-pending"))
    return True


def do_status():
    entry = entry_name_of(ASAR)
    src_bytes = entry_bytes_of(ASAR, entry)
    m = INJECT_RE.search(src_bytes)
    print(L(f"asar: {ASAR}", f"asar: {ASAR}"))
    print(L(f"入口: {entry}（{len(src_bytes)} 字节）", f"entry: {entry}"))
    print(L(f"注入行: {'存在' if m else '不存在'}",
            f"inject line: {'present' if m else 'absent'}"))
    print(L(f"备份: {'存在 ' + str(BAK) if BAK.exists() else '无'}",
            f"backup: {BAK if BAK.exists() else 'none'}"))
    print(L(f"运行时: {RUNTIME}{'（完整）' if (RUNTIME / 'pump.cjs').exists() else '（未部署）'}",
            f"runtime: {RUNTIME}"))
    if TMP.exists():
        print(L(f"待替换: {TMP} 存在 → 退出 ZCode 后跑 --finish-pending",
                f"pending: {TMP}"))


def check_inject() -> bool:
    entry = entry_name_of(ASAR)
    src_bytes = entry_bytes_of(ASAR, entry)
    return bool(INJECT_RE.search(src_bytes))


def main():
    global RUNTIME
    ap = argparse.ArgumentParser(description="ZCode Token 状态条安装器")
    ap.add_argument("--asar", help="app.asar 路径（默认自动探测）")
    ap.add_argument("--runtime", help="运行时目录（默认 ~/.zcode/ztu-statusbar）")
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--remove", action="store_true")
    ap.add_argument("--finish-pending", action="store_true")
    ap.add_argument("--check-inject", action="store_true",
                    help="退出码 0=注入行存在 1=不存在（供脚本校验，与输出语言无关）")
    args = ap.parse_args()

    if args.runtime:
        RUNTIME = Path(args.runtime)

    target = args.asar
    if not target:
        for c in default_asar_candidates():
            if c.is_file():
                target = c
                break
    if not target:
        print(L("未找到 app.asar，请用 --asar 指定", "app.asar not found; use --asar"))
        return 2
    set_target(target)

    if args.check_inject:
        return 0 if check_inject() else 1
    if args.finish_pending:
        return 0 if finish_pending() else 1
    if args.status:
        do_status()
        return 0
    if not ASAR.is_file():
        print(L(f"文件不存在: {ASAR}", f"not found: {ASAR}"))
        return 2
    if args.remove:
        return 0 if do_remove() else 1
    return 0 if do_install() else 1


if __name__ == "__main__":
    sys.exit(main())
