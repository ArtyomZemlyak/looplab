
def resolve_resume_settings(env=None, *, deepspeed: str = "", peft: str = "none") -> dict:
    env = os.environ if env is None else env
    root = (env.get("SFT_RESUME_DIR") or "").strip()
    every_min = float((env.get("SFT_RESUME_EVERY_MIN") or "60").strip() or 0)
    staging = (env.get("SFT_RESUME_STAGING") or "").strip()
    if not staging and root:
        mr_root = (env.get("MR_ROOT") or "/var/tmp/minionerec").strip()
        staging = os.path.join(mr_root, "scratch", "resume-staging", Path(root).name)
    uses_deepspeed = bool(deepspeed) and str(deepspeed).strip().lower() not in ("null", "none")
    why_off = ""
    if not root:
        why_off = "SFT_RESUME_DIR unset"
    elif every_min <= 0:
        why_off = "SFT_RESUME_EVERY_MIN=0"
    elif uses_deepspeed:
        why_off = "DeepSpeed (a ZeRO checkpoint is ~7x the model; not supported)"
    elif peft not in ("", "none", None):
        why_off = f"SFT_PEFT={peft} (not supported)"
    return {
        "enabled": not why_off, "dir": root, "staging": staging,
        "every_seconds": every_min * 60.0, "why_off": why_off,
    }
