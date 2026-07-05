from setuptools import find_packages, setup


setup(
    name="dji-recover",
    version="0.1.0",
    description="Recover HEVC video from crashed DJI MP4 files that are missing a moov atom.",
    long_description=open("README.md", encoding="utf-8").read(),
    long_description_content_type="text/markdown",
    author="Henning and contributors",
    license="GPL-3.0-or-later",
    python_requires=">=3.10",
    package_dir={"": "src"},
    packages=find_packages(where="src"),
    entry_points={"console_scripts": ["dji-recover=dji_recover.cli:main"]},
    classifiers=[
        "Development Status :: 3 - Alpha",
        "Environment :: Console",
        "Intended Audience :: End Users/Desktop",
        "Programming Language :: Python :: 3",
        "Topic :: Multimedia :: Video",
    ],
)
