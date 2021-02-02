from __future__ import annotations
from dataclasses import dataclass, field
from abc import ABCMeta, abstractmethod
from pathlib import Path
import requests

class PluginError(Exception):
    pass

class MalformedConfigError(PluginError):
   pass

class ManualPluginMissingError(PluginError):
   target: PluginJar
   def __init__(self, target: PluginJar, msg: str = None):
       super().__init__(msg or f"Jar must be downloaded manually: {target}")
       self.target = target

@dataclass
class PluginJar:
   config: PluginConfig
   name: str

   def exists(self) -> bool:
       return self.path.is_file()

   @property
   def path(self) -> Path:
       return Path("server/plugins", str(self))

   def vars(self) -> dict:
       return {**self.config.vars(), 'jar_name': self.name}

   def __str__(self) -> Path:
      return f"{self.name}-v{self.config.version}.jar"

@dataclass
class PluginConfig:
    name: str
    version: str
    download_strategy: DownloadStrategy
    jar_names: Optional[list[str]] = None

    @property
    def jars(self) -> list[PluginJar]:
        if self.jar_names is None:
            return [PluginJar(config=self, name=self.name)]
        return [PluginJar(config=self, name=name) for name in self.jar_names]

    def vars(self) -> dict[str, str]:
       """The vars available to custom patterns"""
       return {'plugin_name': self.name, 'version': self.version}

    def check(self):
       for jar in self.jars:
           if not jar.exists():
               if self.jar_names is not None:
                   raise PluginError("Missing jar: {jar}")
               else:
                   raise PluginError("Missing plugin: {self}")

    def __str__(self) -> str:
        return f"{self.name} v{self.version}"

    @staticmethod
    def deserialize(name: str, data: dict) -> PluginConfig:
        try:
            version = data['version']
            jar_names = data.get('jars')
        except KeyError:
            raise MalformedConfigError(f"Missing required config key in {name}")
        manual = data.get('manual-download', False)
        if manual:
            download_strategy = ManualDownloadStrategy()
        elif 'url' in data:
            download_strategy = UrlPatternDownload(data['url'])
        else:
            raise MalformedConfigError(f"No download strategy for {name}")
        return PluginConfig(name=name, version=version, jar_names=jar_names, download_strategy=download_strategy)

    @staticmethod
    def deserialize_all(config: dict) -> list[PluginConfig]:
        res = []
        for name, config in config.items():
            res.append(PluginConfig.deserialize(name, config))
        return res

class DownloadStrategy(metaclass=ABCMeta):
    @abstractmethod
    def download(self, target: PluginJar, *, force: bool = False) -> bool:
        """Download the specified jar, returning whether it was actually refreshed"""
        pass

@dataclass
class UrlPatternDownload(DownloadStrategy):
    url: str

    def download(self, target: PluginJar, *, force: bool = False):
        try:
            url = self.url.format(**target.vars())
        except KeyError as k:
            raise MalformedConfigError(f"Missing key in URL pattern: {self.url}")
        except IndexError:
            raise MalformedConfigError(f"May not use indexes in URL pattern: {self.url}")
        # NOTE: We validate URL before we test for existence
        if force:
            pass # Download unconditionally
        elif target.exists():
            return False # Already exists
        # TODO: Do the `download.part` thing and then move the result
        try:
           r = requests.get(url, stream=True)
           with open(target.path, 'wb') as out:
               for chunk in r.iter_content(8192):
                   out.write(chunk)
        except requests.HTTPError:
           raise PluginError(f"Unable to download jar: {url}")
        except IOError:
           raise PluginError(f"Unable to write to jar: {jar.path}")
        return True

class ManualDownloadStrategy(DownloadStrategy):
    def download(self, target: PluginJar, *, force: bool = False) -> bool:
        if force:
            raise ManualPluginMissingError(f"Can't refresh (force-download) a manually downloaded plugin: {target}")
        elif target.exists():
            pass  # OK
        else:
            raise ManualPluginMissingError(target)
        return False  # We can never refresh, because we're manual
