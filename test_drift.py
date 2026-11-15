#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""仓库自洽自检：所有"被拉起的脚本"必须真的存在

起因（2026-11-15 代码审计）：`quick_cycle.py` 在开发目录里存在、在仓库里没提交，
于是 `frame_guard_core` 的快速重连永远 FileNotFoundError，异常被吞成 `return False`，
守护退化成一整轮 reload_chain——本地能跑、clone 下来不能跑，两个月没暴露。

所以这里不测业务逻辑，只测"引用完整性"：扫源码与批处理里所有按文件名拉起的
`.py`（`spawn("x.py")`、`[PY, "-I", "x.py"]`、`python -I x.py`），逐个
`os.path.exists`。缺一个就 FAIL 并打印是谁在引用它。

直接运行：python -I test_drift.py
"""
import os
import re
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

HERE = os.path.dirname(os.path.abspath(__file__))
SELF = os.path.basename(__file__)

# 只看"代码行"：整行注释与行尾注释里提到的历史脚本不算引用
PAT_QUOTED = re.compile(r'"([A-Za-z0-9_.\-]+\.py)"')
PAT_BAT = re.compile(r'(?:^|\s|\\)([A-Za-z0-9_.\-]+\.py)')
SKIP_LINE = ('#', ';', '::')


def code_lines(path):
    with open(path, 'r', encoding='utf-8', errors='replace') as f:
        for no, raw in enumerate(f, 1):
            line = raw.rstrip('\n')
            stripped = line.strip()
            if not stripped or stripped.startswith(SKIP_LINE):
                continue
            # 去掉行尾注释（够用：这些脚本里没有含 # 的字符串字面量）
            cut = line.split('#', 1)[0]
            yield no, cut


def collect():
    """{(被引用脚本): [(引用者:行号), ...]}"""
    refs = {}
    for name in sorted(os.listdir(HERE)):
        if not name.endswith(('.py', '.bat')):
            continue
        if name == SELF or name.startswith('test_'):
            continue
        path = os.path.join(HERE, name)
        pat = PAT_BAT if name.endswith('.bat') else PAT_QUOTED
        for no, line in code_lines(path):
            for m in pat.finditer(line):
                refs.setdefault(m.group(1), []).append('%s:%d' % (name, no))
    return refs


def main():
    refs = collect()
    bad = []
    for target in sorted(refs):
        where = refs[target]
        ok = os.path.isfile(os.path.join(HERE, target))
        print('%s %-28s 被 %d 处引用  %s' % ('[PASS]' if ok else '[FAIL]', target,
                                            len(where), ', '.join(where[:3])
                                            + (' …' if len(where) > 3 else '')))
        if not ok:
            bad.append((target, where))
    print('\n=== %d/%d 引用完整 ===' % (len(refs) - len(bad), len(refs)))
    if bad:
        print('缺失脚本（会造成静默退化，不报错但也永不生效）：')
        for target, where in bad:
            print('  - %s   ← %s' % (target, ', '.join(where)))
    return 1 if bad else 0


if __name__ == '__main__':
    sys.exit(main())
