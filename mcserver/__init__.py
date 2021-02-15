"""A lightweight application to help test a minecraft server"""
from __future__ import annotations

from typing import Optional, Any

import shutil
import re
import io
import json
from pathlib import Path
from subprocess import run, CalledProcessError, PIPE
from dataclasses import dataclass, field
from functools import cache, lru_cache, cached_property, total_ordering
from abc import ABCMeta, abstractmethod
from contextlib import contextmanager
import hashlib
import os

import requests
import pygit2

YOURKIT_PATH = Path("/opt/yourkit/bin/linux-x86-64/libyjpagent.so")
JAVAC_VERSION_PATTERN = re.compile("^javac (1.(\d+)\.\S+|(\d+)\.[\S\.]+)$")

class JvmException(Exception):
    pass

@dataclass(frozen=True, order=True)
class JvmVersion:
    """A particular version of the JVM

    TODO: Rename to `JvmInstance`"""

    # NOTE: Do not want path to affect version ordering
    base_path: Path = field(compare=False)
    number: int
    """Major version number"""
    version: str
    """Full version name"""

    @property
    def executable(self) -> Path:
        return Path(self.base_path, "bin/java")

    @staticmethod
    def detect_from_dir(base_path: Path) -> JvmVersion:
        javac = Path(base_path, "bin/javac")
        if not javac.exists():
            raise JvmException(f"Unable to find javac: {javac}")
        proc = run([javac, "-version"], encoding='utf-8', stdout=PIPE, stderr=PIPE, check=True)
        raw_version = proc.stdout.strip() or proc.stderr.strip()
        match = JAVAC_VERSION_PATTERN.match(raw_version)
        if not match:
            raise JvmException(f"Unable to match javac version: {raw_version!r}")
        full_name = match[1]
        number = int(match[2] or match[3])
        return JvmVersion(base_path=base_path, number=number, version=full_name)

    @property
    def java_bin(self) -> Path:
        return Path(self.base_path, "bin/java")

    @staticmethod
    def detect_all() -> list[JvmVersion]:
        jvm_dir = Path("/usr/lib/jvm")
        if not jvm_dir.exists():
            raise JvmException("Unable to search for JVMs in {jvm_dir}")
        res = []
        for sub_dir in jvm_dir.iterdir():
            if sub_dir.is_symlink() or sub_dir.is_file():
                continue
            res.append(JvmVersion.detect_from_dir(sub_dir))
        if not res:
            raise JvmException("Didn't find any JVMs in {jvm_dir}")
        return res

    def __eq__(self, other):
        # Must override since excluded from comparison
        return isinstance(other, JvmVersion) and \
                  (self.path == other.path) and \
                  (self.number == other.number) and \
                  (self.name == other.name)

    def __ne__(self, other):
         return not (self == other)

    def run_simple(self, args: list[str], *, cwd: PathLike) -> str:
         return run([self.java_bin, *args], cwd=cwd, stdout=PIPE,
                    stderr=DEVNULL, encoding='utf-8', check=True).stdout

    @staticmethod
    @cache
    def default() -> JvmVersion:
        available_versions = JvmVersion.detect_all()
        if not available_versions:
            raise JvmException("Unable to find any JVMs")
        return max(available_versions)

_MINECRAFT_VERSION_PATTERN = re.compile(r"(\d+)\.(\d+)(?:\.(\d+))?")

@total_ordering
class MinecraftVersion:
    name: str
    # The version numbers for 
    major: int
    minor: int
    patch: int


    _KNOWN_VERSIONS: dict[str, MinecraftVersion] = {}
    def __new__(cls, name: str):
        # We want two separate invocations of MinecaftVersion("1.5") to return the same instance,
        # so they share the same cache.
        try:
            return MinecraftVersion._KNOWN_VERSIONS[name]
        except KeyError:
            return super().__new__(cls)

    def __init__(self, name: str):
        match = _MINECRAFT_VERSION_PATTERN.fullmatch(name)
        if match is None:
            raise ValueError(f"Invalid version name: {name!r}")
        self.name = name
        self.major = int(match[1])
        self.minor = int(match[2])
        self.patch = int(match[3] or "0")
        MinecraftVersion._KNOWN_VERSIONS[name] = self

    def __repr__(self) -> str:
        return f"MinecraftVersion({self.name!r})"

    def __str__(self) -> str:
        return self.name

    def __hash__(self) -> int:
        return hash(self.name)

    def __eq__(self, other: MinecraftVersion):
        if isinstance(other, MinecraftVersion):
            return self.name == other.name
        else:
            return NotImplemented

    def __gt__(self, other: MinecraftVersion):
        if isinstance(other, MinecraftVersion):
            return self.major > other.major or self.minor > other.minor or self.patch > other.patch
        else:
            return NotImplemented

    @cached_property
    def known_paper_builds(self) -> list[int]:
        # See API definition: https://papermc.io/api/
        response = requests.get(f"https://papermc.io/api/v2/projects/paper/versions/{self}/")
        response.raise_for_status()
        data = response.json()
        return data['builds']

    @staticmethod
    def is_valid(name: str) -> bool:
        return _MINECRAFT_VERSION_PATTERN.fullmatch(name) is not None

    @cache
    def list_all() -> list[MinecraftVersion]:
        response = requests.get(f"https://papermc.io/api/v2/projects/paper")
        response.raise_for_status()
        data = response.json()
        return [MinecraftVersion(name) for name in data['versions'] if MinecraftVersion.is_valid(name)]

    @cache
    def fetch_paper_build(self, build_number: int) -> BuildInfo:
        response = requests.get(f"https://papermc.io/api/v2/projects/paper/versions/{self}/builds/{build_number}")
        response.raise_for_status()
        data = response.json()
        parsed = BuildInfo.parse(data)
        assert parsed.project_id == "paper"
        return parsed


class PaperVersionException(Exception):
    pass

@dataclass(frozen=True)
class BuildCommit:
    commit_id: str
    summary: str
    message: str

    def __str__(self):
        return self.message    

@dataclass(frozen=True)
class BuildInfo:
    project_id: str
    project_name: str
    minecraft_version: MinecraftVersion
    build_number: str
    time: str
    changes: list[BuildCommit]
    """A list of commit messages on what changed in this build"""
    download_name: str
    download_hash: str
    """The hash of the download, in SHA256 hex"""

    @contextmanager
    def iter_download(self) -> Iterator[bytes]:
        url = f"https://papermc.io/api/v2/projects/{self.project_id}/versions/{self.minecraft_version}/builds/" \
                f"{self.build_number}/downloads/{self.download_name}"
        with requests.get(url, stream=True) as response:
            response.raise_for_status()
            # This is the data they are going to iterate over
            yield response.iter_content(chunk_size=8192)

    @staticmethod
    def parse(json: Any) -> BuildInfo:
        return BuildInfo(
            project_id=json['project_id'],
            project_name=json['project_name'],
            minecraft_version=MinecraftVersion(json['version']),
            build_number=json['build'],
            time=json['time'],
            changes=[BuildCommit(commit_id=change['commit'], summary=change['summary'], message=change['message']) for change in json['changes']],
            download_name=json['downloads']['application']['name'],
            download_hash=json['downloads']['application']['sha256']
        )

    def __str__(self):
        return f"{self.project_name}-{self.build_number}"

@dataclass
class DevCommit:
    short_id: str
    summary: str
    full_message: str

    @staticmethod
    def revparse(repo: pygit2.Repository, target_id: str, *, strict: bool = True):
        assert isinstance(repo, pygit2.Repository)
        try:
            ref = repo.revparse_single(target_id)
            short_id = ref.short_id
            message = getattr(ref, 'message', None)
        except (KeyError, pygit2.GitError, pygit2.InvalidSpecError):
            if strict:
                raise
            else:
                # Use given target id and an unknown message
                short_id = target_id
                message = None
        if message is None or not message or message.isspace():
            if strict:
                raise ValueError(f"Invalid message for {short_id}: {message!r}")
            else:
                message = "<UNKNOWN MESSGAE>"
        # Strip everything before the first line (giving us the summary)
        if '\n' in message:
            summary = message[:message.index('\n')]
        return DevCommit(short_id, summary, message)


@dataclass
class DevJarSignature:
    """The 'signature' of a jar file compiled from a git repo"""
    jar_hash: str
    """The sha256 hash of the compiled jar file"""
    source_commit: str
    """The git commit hash (sha1)"""
    modified_sources: dict[Path, str]
    """The hashes of any source files that were changed in the workingtree when this jar was compiled"""

    def determine_changed_sources(self, other: DevJarSignature) -> set[str]:
        """Determine the sources that differ from the other signature"""
        res = {}
        all_keys = set(self.modified_sources.keys()) | set(other.modified_sources.keys())
        for key in all_keys:
            if modified_sources.get(key) != other.get(key):
                res.add(key)
        if not res:
            assert self.changed_sources == other.changed_sources
        return res

    def save(self) -> object:
        return {
            "jar_hash": self.jar_hash,
            "source_commit": self.source_commit,
            "modified_sources": {str(p): h for p, h in self.modified_sources.items()}
        }

    @staticmethod
    def parse(data: Any) -> DevJarSignature:
        return DevJarSignature(
            jar_hash=data['jar_hash'],
            source_commit=data['source_commit'],
            modified_sources={Path(p): h for p, h in data['modified_sources'].items()}
        )

_MANIFEST_VERSION_PATTERN = re.compile("Implementation-Version: (\\S.*)")
@dataclass
class PaperJar(metaclass=ABCMeta):
    minecraft_version: MinecraftVersion
    """The minecraft version string"""

    def describe_resolved_version(self) -> Optional[str]:
        """Describe the resolved version of the jar.

        Likely different format than the regular `describe` method."""
        resolved = self.resolved_path
        if not resolved.exists():
            return None
        detected_version: Optional[str] = None
        with io.Iozipfile.ZipFile() as z:
            with z.open("META-INF/MANIFEST.MF") as raw_manifest:
                with TextIOWrapper(raw_manifest, encoding='UTF-8', newline=None) as manifest:
                    for line in manifest:
                        match = _MANIFEST_VERSION_PATTERN.match(line)
                        if match is not None:
                            if detected_version is not None:
                                raise PaperVersionException(f"Detected multiple versions for {resolved}")
                            detected_version = match[1]
        if detected_version is None:
            raise PaperVersionException(f"Unable to detect version for {resolved}")
        return detected_version

    def validate_cache(self):
        res = self.check_updates(ignore_updates=True)
        assert res is None, f"Unexpected result: {res!r}"

    @abstractmethod
    def check_updates(self, *, force: bool = False, ignore_updates: bool = False) -> Optional[PaperJar]:
        # TODO: Better name (check_updates + ignore_updates option is stupid)
        # I'm thinking just make 'validate_cache' abstract and add an option to ignore that
        """Check for updates and verify the correctness of the cache.

        In order of priority:
        1. Return a new jar if there are known updates
           - Force mandates refreshing the cache of the known latest versions
           - The 'ignore_updates' flag skips this step (thus guaranteeing either an exception or `None`)
        2. Raise an exception if the cache is invalid
        3. Return `None` if the cache is valid and the version is current"""
        pass

    @abstractmethod
    def update(self, *, force: bool = False):
        """Update Resolve this jar, compiling or downloading it as necessary."""
        pass

    @abstractmethod
    def describe(self) -> str:
        pass

    @property
    @abstractmethod
    def resolved_path(self) -> Path:
        """The path to the resolved jar, which may or may not exist."""
        pass


@dataclass
class OfficialPaperJar(PaperJar):
    build_number: int
    """The CI build number"""

    def check_updates(self, *, force: bool = False, ignore_updates: bool = False) -> Optional[OfficialPaperJar]:
        if not ignore_updates:
            if force:
                # Clear cache
                del self.minecraft_version.known_paper_builds
            known_builds = self.minecraft_version.known_paper_builds
            if not known_builds:
                raise PaperVersionException(f"No known Paper builds for minecraft {self.minecraft_version}")
            maximum_build = max(known_builds)
            if maximum_build > self.build_number:
                return OfficialPaperJar(
                    minecraft_version=self.minecraft_version,
                    build_number=maximum_build
                )
            elif maximum_build == self.build_number:
                pass
            else:
                assert maximum_build < self.build_number
                raise PaperVersionException(
                    summary=f"Current build number {self.build_number} greater than maximum build", 
                    full_message=(
                        f"According to the Paper API, the maximum build for {self.minecraft_version} is {maximum_build}",
                    )
                )
        # Next, validate the cached jar
        if not self.resolved_path.exists():
            raise CacheInvalidationException(f"Missing downloaded for {self.describe()}")
        # I guess we're going to validate the hash??
        #
        # TODO: Why are we doing this?
        try:
            actual_jar_hash = hash_file(self.resolved_path)
        except FileNotFoundError:
            raise CacheInvalidationException(f"Missing build {self.build_number} for {self.minecraft_version}")
        expected_jar_hash = self.minecraft_version.fetch_paper_build(self.build_number).download_hash
        if actual_jar_hash != expected_jar_hash:
            raise CacheInvalidationException(
                summary=f"Mismatched hash for {self.describe()}", full_message=(
                    f"Expected {expected_jar_hash}",
                    f"Actually {actual_jar_hash}",
                )
            )

    def update(self, *, force: bool = False):
        if not force and self.resolved_path.exists():
            return
        info = self.minecraft_version.fetch_paper_build(self.build_number)
        with info.iter_download() as download:
            self.resolved_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.resolved_path, 'wb') as file:
                for chunk in download:
                    file.write(chunk)
        assert hash_file(self.resolved_path) == info.download_hash

    def describe(self):
        return f"Paper-{self.build_number}"

    @property
    def resolved_path(self) -> Path:
        return Path(f"cache/official-builds/paper-{self.build_number}.jar")


@dataclass
class DevelopmentJar(PaperJar):
    git_directory: Path
    """The git directory, which may or may not be clean"""

    @property
    def current_commit(self) -> str:
        """The current commit the repo points too"""
        # TODO: Do we want short ids?
        head = self.open_repo().head
        if head is None:
            return None # TODO: This is bad
        else:
            return str(head.target)

    @property
    def dirty(self) -> bool:
        """If the repository is dirty, meaning it has unstaged or uncommitted changes."""
        return len(self.detect_changed_files()) != 0

    def detect_changed_files(self) -> list[Path]:
        """Detect the set of changed files, relative to the git_directory"""
        repos = [(self.open_repo(), self.git_directory)]
        # Check server and api dirs too
        # Normally these are ignored but we need to check these
        if (server_repo_path := Path(self.git_directory, "Paper-Server")).exists():
            repos.append((pygit2.Repository(str(server_repo_path)), server_repo_path))
        if (api_repo_path := Path(self.git_directory, "Paper-API")).exists():
            repos.append((pygit2.Repository(str(api_repo_path)), api_repo_path))
        changed = []
        for repo, repo_path in repos:
            changed.extend(p.relative_to(self.git_directory) for p in detect_changed_files(repo, repo_path))
        changed.sort()
        return changed

    def check_updates(self, *, force: bool = False, ignore_updates: bool = False) -> Optional[OfficialPaperJar]:
        # NOTE: We implicitly 
        #
        # Technically `self` is always up to date, as long as it has a valid cache.
        # However, if we are set to 'force' just till the user they must "update" (by recompiling)
        if force and not ignore_updates:
            # Just return ourselves, this indicates we successfully 'updated'
            # but we don't really have any work to do as we implicitly
            # represent the state of the repo.
            return self
        self.validate_cache()
        return None

    def update(self, *, force: bool = False):
        if not force:
            try:
                self.validate_cache()
            except CacheInvalidationException:
                pass
            else:
                # The already compiled jar is valid
                # No need to recompile
                return
        # Run recompile
        # TODO: Handle errors better?
        #
        # Technically, this is a leaky abstraction since it prints to stdout.
        # However, I really want color support and I'm probably just overthinking things ^_^
        try:
            # TODO: Page all this output?
            run(["mvn", "clean", "package"], cwd=self.git_directory, check=True)
        except CalledProcessError as e:
            raise PaperVersionException("Unable to compile jar!") from e
        if not self.resolved_path.exists():
            raise PaperVersionException(f"Unable to find compiled jar: {self.resolved_path}")
        self.save_jar_signature(self.detect_current_signature())

    @staticmethod
    def from_repo(path: Path) -> DevelopmentJar:
        repo = pygit2.Repository(path)
        # Lets play 'detect the minecraft version'
        craftbukkit_pom = Path(path, 'work/CraftBukkit/pom.xml')
        minecraft_version = None
        try:
            with open(craftbukkit_pom, 'rt') as f:
                start_tag = '<minecraft.version>'
                for line in f:
                    index = line.find(start_tag)
                    if index >= 0:
                        try:
                            closing_index = line.index('</minecraft.version>')
                        except ValueError:
                            raise PaperVersionException(f"Invalid CraftBukkit POM line {line.strip()!r}", full_message=[
                                "Missing closing version tag"
                            ])
                        minecraft_version = line[index + len(start_tag):closing_index]
                        break
        except FileNotFoundError:
            raise PaperVersionException(f"Paper repo missing CraftBukkit pom")
        if minecraft_version is None:
            raise PaperVersionException(f"Could not find minecraft version from the CraftBukkit pom: {craftbukkit_pom}")
        try:
            minecraft_version = MinecraftVersion(minecraft_version)
        except ValueError:
            raise PaperVersionException(f"Invalid minecraft version in Craftbukkit POM: {minecraft_version}")
        return DevelopmentJar(minecraft_version, path)


    def validate_cache(self):
        compiled_jar = self.resolved_path
        signature_file = self.jar_signature_path
        if self.git_directory in Path.home().parents:
            repo_name = str(self.git_directory.relative_to(Path.home()))
        else:
            repo_name = str(self.git_directory)
        if not compiled_jar.exists():
            raise CacheInvalidationException(f"Missing compiled jar for git repo", full_message=[
                f"Expected location: {compiled_jar}"
            ])
        if not signature_file.exists():
            raise CacheInvalidationException(f"Missing development jar signature: {signature_file.name}")
        # NOTE: Implicitly loads if missing
        expected_signature = self.cached_jar_signature
        actual_signature = self.detect_current_signature()
        if actual_signature.jar_hash != expected_signature.jar_hash:
            # Jar changed on disk. It is possible the user recompiled.
            # We can not know for sure what sources the user compiled with
            #
            # It is possible they made a change, manually compiled, and then reverted the change
            # so even if they hash the same the sources on disk do not necessarily correspond to the jar
            raise CacheInvalidationException("Compiled jar changed on disk (hash)")
        if expected_signature.source_commit != self.current_commit:
            repo = self.open_repo()
            actual_commit = DevCommit.revparse(repo, self.current_commit, strict=False)
            expected_commit = DevCommit.revparse(repo, expected_signature.source_commit, strict=False)
            raise CacheInvalidationException(f"Mismatched commits for {repo_name}", full_message=[
                f"Expected commit {expected_commit.short_id}: {expected_commit.summary}",
                f"Actual commit {actual_commit.short_id}: {actual_commit.summary}"
            ])
        changed_files = set(actual_signature.modified_sources)
        # The commits match and the user hasn't touched the jar. Therefore,
        # the only other possible changed input is untracked/uncommited changes.
        # Check that those hashes match
        if changed_files != expected_signature.modified_sources.keys():
            all_changed_files = changed_files | expected_signature.modified_sources.keys()
            change_descriptions = []
            for name in sorted(set(all_changed_files)):
                if name not in expected_signature.modified_sources:
                    assert name in actual_signature.modified_sources
                    descr = "Added"
                elif name not in actual_signature.modified_sources:
                    assert name in expected_signature.modified_sources
                    descr = "Removed"
                else:
                    descr = "Modified"
                descr += ":"
                change_descriptions.append(f"{descr:10} {name}")
            raise CacheInvalidationException(
                summary=f"Detected {len(change_descriptions)} changes to uncommited files", 
                full_message=tuple(change_descriptions)
            )
        # The signatures should match at this point
        assert expected_signature == actual_signature

    def detect_current_signature(self) -> DevJarSignature:
        return DevJarSignature(
            jar_hash=hash_file(self.resolved_path),
            source_commit=self.current_commit,
            # NOTE: We're going to allow directories, in case they are untracked by git
            #
            # In that case, we don't respect gitignore and hash everything indiscriminately
            modified_sources={p: hash_file(Path(self.git_directory), hash_dir_as_repo=True) for p in self.detect_changed_files()},
        )

    @property
    def jar_signature_path(self) -> Path:
        return Path(f"cache/dev-signature-{self.minecraft_version}.json")

    @cached_property
    def cached_jar_signature(self) -> DevJarSignature:
        """Get the cached jar signature, implicitly loading if missing"""
        with open(self.jar_signature_path, 'rt') as f:
            return DevJarSignature.parse(json.load(f))

    def save_jar_signature(self, signature: DevJarSignature):
        with open(self.jar_signature_path, 'wt') as f:
            json.dump(signature.save(), f)
        # Then, invalidate cache
        self.cached_jar_signature = signature


    def open_repo(self) -> pygit2.Repository:
        return pygit2.Repository(str(self.git_directory))

    def describe(self) -> str:
        # NOTE: we're describing the *intended* version for this repo
        descr = f"Paper-{self.current_commit}"
        if self.dirty:
            descr += "-dirty"
        return descr

    @property
    def resolved_path(self) -> Path:
        return Path(self.git_directory, f"Paper-Server/target/paper-{self.minecraft_version}.jar")

class CacheInvalidationException(PaperVersionException):
    full_message: tuple[str, ...]
    """The full rest of the message, in addition to the summary"""

    def __init__(self, summary: str, full_message: tuple[str, ...] = ()):
        super().__init__(summary)
        self.full_message = full_message

    def print(self, name: str, *, fmt: str = "Cached version of {name} is invalid: {summary}", include_full: bool = True):
        print(fmt.format(name=name, summary=str(self)))
        if include_full:
            self.print_full_message()

    def print_full_message(self):
        for msg in self.full_message:
            if msg and not msg.isspace():
                print(' ' * 4, msg, sep='')
            else:
                print()

def detect_changed_files(repo: pygit2.Repository, repo_path: Path) -> Iterator[Path]:
    submodules = repo.listall_submodules()
    for file, flags in repo.status().items():
        if flags not in (pygit2.GIT_STATUS_CURRENT, pygit2.GIT_STATUS_IGNORED):
            target_path = Path(repo_path, file)
            if not target_path.is_dir():
                yield target_path
            else:
                relative_path = target_path.relative_to(repo_path)
                # NOTE: Special treatment for sub-modules
                if str(relative_path) in submodules:
                    sub_repo = pygit2.Repository(target_path)  # TODO: What if it's no longer a repository?
                    # Mark the subrepo itself as modified. It has additional commit metadata
                    # that might have changed
                    yield target_path
                    # Detect any modified files within the sub-repo
                    # NOTE: This is faster than plain hashing because it implicitly takes advantage of
                    # the tracking git has already done.
                    yield from detect_changed_files(sub_repo, target_path)
                else:
                    # Not a submodule: just mark all (non-ignored) subfiles as changed
                    #
                    # NOTE: We do not yield the directory itself because git ignores that.
                    # There is no extra metadata to add in that case.
                    detected_modification = False
                    for dirpath, dirnames, filenames in os.walk(target_path):
                        relative_dirpath = Path(dirpath).relative_to(repo_path)
                        for name in filenames:
                            if not repo.path_is_ignored(str(Path(relative_dirpath, name))):
                                yield Path(dirpath, name)
                                detected_modification = True
                        for sub_dir in list(dirnames):
                            if repo.path_is_ignored(str(Path(relative_dirpath, sub_dir))):
                                dirnames.remove(sub_dir)
                            else:
                                yield Path(dirpath, sub_dir)
                                detected_modification = True
                    if not detected_modification:
                        raise AssertionError(f"Unable to find git's claimed modification (flags={flags:04x}): {target_path}")

def hash_file(target: Path, *, hash_dir_as_repo: bool = False) -> str:
    m = hashlib.sha256()
    try:
        with open(target, 'rb') as f:
            while (buffer := f.read(8192)):
                m.update(buffer)
    except IsADirectoryError:
        if not hash_dir_as_repo:
            raise
        try:
            repo = pygit2.Repository(target)
        except GitError:
            raise ValueError(f"Unable to hash as git repo: {target}")
        # Just hash the current commit head
        head = repo.head
        if head is not None:
            m.update(head.target.raw)
        else:
            m.update(b"NONE")
    return m.hexdigest()
