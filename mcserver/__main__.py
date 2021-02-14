#!/usr/bin/env python3
from __future__ import annotations

import sys
import re
from os import PathLike
from pathlib import Path
from subprocess import run, PIPE, DEVNULL, CalledProcessError
from dataclasses import dataclass, field
import io
from zipfile import ZipFile, BadZipFile

import click
import toml
import pygit2
from click import ClickException

from . import plugins, DevelopmentJar, CacheInvalidationException, MinecraftVersion, OfficialPaperJar, JvmVersion
from .plugins import PluginConfig

DEFAULT_MEMORY: str = "1G"

# Based of Aikar's JVM flags: https://mcflags.emc.gs
JVM_FLAGS: tuple[str] = ("-XX:+UnlockExperimentalVMOptions",
"-XX:+DisableExplicitGC", # Some plugins explicitly call System.gc() -_-
"-XX:+AlwaysPreTouch", # Eagerly initialize memory before use
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

_ANSI_COLOR_CODES = {
    "black": 0,
    "red": 1,
    "green": 2,
    "yellow": 3,
    "blue": 4,
    "magenta": 5,
    "cyan": 6,
    "white": 7
}
def colorize(target: str, *, color: Optional[str], bold: bool = False, underline: bool = False):
    # TODO: Replace with click.style
    if os.name != 'posix' and sys.platform != 'cygwin':
        # No ANSI color codes here :(
        return target
    parts = []
    if color is not None:
        parts.append(str(30 + _ANSI_COLOR_CODES[color]))
    if bold:
        parts.append("1")
    if underline:
        parts.append("4")
    if not parts:
        return target
    return f"\033[{';'.join(parts)}m{target}\033[0m"  

@click.group(invoke_without_command=True)
@click.option('--jvm', help="The desired JVM version")
@click.pass_context
def minecraft(ctx: Context, jvm):
    """Manages a minecraft server for you"""
    ctx.ensure_object(Context)
    considered_jvm_versions = JvmVersion.detect_all()
    if jvm:
        try:
            desired_jvm_version = int(jvm)
        except ValueError:
            raise ClickException("Unknown JVM version: {jvm!r}")
        considered_jvm_versions = [jvm for jvm in JvmVersion.detect_all() if ctx.jvm.version == desired_jvm_version]
        if not considered_jvm_versions:
            raise ClickException("Unknown JVM version: {jvm!r}")
        ctx.jvm = max(considered_jvm_versions)
    else:
        ctx.jvm = JvmVersion.default()
    if len(considered_jvm_versions) > 1:
        print("Considered JVM versions:", ', '.join(set(jvm.version for jvm in considered_jvm_versions)))
    print(f"Using JVM version {ctx.jvm.version} from {ctx.jvm.base_path!r}")

_CACHED_PLUGIN_CONFIGS = None
def load_plugin_configs() -> list[PluginConfig]:
    global _CACHED_PLUGIN_CONFIGS
    if _CACHED_PLUGIN_CONFIGS is not None:
        return _CACHED_PLUGIN_CONFIGS.copy()
    try:
        with open('plugins.toml') as f:
            raw = toml.load(f)
    except (IOError, toml.TomlDecodeError):
        raise ClickException("Unable to load plugins.toml")
    try:
        return (_CACHED_PLUGIN_CONFIGS := PluginConfig.deserialize_all(raw))
    except plugin.MalformedConfigError as e:
        raise ClickException(e)

class Context:
    jvm: JvmVersion

@minecraft.command()
@click.option('--force', is_flag=True, default=False, help="Forcibly downloads, even if alreay exists")
@click.option('--ignore', 'ignores', help="A plugin to ignore", multiple=True)
def update_plugins(ignores: list[str], force: bool):
    """Downloads all plugins that are needed"""
    known_names = set(config.name for config in load_plugin_configs())
    for ignore in ignores:
        if ignore not in known_names:
            raise ClickException(f"Unknown plugin name: {ignore}")
    for config in load_plugin_configs():
        if config.name in ignores:
            print(f"Skipping {config}")
            continue
        print(f"Downloading {config}")
        for jar in config.jars:
            if len(config.jars) > 1:
                print(f"  - Downloading {jar}")
            try:
                refresh = config.download_strategy.download(jar, force=force)
            except plugins.PluginError as e:
                raise ClickException(e)
            if not refresh:
                print(f"  - Already exists: {jar}")


@minecraft.group('run')
@click.option('--ram', help="The amount of RAM to use", default='1G')
@click.option('--yourkit', is_flag=True, help="Attach a yourkit profiling agent")
@click.option('--dry-run', is_flag=True, help="Do a dry run, compiling and printing startup flags without actually running")
@click.option('--minecraft-version', '--mc', default=str(max(MinecraftVersion.list_all())),
    help="The minecraft version to run (defaults to latest)")
@click.pass_context
def run_server(ctx, ram, yourkit, dry_run, minecraft_version):
    """Actually runs the server"""
    ctx.jvm = ctx.parent.jvm # This should auto-inherit -_-
    java_args = [f"-Xms{ram}", f"-Xmx{ram}"]
    if yourkit:
        if not YOURKIT_PATH.is_file():
            raise ClickException(f"Missing yourkit profiler: {YOURKIT_PATH}")
        java_args.append(f"-agentpath:{YOURKIT_PATH}=exceptions=disable,delay=10000")
    # Extend with the JVM flags
    java_args.extend(JVM_FLAGS)
    ctx.java_args = java_args
    ctx.dry_run = dry_run
    try:
       ctx.minecraft_version = MinecraftVersion(minecraft_version)
    except ValueError:
        raise ClickException(f"Invalid minecraft version: {minecraft_version!r}")

@run_server.command('dev')
@click.option('--recompile', '-r', is_flag=True, help="Recompile the jar")
@click.option(
    '--repo', type=click.Path(exists=True, file_okay=False),
    default=str(Path(Path.home(), "git/Paper")), help="The path to the paper repository"
)
@click.pass_context
def run_dev(ctx, repo):
    """Run the development server, compiling as needed"""
    try:
        jar = DevelopmentJar.from_repo(repo)
    except pygit2.GitError:
        raise ClickException(f"Invalid git repository: {repo}")
    if jar.minecraft_version != ctx.minecraft_version:
        raise ClickException(f"Detected version {jar.minecraft_version} for {repo} (expected {ctx.minecraft_version})")
    should_recompile = None
    if recompile:
        try:
            jar.validate_cache()
        except CacheInvalidationException as e:
            # Tell them that the cache is invalid, assuring them
            # it's reasonable for them to recompile
            e.print("Paper development jar", include_full=False)
        else:
            print("NOTE: The cached paper development jar is already up to date.")
            click.confirm("Are you sure you want to recompile?", abort=True)
        should_recompile = True
    else:
        try:
            jar.validate_cache()
        except CacheInvalidationException as e:
            # Tell them the cache is invalid
            e.print("Paper development jar")
            should_recompile = True
        else:
            should_recompile = False
    if should_recompile:
        target_commit = jar.describe_commit("HEAD")
        print('*' * click.get_terminal_size()[0])
        print(f"Compiling commit {colorize(target_commit.short_id, underline=True)}:")
        for index, line in enumerate(target_commit.message.splitlines()):
            if index == 0:
                print(' ' * 4, colorize(line, bold=True), sep='')
            elif line and not line.isspace():
                print(' ' * 4, line, sep='')
            else:
                print()
        print()
        jar.run_resolve()
    return jar.resolved_path

@run_server.command()
@click.option('--build-number', '--build', type=int, help="Explicitly specify the build to use")
@click.pass_context
def official(ctx, build_number):
    """Run the latest build of the official server"""
    # TODO: Cache API calls to be nice too kashike and the gang
    known_builds = ctx.minecraft_version.known_paper_builds()
    if not known_builds:
        raise ClickException()
    if build_number not in known_names:
        print(f"Known builds for {ctx.minecraft_version}:", file=sys.stderr)
        for build in sorted(known_builds):
            print(' ' * 4, build, sep='', file=sys.stderr)
        raise ClickException(f"Build {build_number} is not a valid build for {ctx.minecraft_version}")
    latest_build = max(known_paper_builds)
    if build_number != latest_build:
        click.echo(f"The latest build for {ctx.minecraft_version} is {latest_build}.")
        click.confirm(f"Are you sure you want to use {build_number} instead?", abort=True)
    jar = OfficialPaperJar(ctx.minecraft_version, build)
    try:
        jar.validate_cache()
    except CacheInvalidationException as e:
        e.show()
        print()
        print("Downloading Paper {build_number}....")
        jar.run_resolve()
    assert jar.resolved_path.exists()
    return jar.resolved_path



@run_server.resultcallback()
@click.pass_context
def process_run(desired_jar: Path, ctx):
    # Ensure all plugins exist
    print("Checking plugins...")
    for config in load_plugin_configs():
        try:
            config.check()
        except plugins.PluginError as e:
            raise ClickException(e)
    java_args = ctx.java_args
    print(f"Minecraft version: {minecraft_version()}")
    print(f"Server version: {paper_version()}")
    if ctx.dry_run:
        print("NOTE: This was a 'dry run'. Not actually starting server")
        print(f"Desired jar: {desired_jar}")
        print()
        print(f"Arguments for {ctx.jvm_bin}:")
        split = []
        for i in range(0, len(java_args), 10):
            split = java_args[i:i + 10]
            print(' ' * 4, ', '.join(split), sep = '')
        return
    # This is good enough for now :)
    print("Beginning server....")
    print()
    print()
    # TODO: Handle Interrupts
    run([ctx.jvm.java_bin, *ctx.java_args, "-jar", desired_jar, "--nogui"], cwd="server")

minecraft()