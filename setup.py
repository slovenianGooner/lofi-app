import os
from setuptools import setup

APP = ["lofi.py"]
OPTIONS = {
    "iconfile": "lofi.icns",
    "argv_emulation": False,
    "plist": {
        "CFBundleName": "lofi",
        "CFBundleDisplayName": "lofi",
        "CFBundleIdentifier": "com.sloveniangooner.lofi",
        "CFBundleVersion": "1.0",
        "CFBundleShortVersionString": "1.0",
        "LSMinimumSystemVersion": "12.0",
        "NSHighResolutionCapable": True,
        "LSUIElement": False,
    },
    "packages": [],
    "excludes": ["unittest", "email", "html", "http", "urllib"],
}

setup(
    app=APP,
    options={"py2app": OPTIONS},
    setup_requires=["py2app"],
)
