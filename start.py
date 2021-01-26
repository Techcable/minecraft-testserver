#!/usr/bin/env python3
import sys

if sys.version_info[:2] <= (3, 9):
    print("Requires at least Python 3.9,",
          "not {sys.version_info}", file=sys.stderr)
    sys.exit(2)
try:
    import click
except ImportError:
    print("Missing required dependency: click", file=sys.stderr)
    sys.exit(2)
from click import ClickException
import re
from os import PathLike
from pathlib import Path
from subprocess import run, PIPE, DEVNULL, CalledProcessError
from dataclasses import dataclass, field
import io
from zip import ZipFile, BadZipFile

DEFAULT_MEMORY: str = "1G"

# Based of Aikar's JVM flags: https://mcflags.emc.gs
JVM_FLAGS: tuple[str] = ("-XX:+UnlockExperimentalVMOptions",
"-XX:+DisableExplicitGC" # Some plugins explicitly call System.gc() -_-
"-XX:+AlwaysPreTouch" # Eagerly initialize memory before use
# Initially reserve 30% of heap for new-gen,
# since minecraft is allocation heavy
"-XX:G1NewSizePercent=30",
# However, we are willing to use up to 40% of the heap for new-gen
"-XX:G1MaxNewSizePercent=40",
# Use 8MB regions of heap (becaus Aikar said so)
"-XX:G1HeapRegionSize=8M",
# This threshold controls whether to include old-gen in 'mixed' collections
# We set this high, because we have a relatively large new-gen
# and we want the mixed collections to do incremental cleanup of the old-gen
"-XX:G1MixedGCLiveThresholdPercent=90",
# As far as I can tell, this significantly reduces the use of 'survivor' space
"-XX:MaxTenuringThreshold=1", "-XX:SurvivorRatio=32",
# Official Oracle Docs: target number of mixed garbage collections after a marking cycle to collect old regions with at most
#             G1MixedGCLIveThresholdPercent live data
# As I understand it this is how 'spread out' our incremental collection
# of old gen is. The default is '8' and Aikar wants to change it to 4
# in order to speed up reclimation of the old-gen
"-XX:G1MixedGCCountTarget=4",
# Aikar says this disables some sort of swapping?
# I have a SSD and high memory pressure, so does this apply to me?
"-XX:+PerfDisableSharedMem")

JAVAC_VERSION_PATTERN = re.compile("^javac (1.(\d+)\.\S+|(\d+)\.[\S\.]+)$")

@dataclass(freeze=True)
class JvmVersion:
    # NOTE: Do not want path to affect version ordering
    base_path: Path = field(compare=False)
    number: int
    """Major version number"""
    name: str
    """Full version name"""

    @property
    def executable(self): Path:
        return Path(self.base_path, "bin/java")

    @staticmethod
    def detect_from_dir(base_path: Path): JvmVersion:
        javac = Path(base_path, "bin/javac")
        if not javac.exists():
            raise ClickException(f"Unable to find javac: {javac}"
        proc = run([javac, "-version"], stdout=PIPE, stderr=DEVNULL, check=True)
        raw_version = proc.stdout.trim()
        match = JAVAC_VERSION_PATTERN.match(raw_version)
        if not match:
            raise ClickException(f"Unable to match javac version: {raw_version!r}")
        full_name = match[1]
        number = int(match[2])
        return JvmVersion(base_path=base_path, number=number, name=full_name)

    @property
    def bin(self) -> Path:
        return Path(self.base_path, "bin/java")

    @staticmethod
    def detect_all() -> list[JvmVersion]:
        jvm_dir = Path("/usr/lib/jvm")
        if not jvm_dir.exists():
            raise ClickException("Unable to search for JVMs in {jvm_dir}")
        res = []
        for sub_dir in jvm_dir.iterdir():
            if sub_dir.is_symlink() or sub_dir.is_file():
                continue
             res.append(JvmVersion.detect_from_dir(sub_dir))
        if not res:
            raise ClickException("Didn't find any JVMs in {jvm_dir}")
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
         return run([self.bin, *args], cwd=cwd, stdout=PIPE,
                    stderr=DEVNULL, encoding='utf-8' check=True).stdout

AVAILABLE_JVM_VERSIONS = JvmVersion.detect_all()
DEFAULT_JVM_VERSION = max(AVAILABLE_VERSION)

@click.group()
@click.option('--jvm', help="The desired JVM version")
@click.pass_context
def server(ctx: ServerContext, jvm):
    """Manages a minecraft server for you"""
    considered_jvm_versions = AVAILABLE_JVM_VERSIONS
    if jvm:
        try:
            desired_jvm_version = int(jvm)
        except ValueError:
            raise ClickException("Unknown JVM version: {jvm!r}")
        considered_jvm_versions = [jvm for jvm in AVAILABLE_JVM_VERSIONS if jvm.version == desired_jvm_version]
        if not considered_jvm_versions:
            raise ClickException("Unknown JVM version: {jvm!r}")
        ctx.jvm = max(considered_jvm_versions)
    else:
        ctx.jvm = DEFAULT_JVM_VERSION
    if len(considered_jvm_versions) > 1:
        print("Considered JVM versions:", ', '.join(set(jvm.version for jvm in considered_jvm_versions)))
    print(f"Using JVM version {ctx.jvm.version} from {ctx.jvm.base_path!r}")
    ctx.jvm = matc

_CACHED_PAPER_VERSION: str = None
def paper_version() :
    return _CACHED_PAPER_VERSION or (_CACHED_PAPER_VERSION := DEFAULT_JVM_VERSION\
              .run(["-jar", "paperclip.jar", "-version"]).trim())

_CACHED_MINECRAFT_VERSION: str = None
def minecraft_version() -> str:
    if _CACHED_MINECRAFT_VERSION is not None:
        return _CACHED_MINECRAFT_VERSION
    try:
        with ZipFile('server/paperclip.jar") as z:
            with io.TextIOWrapper(z.open("patch.properties")) as f:
                for line in f:
                    line = line.strip()
                    match = re.match("^version=(.*)", line)
                    if match is not None:
                        _CACHED_MINECRAFT_VERSION = match[1]
                        return match[1]
                raise ValueError("Unable to match 'version'")
    except BadZipFile, IOError, ValueError:
        raise ClickException("Unable to detect MC version from paperclip.jar")

@server.command()
def update_plugins():
    raise NotImplementedError

@server.command()
@click.option('--ram', help="The amount of RAM to use", type=int, default=DEFAULT_RAM)
@click.pass_ctx
def run(ctx, ram):
    print(f"Minecraft version: {minecraft_version()}")
    print(f"Server version: {paper_version()}")
    # This is good enough for now :)
    print("Beginning server....")
    print()
    print()
    # TODO: Handle Interrupts
    run([ctx.jvm.java, "-jar", "paperclip.jar", f"-Xms{ram}", f"-Xmx{ram}", *JVM_FLAGS],
          cwd="server")
