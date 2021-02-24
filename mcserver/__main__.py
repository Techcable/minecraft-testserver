#!/usr/bin/env python3
from __future__ import annotations

import sys
import os
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

from . import plugins, DevelopmentJar, CacheInvalidationException, MinecraftVersion, OfficialPaperJar, JvmVersion, PaperJar, DevCommit
from .plugins import PluginConfig

YOURKIT_PATH = Path("/opt/yourkit/bin/linux-x86-64/libyjpagent.so")
"""The hardcoded path to the yourkit instance we use"""

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

def print_wrapped(text: str, *, indent: int = 0):
    click.echo(click.wrap_text(
        text,
        width=click.get_terminal_size()[0] - 4,
        initial_indent=' ' * 4,
        subsequent_indent=' ' * 4
    ))

@click.group()
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

# NOTE: Despite the claim of 'extremely low overhead', it's too expensive to enable 'alloc_object_counting'
# by default. It simply brings startup to a standstill (we even get a tps warning immediately on boot).
#
# On the other hand, sampling and monitors profiling seems to have acceptible overhead
DEFAULT_YOURKIT_MODES = frozenset({'sampling', 'monitors'})
YOURKIT_CPU_MODES = frozenset({'sampling', 'call_counting', 'tracing', 'async_sampling_cpu'})
VALID_YOURKIT_MODES  = DEFAULT_YOURKIT_MODES | {'alloc_object_counting', 'alloceach', 'exceptions'} | YOURKIT_CPU_MODES
YOURKIT_ALLOC_RECORD_FREQUENCY = 10
"""When 'alloceach' is enabled, record every nth allocations (by default every 10th)"""

# NOTE: This is a workaround for the subcommands not working right -_-
@minecraft.group('run', invoke_without_command=True)
@click.option('--ram', help="The amount of RAM to use", default='1G', show_default=True)
@click.option('--yourkit', is_flag=True, help="Attach a yourkit profiling agent")
@click.option(
    '--yourkit-delay', type=int, default=10_000, show_default=True,
    help="If yourkit is enabled, the delay before starting profiling (helps avoid profiling plugin/server startup)"
)
@click.option(
    '--yourkit-mode', 'yourkit_modes', type=click.Choice(VALID_YOURKIT_MODES), multiple=True, default=tuple(DEFAULT_YOURKIT_MODES), 
    show_default=True, help="If yourkit is enabled, the profiling mode(s) to automatically enable on startup"
)
@click.option('--dry-run', is_flag=True, help="Do a dry run, compiling and printing startup flags without actually running")
@click.option('--minecraft-version', '--mc', default=str(max(MinecraftVersion.list_all())), show_default=True,
    help="The minecraft version to run (defaults to latest)")
@click.pass_context
def run_server(ctx, ram, yourkit, dry_run, minecraft_version, yourkit_delay, yourkit_modes):
    """Actually runs the server"""
    ctx.jvm = ctx.parent.jvm # This should auto-inherit -_-
    try:
       ctx.minecraft_version = MinecraftVersion(minecraft_version)
    except ValueError:
        raise ClickException(f"Invalid minecraft version: {minecraft_version!r}")
    if ctx.invoked_subcommand is None:
        print()
        print("No command specified!")
        click.echo(ctx.get_help())

@run_server.command('dev')
@click.option('--recompile', '-r', is_flag=True, help="Recompile the jar")
@click.option(
    '--repo', type=click.Path(exists=True, file_okay=False),
    default=str(Path(Path.home(), "git/Paper")), help="The path to the paper repository"
)
@click.pass_context
def run_dev(ctx, repo, recompile):
    """Run the development server, compiling as needed"""
    requested_minecraft_version = ctx.parent.minecraft_version
    try:
        jar = DevelopmentJar.from_repo(repo)
    except pygit2.GitError:
        raise ClickException(f"Invalid git repository: {repo}")
    if jar.minecraft_version != requested_minecraft_version:
        raise ClickException(f"Detected version {jar.minecraft_version} for {repo} (expected {requested_minecraft_version})")
    should_recompile = None
    if recompile:
        try:
            jar.validate_cache()
        except CacheInvalidationException as e:
            # Tell them that the cache is invalid, assuring them
            # it's reasonable for them to recompile
            e.print("Paper development jar", include_full=False)
        else:
            print(f"{colorize('WARNING', color='magenta', bold=True)}: The cached paper development jar is already up to date.")
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
        target_commit = DevCommit.revparse(jar.open_repo(), "HEAD")
        print('*' * click.get_terminal_size()[0])
        print(f"Compiling from commit {colorize(target_commit.short_id, color=None, underline=True)}:")
        for index, line in enumerate(target_commit.full_message.splitlines()):
            if index == 0:
                print(' ' * 4, colorize(line, color=None, bold=True), sep='')
            elif line and not line.isspace():
                print(' ' * 4, line, sep='')
            else:
                print()
        print()
        click.confirm("Are you sure you want to compile this?", default=True, abort=True)
        jar.update()
    else:
        print(f"Reusing existing jar")
    return jar

@run_server.command()
@click.option('--build-number', '--build', type=int, help="Explicitly specify the build to use")
@click.pass_context
def official(ctx, build_number):
    """Run the latest build of the official server"""
    # TODO: Cache API calls to be nice too kashike and the gang
    minecraft_version = ctx.parent.minecraft_version
    known_builds = minecraft_version.known_paper_builds
    if not known_builds:
        raise ClickException()
    if build_number is None:
        build_number = max(known_builds)
    if build_number not in known_builds:
        print(f"Known builds for {minecraft_version}:", file=sys.stderr)
        print_wrapped(', '.join(map(str, known_builds)))
        raise ClickException(f"Build {build_number} is not a valid build for {minecraft_version}")
    latest_build = max(known_builds)
    if build_number != latest_build:
        click.echo(f"The latest build for {minecraft_version} is {latest_build}.")
        click.confirm(f"Are you sure you want to use {build_number} instead?", abort=True)
    jar = OfficialPaperJar(minecraft_version, build_number)
    try:
        jar.validate_cache()
    except CacheInvalidationException as e:
        e.print("Paper jar")
        print()
        print(f"Downloading Paper {build_number}....")
        jar.update()
    assert jar.resolved_path.exists()
    return jar



@run_server.resultcallback()
@click.pass_context
def process_run(ctx, desired_jar: PaperJar, *, dry_run: bool, ram: str, yourkit_modes: tuple[str, ...], yourkit: bool, yourkit_delay: int, **kwargs):
    # Ensure all plugins exist
    assert isinstance(desired_jar, PaperJar), f"Given jar: {desired_jar!r}"
    assert isinstance(ctx, click.Context)
    assert desired_jar.resolved_path.exists()
    print("Checking plugins...")
    for config in load_plugin_configs():
        try:
            config.check()
        except plugins.PluginError as e:
            raise ClickException(e)
    # Determine process arguments
    java_args = [f"-Xmx{ram}", f"-Xms{ram}"]
    yourkit_options = {}
    if len(enabled_cpu_modes := (set(yourkit_modes) & YOURKIT_CPU_MODES)) > 1:
        print("WARNING: Enabled multiple cpu profiling modes:")
        for index, enabled_mode in enumerate(list(enabled_cpu_modes)):
            print(' ' * 4, f"{index + 1}. {enabled_mode}", space='')
        print("WARNING: This is possibly redundant")
    # Treat delay of '0' as disabling the delay completely
    if yourkit_delay == 0:
        del yourkit_options['delay']
    else:
        yourkit_options['delay'] = yourkit_delay
    for mode in yourkit_modes:
        if mode == 'alloceach':
            yourkit_options['alloceach'] = YOURKIT_ALLOC_RECORD_FREQUENCY
        elif mode == 'exceptions':
            yourkit_options['exceptions'] = 'on'
        else:
            yourkit_options[mode] = None
    # Give the yourkit process a nicer name
    yourkit_options['sessionname'] = desired_jar.describe()
    if yourkit:
        if not YOURKIT_PATH.is_file():
            raise ClickException(f"Missing yourkit profiler: {YOURKIT_PATH}")
        assert yourkit_options
        # Actually append options and agent to path
        yourkit_opt_string = ','.join(f"{name}={val}" if val else name for name, val in yourkit_options.items())
        java_args.append(f"-agentpath:{YOURKIT_PATH}={yourkit_opt_string}")
    # Extend with our magic set of JVM flags
    java_args.extend(JVM_FLAGS)
    #
    # ***** Print info *****
    #
    print(f"Minecraft version: {ctx.minecraft_version}")
    print(f"Server version: {desired_jar.describe()}")
    if dry_run:
        print("NOTE: This was a 'dry run'. Not actually starting server")
        print(f"Desired jar path: {desired_jar.resolved_path}")
        print()
        print(f"Arguments for {ctx.jvm.java_bin}:")
        print_wrapped(', '.join(java_args))
        return
    # This is good enough for now :)
    print("Beginning server....")
    print()
    print()
    # TODO: Handle Interrupts
    # NOTE: We must use `Path.resolve` because `OfficialPaperServer` returns relative paths :(
    run([ctx.jvm.java_bin, *java_args, "-jar", desired_jar.resolved_path.resolve(), "--nogui"], cwd="server")

minecraft()
