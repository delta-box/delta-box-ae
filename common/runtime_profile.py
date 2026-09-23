"""The checkpoint protocol shared by live-agent and trace-replay entry points.

Historical full dumps are an explicit comparison arm, not incremental evidence.
Per-experiment ablations (e.g. no-dump memory policies) are recorded separately.
"""
PROFILES = ("runtime-default", "historical-async-full", "async-incremental", "async-incremental-lazy")


def checkpoint_environment(profile: str = "runtime-default", mode: str = "fast") -> dict[str, str]:
    if profile not in PROFILES or mode not in ("fast", "slow"):
        raise ValueError(f"unknown checkpoint configuration: {profile}/{mode}")
    incremental = profile != "historical-async-full"
    detached = profile in ("async-incremental", "async-incremental-lazy")
    return {
        "DELTABOX_CHECKPOINT_STASH_TEMPLATE": "1",
        "DELTABOX_ASYNC_INCREMENTAL_DUMP": "1" if detached else "0",
        "DELTABOX_FRESH_PIDNS_ACTIVE": "0",
        "DELTABOX_ASYNC_TEMPLATE_FULL_DUMP": "0" if incremental else "1",
        "DELTABOX_FIXED_ACTIVE_PID": "100" if incremental and not detached else "0",
        "DELTABOX_RESTAMP_PARENT_INVENTORY": "0",
        "DELTABOX_RESTORE_FASTFORK_DUMP_PID": "0",
        "DELTABOX_FIXED_SLOT_TIMEOUT_S": "5",
        "DELTABOX_FORCE_CRIU_RESTORE": "1" if mode == "slow" else "0",
        "DELTABOX_CRIU_LAZY_RESTORE": "1" if mode == "slow" and profile != "async-incremental" else "0",
        "DELTABOX_CRIU_LAZY_RESTORE_PARALLEL": "0",
    }
