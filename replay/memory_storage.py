"""Private, non-swappable RAM disks for measured VM I/O (host side only)."""
from contextlib import contextmanager
import errno
import json
import os
from pathlib import Path
import subprocess


def readonly_image_capacity(path):
    """Bound resident pages using logical data extents, never compressed st_blocks.

    Source holes are guaranteed zero bytes and remain holes during sparse staging.
    Unknown extent semantics fail closed to the full logical image size. Only the
    read-only data drive can use this bound; the writable rootfs can grow anywhere.
    """
    size = path.stat().st_size
    page = os.sysconf("SC_PAGE_SIZE")
    logical = ((size + page - 1) // page) * page
    if not hasattr(os, "SEEK_DATA") or not hasattr(os, "SEEK_HOLE"):
        return dict(bytes=logical, method="logical-size")
    total, cursor, end_page = 0, 0, 0
    with path.open("rb") as stream:
        while cursor < size:
            try:
                start = os.lseek(stream.fileno(), cursor, os.SEEK_DATA)
            except OSError as exc:
                if exc.errno == errno.ENXIO:
                    break
                if exc.errno in (errno.EINVAL, errno.ENOTSUP, errno.ENOSYS):
                    return dict(bytes=logical, method="logical-size")
                raise
            try:
                end = min(size, os.lseek(stream.fileno(), start, os.SEEK_HOLE))
            except OSError as exc:
                if exc.errno in (errno.EINVAL, errno.ENOTSUP, errno.ENOSYS):
                    return dict(bytes=logical, method="logical-size")
                raise
            if not cursor <= start < end <= size:
                raise RuntimeError("Invalid read-only image extent map")
            first, last = start // page, (end + page - 1) // page
            total += max(0, last - max(first, end_page)) * page
            end_page, cursor = last, end
    return dict(bytes=total, method="page-rounded-seek-data-extents")


def memory_budget(config, sources, policy, node_root=Path("/sys/devices/system/node"), *, stage_only=False, staged_base=None):
    fields = dict(line.split(":", 1) for line in policy.splitlines() if ":" in line)
    if fields.get("policy", "").strip() != "bind":
        raise RuntimeError("RAM measurements require an explicit NUMA memory bind")
    nodes = [int(value) for value in fields.get("membind", "").split()]
    if not nodes:
        raise RuntimeError("Cannot determine the NUMA memory binding")
    free, reclaimable = {}, {}
    for node in nodes:
        lines = (node_root / f"node{node}" / "meminfo").read_text().splitlines()
        values = {line.split()[2].rstrip(":"): int(line.split()[3]) * 1024 for line in lines if line.endswith(" kB")}
        free[str(node)] = values["MemFree"]
        # Both active and inactive clean file pages are reclaimable. Repeated
        # image verification promotes this run's source cache to active LRU;
        # excluding it falsely rejects RAM capacity after earlier inputs pass.
        # Discount all dirty/writeback/mapped pages; shmem is on the anon LRU.
        reclaimable[str(node)] = max(0, values.get("Inactive(file)", 0)
            + values.get("Active(file)", 0)
            - values.get("Dirty", 0) - values.get("Writeback", 0) - values.get("Mapped", 0))
    # Conservative capacity gate: data + one base becoming the writable rootfs,
    # full guest RAM and 2 GiB host headroom; no swap or remote-node allocation.
    data = readonly_image_capacity(sources["data_xfs"])
    required = (sources["base_xfs"].stat().st_size + data["bytes"]
                + int(config["mem_mib"]) * (1 << 20) + (2 << 30))
    if stage_only:
        required -= int(config["mem_mib"]) * (1 << 20)
    rootfs_growth = 0
    if staged_base is not None:
        info = Path(staged_base).stat()
        rootfs_growth = max(0, info.st_size - info.st_blocks * 512)
        # Staged tmpfs pages are already charged to the node. Reserve the full
        # guest allocation and all remaining writable rootfs holes before boot.
        required = int(config["mem_mib"]) * (1 << 20) + rootfs_growth + (2 << 30)
    return dict(nodes=nodes, free_bytes=free, reclaimable_file_bytes=reclaimable,
                available_bytes=sum(free.values()) + sum(reclaimable.values()),
                required_bytes=required, reserve_bytes=2 << 30,
                phase="staging" if stage_only else "guest-boot" if staged_base is not None else "combined",
                rootfs_growth_bytes=rootfs_growth, readonly_data=data)


@contextmanager
def memory_images(runtime, config, record_path):
    """Stage both disks before VM boot; never silently fall back to disk."""
    runtime = Path(runtime)
    sources = {name: Path(config[name]) for name in ("base_xfs", "data_xfs")}
    policy = subprocess.check_output(["numactl", "--show"], text=True)
    capacity = memory_budget(config, sources, policy, stage_only=True)
    record = dict(mode="tmpfs", status="preparing", numa_policy=policy, capacity=capacity,
                  staging_outside_measurement=True)
    if capacity["available_bytes"] < capacity["required_bytes"]:
        record["status"] = "insufficient-numa-memory"
        Path(record_path).write_text(json.dumps(record, indent=2) + "\n")
        raise RuntimeError("Insufficient free memory on bound NUMA node(s), including clean unmapped file cache: "
                           f"{capacity['available_bytes'] / 2**30:.1f} GiB available, "
                           f"{capacity['required_bytes'] / 2**30:.1f} GiB required; "
                           "release other workloads before retrying")
    # The verified private base becomes the writable rootfs without a second copy.
    size = sum(path.stat().st_size for path in sources.values()) + (1 << 30)
    subprocess.run(["mount", "-t", "tmpfs", "-o", f"size={size},noswap",
                    "deltabox-vm-ram", str(runtime)], check=True)
    try:
        mount = json.loads(subprocess.check_output(
            ["findmnt", "--json", "--mountpoint", str(runtime),
             "--output", "TARGET,FSTYPE,OPTIONS"], text=True))["filesystems"][0]
        if mount["fstype"] != "tmpfs" or "noswap" not in mount["options"].split(","):
            raise RuntimeError("VM disks must be on a verified noswap tmpfs")
        disks = {}
        for name, source in sources.items():
            target = runtime / ("base.xfs" if name == "base_xfs" else "data.xfs")
            # Preserve zero extents; copying occurs under the inherited NUMA policy.
            subprocess.run(["cp", "--sparse=always", "--reflink=never", str(source), str(target)], check=True)
            from provenance import file_digest, signature
            identity = file_digest(target)
            if identity["sha256"] != config["images"][name]["sha256"]:
                raise RuntimeError("RAM-staged image differs from its recorded source: " + name)
            recorded = {k: v for k, v in config["images"][name].items() if k != "sha256"}
            if signature(source) != recorded:
                raise RuntimeError("Source image changed during RAM staging: " + name)
            if name == "data_xfs" and "readonly_data" in capacity:
                if target.stat().st_blocks * 512 > capacity["readonly_data"]["bytes"]:
                    raise RuntimeError("RAM-staged data exceeds its reserved resident-page bound")
            disks[name] = dict(source=str(source), staged=str(target), sha256=identity["sha256"],
                               bytes=identity["size"] if "size" in identity else target.stat().st_size,
                               allocated_bytes=target.stat().st_blocks * 512)
        boot_capacity = memory_budget(config, sources, policy, staged_base=runtime / "base.xfs")
        record.update(disks=disks, boot_capacity=boot_capacity, mount=mount)
        if boot_capacity["available_bytes"] < boot_capacity["required_bytes"]:
            record["status"] = "insufficient-numa-memory-after-staging"
            Path(record_path).write_text(json.dumps(record, indent=2) + "\n")
            raise RuntimeError("Insufficient free memory after RAM staging: "
                f"{boot_capacity['available_bytes'] / 2**30:.1f} GiB available, "
                f"{boot_capacity['required_bytes'] / 2**30:.1f} GiB required for guest/rootfs growth/reserve")
        record.update(status="ready", mount=mount, disks=disks, writable_rootfs=str(runtime / "rootfs.xfs"),
                      rootfs_preparation="rename verified private base to fresh writable rootfs before VM boot")
        Path(record_path).write_text(json.dumps(record, indent=2) + "\n")
        yield runtime / "base.xfs", runtime / "data.xfs"
    finally:
        # This mount is private to the VM runner's existing mount namespace.
        subprocess.run(["umount", str(runtime)], check=True)
