"""Restricted replay resource contract for disposable asynchronous dump workers.

This is not a general FD snapshot adapter. Persistent application files,
writable or non-image shared mappings, sockets and unowned children are rejected. Named replay
FIFOs and append-only diagnostic stdio are external transports, not rollback
state. Library mappings outside the covered overlay rely on the caller's
immutable guest-image contract.
"""
from contextlib import contextmanager
import os
from pathlib import Path
import stat


class UnsupportedAsyncResources(RuntimeError):
    pass


DEFAULT_FIFOS = ("/tmp/agent.in", "/tmp/agent.out", "/tmp/template_ctrl.in",
                 "/tmp/template_ctrl.out", "/tmp/npd_req.fifo", "/tmp/npd_notify.fifo")
DEFAULT_STDIO = ("/tmp/replay-agent.log", "/tmp/criu_agent.log", "/tmp/replay.log")
IMMUTABLE_MAPPING_ROOTS = ("/usr", "/lib", "/lib64", "/bin", "/sbin", "/opt", "/app",
                           "/mnt/data/opt")  # read-only data disk backs /opt in replay


def _absolute(path):
    if not isinstance(path, str) or not path.startswith("/") or "\x00" in path:
        raise UnsupportedAsyncResources(f"Expected an absolute resource path: {path!r}")
    return os.path.normpath(path)


def _within(path, root):
    return path == root or path.startswith(root.rstrip("/") + "/")


def _status_identity(proc):
    _, sep, tail = (proc / "stat").read_text().rpartition(")")
    fields = tail.split()
    if not sep or len(fields) < 20:
        raise UnsupportedAsyncResources("Cannot verify worker process identity")
    return fields[0], int(fields[19])


def _identity(proc):
    return _status_identity(proc)[1]


def validate_replay_resources(pid, *, overlay_mount_point, allowed_child_pids=(),
                              allowed_fifo_paths=None, allowed_stdio_paths=None,
                              proc_root="/proc"):
    """Inspect an idle, single-threaded worker before changing its filesystem.

    Caller must hold the workload command gate until both clones are frozen.
    ``allowed_child_pids`` must be owned, frozen disposable dump workers. The
    immutable-image assumption is explicit in the returned evidence; this
    does not authorize mutations outside the covered task overlay.
    """
    proc = Path(proc_root) / str(pid)
    overlay = _absolute(overlay_mount_point)
    fifos = {_absolute(p) for p in (DEFAULT_FIFOS if allowed_fifo_paths is None else allowed_fifo_paths)}
    stdio = {_absolute(p) for p in (DEFAULT_STDIO if allowed_stdio_paths is None else allowed_stdio_paths)}
    try:
        identity = _identity(proc)
        tasks = sorted(p.name for p in (proc / "task").iterdir())
        if tasks != [str(pid)]:
            raise UnsupportedAsyncResources(f"Worker must be single-threaded; tasks={tasks}")
        children = set(map(int, (proc / "task" / str(pid) / "children").read_text().split()))
        unsupported_children, exited_children = set(), set()
        for child in children - set(allowed_child_pids):
            try:
                state, _ = _status_identity(Path(proc_root) / str(child))
            except FileNotFoundError:
                exited_children.add(child)
                continue
            if state in ("Z", "X"):
                exited_children.add(child)
            else:
                unsupported_children.add(child)
        if unsupported_children:
            raise UnsupportedAsyncResources(f"Live/unowned worker children: {sorted(unsupported_children)}")
        cwd = os.readlink(proc / "cwd")
        if not cwd.startswith("/") or cwd.endswith(" (deleted)"):
            raise UnsupportedAsyncResources(f"Unsupported cwd: {cwd!r}")
        if _within(cwd, overlay):
            raise UnsupportedAsyncResources(f"Worker cwd refers to mutable task overlay: {cwd}")
        executable = os.readlink(proc / "exe")
        if (executable.endswith(" (deleted)") or _within(executable, overlay)
                or not any(_within(executable, root) for root in IMMUTABLE_MAPPING_ROOTS)):
            raise UnsupportedAsyncResources(f"Executable outside the immutable image contract: {executable}")
        fds = []
        fd_names = sorted((p.name for p in (proc / "fd").iterdir()), key=int)
        seen_fifo_paths = set()
        for fd_name in fd_names:
            fd = int(fd_name)
            fd_path = proc / "fd" / fd_name
            path = os.readlink(fd_path)
            info = dict(line.split(":", 1) for line in (proc / "fdinfo" / fd_name).read_text().splitlines() if ":" in line)
            flags = int(info["flags"].strip(), 8)
            mode = os.stat(fd_path).st_mode
            if path.endswith(" (deleted)"):
                raise UnsupportedAsyncResources(f"Deleted-open fd {fd}: {path}")
            if stat.S_ISFIFO(mode) and path in fifos:
                if flags & os.O_ACCMODE != os.O_RDWR or not flags & os.O_NONBLOCK:
                    raise UnsupportedAsyncResources(f"Protocol FIFO fd {fd} must be RDWR|NONBLOCK")
                if path in seen_fifo_paths:
                    raise UnsupportedAsyncResources(f"Duplicated protocol FIFO descriptors are unsupported: {path}")
                seen_fifo_paths.add(path)
                kind = "protocol_fifo"
            elif stat.S_ISCHR(mode) and path == "/dev/null" and fd in (0, 1, 2):
                kind = "null_stdio"
            elif stat.S_ISREG(mode) and fd in (1, 2) and path in stdio and flags & os.O_APPEND:
                kind = "diagnostic_stdio"
            else:
                raise UnsupportedAsyncResources(f"Unsupported persistent fd {fd}: {path!r} flags={flags:o}")
            fds.append(dict(fd=fd, path=path, flags=flags, kind=kind,
                            cloexec=bool(flags & os.O_CLOEXEC),
                            inode=os.stat(fd_path).st_ino,
                            mnt_id=int(info["mnt_id"].strip())))
        mappings = []
        for line in (proc / "maps").read_text().splitlines():
            fields = line.split(None, 5)
            if len(fields) < 5 or len(fields[1]) != 4:
                raise UnsupportedAsyncResources(f"Malformed worker mapping: {line!r}")
            address, perms = fields[:2]
            path = fields[5] if len(fields) == 6 else ""
            if path in ("[vvar]", "[vvar_vclock]", "[vdso]", "[vsyscall]"):
                continue
            immutable_shared = (perms == "r--s" and path.startswith("/")
                                and not _within(path, overlay)
                                and any(_within(path, root) for root in IMMUTABLE_MAPPING_ROOTS))
            if perms[3] != "p" and not immutable_shared:
                raise UnsupportedAsyncResources(f"Shared mapping cannot be frozen by fork: {line}")
            if path.endswith(" (deleted)"):
                raise UnsupportedAsyncResources(f"Deleted file mapping is unsupported: {line}")
            if path.startswith("/"):
                if _within(path, overlay):
                    raise UnsupportedAsyncResources(f"Persistent mapping of mutable task files: {line}")
                if not any(_within(path, root) for root in IMMUTABLE_MAPPING_ROOTS):
                    raise UnsupportedAsyncResources(f"Mapping outside the immutable image contract: {line}")
                mappings.append(dict(address=address, permissions=perms, path=path))
            elif path and not (path in ("[heap]", "[stack]") or path.startswith("[anon:")):
                raise UnsupportedAsyncResources(f"Unknown mapping resource: {line}")
        if identity != _identity(proc) or fd_names != sorted((p.name for p in (proc / "fd").iterdir()), key=int):
            raise UnsupportedAsyncResources("Worker resources changed during inspection")
        return dict(version=1, pid=pid, starttime=identity, overlay_mount_point=overlay,
                    cwd=cwd, executable=executable, fds=fds, immutable_image_mappings=mappings,
                    immutable_image_required=True, protocol_fifos_external=True,
                    diagnostic_stdio_external=True, mutable_task_file_references=False,
                    exited_children=sorted(exited_children))
    except (OSError, ValueError, KeyError) as error:
        raise UnsupportedAsyncResources(f"Cannot verify async replay resources: {error}") from error


def _validate_contract(contract):
    if (contract.get("version") != 1 or contract.get("protocol_fifos_external") is not True
            or contract.get("mutable_task_file_references") is not False):
        raise UnsupportedAsyncResources("Unknown or unverified async resource contract")
    return _absolute(contract["overlay_mount_point"])


def dump_child_contract(contract):
    """Copy only child setup fields into the bounded FIFO request payload.

    Full mapping and process-identity evidence remains in the controller's
    checkpoint entry. It can be much larger than the protocol pipe capacity.
    This does not replace the protocol's deadline-aware write-all handling.
    """
    _validate_contract(contract)
    keys = ("version", "overlay_mount_point", "cwd", "protocol_fifos_external",
            "mutable_task_file_references")
    result = {key: contract[key] for key in keys}
    result.update(immutable_image_required=True, diagnostic_stdio_external=True)
    result["fds"] = [{key: item[key] for key in
                      ("fd", "path", "flags", "kind", "cloexec")}
                     for item in contract["fds"]]
    return result


def freeze_dump_view(contract, *, lower_layers=None, workspace=None):
    """Child-only: detach permitted transport OFDs under the replay contract.

    Also accepts ``{contract, lower_layers, workspace}`` as one protocol payload.
    On error the disposable child must exit; never run this in active/template
    or the controller. The caller pins lower-layer leases. No cwd, executable,
    FD or mapping refers to the mutable task mount; CRIU treats that mount as
    external and the controller restores its saved generation before resume.
    We deliberately do not unshare mounts: inherited file-backed VMAs would
    retain mount references that an untested new mount tree may not resolve.
    """
    if "contract" in contract:
        payload = contract
        contract, lower_layers, workspace = payload["contract"], payload["lower_layers"], payload["workspace"]
    target = _validate_contract(contract)
    if _within(contract["cwd"], target):
        raise UnsupportedAsyncResources("Guard-only replay cannot freeze an overlay cwd")
    lowers = [_absolute(path) for path in (lower_layers or [])]
    for item in contract["fds"]:
        # Explicitly permitted transports carry no rollback state. Separate
        # OFDs prevent CRIU's operations from changing active FD status/offsets.
        newfd = os.open(item["path"], item["flags"] & ~os.O_CLOEXEC)
        try:
            if newfd != item["fd"]:
                os.dup2(newfd, item["fd"], inheritable=not item["cloexec"])
            else:
                os.set_inheritable(newfd, not item["cloexec"])
        finally:
            if newfd != item["fd"]:
                os.close(newfd)
    return dict(version=1, overlay_mount_point=target, lower_layers=lowers,
                workspace=workspace, isolated_overlay=False, protocol_fifos_external=True,
                diagnostic_stdio_external=True, mutable_task_file_references=False,
                immutable_image_required=True)


@contextmanager
def protocol_restore_fds(contract):
    """Supply external replay FIFOs to pinned CRIU without replaying old bytes.

    Pinned CRIU fifo.c -> open_path(reg_d) checks inherited_fd before calling
    do_open_fifo/restore_pipe_data. reg_file_path uses a root-relative name.
    Caller retains the command gate and performs epoch reactivation separately.
    Real CRIU validation is required; this function does not attest that it ran.
    """
    _validate_contract(contract)
    fds, args = [], []
    try:
        seen = set()
        for item in contract["fds"]:
            if item["kind"] not in ("protocol_fifo", "diagnostic_stdio") or item['path'] in seen:
                continue
            seen.add(item['path'])
            fd = os.open(item["path"], item["flags"] & ~os.O_CLOEXEC)
            fds.append(fd)
            mode = os.fstat(fd).st_mode
            if ((item['kind'] == 'protocol_fifo' and not stat.S_ISFIFO(mode))
                    or (item['kind'] == 'diagnostic_stdio' and not stat.S_ISREG(mode))):
                raise UnsupportedAsyncResources(f"External replay transport replaced: {item['path']}")
            args.extend(["--inherit-fd", f"fd[{fd}]:{item['path'].lstrip('/')}"])
        yield args, tuple(fds)
    finally:
        for fd in fds:
            os.close(fd)
