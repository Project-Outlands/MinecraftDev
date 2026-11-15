#!/usr/bin/env python3

import argparse
import base64
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
import zipfile
from datetime import datetime, timezone
from pathlib import Path


PLUGIN_ID = "top.outlands.minecraft-dev"
ARTIFACT_ID = "minecraft-dev"
GROUP_ID = "top.outlands"
REPOSITORY_URL = "https://maven.outlands.top/releases"
DIRECTORY = "top/outlands/minecraft-dev"
CORE_PATTERN = r"[0-9]+\.[0-9]+\.[0-9]+"
LINE_PATTERN = r"20[0-9]{2}\.[1-3]"
BOT = ["-c", "user.name=github-actions[bot]", "-c",
       "user.email=41898282+github-actions[bot]@users.noreply.github.com", "-c", "commit.gpgSign=false"]


def require(condition, message):
    if not condition:
        raise ValueError(message)


def command(*args, cwd=None, env=None, check=True):
    result = subprocess.run(args, cwd=cwd, env=env, text=True, encoding="utf-8",
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if check and result.returncode:
        raise RuntimeError(f"Command failed: {' '.join(args)}\n{result.stdout}{result.stderr}")
    return result


def git(*args, **kwargs):
    return command("git", *args, **kwargs).stdout.strip()


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def output(**values):
    path = os.environ.get("GITHUB_OUTPUT")
    if path:
        with open(path, "a", encoding="utf-8") as stream:
            for key, value in values.items():
                stream.write(f"{key}={value}\n")


def properties(text):
    return dict(re.findall(r"^\s*([A-Za-z][A-Za-z0-9]*)\s*=\s*(.*?)\s*$", text, re.MULTILINE))


def read_branches(path):
    branches = [line.split("#", 1)[0].strip() for line in Path(path).read_text(encoding="utf-8").splitlines()]
    branches = [line for line in branches if line]
    require(branches and all(re.fullmatch(LINE_PATTERN, line) for line in branches), "Invalid release branch list")
    require(branches == sorted(set(branches), key=lambda line: tuple(map(int, line.split(".")))),
            "Release branches must be unique and in ascending IDE order")
    return branches


def baseline(line):
    require(re.fullmatch(LINE_PATTERN, line), f"Invalid IDE line: {line}")
    year, release = map(int, line.split("."))
    return (year % 100) * 10 + release


def ancestor(source, target):
    return command("git", "merge-base", "--is-ancestor", source, target, check=False).returncode == 0


def remote_commit(ref):
    refs = {}
    for line in git("ls-remote", "origin", ref, f"{ref}^{{}}").splitlines():
        commit, name = line.split()
        refs[name] = commit
    return refs.get(f"{ref}^{{}}", refs.get(ref))


def prepare(version, dry_run, destination):
    require(re.fullmatch(CORE_PATTERN, version), f"Invalid core version: {version}")
    require(dry_run in ("true", "false"), "dry-run must be true or false")
    require(not git("status", "--porcelain", "--untracked-files=no"), "Tracked working tree must be clean")
    source = git("rev-parse", "HEAD^{commit}")
    if dry_run == "false":
        require(ancestor(source, "refs/remotes/origin/dev"), "Release source must belong to origin/dev")
        tag_source = remote_commit(f"refs/tags/v{version}")
        require(tag_source in (None, source), "The release tag points at a different source commit")
    require(properties(Path("gradle.properties").read_text(encoding="utf-8")).get("coreVersion") == version,
            "Release version must match the committed coreVersion")
    branches = read_branches(".github/release-branches")
    destination = Path(destination).resolve()
    require(not destination.exists() or not any(destination.iterdir()), "Release plan destination must be empty")
    destination.mkdir(parents=True, exist_ok=True)
    date = git("show", "-s", "--format=%cI", source)
    merge_env = dict(os.environ, GIT_AUTHOR_DATE=date, GIT_COMMITTER_DATE=date, GIT_MERGE_AUTOEDIT="no")
    entries = []
    previous = source
    refs = ["refs/outlands/release/source"]
    git("update-ref", refs[0], source)
    try:
        for branch in branches:
            remote = f"refs/remotes/origin/{branch}"
            require(command("git", "show-ref", "--verify", "--quiet", remote, check=False).returncode == 0,
                    f"Configured release branch is missing: origin/{branch}")
            original = git("rev-parse", remote)
            tag = f"{version}-{branch}"
            tag_exists = command("git", "show-ref", "--verify", "--quiet", f"refs/tags/{tag}", check=False).returncode == 0
            if tag_exists and dry_run == "false":
                commit = git("rev-parse", f"refs/tags/{tag}^{{commit}}")
                message = git("for-each-ref", "--format=%(contents)", f"refs/tags/{tag}")
                require(f"source={source}" in message.splitlines(), f"Tag {tag} belongs to a different release plan")
                require(ancestor(previous, commit) and ancestor(commit, original), f"Invalid existing release tag: {tag}")
                git("checkout", "--detach", commit)
            else:
                git("checkout", "--detach", original)
                git(*BOT, "merge", "--no-edit", "--no-ff", previous, env=merge_env)
                commit = git("rev-parse", "HEAD")
            actual = properties(Path("gradle.properties").read_text(encoding="utf-8"))
            require(actual.get("coreVersion") == version, f"Wrong coreVersion after merging {branch}")
            require(actual.get("ideaVersionName") == branch, f"Wrong ideaVersionName after merging {branch}")
            require(ancestor(previous, commit), f"Broken forward-merge chain at {branch}")
            ref = f"refs/outlands/release/{branch}"
            git("update-ref", ref, commit)
            refs.append(ref)
            entries.append(dict(branch=branch, original=original, commit=commit, ref=ref, tag=tag))
            previous = commit
        plan = dict(schema=1, version=version, tag=f"v{version}", source=source, date=date,
                    dryRun=dry_run == "true", entries=entries)
        write_json(destination / "plan.json", plan)
        git("bundle", "create", str(destination / "merged.bundle"), *refs)
        git("bundle", "verify", str(destination / "merged.bundle"))
    finally:
        if command("git", "rev-parse", "--verify", "MERGE_HEAD", check=False).returncode == 0:
            git("merge", "--abort")
        git("checkout", "--detach", source)
    output(matrix=json.dumps({"include": entries}, separators=(",", ":")), version=version, source=source,
           tag=f"v{version}", dry_run=dry_run, branches=" ".join(branches))
    print(f"Planned {version}: {' '.join(branches)} (dry-run={dry_run})")
    return plan


def load_plan(directory):
    plan = read_json(Path(directory) / "plan.json")
    require(plan.get("schema") == 1 and re.fullmatch(CORE_PATTERN, plan["version"]), "Invalid release plan")
    require(re.fullmatch(r"[0-9a-f]{40}", plan["source"]), "Invalid source commit")
    entries = plan["entries"]
    branches = [entry["branch"] for entry in entries]
    require(branches and branches == sorted(set(branches), key=lambda line: baseline(line)), "Invalid plan branch order")
    for entry in entries:
        require(all(re.fullmatch(r"[0-9a-f]{40}", entry[key]) for key in ("commit", "original")), "Invalid plan commit")
        require(entry["ref"] == f"refs/outlands/release/{entry['branch']}", "Invalid bundle ref")
        require(entry["tag"] == f"{plan['version']}-{entry['branch']}", "Invalid per-line tag")
    return plan


def restore(directory, branch):
    plan = load_plan(directory)
    bundle = str((Path(directory) / "merged.bundle").resolve())
    git("bundle", "verify", bundle)
    git("fetch", "--no-tags", bundle, "+refs/outlands/release/*:refs/outlands/release/*")
    require(git("rev-parse", "refs/outlands/release/source") == plan["source"], "Bundle source mismatch")
    for entry in plan["entries"]:
        require(git("rev-parse", entry["ref"]) == entry["commit"], "Bundle commit mismatch")
    if branch:
        entry = next((entry for entry in plan["entries"] if entry["branch"] == branch), None)
        require(entry is not None, f"Branch {branch} is not in the plan")
        git("checkout", "--detach", entry["commit"])
        git("submodule", "update", "--init", "--recursive")
    return plan


def validate_metadata(metadata, branch, version):
    require(metadata["id"] == PLUGIN_ID, "Unexpected plugin id")
    require(metadata["ideaLine"] == branch, "Metadata IDE line mismatch")
    require(metadata["coreVersion"] == version and metadata["version"] == f"{branch}-{version}", "Metadata version mismatch")
    require(metadata["sinceBuild"].split(".")[0] == str(baseline(branch)), "Metadata baseline mismatch")
    require(metadata["untilBuild"] == f"{baseline(branch)}.*", "Metadata until-build mismatch")
    require(isinstance(metadata["jdk"], int) and metadata["jdk"] >= 17, "Invalid toolchain version")
    require(Path(metadata["archive"]).name == metadata["archive"], "Invalid archive name")


def plugin_descriptor(archive):
    descriptors = []
    with zipfile.ZipFile(archive) as distribution:
        for name in distribution.namelist():
            if name.endswith(".jar"):
                with zipfile.ZipFile(io.BytesIO(distribution.read(name))) as jar:
                    if "META-INF/plugin.xml" in jar.namelist():
                        descriptors.append(ET.fromstring(jar.read("META-INF/plugin.xml")))
    require(len(descriptors) == 1, "Expected exactly one packaged plugin descriptor")
    return descriptors[0]


def sha256(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def xml_bytes(element):
    ET.indent(element)
    return ET.tostring(element, encoding="utf-8", xml_declaration=True) + b"\n"


def package(directory, branch, metadata_path, destination):
    plan = load_plan(directory)
    entry = next(entry for entry in plan["entries"] if entry["branch"] == branch)
    require(git("rev-parse", "HEAD") == entry["commit"], "Package was not built from its planned commit")
    metadata = read_json(metadata_path)
    validate_metadata(metadata, branch, plan["version"])
    archive = Path("build/distributions") / metadata["archive"]
    descriptor = plugin_descriptor(archive)
    for key, tag in (("id", "id"), ("name", "name"), ("version", "version")):
        require(descriptor.findtext(tag) == metadata[key], f"Packaged {tag} mismatch")
    require(descriptor.find("idea-version").attrib == {
        "since-build": metadata["sinceBuild"], "until-build": metadata["untilBuild"]}, "Packaged IDE range mismatch")
    pom = Path("build/publications/pluginZip/pom-default.xml")
    pom_xml = ET.parse(pom).getroot()
    namespace = {"m": "http://maven.apache.org/POM/4.0.0"}
    for tag, expected in (("groupId", GROUP_ID), ("artifactId", ARTIFACT_ID),
                          ("version", metadata["version"]), ("packaging", "zip")):
        require(pom_xml.findtext(f"m:{tag}", namespaces=namespace) == expected, f"Maven POM {tag} mismatch")
    destination = Path(destination) / branch
    destination.mkdir(parents=True, exist_ok=True)
    stem = f"{ARTIFACT_ID}-{metadata['version']}"
    shutil.copyfile(archive, destination / f"{stem}.zip")
    shutil.copyfile(pom, destination / f"{stem}.pom")
    repository = ET.Element("plugins")
    plugin = ET.SubElement(repository, "plugin", id=PLUGIN_ID, version=metadata["version"],
                           url=f"{REPOSITORY_URL}/{DIRECTORY}/{metadata['version']}/{stem}.zip")
    ET.SubElement(plugin, "name").text = metadata["name"]
    ET.SubElement(plugin, "idea-version", {"since-build": metadata["sinceBuild"], "until-build": metadata["untilBuild"]})
    update_name = f"updatePlugins-{baseline(branch)}.xml"
    (destination / update_name).write_bytes(xml_bytes(repository))
    files = {name: sha256(destination / name) for name in (f"{stem}.zip", f"{stem}.pom", update_name)}
    write_json(destination / "release.json", dict(metadata, source=plan["source"], commit=entry["commit"], files=files))
    return destination


def check_package(plan, entry, directory):
    directory = Path(directory) / entry["branch"]
    metadata = read_json(directory / "release.json")
    validate_metadata(metadata, entry["branch"], plan["version"])
    require(metadata["source"] == plan["source"] and metadata["commit"] == entry["commit"], "Package provenance mismatch")
    stem = f"{ARTIFACT_ID}-{metadata['version']}"
    require(set(metadata["files"]) == {f"{stem}.zip", f"{stem}.pom", f"updatePlugins-{baseline(entry['branch'])}.xml"},
            "Unexpected package files")
    for name, digest in metadata["files"].items():
        require(sha256(directory / name) == digest, f"Checksum mismatch: {directory / name}")
    descriptor = plugin_descriptor(directory / f"{stem}.zip")
    require(descriptor.findtext("id") == PLUGIN_ID and descriptor.findtext("version") == metadata["version"],
            "Verified ZIP identity mismatch")
    return metadata


def aggregate(plan_directory, packages, destination):
    plan = load_plan(plan_directory)
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=True)
    manifest = dict(version=plan["version"], source=plan["source"], builds=[])
    for entry in plan["entries"]:
        metadata = check_package(plan, entry, packages)
        name = f"{ARTIFACT_ID}-{metadata['version']}.zip"
        shutil.copyfile(Path(packages) / entry["branch"] / name, destination / name)
        manifest["builds"].append(dict(branch=entry["branch"], commit=entry["commit"],
                                      file=name, sha256=metadata["files"][name],
                                      sinceBuild=metadata["sinceBuild"], untilBuild=metadata["untilBuild"], jdk=metadata["jdk"]))
    write_json(destination / "release-manifest.json", manifest)
    names = [build["file"] for build in manifest["builds"]] + ["release-manifest.json"]
    (destination / "SHA256SUMS").write_text("".join(f"{sha256(destination / name)}  {name}\n" for name in names), encoding="utf-8")
    print(f"Verified {len(manifest['builds'])} IDE packages for one GitHub Release: {plan['tag']}")
    return manifest


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class MavenRepository:
    def __init__(self, url, username, password):
        self.url = url.rstrip("/")
        self.authorization = "Basic " + base64.b64encode(f"{username}:{password}".encode()).decode()
        self.opener = urllib.request.build_opener(NoRedirect())

    def get(self, path):
        request = urllib.request.Request(f"{self.url}/{path}")
        try:
            with self.opener.open(request, timeout=60) as response:
                return response.read()
        except urllib.error.HTTPError as error:
            error.close()
            if error.code == 404:
                return None
            raise RuntimeError(f"Anonymous Maven GET failed: HTTP {error.code} ({path})") from None

    def put(self, path, data, immutable=False):
        headers = {"Authorization": self.authorization, "Content-Type": "application/octet-stream"}
        if immutable:
            headers["If-None-Match"] = "*"
        request = urllib.request.Request(f"{self.url}/{path}", data=data, headers=headers, method="PUT")
        try:
            with self.opener.open(request, timeout=60) as response:
                require(200 <= response.status < 300, f"Maven upload failed: {path}")
        except urllib.error.HTTPError as error:
            error.close()
            if error.code not in (409, 412) or not immutable:
                raise RuntimeError(f"Maven PUT failed: HTTP {error.code} ({path})") from None
        for attempt in range(3):
            if self.get(path) == data:
                return
            if attempt < 2:
                time.sleep(attempt + 1)
        raise ValueError(f"Anonymous Maven read-back differs from verified artifact: {path}")

    def publish(self, files):
        missing = []
        for path, data in files.items():
            existing = self.get(path)
            require(existing is None or existing == data, f"Refusing to overwrite immutable Maven artifact: {path}")
            if existing is None:
                missing.append(path)
        for path in missing:
            self.put(path, files[path], immutable=True)


def checksum_files(path, data):
    files = {path: data}
    for algorithm in ("md5", "sha1", "sha256", "sha512"):
        digest = hashlib.new(algorithm, data, usedforsecurity=False).hexdigest()
        files[f"{path}.{algorithm}"] = digest.encode("ascii")
    return files


def ensure_live(plan):
    require(plan["dryRun"] is False, "A dry-run release plan cannot perform external writes")


def publish(plan_directory, branch, packages, repository):
    plan = load_plan(plan_directory)
    ensure_live(plan)
    entry = next(entry for entry in plan["entries"] if entry["branch"] == branch)
    metadata = check_package(plan, entry, packages)
    files = {}
    for extension in ("pom", "zip"):
        name = f"{ARTIFACT_ID}-{metadata['version']}.{extension}"
        path = f"{DIRECTORY}/{metadata['version']}/{name}"
        files.update(checksum_files(path, (Path(packages) / branch / name).read_bytes()))
    repository.publish(files)
    print(f"Maven artifact verified: {GROUP_ID}:{ARTIFACT_ID}:{metadata['version']}")


def version_key(value):
    return tuple(int(part) for part in re.findall(r"[0-9]+", value))


def promote(plan_directory, packages, repository):
    plan = load_plan(plan_directory)
    ensure_live(plan)
    descriptors = []
    versions = []
    for entry in plan["entries"]:
        metadata = check_package(plan, entry, packages)
        stem = f"{ARTIFACT_ID}-{metadata['version']}"
        for extension in ("pom", "zip"):
            name = f"{stem}.{extension}"
            require(repository.get(f"{DIRECTORY}/{metadata['version']}/{name}") ==
                    (Path(packages) / entry["branch"] / name).read_bytes(), "Maven publication is incomplete")
        name = f"updatePlugins-{baseline(entry['branch'])}.xml"
        candidate = (Path(packages) / entry["branch"] / name).read_bytes()
        existing = repository.get(f"{DIRECTORY}/{name}")
        if existing:
            plugin = ET.fromstring(existing).find("plugin")
            require(plugin is not None and plugin.get("id") == PLUGIN_ID, "Unexpected existing update descriptor")
            require(version_key(plugin.get("version", "")) <= version_key(metadata["version"]),
                    "Refusing to roll back a newer update descriptor")
        descriptors.append((f"{DIRECTORY}/{name}", candidate))
        versions.append(metadata["version"])
    path = f"{DIRECTORY}/maven-metadata.xml"
    existing = repository.get(path)
    root = ET.fromstring(existing) if existing else ET.Element("metadata")
    for tag, text in (("groupId", GROUP_ID), ("artifactId", ARTIFACT_ID)):
        element = root.find(tag)
        if element is None:
            element = ET.SubElement(root, tag)
        require(element.text in (None, text), f"Existing Maven metadata {tag} mismatch")
        element.text = text
    versioning = root.find("versioning")
    if versioning is None:
        versioning = ET.SubElement(root, "versioning")
    all_versions = set(versions) | {node.text for node in root.findall("versioning/versions/version") if node.text}
    for child in list(versioning):
        versioning.remove(child)
    latest = max(all_versions, key=version_key)
    ET.SubElement(versioning, "latest").text = latest
    ET.SubElement(versioning, "release").text = latest
    listing = ET.SubElement(versioning, "versions")
    for version in sorted(all_versions, key=version_key):
        ET.SubElement(listing, "version").text = version
    ET.SubElement(versioning, "lastUpdated").text = datetime.fromisoformat(plan["date"]).astimezone(timezone.utc).strftime("%Y%m%d%H%M%S")
    for name, data in checksum_files(path, xml_bytes(root)).items():
        repository.put(name, data)
    for name, data in descriptors:
        repository.put(name, data)
    print("Promoted all stable IDE descriptors after verifying every Maven ZIP and POM")


def github_release(plan_directory, assets):
    plan = load_plan(plan_directory)
    ensure_live(plan)
    require(remote_commit(f"refs/tags/{plan['tag']}") in (None, plan["source"]), "The release tag points at a different source commit")
    assets = Path(assets)
    manifest = read_json(assets / "release-manifest.json")
    require(manifest["source"] == plan["source"] and manifest["version"] == plan["version"], "Release manifest mismatch")
    expected = [(entry["branch"], entry["commit"]) for entry in plan["entries"]]
    actual = [(build["branch"], build["commit"]) for build in manifest["builds"]]
    require(actual == expected, "Incomplete or inconsistent GitHub Release")
    names = []
    for build in manifest["builds"]:
        name = f"{ARTIFACT_ID}-{build['branch']}-{plan['version']}.zip"
        require(build["file"] == name, "Unexpected release asset name")
        require(sha256(assets / name) == build["sha256"], "Release asset checksum mismatch")
        names.append(name)
    names.append("release-manifest.json")
    checksums = "".join(f"{sha256(assets / name)}  {name}\n" for name in names)
    require((assets / "SHA256SUMS").read_text(encoding="utf-8") == checksums, "Release checksum manifest mismatch")
    names.append("SHA256SUMS")
    existing = command("gh", "release", "view", plan["tag"], "--json", "isDraft,assets", check=False)
    draft = True
    if existing.returncode:
        require("not found" in existing.stderr.lower() or "HTTP 404" in existing.stderr,
                f"Cannot check GitHub Release: {existing.stderr}")
        command("gh", "release", "create", plan["tag"], "--target", plan["source"], "--draft",
                "--title", f"Minecraft Development {plan['version']}",
                "--notes", "IDE builds: " + ", ".join(entry["branch"] for entry in plan["entries"]))
        remote_assets = set()
    else:
        existing_release = json.loads(existing.stdout)
        draft = existing_release["isDraft"]
        remote_assets = {asset["name"] for asset in existing_release["assets"]}
        require(not any(name.endswith(".zip") and name not in names for name in remote_assets), "Unexpected existing IDE asset")
    for name in names:
        if name in remote_assets:
            with tempfile.TemporaryDirectory() as directory:
                command("gh", "release", "download", plan["tag"], "--pattern", name, "--dir", directory)
                require(sha256(Path(directory) / name) == sha256(assets / name),
                        f"Existing GitHub Release asset differs: {name}")
    for name in names:
        if name not in remote_assets:
            command("gh", "release", "upload", plan["tag"], str(assets / name))
    if draft:
        command("gh", "release", "edit", plan["tag"], "--draft=false")
    print(f"Published one GitHub Release with {len(plan['entries'])} IDE builds: {plan['tag']}")


def finalize(plan_directory):
    ensure_live(load_plan(plan_directory))
    plan = restore(plan_directory, None)
    refspecs = []
    leases = []
    for entry in plan["entries"]:
        branch_ref = f"refs/heads/{entry['branch']}"
        current = remote_commit(branch_ref)
        require(current, f"Remote branch disappeared: {entry['branch']}")
        tag_ref = f"refs/tags/{entry['tag']}"
        tag_remote = remote_commit(tag_ref)
        if tag_remote:
            require(tag_remote == entry["commit"], "Existing per-line tag mismatch")
            git("fetch", "--no-tags", "origin", branch_ref)
            require(ancestor(entry["commit"], current), "Released commit is no longer on its release branch")
            continue
        require(current == entry["original"], f"Release branch moved during publication: {entry['branch']}")
        require(ancestor(current, entry["commit"]), "Refusing a non-fast-forward release branch update")
        local_tag = command("git", "rev-parse", "--verify", f"{tag_ref}^{{commit}}", check=False)
        if local_tag.returncode == 0:
            require(local_tag.stdout.strip() == entry["commit"], "Existing local audit tag mismatch")
        else:
            git(*BOT, "tag", "-a", entry["tag"], entry["commit"], "-m", f"Outlands {plan['tag']}\nsource={plan['source']}")
        leases.append(f"--force-with-lease={branch_ref}:{current}")
        refspecs.extend((f"{entry['commit']}:{branch_ref}", tag_ref))
    if refspecs:
        git("push", "--atomic", *leases, "origin", *refspecs)
    print("Release branches and per-IDE audit tags are up to date")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("prepare", "restore", "metadata", "package", "aggregate", "publish", "promote", "release", "finalize"))
    parser.add_argument("--plan", default="release-plan")
    parser.add_argument("--version")
    parser.add_argument("--dry-run", default="true")
    parser.add_argument("--branch")
    parser.add_argument("--metadata", default="build/outlands/release.json")
    parser.add_argument("--packages", default="release-packages")
    parser.add_argument("--assets", default="release-assets")
    args = parser.parse_args()
    if args.action == "prepare":
        prepare(args.version, args.dry_run, args.plan)
    elif args.action == "restore":
        restore(args.plan, args.branch)
    elif args.action == "metadata":
        metadata = read_json(args.metadata)
        validate_metadata(metadata, args.branch, args.version)
        output(jdk=metadata["jdk"])
    elif args.action == "package":
        package(args.plan, args.branch, args.metadata, args.packages)
    elif args.action == "aggregate":
        aggregate(args.plan, args.packages, args.assets)
    elif args.action in ("publish", "promote"):
        ensure_live(load_plan(args.plan))
        username = os.environ.get("MAVEN_USERNAME", "")
        password = os.environ.get("MAVEN_PASSWORD", "")
        require(username and password, "MAVEN_USERNAME and MAVEN_PASSWORD are required")
        repository = MavenRepository(REPOSITORY_URL, username, password)
        if args.action == "publish":
            publish(args.plan, args.branch, args.packages, repository)
        else:
            promote(args.plan, args.packages, repository)
    elif args.action == "release":
        github_release(args.plan, args.assets)
    elif args.action == "finalize":
        finalize(args.plan)


if __name__ == "__main__":
    try:
        main()
    except (ValueError, RuntimeError) as error:
        sys.exit(str(error))
