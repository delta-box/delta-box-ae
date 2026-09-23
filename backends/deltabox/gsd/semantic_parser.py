import re
import shlex

class InstructionSemanticParser:
    """
    Agent 指令语义解析器。
    通过静态分析传入的 shell 命令字符串，推测其对系统资源（文件系统、内存、进程层级）的破坏力，
    动态决定最佳的 CRIU 快照策略。

    策略层级 (由轻到重):
      lightweight  — 纯读命令，upper 层保持干净，无 CRIU dump、无 OverlayFS sink
      standard     — 标准增量 CRIU dump (--track-mem)
      predump      — 两段式: 先 pre-dump 再正式 dump (针对胖进程树)

    LW 的严格语义：upper 层保证不脏 ⇒ Phase 2 sink 不触发 ⇒
    LW 节点的 layers chain 等于父节点的 layers chain ⇒
    restore 到 LW 节点时直接使用父 std 节点的状态即可（FS + 进程都对齐）。
    任何会写文件的命令（包括 sed -i、cp、mv、rm、touch 等，以及
    swe_runner 注入的 __EDIT_ACTION__ 哨兵）必须归入 standard，
    以避免 LW sunk_layer 在 restore 时丢失（这是早期 R0/R4 设计遗留 bug）。
    """

    # 明确无副作用、纯读、不产生大内存的命令前缀
    READONLY_COMMANDS = {
        "cat", "ls", "pwd", "whoami", "echo", "head", "tail", "grep",
        "find", "stat", "wc", "file", "date", "hostname", "id", "env",
        "printenv", "uname", "df", "du", "free", "uptime", "which",
        "type", "true", "false", "test", "sleep",
    }

    # 已知会启动后台守护进程或长期阻塞的命令
    BACKGROUND_COMMANDS = {
        "nohup", "screen", "tmux", "systemctl", "service"
    }

    @classmethod
    def parse_strategy(cls, command_str: str) -> str:
        """
        根据终端输入的 raw 命令字符串，推荐 CRIU 快照策略。
        返回: 'lightweight' (轻量级跳过), 'predump' (两段式/重载), 或 'standard' (增量)
        """
        if not command_str or not command_str.strip():
            return "standard"

        # R-1: redirect to a real file dirties the upper layer → standard.
        # But skip safe redirect patterns that don't write to user FS:
        #   `2>/dev/null`, `&>/dev/null`, `>/dev/null`  (discard targets)
        #   `2>&1`, `&>&1`, `1>&2`                       (fd duplication)
        # We strip those first, then any remaining `>` indicates a real file write.
        _cmd_clean = re.sub(r'[0-9&]?>>?\s*(?:/dev/null|&\d)', '', command_str)
        if re.search(r'>\s*\S', _cmd_clean):
            return "standard"

        # R6 (compound): if `&&` / `;` chain, classify each sub-cmd recursively
        # and merge. Must run BEFORE R3/R5 — otherwise R5 fires on a leading
        # `cd` token and returns LW for `cd X && sed ...` (which actually writes).
        if "&&" in command_str or ";" in command_str:
            sub_cmds = [sc.strip() for sc in re.split(r'&&|;', command_str) if sc.strip()]
            sub_strategies = [cls.parse_strategy(sc) for sc in sub_cmds]
            if any(s == "predump" for s in sub_strategies):
                return "predump"
            if any(s == "standard" for s in sub_strategies):
                return "standard"
            if sub_strategies and all(s == "lightweight" for s in sub_strategies):
                return "lightweight"
            return "standard"

        # 1. 尝试解析命令 token
        try:
            # 简单处理管道，只看第一个命令
            first_cmd_part = command_str.split('|')[0].strip()
            # 去掉重定向得到纯命令部分用于 token 解析
            cmd_no_redir = re.split(r'>{1,2}', first_cmd_part)[0].strip()
            tokens = shlex.split(cmd_no_redir)
        except ValueError:  # 引号不匹配等解析错误
            tokens = [command_str.split()[0]] if command_str.split() else []

        if not tokens:
            return "standard"

        base_cmd = tokens[0].lower()
        # 处理路径前缀: /usr/bin/echo -> echo
        if '/' in base_cmd:
            base_cmd = base_cmd.rsplit('/', 1)[-1]

        # 2. 后台进程特征 -> predump (准备应对胖进程树)
        #    优先判断，避免 `nohup echo &` 被误判为 lightweight
        if base_cmd in cls.BACKGROUND_COMMANDS or command_str.strip().endswith("&"):
            return "predump"

        # 特殊探测：SWE-bench 中常有 python run_tests.py --parallel 导致僵尸进程爆炸
        if base_cmd in ["python", "python3", "pytest", "tox"]:
            if "--parallel" in command_str or "-n" in command_str:
                return "predump"

        # 3. 纯读命令 -> lightweight
        #    cat/ls/grep 等不改变进程内存且不写文件
        if base_cmd in cls.READONLY_COMMANDS:
            return "lightweight"

        # 4. sed without `-i` is a stream reader (pager / print mode).
        #    `sed -i …` writes the file in place → standard.
        #    `sed -n '…p' file` / `sed 'expr' file` → reads only → lightweight.
        if base_cmd == "sed":
            if "-i" in tokens:
                return "standard"
            return "lightweight"

        # 5. shell 内建的 cd + 简单赋值 -> lightweight
        if base_cmd in ("cd", "export", "set", "unset", "alias", "source", "."):
            return "lightweight"

        return "standard"
