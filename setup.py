"""Setuptools commands that embed the production React bundle in binary wheels.

The UI remains a normal top-level Vite project during development.  A wheel, however, must be
self-contained: there is no post-install hook and an installed package must not need Node/npm.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

from setuptools import setup
from setuptools.command.bdist_wheel import bdist_wheel as _bdist_wheel
from setuptools.command.build_py import build_py as _build_py
from setuptools.command.sdist import sdist as _sdist


ROOT = Path(__file__).resolve().parent
PACKAGE_UI_DEST = Path("looplab") / "serve" / "ui_dist"


def _production_ui_dist() -> Path:
    """Build or resolve the exact production bundle that this wheel will carry."""
    override = os.environ.get("LOOPLAB_UI_PREBUILT")
    if override:
        dist = Path(override).resolve()
    else:
        source = ROOT / "ui"
        if not (source / "package.json").is_file():
            raise RuntimeError(
                "LoopLab wheel build requires the ui/ source tree or LOOPLAB_UI_PREBUILT.")
        npm = shutil.which("npm")
        dist = source / "dist"
        if (source / "node_modules").is_dir():
            if npm is None:
                raise RuntimeError(
                    "LoopLab UI dependencies exist but npm is unavailable; "
                    "set LOOPLAB_UI_PREBUILT to a verified bundle.")
            subprocess.run([npm, "run", "build"], cwd=source, check=True)
        elif (dist / "index.html").is_file() and any((dist / "assets").glob("*.js")):
            # An sdist carries the release-built bundle so downstream wheel builders need neither
            # Node nor network access.
            pass
        else:
            if npm is None:
                raise RuntimeError(
                    "LoopLab source builds require npm, or a release sdist/prebuilt bundle via "
                    "LOOPLAB_UI_PREBUILT.")
            subprocess.run([npm, "ci"], cwd=source, check=True)
            subprocess.run([npm, "run", "build"], cwd=source, check=True)
    if not (dist / "index.html").is_file():
        raise RuntimeError(f"production UI bundle has no index.html: {dist}")
    if not any((dist / "assets").glob("*.js")):
        raise RuntimeError(f"production UI bundle has no JavaScript assets: {dist / 'assets'}")
    return dist


class BuildPyWithUI(_build_py):
    """Copy the prepared bundle into build_lib so wheel RECORD includes every hashed asset."""

    def run(self) -> None:
        super().run()
        source = getattr(self.distribution, "_looplab_ui_dist", None)
        if source is None:
            # Ordinary source/editable installs retain the on-demand builder.  If a bundle already
            # exists, including it is useful but never triggers npm outside a wheel build.
            candidate = ROOT / "ui" / "dist"
            source = candidate if (candidate / "index.html").is_file() else None
        if source is None:
            return
        destination = Path(self.build_lib) / PACKAGE_UI_DEST
        if destination.exists():
            shutil.rmtree(destination)
        shutil.copytree(Path(source), destination)

    def get_outputs(self, include_bytecode: int = 1) -> list[str]:
        outputs = list(super().get_outputs(include_bytecode))
        destination = Path(self.build_lib) / PACKAGE_UI_DEST
        if destination.is_dir():
            outputs.extend(str(path) for path in destination.rglob("*") if path.is_file())
        return outputs


class BDistWheelWithUI(_bdist_wheel):
    """Require one verified production bundle before constructing a distributable wheel."""

    def run(self) -> None:
        self.distribution._looplab_ui_dist = _production_ui_dist()
        super().run()


class SDistWithUI(_sdist):
    """Build once before archiving so a published sdist is a self-contained wheel input."""

    def run(self) -> None:
        self.distribution._looplab_ui_dist = _production_ui_dist()
        super().run()


setup(cmdclass={
    "build_py": BuildPyWithUI,
    "bdist_wheel": BDistWheelWithUI,
    "sdist": SDistWithUI,
})
