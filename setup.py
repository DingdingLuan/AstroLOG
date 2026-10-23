"""Include the buildless interface in Python wheels without duplicating sources."""

from pathlib import Path
import shutil

from setuptools import setup
from setuptools.command.build_py import build_py


class BuildWithWeb(build_py):
    def run(self):
        super().run()
        source = Path(__file__).resolve().parent / "web"
        destination = Path(self.build_lib) / "logpresent" / "web"
        shutil.copytree(source, destination, dirs_exist_ok=True)


setup(cmdclass={"build_py": BuildWithWeb})
