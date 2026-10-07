"""Explicitly approved, non-recursive CUELO deployment permission setup.

Run as root in a network-disabled helper with only /opt/cuelo mounted at /target.
Uses Linux POSIX ACL xattrs; no package installation or profile traversal.
"""
import errno
import os
from pathlib import Path
import stat
import struct
import sys

uid = int(sys.argv[1])
if uid <= 0:
    raise SystemExit("Refusing a root deployment identity")
root = Path("/target")
paths = [(root, 5), (root / "compose.yaml", 4)]
if (root / ".env").exists():
    paths.append((root / ".env", 4))
updates = []
for path, access in paths:
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode) or info.st_uid != 0:
        raise SystemExit(f"Unexpected owner or symlink: {path.name}")
    mode = stat.S_IMODE(info.st_mode)
    # Preserve owner/group/other rights; add only the named runner user.
    group = (mode >> 3) & 7
    entries = [(1, (mode >> 6) & 7, 0xFFFFFFFF), (2, access, uid),
               (4, group, 0xFFFFFFFF), (16, group | access, 0xFFFFFFFF),
               (32, mode & 7, 0xFFFFFFFF)]
    try:
        old = os.getxattr(path, "system.posix_acl_access")
    except OSError as error:
        if error.errno != errno.ENODATA:
            raise
        old = None
    if old is not None:
        prior = [struct.unpack("<HHI", old[n:n + 8]) for n in range(4, len(old), 8)]
        # Idempotent replay only. Never widen an unrelated existing ACL mask.
        if len(prior) != 5 or prior[1] != (2, access, uid) or [e[0] for e in prior] != [1, 2, 4, 16, 32]:
            raise SystemExit(f"Existing ACL needs operator review: {path.name}")
        continue
    updates.append((path, struct.pack("<I", 2) + b"".join(struct.pack("<HHI", *e) for e in entries)))
deploy = root / "deploy"
if deploy.exists() and (deploy.is_symlink() or not deploy.is_dir() or deploy.stat().st_uid != uid):
    raise SystemExit("Existing deployment directory needs operator review")
for path, acl in updates:
    os.setxattr(path, "system.posix_acl_access", acl)
    print(f"Runner ACL applied: {path.name}, no recursive changes")
if not deploy.exists():
    deploy.mkdir(mode=0o700)
    os.chown(deploy, uid, -1)
print("Deployment directory ready; home, workspace and profile permissions untouched")
