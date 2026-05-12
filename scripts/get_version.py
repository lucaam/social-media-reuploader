#!/usr/bin/env python3
import subprocess
import re
import os


def get_latest_tag():
    try:
        tag = subprocess.check_output(["git", "describe", "--tags", "--abbrev=0"]).decode().strip()
        return tag.lstrip("v")
    except Exception:
        # fallback: read __version__ from src/__init__.py
        here = os.path.dirname(os.path.dirname(__file__))
        init_py = os.path.join(here, 'src', '__init__.py')
        if os.path.exists(init_py):
            text = open(init_py, 'r', encoding='utf-8').read()
            m = re.search(r"__version__\s*=\s*['\"]([^'\"]+)['\"]", text)
            if m:
                return m.group(1)
    return '0.0.0'


if __name__ == '__main__':
    print(get_latest_tag())
