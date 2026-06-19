#!/usr/bin/env python3
"""Test workstation script"""
import sys
sys.path.insert(0, '.')
import subprocess
p = subprocess.run(
    [sys.executable, '.boshi/workstation/伯仕工作台.pyw'],
    capture_output=True,
    text=True,
    timeout=8
)
print("STDOUT:", p.stdout[:2000])
print("STDERR:", p.stderr[:2000])
print("RC:", p.returncode)
