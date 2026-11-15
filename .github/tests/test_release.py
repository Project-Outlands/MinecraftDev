import base64
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch
import xml.etree.ElementTree as ET
import zipfile


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "release.py"
SPEC = importlib.util.spec_from_file_location("release", SCRIPT)
release = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(release)
BRANCHES = ["2025.3", "2026.1", "2026.2", "2026.3"]


class GitFixture(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.origin = self.root / "origin.git"
        self.repo = self.root / "working"
        self.repo.mkdir()
        self.run_git("init", "--bare", str(self.origin), cwd=self.root)
        self.run_git("init", "-b", "dev")
        self.run_git("config", "user.name", "Release Tests")
        self.run_git("config", "user.email", "release-tests@example.invalid")
        self.run_git("config", "core.autocrlf", "false")
        (self.repo / ".github").mkdir()
        (self.repo / ".github/release-branches").write_text("\n".join(BRANCHES) + "\n")
        (self.repo / "gradle.properties").write_text("ideaVersionName = 2025.3\ncoreVersion = 1.9.1\n")
        self.commit("Base")
        for branch in BRANCHES:
            self.run_git("checkout", "-b", branch)
            (self.repo / "gradle.properties").write_text(f"ideaVersionName = {branch}\ncoreVersion = 1.9.1\n")
            (self.repo / f"api-{branch}").write_text(branch)
            self.commit(f"Platform {branch}")
        self.run_git("checkout", "dev")
        (self.repo / "release-code").write_text("New shared release code\n")
        self.commit("Shared change")
        self.source = self.run_git("rev-parse", "HEAD")
        self.run_git("remote", "add", "origin", str(self.origin))
        self.run_git("push", "origin", "dev", *BRANCHES)
        self.run_git("fetch", "origin")

    def run_git(self, *args, cwd=None):
        result = subprocess.run(["git", *args], cwd=cwd or self.repo, encoding="utf-8", text=True,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result.stdout.strip()

    def commit(self, message):
        self.run_git("add", ".")
        self.run_git("commit", "-m", message)

    @contextlib.contextmanager
    def working(self, directory=None):
        previous = Path.cwd()
        os.chdir(directory or self.repo)
        try:
            yield
        finally:
            os.chdir(previous)

    def plan(self, dry_run="false", destination="release-plan"):
        with self.working():
            return release.prepare("1.9.1", dry_run, destination)

    def packages(self, plan):
        for entry in plan["entries"]:
            self.run_git("checkout", "--detach", entry["commit"])
            branch = entry["branch"]
            metadata = dict(id=release.PLUGIN_ID, name="Minecraft & Development", ideaLine=branch,
                            coreVersion="1.9.1", version=f"{branch}-1.9.1", sinceBuild=str(release.baseline(branch)),
                            untilBuild=f"{release.baseline(branch)}.*", jdk=21 if branch in BRANCHES[:2] else 25,
                            archive="built.zip")
            build = self.repo / "build"
            (build / "distributions").mkdir(parents=True, exist_ok=True)
            descriptor = ET.Element("idea-plugin")
            for key in ("id", "name", "version"):
                ET.SubElement(descriptor, key).text = metadata[key]
            ET.SubElement(descriptor, "idea-version", {"since-build": metadata["sinceBuild"], "until-build": metadata["untilBuild"]})
            jar_bytes = io.BytesIO()
            with zipfile.ZipFile(jar_bytes, "w") as jar:
                jar.writestr("META-INF/plugin.xml", release.xml_bytes(descriptor))
            with zipfile.ZipFile(build / "distributions/built.zip", "w") as distribution:
                distribution.writestr("plugin/lib/plugin.jar", jar_bytes.getvalue())
            namespace = "http://maven.apache.org/POM/4.0.0"
            pom = ET.Element(f"{{{namespace}}}project")
            for key, value in (("groupId", release.GROUP_ID), ("artifactId", release.ARTIFACT_ID),
                               ("version", metadata["version"]), ("packaging", "zip")):
                ET.SubElement(pom, f"{{{namespace}}}{key}").text = value
            pom_path = build / "publications/pluginZip/pom-default.xml"
            pom_path.parent.mkdir(parents=True, exist_ok=True)
            pom_path.write_bytes(release.xml_bytes(pom))
            release.write_json(build / "outlands/release.json", metadata)
            with self.working():
                release.package("release-plan", branch, "build/outlands/release.json", "release-packages")
        return self.repo / "release-packages"


class PlanningTests(GitFixture):
    def test_explicit_sha_merges_are_deterministic_and_bundle_restores_every_line(self):
        first = self.plan()
        second = self.plan(destination="second-plan")
        self.assertEqual(first["entries"], second["entries"])
        self.assertEqual([entry["branch"] for entry in first["entries"]], BRANCHES)
        previous = self.source
        for entry in first["entries"]:
            self.run_git("merge-base", "--is-ancestor", previous, entry["commit"])
            previous = entry["commit"]
        restored = self.root / "restored"
        self.run_git("clone", "--branch", "dev", str(self.origin), str(restored), cwd=self.root)
        with self.working(restored):
            for entry in first["entries"]:
                release.restore(self.repo / "release-plan", entry["branch"])
                self.assertEqual(release.git("rev-parse", "HEAD"), entry["commit"])

    def test_missing_configured_branch_fails_instead_of_partial_release(self):
        self.run_git("update-ref", "-d", "refs/remotes/origin/2026.2")
        with self.assertRaisesRegex(ValueError, "Configured release branch is missing"):
            self.plan()
        self.assertEqual(self.run_git("rev-parse", "HEAD"), self.source)

    def test_versions_and_branch_order_are_checked(self):
        with self.working(), self.assertRaisesRegex(ValueError, "committed coreVersion"):
            release.prepare("1.9.2", "true", "release-plan")
        listing = self.root / "branches"
        listing.write_text("2026.2\n2025.3\n")
        with self.assertRaisesRegex(ValueError, "ascending"):
            release.read_branches(listing)
        listing.write_text("2026.1\n2026.1\n")
        with self.assertRaisesRegex(ValueError, "unique"):
            release.read_branches(listing)

    def test_finalize_is_atomic_and_retry_uses_the_same_tagged_commits(self):
        plan = self.plan()
        with self.working():
            release.finalize("release-plan")
            release.finalize("release-plan")
        for entry in plan["entries"]:
            self.assertEqual(self.run_git("ls-remote", "origin", f"refs/heads/{entry['branch']}").split()[0], entry["commit"])
            self.assertEqual(self.run_git("rev-parse", f"{entry['tag']}^{{commit}}"), entry["commit"])
        self.run_git("checkout", "--detach", self.source)
        retried = self.plan(destination="retried-plan")
        self.assertEqual([entry["commit"] for entry in plan["entries"]], [entry["commit"] for entry in retried["entries"]])

    def test_moved_branch_prevents_any_finalize_push(self):
        plan = self.plan()
        original = plan["entries"][0]["original"]
        tree = self.run_git("rev-parse", f"{original}^{{tree}}")
        moved = self.run_git("commit-tree", tree, "-p", original, "-m", "Concurrent work")
        self.run_git("push", "origin", f"{moved}:refs/heads/2025.3")
        with self.working(), self.assertRaisesRegex(ValueError, "moved during publication"):
            release.finalize("release-plan")
        self.assertFalse(self.run_git("ls-remote", "origin", "refs/tags/1.9.1-*"))


class RepositoryHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        self.server.get_auth.append(self.headers.get("Authorization"))
        data = self.server.files.get(self.path)
        self.send_response(404 if data is None else 200)
        self.end_headers()
        if data is not None:
            self.wfile.write(data)

    def do_PUT(self):
        expected = "Basic " + base64.b64encode(b"test-user:test-password").decode()
        if self.headers.get("Authorization") != expected:
            self.send_response(401)
            self.end_headers()
            return
        if self.headers.get("If-None-Match") == "*" and self.path in self.server.files:
            self.send_response(412)
            self.end_headers()
            return
        self.server.files[self.path] = self.rfile.read(int(self.headers["Content-Length"]))
        self.server.puts.append(self.path)
        self.send_response(201)
        self.end_headers()


@contextlib.contextmanager
def repository_server():
    server = ThreadingHTTPServer(("127.0.0.1", 0), RepositoryHandler)
    server.files = {}
    server.puts = []
    server.get_auth = []
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        repository = release.MavenRepository(f"http://127.0.0.1:{server.server_port}", "test-user", "test-password")
        yield repository, server
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


class PublicationTests(GitFixture):
    def test_four_packages_publish_promote_aggregate_and_retry_without_reupload(self):
        plan = self.plan()
        packages = self.packages(plan)
        with self.working(), repository_server() as (repository, server):
            release.publish("release-plan", BRANCHES[0], packages, repository)
            before = len(server.puts)
            with self.assertRaisesRegex(ValueError, "incomplete"):
                release.promote("release-plan", packages, repository)
            self.assertEqual(before, len(server.puts))
            for branch in BRANCHES:
                release.publish("release-plan", branch, packages, repository)
            uploaded = len(server.puts)
            for branch in BRANCHES:
                release.publish("release-plan", branch, packages, repository)
            self.assertEqual(uploaded, len(server.puts))
            release.promote("release-plan", packages, repository)
            manifest = release.aggregate("release-plan", packages, "release-assets")
            self.assertEqual(len(manifest["builds"]), 4)
            self.assertEqual(len(list(Path("release-assets").glob("*.zip"))), 4)
            for entry in plan["entries"]:
                url = f"/{release.DIRECTORY}/updatePlugins-{release.baseline(entry['branch'])}.xml"
                plugin = ET.fromstring(server.files[url]).find("plugin")
                self.assertEqual(plugin.get("version"), f"{entry['branch']}-1.9.1")
                self.assertIn("&amp;", server.files[url].decode())
            self.assertTrue(all(auth is None for auth in server.get_auth))
            metadata = ET.fromstring(server.files[f"/{release.DIRECTORY}/maven-metadata.xml"])
            self.assertEqual(len(metadata.findall("versioning/versions/version")), 4)

    def test_zip_checksum_tampering_fails_before_upload(self):
        plan = self.plan()
        packages = self.packages(plan)
        archive = next((packages / BRANCHES[0]).glob("*.zip"))
        archive.write_bytes(b"corrupted")
        with self.working(), repository_server() as (repository, server):
            with self.assertRaisesRegex(ValueError, "Checksum mismatch"):
                release.publish("release-plan", BRANCHES[0], packages, repository)
            self.assertEqual(server.puts, [])

    def test_immutable_conflict_and_partial_upload_recovery(self):
        with repository_server() as (repository, server):
            server.files["/existing"] = b"old"
            with self.assertRaisesRegex(ValueError, "Refusing to overwrite"):
                repository.publish({"new": b"new", "existing": b"different"})
            self.assertEqual(server.puts, [])
            repository.publish({"new": b"new", "existing": b"old"})
            self.assertEqual(server.puts, ["/new"])
            repository.publish({"new": b"new", "existing": b"old"})
            self.assertEqual(server.puts, ["/new"])

    def test_dry_run_cannot_upload_promote_release_or_push(self):
        self.plan(dry_run="true")
        with self.working(), repository_server() as (repository, server):
            actions = [lambda: release.publish("release-plan", BRANCHES[0], "missing", repository),
                       lambda: release.promote("release-plan", "missing", repository),
                       lambda: release.github_release("release-plan", "missing"),
                       lambda: release.finalize("release-plan")]
            for action in actions:
                with self.assertRaisesRegex(ValueError, "dry-run"):
                    action()
            self.assertEqual(server.puts, [])
            self.assertFalse(self.run_git("ls-remote", "origin", "refs/tags/1.9.1-*"))

    def test_wrong_branch_baseline_and_version_suffix_are_rejected(self):
        plan = self.plan()
        packages = self.packages(plan)
        metadata = release.read_json(packages / BRANCHES[0] / "release.json")
        metadata["untilBuild"] = "263.*"
        with self.assertRaisesRegex(ValueError, "until-build"):
            release.validate_metadata(metadata, BRANCHES[0], "1.9.1")
        metadata["untilBuild"] = "253.*"
        metadata["version"] += "-rc.1"
        with self.assertRaisesRegex(ValueError, "version mismatch"):
            release.validate_metadata(metadata, BRANCHES[0], "1.9.1")


class GitHubReleaseTests(GitFixture):
    def fake_gh(self, state):
        original_command = release.command

        def run(*args, **kwargs):
            if args[0] != "gh":
                return original_command(*args, **kwargs)
            action = args[2]
            result = subprocess.CompletedProcess(args, 0, "", "")
            if action == "view":
                if not state["exists"]:
                    return subprocess.CompletedProcess(args, 1, "", "release not found")
                result.stdout = json.dumps(dict(isDraft=state["draft"], assets=[dict(name=name) for name in state["files"]]))
            elif action == "create":
                self.assertEqual(args[3], "v1.9.1")
                state["creates"] += 1
                state["exists"] = True
                state["draft"] = True
            elif action == "download":
                name = args[args.index("--pattern") + 1]
                directory = Path(args[args.index("--dir") + 1])
                (directory / name).write_bytes(state["files"][name])
            elif action == "upload":
                if state.get("fail_once") and len(state["files"]) == 2:
                    state["fail_once"] = False
                    raise RuntimeError("Simulated upload interruption")
                path = Path(args[4])
                state["files"][path.name] = path.read_bytes()
                state["uploads"] += 1
            elif action == "edit":
                self.assertEqual(len([name for name in state["files"] if name.endswith(".zip")]), 4)
                state["draft"] = False
            else:
                self.fail(f"Unexpected GitHub CLI action: {action}")
            return result

        return run

    def test_one_release_with_four_ide_assets_resumes_after_partial_upload(self):
        plan = self.plan()
        packages = self.packages(plan)
        state = dict(exists=False, draft=True, creates=0, uploads=0, files={}, fail_once=True)
        with self.working():
            release.aggregate("release-plan", packages, "release-assets")
            with patch.object(release, "command", side_effect=self.fake_gh(state)):
                with self.assertRaisesRegex(RuntimeError, "interruption"):
                    release.github_release("release-plan", "release-assets")
                self.assertTrue(state["draft"])
                release.github_release("release-plan", "release-assets")
                release.github_release("release-plan", "release-assets")
        self.assertEqual(state["creates"], 1)
        self.assertEqual(state["uploads"], 6)
        self.assertEqual(len(state["files"]), 6)
        self.assertFalse(state["draft"])

    def test_existing_release_asset_conflict_is_rejected_before_more_uploads(self):
        plan = self.plan()
        packages = self.packages(plan)
        state = dict(exists=True, draft=True, creates=0, uploads=0,
                     files={"minecraft-dev-2025.3-1.9.1.zip": b"wrong"})
        with self.working():
            release.aggregate("release-plan", packages, "release-assets")
            with patch.object(release, "command", side_effect=self.fake_gh(state)):
                with self.assertRaisesRegex(ValueError, "Existing GitHub Release asset differs"):
                    release.github_release("release-plan", "release-assets")
        self.assertEqual(state["uploads"], 0)

    def test_existing_tag_cannot_be_retargeted(self):
        self.run_git("tag", "v1.9.1", "2025.3")
        self.run_git("push", "origin", "refs/tags/v1.9.1")
        with self.assertRaisesRegex(ValueError, "different source commit"):
            self.plan()

    def test_dry_run_can_rehearse_a_feature_branch_without_reusing_old_tags(self):
        self.run_git("tag", "1.9.1-2025.3", "2025.3")
        (self.repo / "feature-change").write_text("New workflow\n")
        self.commit("Feature branch workflow")
        plan = self.plan(dry_run="true")
        self.assertTrue(plan["dryRun"])
        self.assertEqual(len(plan["entries"]), 4)


if __name__ == "__main__":
    unittest.main()
