"""CUELO 클라우드 이미지 inventory·cleanup(deploy/cuelo/host.sh)을 fake Docker CLI로 검증한다.

실제 Docker 없이 PATH에 fake `docker`를 주입해 bash 경계(`host.sh images|cleanup-images`)를 그대로 실행한다.
fake는 모든 호출을 로그에 남기고, `image rm`은 실제 Docker처럼 마지막 태그를 지울 때 이미지를 삭제하며
컨테이너가 참조하면 거절하고 `-f`는 받지 않는다. 표준 라이브러리 unittest만 쓴다(pytest도 그대로 수집한다).
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
HOST_SH = REPO_ROOT / "deploy" / "cuelo" / "host.sh"
FORBIDDEN_VERBS = "rm rmi stop kill restart run exec load tag".split()
CURRENT_NAME = "/cuelo-cloud-cuelo-1"
ROLLBACK_PREFIX = "cuelo-cloud-rollback:"
DEFAULT_SIZE = 1_500_000_000

FAKE_DOCKER = r"""import json, os, sys

STATE = os.environ["FAKE_DOCKER_STATE"]
args = sys.argv[1:]
st = json.load(open(STATE))
with open(os.environ["FAKE_DOCKER_LOG"], "a") as f:
    f.write(json.dumps(args) + "\n")


def save():
    json.dump(st, open(STATE, "w"))


def fmt():
    for i, a in enumerate(args):
        if a in ("-f", "--format"):
            return args[i + 1]
    return ""


def resolve(ref):
    if ref in st["images"]:
        return ref
    for iid, img in st["images"].items():
        if ref in img["tags"]:
            return iid
    return None


def referenced(iid):
    return [c for c in st["containers"] if c["image"] == iid]


def die(msg, code=1):
    print(msg, file=sys.stderr)
    sys.exit(code)


if args[:2] == ["image", "ls"]:
    st["ls_calls"] = st.get("ls_calls", 0) + 1
    inj = st.get("inject_on_ls")
    if inj and st["ls_calls"] == inj["call"]:
        if "container" in inj:
            st["containers"].append(inj["container"])
        if "add_tag" in inj:
            st["images"][inj["add_tag"]["id"]]["tags"].append(inj["add_tag"]["tag"])
    save()
    for iid, img in st["images"].items():
        for t in img["tags"] or ["<none>:<none>"]:
            repo, tag = t.rsplit(":", 1)
            print(f"{iid}|{repo}|{tag}")
elif args[:2] == ["image", "inspect"]:
    iid = resolve(args[-1])
    if iid is None:
        die("Error: No such image: " + args[-1])
    img = st["images"][iid]
    f = fmt()
    if f == "{{.Id}}":
        print(iid)
    elif f == "{{.Created}}|{{.Size}}":
        print(f"{img['created']}|{img['size']}")
    else:
        die("unhandled image inspect format " + f, 9)
elif args[:2] == ["image", "rm"]:
    if any(a in ("-f", "--force") for a in args[2:]) or len(args) != 3:
        die("forbidden image rm form: " + " ".join(args), 99)
    ref = args[2]
    if ref in st.get("rm_fail", []):
        die("Error response from daemon: fake rm failure for " + ref)
    iid = resolve(ref)
    if iid is None:
        die("Error response from daemon: No such image: " + ref)
    img = st["images"][iid]
    if ref == iid and len(img["tags"]) > 1:
        die("Error response from daemon: conflict: unable to delete (must be forced) - image is referenced in multiple repositories")
    last = len(img["tags"]) <= 1
    users = referenced(iid)
    if last and users:
        die("Error response from daemon: conflict: unable to remove repository reference (must force) - container " + users[0]["id"] + " is using its referenced image")
    if ref != iid:
        img["tags"].remove(ref)
        print("Untagged: " + ref)
    if last:
        del st["images"][iid]
        print("Deleted: " + iid)
    save()
elif args[:1] == ["ps"]:
    labels = [a.split("=", 1)[1] for i, a in enumerate(args) if i and args[i - 1] == "--filter" and a.startswith("label=")]
    for c in st["containers"]:
        if "-aq" not in args and c["state"] != "running":
            continue
        have = {f"{k}={v}" for k, v in c.get("labels", {}).items()}
        if all(l in have for l in labels):
            print(c["id"])
elif args[:1] == ["inspect"]:
    f = fmt()
    ids = args[3:]
    by_id = {c["id"]: c for c in st["containers"]}
    for cid in ids:
        c = by_id.get(cid)
        if c is None:
            die("Error: No such object: " + cid)
        if f == "{{.Image}}|{{.Name}}|{{.State.Status}}":
            print(f"{c['image']}|{c['name']}|{c['state']}")
        elif f == "{{.Image}}":
            print(c["image"])
        else:
            die("unhandled inspect format " + f, 9)
elif args[:2] == ["system", "df"]:
    total = len(st["images"])
    active = len({c["image"] for c in st["containers"]})
    size = sum(i["size"] for i in st["images"].values())
    print("Images|%d|%d|%.2fGB|0B (0%%)" % (total, active, size / 1e9))
    print("Containers|%d|%d|10MB|0B (0%%)" % (len(st["containers"]), len(st["containers"])))
    print("Build Cache|0|0|0B|0B")
elif args[:1] == ["info"]:
    print(st["root_dir"])
elif args[:1] == ["compose"] and args[-2:] == ["config", "--images"]:
    for r in st["compose_images"][str(args.count("-f"))]:
        print(r)
else:
    die("unhandled fake docker call: " + " ".join(args), 9)
"""


def sid(name: str) -> str:
    return "sha256:" + hashlib.sha256(name.encode()).hexdigest()


def img(tags: list[str], created: str, size: int = DEFAULT_SIZE) -> dict:
    return {"tags": tags, "created": created, "size": size}


def container(cid: str, image: str, name: str, state: str, labels=None) -> dict:
    return {
        "id": cid * 64,
        "image": image,
        "name": name,
        "state": state,
        "labels": labels or {},
    }


NAMES = (
    "current",
    "rollback",
    "old96",
    "old95",
    "held",
    "shared",
    "newer",
    "manual",
    "ovr",
    "redis",
    "dangling",
)
IDS = {name: sid(name) for name in NAMES}
SIZES = {"redis": 100_000_000, "dangling": 50_000_000}
IMAGE_ROWS = (
    ("current", ["cuelo-cloud:0d135bf4fd8b"], "2026-10-09T00:10:00.123456789Z"),
    (
        "rollback",
        ["cuelo-cloud-rollback:37904014962-1", "cuelo-cloud:772554adc16e"],
        "2026-10-07T01:00:00Z",
    ),
    (
        "old96",
        ["cuelo:cloud-20261006-sticker", "cuelo-cloud-rollback:37560801155-1"],
        "2026-10-06T00:00:00Z",
    ),
    ("old95", ["cuelo:cloud-20261006-final"], "2026-10-05T00:00:00Z"),
    ("held", ["cuelo:cloud-20261001-held"], "2026-10-01T00:00:00Z"),
    (
        "shared",
        ["cuelo-cloud:aaaaaaaaaaaa", "kasset-trader-core:f1d8bf4b"],
        "2026-10-02T00:00:00Z",
    ),
    ("newer", ["cuelo-cloud:bbbbbbbbbbbb"], "2026-10-08T00:00:00Z"),
    ("manual", ["cuelo-cloud-rollback:manual-backup"], "2026-10-03T00:00:00Z"),
    ("ovr", ["cuelo-cloud:cccccccccccc"], "2026-10-04T00:00:00Z"),
    ("redis", ["redis:7-alpine"], "2026-09-01T00:00:00Z"),
    ("dangling", [], "2026-09-02T00:00:00Z"),
)


def make_state(root_dir: str) -> dict:
    labels = {
        "com.docker.compose.project": "cuelo-cloud",
        "com.docker.compose.service": "cuelo",
    }
    images = {
        IDS[name]: img(tags, created, SIZES.get(name, DEFAULT_SIZE))
        for name, tags, created in IMAGE_ROWS
    }
    return {
        "root_dir": root_dir,
        "compose_images": {
            "1": ["cuelo:cloud-20261006-sticker"],
            "2": ["cuelo-cloud:0d135bf4fd8b"],
        },
        "images": images,
        "containers": [
            container("c", IDS["current"], CURRENT_NAME, "running", labels),
            container("d", IDS["held"], "/held-run", "exited"),
            container("e", IDS["redis"], "/kasset-private-redis", "running"),
        ],
    }


class Result:
    def __init__(
        self,
        proc: subprocess.CompletedProcess,
        calls: list[list[str]],
        state: dict,
    ):
        self.rc = proc.returncode
        self.out = proc.stdout + proc.stderr
        self.calls = calls
        self.state = state

    def rm_calls(self) -> list[list[str]]:
        return [c for c in self.calls if c[:2] == ["image", "rm"]]

    def verdicts(self) -> dict[str, tuple[str, str]]:
        pattern = r"IMAGE (sha256:[0-9a-f]{64}) (PROTECT|CANDIDATE) reasons=(\S+)"
        return {i: (v, r) for i, v, r in re.findall(pattern, self.out)}


class CueloImageCleanupTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="cuelo-cleanup-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        (self.tmp / "bin").mkdir()
        # fake는 격리 모드(-I)의 Python으로 돌려 pytest의 자식 프로세스 가드를 타지 않게 한다.
        script = self.tmp / "fake_docker.py"
        script.write_text(FAKE_DOCKER)
        wrapper = [
            "#!/usr/bin/env bash",
            f'exec "{sys.executable}" -I "{script}" "$@"',
        ]
        fake = self.tmp / "bin" / "docker"
        fake.write_text("\n".join(wrapper) + "\n")
        fake.chmod(0o755)
        self.root = self.tmp / "opt"
        (self.root / "deploy").mkdir(parents=True)
        (self.root / "compose.yaml").write_text("services: {}\n")
        (self.root / ".env").write_text("SECRET_TOKEN=do-not-print-this\n")
        self.set_override("cuelo-cloud:0d135bf4fd8b")
        self.state = make_state(str(self.tmp))

    def set_override(self, ref: str) -> None:
        path = self.root / "deploy" / "compose.image.yaml"
        path.write_text(f"services:\n  cuelo:\n    image: {ref}\n")

    def run_host(self, mode: str, ids=(), apply: str | None = None) -> Result:
        state_file = self.tmp / "state.json"
        log_file = self.tmp / "calls.log"
        state_file.write_text(json.dumps(self.state))
        log_file.write_text("")
        bin_dir = self.tmp / "bin"
        base_path = os.environ["PATH"]
        env = {
            "PATH": f"{bin_dir}{os.pathsep}{base_path}",
            "HOME": str(self.tmp),
            "CUELO_ROOT": str(self.root),
            "FAKE_DOCKER_STATE": str(state_file),
            "FAKE_DOCKER_LOG": str(log_file),
        }
        if ids:
            env["CLEANUP_IMAGE_IDS"] = " ".join(ids)
        if apply is not None:
            env["APPLY_CLEANUP"] = apply
        proc = subprocess.run(
            ["bash", str(HOST_SH), mode],
            capture_output=True,
            text=True,
            env=env,
            cwd=self.tmp,
            check=False,
            timeout=60,
        )
        lines = log_file.read_text().splitlines()
        calls = [json.loads(line) for line in lines if line]
        return Result(proc, calls, json.loads(state_file.read_text()))

    def assert_unchanged(self, result: Result) -> None:
        self.assertEqual(result.state["images"], self.state["images"])
        self.assertEqual(result.state["containers"], self.state["containers"])

    def assert_no_unsafe_docker(self, result: Result, allow_rm: bool = False) -> None:
        # 어떤 시나리오에서도 -f/prune/컨테이너·볼륨 삭제/compose 변경 호출이 없어야 한다.
        for c in result.calls:
            joined = " ".join(c)
            self.assertNotIn("prune", joined)
            self.assertNotIn("volume", joined)
            self.assertNotIn("--force", joined)
            self.assertNotIn(c[0], FORBIDDEN_VERBS, joined)
            if c[:1] == ["compose"]:
                self.assertEqual(c[-2:], ["config", "--images"], joined)
            if c[:2] == ["image", "rm"]:
                self.assertTrue(allow_rm, joined)
                self.assertEqual(len(c), 3, joined)
                self.assertNotIn("-f", c)
        self.assertNotEqual(result.rc, 9, result.out)
        self.assertNotIn("unhandled", result.out)
        self.assertNotIn("do-not-print-this", result.out)
        self.assertNotIn("kasset-private-redis", result.out)

    def test_images_inventory_reports_reasons_without_changes(self) -> None:
        r = self.run_host("images")
        self.assertEqual(r.rc, 0, r.out)
        self.assertEqual(r.rm_calls(), [])
        self.assert_unchanged(r)
        self.assert_no_unsafe_docker(r)
        v = r.verdicts()
        # 비CUELO·dangling 이미지는 목록에 나오지 않는다.
        listed = set(NAMES) - {"redis", "dangling"}
        self.assertEqual(set(v), {IDS[n] for n in listed})
        why = {name: v[IDS[name]][1] for name in listed}
        self.assertEqual(v[IDS["current"]][0], "PROTECT")
        self.assertIn("current", why["current"])
        self.assertIn(
            "rollback-latest:cuelo-cloud-rollback:37904014962-1",
            why["rollback"],
        )
        self.assertIn("container-ref:held-run(exited)", why["held"])
        self.assertIn("foreign-tag:kasset-trader-core:f1d8bf4b", why["shared"])
        self.assertIn("not-older-than-rollback", why["newer"])
        self.assertIn(
            "rollback-unrecognized:cuelo-cloud-rollback:manual-backup",
            why["manual"],
        )
        for name in ("old96", "old95", "ovr"):
            self.assertEqual(
                v[IDS[name]],
                ("CANDIDATE", "unreferenced,older-than-rollback"),
                name,
            )
        # 다중 태그, 컨테이너 참조, 용량 합산 금지 안내, compose 기본 image 참조 안내가 출력에 있다.
        self.assertIn(
            "cuelo-cloud-rollback:37560801155-1 cuelo:cloud-20261006-sticker",
            r.out,
        )
        self.assertIn(
            "compose.yaml 기본 image가 cuelo:cloud-20261006-sticker",
            r.out,
        )
        self.assertIn("합산해도 회수량이 아니다", r.out)
        self.assertRegex(r.out, r"docker system df Images.*Images\|11\|")

    def test_override_image_is_protected(self) -> None:
        self.set_override("cuelo-cloud:cccccccccccc")
        r = self.run_host("images")
        self.assertEqual(r.rc, 0, r.out)
        verdict, why = r.verdicts()[IDS["ovr"]]
        self.assertEqual(verdict, "PROTECT")
        self.assertIn("override-ref:cuelo-cloud:cccccccccccc", why)

    def test_cleanup_defaults_to_dry_run(self) -> None:
        old96, old95 = IDS["old96"], IDS["old95"]
        tags = "cuelo-cloud-rollback:37560801155-1 cuelo:cloud-20261006-sticker"
        plan = f"PLAN {old96}: 태그 전체 {tags}"
        for apply in (None, "false"):
            r = self.run_host("cleanup-images", ids=[old96, old95], apply=apply)
            self.assertEqual(r.rc, 0, r.out)
            self.assertEqual(r.rm_calls(), [])
            self.assert_unchanged(r)
            self.assertIn("DRY-RUN 완료", r.out)
            # 다중 태그 전체가 계획에 보인다.
            self.assertIn(plan, r.out)
            self.assert_no_unsafe_docker(r)

    def test_apply_removes_only_explicit_candidates_tag_by_tag(self) -> None:
        old96, old95 = IDS["old96"], IDS["old95"]
        r = self.run_host("cleanup-images", ids=[old96, old95], apply="true")
        self.assertEqual(r.rc, 0, r.out)
        self.assert_no_unsafe_docker(r, allow_rm=True)
        removed_tags = [c[2] for c in r.rm_calls()]
        expected = [
            "cuelo-cloud-rollback:37560801155-1",
            "cuelo:cloud-20261006-sticker",
            "cuelo:cloud-20261006-final",
        ]
        self.assertEqual(removed_tags, expected)
        gone = set(self.state["images"]) - set(r.state["images"])
        self.assertEqual(gone, {old96, old95})
        # 보호 대상·후보가 아닌 것은 그대로다.
        self.assertEqual(r.state["containers"], self.state["containers"])
        self.assertIn(IDS["ovr"], r.state["images"])
        rollback_before = self.state["images"][IDS["rollback"]]
        self.assertEqual(r.state["images"][IDS["rollback"]], rollback_before)
        # receipt와 전후 저장소 값.
        self.assertIn(
            "삭제 receipt: 삭제 완료 2개, 실패·중단 0개, 미시도 0개",
            r.out,
        )
        self.assertIn(f"RECEIPT removed {old95}", r.out)
        self.assertIn("Untagged: cuelo:cloud-20261006-final", r.out)
        self.assertIn("Deleted: " + old95, r.out)
        self.assertIn("[삭제 전] docker system df Images", r.out)
        self.assertIn("[삭제 후] docker system df Images", r.out)
        self.assertRegex(r.out, r"Docker 저장소 여유 변화: -?\d+KB")

    def test_protected_or_out_of_scope_ids_are_refused_all_or_nothing(self) -> None:
        old95 = IDS["old95"]
        refused = {
            "current": IDS["current"],
            "rollback": IDS["rollback"],
            "container-ref": IDS["held"],
            "shared-foreign-tag": IDS["shared"],
            "newer": IDS["newer"],
            "unrecognized-rollback": IDS["manual"],
            "non-cuelo": IDS["redis"],
            "dangling": IDS["dangling"],
            "absent": sid("absent"),
        }
        for label, bad in refused.items():
            with self.subTest(label):
                # 정상 후보와 같이 요청해도 하나라도 거부되면 아무것도 지우지 않는다.
                r = self.run_host("cleanup-images", ids=[old95, bad], apply="true")
                self.assertNotEqual(r.rc, 0, r.out)
                self.assertEqual(r.rm_calls(), [])
                self.assert_unchanged(r)
                self.assertIn("삭제할 수 없는 것이 있다", r.out)
                self.assert_no_unsafe_docker(r)

    def test_override_ref_is_refused(self) -> None:
        self.set_override("cuelo-cloud:cccccccccccc")
        r = self.run_host("cleanup-images", ids=[IDS["ovr"]], apply="true")
        self.assertNotEqual(r.rc, 0, r.out)
        self.assertEqual(r.rm_calls(), [])
        self.assertIn("override-ref", r.out)

    def test_override_location_that_cannot_be_read_refuses_everything(self) -> None:
        old95 = IDS["old95"]
        msg = "override 이미지 보호 기준을 세울 수 없다"
        (self.root / "deploy" / "compose.image.yaml").write_text("services: {}\n")
        r = self.run_host("cleanup-images", ids=[old95], apply="true")
        self.assertNotEqual(r.rc, 0, r.out)
        self.assertEqual(r.rm_calls(), [])
        self.assertIn(msg, r.out)
        shutil.rmtree(self.root / "deploy")
        r = self.run_host("cleanup-images", ids=[old95], apply="true")
        self.assertNotEqual(r.rc, 0, r.out)
        self.assertEqual(r.rm_calls(), [])
        self.assertIn(msg, r.out)

    def test_invalid_input_stops_before_any_docker_call(self) -> None:
        old95 = IDS["old95"]
        short = old95[:19]
        upper = old95.upper().replace("SHA256", "sha256")
        cases = {
            "empty": ([], "true"),
            "short id": ([short], "true"),
            "tag": (["cuelo:cloud-20261006-final"], "true"),
            "glob": (["sha256:*"], "true"),
            "uppercase": ([upper], "true"),
            "too many": ([sid(f"x{i}") for i in range(21)], "true"),
            "bad apply": ([old95], "yes"),
        }
        for label, (ids, apply) in cases.items():
            with self.subTest(label):
                r = self.run_host("cleanup-images", ids=ids, apply=apply)
                self.assertNotEqual(r.rc, 0, r.out)
                self.assertEqual(r.calls, [], r.out)

    def test_missing_baselines_refuse_everything(self) -> None:
        old95 = IDS["old95"]
        # 현재 cuelo 컨테이너가 없으면 현재 이미지 보호 기준이 없다.
        keep = [c for c in self.state["containers"] if c["name"] != CURRENT_NAME]
        self.state["containers"] = keep
        r = self.run_host("cleanup-images", ids=[old95], apply="true")
        self.assertNotEqual(r.rc, 0, r.out)
        self.assertEqual(r.rm_calls(), [])
        self.assertIn("현재 이미지 보호 기준을 세울 수 없다", r.out)
        self.assertEqual({v for v, _ in r.verdicts().values()}, {"PROTECT"})
        # 직전 복구 태그가 없으면 같은 결과다.
        self.setUp()
        for image in self.state["images"].values():
            tags = image["tags"]
            image["tags"] = [t for t in tags if not t.startswith(ROLLBACK_PREFIX)]
        r = self.run_host("cleanup-images", ids=[old95], apply="true")
        self.assertNotEqual(r.rc, 0, r.out)
        self.assertEqual(r.rm_calls(), [])
        self.assertIn("직전 복구본 보호 기준을 세울 수 없다", r.out)
        r = self.run_host("images")
        self.assertNotEqual(r.rc, 0, r.out)
        self.assertEqual({v for v, _ in r.verdicts().values()}, {"PROTECT"})

    def test_change_between_preflight_and_removal_stops_without_deleting(self) -> None:
        old95, old96 = IDS["old95"], IDS["old96"]
        late_container = container("f", old95, "/late-run", "created")
        late_tag = {"id": old95, "tag": "cuelo-cloud:late"}
        # preflight는 image ls 1번째 호출이고, 삭제 직전 재조회는 2번째 호출이다.
        scenarios = {
            "container appears": {"call": 2, "container": late_container},
            "tag appears": {"call": 2, "add_tag": late_tag},
        }
        for label, inject in scenarios.items():
            with self.subTest(label):
                self.setUp()
                self.state["inject_on_ls"] = inject
                r = self.run_host("cleanup-images", ids=[old95, old96], apply="true")
                self.assertNotEqual(r.rc, 0, r.out)
                self.assertEqual(r.rm_calls(), [])
                self.assertIn(old95, r.state["images"])
                self.assertIn(old96, r.state["images"])
                self.assertIn("RM-STOP", r.out)
                self.assertIn(f"RECEIPT failed {old95}", r.out)
                self.assertIn(f"RECEIPT not-attempted {old96}", r.out)

    def test_rm_failure_stops_and_reports_partial_receipt(self) -> None:
        old96, old95 = IDS["old96"], IDS["old95"]
        first = "cuelo-cloud-rollback:37560801155-1"
        second = "cuelo:cloud-20261006-sticker"
        self.state["rm_fail"] = [second]
        r = self.run_host("cleanup-images", ids=[old96, old95], apply="true")
        self.assertNotEqual(r.rc, 0, r.out)
        self.assert_no_unsafe_docker(r, allow_rm=True)
        # old96의 첫 태그는 지워졌지만 둘째 태그 삭제가 실패해 old95는 시도하지 않는다.
        tried = [c[2] for c in r.rm_calls()]
        self.assertEqual(tried, [first, second])
        self.assertIn(old96, r.state["images"])
        self.assertEqual(r.state["images"][old96]["tags"], [second])
        self.assertIn(old95, r.state["images"])
        self.assertIn("RM-FAIL", r.out)
        self.assertIn(
            "삭제 receipt: 삭제 완료 0개, 실패·중단 1개, 미시도 1개",
            r.out,
        )
        self.assertIn(f"RECEIPT not-attempted {old95}", r.out)


if __name__ == "__main__":
    unittest.main()
