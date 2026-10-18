"""Restore must never widen ACLs: files placed into narrow private folders stay narrow. Windows only.
All writes stay in a TemporaryDirectory; no names are resolved (SIDs only)."""
from __future__ import annotations

import os
import sqlite3
import subprocess
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

import snapshots
from snapshots import SnapshotError, SnapshotManager

BROAD = {"S-1-5-32-545", "S-1-5-11", "S-1-1-0"}


def me() -> str:
    out = subprocess.run(["whoami", "/user", "/fo", "csv", "/nh"], capture_output=True, text=True).stdout
    return out.strip().split(",")[-1].strip('"')


@unittest.skipUnless(os.name == "nt", "Windows ACL behaviour")
class RestoreAclTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="restore-acl-")
        self.root = Path(self.temp.name) / "恢复 ACL"
        self.root.mkdir()
        user = me()
        self.wide = f"D:P(A;OICI;FA;;;SY)(A;OICI;FA;;;BA)(A;OICI;FA;;;{user})(A;OICI;0x1200a9;;;BU)(A;OICI;0x1301bf;;;AU)"
        self.narrow = f"D:P(A;OICI;FA;;;SY)(A;OICI;FA;;;BA)(A;OICI;FA;;;{user})"
        snapshots._set_dacl(self.root, self.wide)  # production root: Users read, Authenticated Users modify
        for name in ("memory", "dreams", ".lounge", "state", "attachments", "snapshots"):
            (self.root / name).mkdir()
            snapshots._set_dacl(self.root / name, self.narrow)
        (self.root / ".games" / "minigames").mkdir(parents=True)  # stays wide, like production .games
        self.write("memory/life/daily/2026-10-01_a__gpt__agent.md", "---\nid: a\n---\nalpha")
        self.write("memory/life/daily/2026-10-01_b__gpt__agent.md", "---\nid: b\n---\nbeta")
        self.write("dreams/gpt/2026-10/2026-10-01.md", "dream")
        self.write(".lounge/messages.jsonl", '{"seq": 1}\n')
        self.write("state/now.md", "now")
        self.write("attachments/00e3715c128dad831419a06dc8437f68.json", "{}")
        self.write(".games/minigames/gomoku.json", '{"board": []}')
        self.write("owner-config.json", '{"owners": {}}')
        snapshots._set_dacl(self.root / "owner-config.json", f"D:P(A;;FA;;;SY)(A;;FA;;;BA)(A;;FA;;;{user})")
        with closing(sqlite3.connect(self.root / "state" / "execution-receipts.sqlite3")) as db, db:
            db.execute("CREATE TABLE t (v TEXT)")
            db.execute("INSERT INTO t VALUES ('one')")
        self.manager = SnapshotManager(self.root)
        self.snap = self.manager.create(label="acl")["snapshot_id"]

    def tearDown(self) -> None:
        self.temp.cleanup()

    def write(self, rel: str, text: str) -> None:
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    def private_files(self) -> dict[str, str]:
        out = {}
        for top in ("memory", "dreams", ".lounge", "state", "attachments"):
            for p in (self.root / top).rglob("*"):
                if p.is_file() and not p.name.endswith(("-wal", "-shm", "-journal")) \
                        and p.name != "snapshot-restore-tokens.json":  # written by issuing a token, not by restore
                    out[p.relative_to(self.root).as_posix()] = snapshots._dacl_sddl(p)
        out["owner-config.json"] = snapshots._dacl_sddl(self.root / "owner-config.json")
        return out

    def assert_narrow(self, path: Path) -> None:
        self.assertFalse(snapshots._granted_sids(path) & BROAD, f"broad ACE on {path.relative_to(self.root)}")

    def mutate(self) -> None:
        self.write("memory/life/daily/2026-10-01_a__gpt__agent.md", "---\nid: a\n---\nchanged")   # overwrite
        (self.root / "memory/life/daily/2026-10-01_b__gpt__agent.md").unlink()                   # add back
        self.write("memory/life/daily/2026-10-02_c__gpt__agent.md", "---\nid: c\n---\nnew")       # remove
        for p in sorted((self.root / "dreams" / "gpt").rglob("*"), reverse=True):                 # new folder
            p.unlink() if p.is_file() else p.rmdir()
        self.write("owner-config.json", '{"owners": {"gpt": {}}}')                                # protected file
        self.write(".games/minigames/gomoku.json", '{"board": [1]}')                               # wide folder
        with closing(sqlite3.connect(self.root / "state" / "execution-receipts.sqlite3")) as db, db:
            db.execute("INSERT INTO t VALUES ('two')")

    def restore(self) -> dict:
        token = self.manager.issue_restore_token(self.snap)["restore_token"]
        return self.manager.restore(self.snap, confirm=token)

    # 1 + 3: narrow targets stay narrow, new files and folders included; owner-config stays protected
    def test_restore_into_narrow_folders_under_wide_root(self) -> None:
        dir_acl = {n: snapshots._dacl_sddl(self.root / n) for n in ("memory", "dreams", ".lounge", "state", "snapshots")}
        owner_acl = snapshots._explicit_dacl(snapshots._dacl_sddl(self.root / "owner-config.json"))
        self.mutate()
        self.assertTrue(self.restore()["ok"])
        for rel in self.private_files():
            self.assert_narrow(self.root / rel)
        self.assert_narrow(self.root / "dreams" / "gpt" / "2026-10")
        self.assertEqual({n: snapshots._dacl_sddl(self.root / n) for n in dir_acl}, dir_acl)
        self.assertEqual(snapshots._explicit_dacl(snapshots._dacl_sddl(self.root / "owner-config.json")), owner_acl)
        self.assertIn("alpha", (self.root / "memory/life/daily/2026-10-01_a__gpt__agent.md").read_text(encoding="utf-8"))
        self.assertTrue((self.root / "memory/life/daily/2026-10-01_b__gpt__agent.md").exists())
        self.assertFalse((self.root / "memory/life/daily/2026-10-02_c__gpt__agent.md").exists())
        with closing(sqlite3.connect(self.root / "state" / "execution-receipts.sqlite3")) as db:
            self.assertEqual(db.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            self.assertEqual(db.execute("SELECT count(*) FROM t").fetchone()[0], 1)
        self.assertEqual(self.root.joinpath(".games/minigames/gomoku.json").read_text(encoding="utf-8"), '{"board": []}')

    # 2: staging and rollback are protected while they exist
    def test_staging_and_rollback_are_protected_from_creation(self) -> None:
        self.mutate()
        seen = {}
        real = os.replace

        def spy(src, dst):
            s, d = str(src), str(dst)
            if ".restore-staging-" in s and "staging" not in seen:
                staging_root = Path(s.split(".restore-staging-")[0]) / (".restore-staging-" + s.split(".restore-staging-")[1].split(os.sep)[0])
                seen["staging"] = snapshots._granted_sids(staging_root) | snapshots._inheritable_sids(staging_root)
                seen["staged_file"] = snapshots._granted_sids(Path(s))
            if ".restore-rollback-" in d and "rollback" not in seen:
                rb_root = Path(d.split(".restore-rollback-")[0]) / (".restore-rollback-" + d.split(".restore-rollback-")[1].split(os.sep)[0])
                seen["rollback"] = snapshots._granted_sids(rb_root) | snapshots._inheritable_sids(rb_root)
            return real(src, dst)

        with patch.object(snapshots.os, "replace", side_effect=spy):
            self.assertTrue(self.restore()["ok"])
        self.assertEqual(set(seen), {"staging", "staged_file", "rollback"})
        for key, sids in seen.items():
            self.assertFalse(sids & BROAD, key)

    # 4: a failure mid-swap rolls back and leaves every original ACL untouched
    def test_rollback_keeps_original_acls(self) -> None:
        self.mutate()
        before = {rel: (sddl, (self.root / rel).read_bytes()) for rel, sddl in self.private_files().items()}
        token = self.manager.issue_restore_token(self.snap)["restore_token"]
        real, calls = os.replace, {"n": 0}

        def flaky(src, dst):
            if ".restore-staging-" in str(src):
                calls["n"] += 1
                if calls["n"] == 2:
                    raise PermissionError("file in use")
            return real(src, dst)

        with patch.object(snapshots.os, "replace", side_effect=flaky):
            with self.assertRaises(SnapshotError):
                self.manager.restore(self.snap, confirm=token)
        after = {rel: (sddl, (self.root / rel).read_bytes()) for rel, sddl in self.private_files().items()}
        self.assertEqual(after, before)
        self.assertFalse(any(p.name.startswith(".restore-") for p in self.root.iterdir()))

    # 5: if moving files back also fails, the kept copies stay protected and recoverable
    def test_failed_rollback_keeps_protected_copies(self) -> None:
        self.mutate()
        original = {rel: snapshots._dacl_sddl(self.root / rel) for rel in self.private_files()}
        original[".games/minigames/gomoku.json"] = snapshots._dacl_sddl(self.root / ".games/minigames/gomoku.json")
        token = self.manager.issue_restore_token(self.snap)["restore_token"]
        real, calls = os.replace, {"staged": 0, "back": 0}

        def flaky(src, dst):
            if ".restore-staging-" in str(src):
                calls["staged"] += 1
                if calls["staged"] == 2:
                    raise PermissionError("file in use")
            if ".restore-rollback-" in str(src):
                calls["back"] += 1
                if calls["back"] == 1:
                    raise PermissionError("still locked")
            return real(src, dst)

        with patch.object(snapshots.os, "replace", side_effect=flaky):
            with self.assertRaises(SnapshotError):
                self.manager.restore(self.snap, confirm=token)
        kept = [p for p in self.root.iterdir() if p.name.startswith(".restore-rollback-")]
        self.assertEqual(len(kept), 1)
        self.assertFalse((snapshots._granted_sids(kept[0]) | snapshots._inheritable_sids(kept[0])) & BROAD)
        files = [p for p in kept[0].rglob("*") if p.is_file()]
        self.assertTrue(files)
        for p in files:  # each kept copy still carries exactly the ACL it had in production
            rel = p.relative_to(kept[0]).as_posix()
            self.assertEqual(snapshots._dacl_sddl(p), original[rel], rel)
            if not rel.startswith(".games/"):
                self.assert_narrow(p)

    # 6: snapshot data files with broad ACLs (e.g. made before hardening) do not leak their ACL
    def test_broad_acl_inside_old_snapshot_does_not_pollute(self) -> None:
        data = self.root / "snapshots" / self.snap / "data"
        for p in data.rglob("*"):
            if p.is_file():
                snapshots._set_dacl(p, "D:P(A;;FA;;;SY)(A;;FA;;;BA)(A;;FR;;;BU)(A;;FR;;;WD)")
        self.mutate()
        self.assertTrue(self.restore()["ok"])
        for rel in self.private_files():
            self.assert_narrow(self.root / rel)

    # refusal: if staging could not be protected, nothing is replaced
    def test_refuses_when_staging_would_be_broad(self) -> None:
        self.mutate()
        before = {rel: (self.root / rel).read_bytes() for rel in self.private_files()}
        broken = lambda template_dir: self.wide  # noqa: E731  simulate a misconfigured template
        with patch.object(snapshots, "_protected_template", side_effect=broken):
            with self.assertRaises(SnapshotError) as ctx:
                self.restore()
        self.assertIn("Users", str(ctx.exception))
        self.assertEqual({rel: (self.root / rel).read_bytes() for rel in self.private_files()}, before)
        self.assertFalse(any(p.name.startswith(".restore-") for p in self.root.iterdir()))

    # refusal after placing: a file that would end up broader than its destination is rolled back
    def test_refuses_when_placed_file_would_be_broader(self) -> None:
        self.mutate()
        before = {rel: (self.root / rel).read_bytes() for rel in self.private_files()}
        # Simulate a faulty re-apply step that hands placed files a broad ACL: staging is fine, so only the
        # post-placement check can catch it, and it must roll back.
        real_set = snapshots._set_dacl
        with patch.object(snapshots, "_set_dacl", side_effect=lambda path, sddl: real_set(path, self.wide)):
            with self.assertRaises(SnapshotError) as ctx:
                self.restore()
        self.assertIn("rolled back", str(ctx.exception))
        self.assertRegex(str(ctx.exception), "Users|Authenticated Users")
        self.assertEqual({rel: (self.root / rel).read_bytes() for rel in self.private_files()}, before)
        self.assertFalse(any(p.name.startswith(".restore-") for p in self.root.iterdir()))

    # 8: token stays one-time
    def test_token_is_one_time(self) -> None:
        token = self.manager.issue_restore_token(self.snap)["restore_token"]
        self.assertTrue(self.manager.restore(self.snap, confirm=token)["ok"])
        with self.assertRaises(SnapshotError):
            self.manager.restore(self.snap, confirm=token)

    # platform branch: with the Windows guard switched off the restore still works
    def test_non_windows_branch_is_a_noop(self) -> None:
        self.mutate()
        with patch.object(snapshots, "_WINDOWS", False):
            self.assertEqual(snapshots._dacl_sddl(self.root), "")
            self.assertEqual(snapshots._granted_sids(self.root), set())
            self.assertTrue(self.restore()["ok"])
        self.assertIn("alpha", (self.root / "memory/life/daily/2026-10-01_a__gpt__agent.md").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
