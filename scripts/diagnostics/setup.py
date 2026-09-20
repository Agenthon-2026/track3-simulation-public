"""Compile separate diagnostic modules; never replace the submitted extension."""

from Cython.Build import cythonize
from setuptools import Extension, setup

extensions = []
for suffix, profile in (("diagnostic", False), ("profile", True)):
    name = "fast_sim._native_" + suffix
    extensions.extend(
        cythonize(
            [
                Extension(
                    name, [name.replace(".", "/") + ".pyx"], extra_compile_args=["-O3"]
                )
            ],
            compiler_directives={
                "language_level": 3,
                "boundscheck": False,
                "wraparound": True,
                "profile": profile,
            },
        )
    )
setup(name="track3-diagnostic-only", ext_modules=extensions)
